"""Comprehensive test suite for repository intelligence and coding diagnostics."""

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.repo.config_discovery import discover_config, parse_package_json, parse_pyproject_toml
from jarvis.repo.diagnostics import generate_diagnostic_report
from jarvis.repo.git_intel import get_git_status, identify_affected_tests
from jarvis.repo.inventory import categorize_file, scan_repository
from jarvis.repo.models import FileCategory, SymbolType
from jarvis.repo.path_security import PathSecurityError, safe_resolve_path, to_relative_posix, validate_repo_root
from jarvis.repo.search import extract_python_symbols, search_filenames, search_symbols, search_text
from jarvis.repo.secret_redactor import redact_secrets
from jarvis.repo.service import RepoIntelligenceService
from jarvis.repo.test_discovery import discover_tests_and_build

OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTH_TOKEN = "repo-intel-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


# ============================================================================
# 1. PATH SECURITY & BOUNDARY TESTS
# ============================================================================

def test_validate_repo_root_success_and_failure(tmp_path):
    assert validate_repo_root(tmp_path) == tmp_path.resolve()

    with pytest.raises(PathSecurityError, match="must not be empty"):
        validate_repo_root("")

    with pytest.raises(PathSecurityError, match="invalid or not accessible"):
        validate_repo_root(tmp_path / "non_existent_subdir")


def test_safe_resolve_path_within_root(tmp_path):
    f = tmp_path / "app" / "main.py"
    f.parent.mkdir(parents=True)
    f.write_text("print('hello')", encoding="utf-8")

    resolved = safe_resolve_path(tmp_path, "app/main.py", must_exist=True)
    assert resolved == f.resolve()
    assert to_relative_posix(tmp_path, resolved) == "app/main.py"


def test_safe_resolve_path_rejects_internal_symlink_before_resolution(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("inside", encoding="utf-8")
    link = tmp_path / "alias.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symbolic links are unavailable in this environment: {type(exc).__name__}")

    with pytest.raises(PathSecurityError, match="Symbolic links are disallowed"):
        safe_resolve_path(tmp_path, "alias.txt", must_exist=True)


def test_safe_resolve_path_rejects_lstat_symlink_without_os_symlink_support(tmp_path, monkeypatch):
    link = tmp_path / "alias.txt"
    original_lstat = Path.lstat

    def lstat_with_link(path):
        if path == link:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_link)

    with pytest.raises(PathSecurityError, match="Symbolic links are disallowed"):
        safe_resolve_path(tmp_path, "alias.txt", must_exist=False)


def test_safe_resolve_path_rejects_external_symlink_before_resolution(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "alias.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symbolic links are unavailable in this environment: {type(exc).__name__}")

    with pytest.raises(PathSecurityError, match="Symbolic links are disallowed"):
        safe_resolve_path(root, "alias.txt", must_exist=True)


def test_safe_resolve_path_nonexistent_target_modes(tmp_path):
    expected = tmp_path / "not-created" / "module.py"
    assert safe_resolve_path(tmp_path, "not-created/module.py") == expected

    with pytest.raises(PathSecurityError, match="Path does not exist"):
        safe_resolve_path(tmp_path, "not-created/module.py", must_exist=True)


def test_safe_resolve_path_traversal_rejected(tmp_path):
    with pytest.raises(PathSecurityError, match="Path traversal"):
        safe_resolve_path(tmp_path, "../outside.py")

    with pytest.raises(PathSecurityError, match="Path traversal"):
        safe_resolve_path(tmp_path, "foo/../../outside.py")


def test_safe_resolve_path_nul_byte_rejected(tmp_path):
    with pytest.raises(PathSecurityError, match="NUL"):
        safe_resolve_path(tmp_path, "foo\x00bar.py")


def test_safe_resolve_path_windows_reserved_names(tmp_path):
    with pytest.raises(PathSecurityError, match="reserved Windows device name"):
        safe_resolve_path(tmp_path, "con.py")

    with pytest.raises(PathSecurityError, match="reserved Windows device name"):
        safe_resolve_path(tmp_path, "sub/aux.txt")


def test_safe_resolve_path_alternate_data_streams_rejected(tmp_path):
    with pytest.raises(PathSecurityError, match="Alternate data streams"):
        safe_resolve_path(tmp_path, "file.txt:stream")


# ============================================================================
# 2. SECRET REDACTION TESTS
# ============================================================================

def test_redact_secrets_openai_and_anthropic_keys():
    text = "Here is my key: sk-abcdefghijklmnopqrstuvwxyz1234567890 and sk-ant-api03-abcdefghijklmnop"
    redacted, count = redact_secrets(text)
    assert "sk-abcdefgh" not in redacted
    assert "[REDACTED_SECRET: OPENAI_KEY]" in redacted
    assert "[REDACTED_SECRET: ANTHROPIC_KEY]" in redacted
    assert count >= 2


def test_redact_secrets_github_pat():
    text = "Token: ghp_123456789012345678901234567890123456"
    redacted, count = redact_secrets(text)
    assert "ghp_1234" not in redacted
    assert "[REDACTED_SECRET: GITHUB_TOKEN]" in redacted
    assert count == 1


def test_redact_secrets_database_url_password():
    url = "DATABASE_URL=postgresql://jarvis_user:SuperSecretPassword123@localhost:5432/jarvis_db"
    redacted, count = redact_secrets(url)
    assert "SuperSecretPassword123" not in redacted
    assert "postgresql://jarvis_user:[REDACTED_SECRET: DB_PASSWORD]@localhost:5432/jarvis_db" in redacted
    assert count == 1


def test_redact_secrets_bearer_authorization():
    header = "Authorization: Bearer my-secret-jwt-token-value-here"
    redacted, count = redact_secrets(header)
    assert "my-secret-jwt-token-value-here" not in redacted
    assert "[REDACTED_SECRET: BEARER_TOKEN]" in redacted


def test_redact_secrets_private_key_block():
    key_block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
    redacted, count = redact_secrets(key_block)
    assert "MIIEowIBAAKCAQEA0" not in redacted
    assert "[REDACTED_SECRET: PRIVATE_KEY]" in redacted


# ============================================================================
# 3. REPOSITORY INVENTORY TESTS
# ============================================================================

def test_inventory_empty_repository(tmp_path):
    inv = scan_repository(tmp_path)
    assert inv.total_files == 0
    assert inv.total_size_bytes == 0
    assert inv.files == []
    assert inv.truncated is False


def test_inventory_typical_project_structure(tmp_path):
    # Setup files
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "main.py").write_text("print('start')", encoding="utf-8")
    (tmp_path / "jarvis" / "utils.py").write_text("def helper(): pass", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): assert True", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("# Documentation", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")

    # Add ignored dir and files
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dummy.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "cache.pyc").write_bytes(b"\x00\x01\x02")

    inv = scan_repository(tmp_path)
    assert inv.total_files == 5  # main.py, utils.py, test_main.py, README.md, pyproject.toml
    assert "Python" in inv.languages
    assert inv.languages["Python"] == 3
    assert "Markdown" in inv.languages
    assert "pyproject.toml" in inv.manifests
    assert "jarvis/main.py" in inv.entry_points
    assert "tests" in inv.test_directories
    assert "docs/README.md" in inv.documentation_files


def test_inventory_file_count_limit(tmp_path):
    for i in range(15):
        (tmp_path / f"file_{i}.txt").write_text(f"content {i}", encoding="utf-8")

    inv = scan_repository(tmp_path, max_files=10)
    assert inv.total_files == 10
    assert inv.truncated is True


def test_categorize_file_rules():
    assert categorize_file("jarvis/main.py", ".py") == FileCategory.SOURCE
    assert categorize_file("tests/test_api.py", ".py") == FileCategory.TEST
    assert categorize_file("frontend/src/App.test.tsx", ".tsx") == FileCategory.TEST
    assert categorize_file("pyproject.toml", ".toml") == FileCategory.MANIFEST
    assert categorize_file("docs/ARCHITECTURE.md", ".md") == FileCategory.DOCUMENTATION
    assert categorize_file("config.json", ".json") == FileCategory.CONFIG
    assert categorize_file("Makefile", "") == FileCategory.BUILD
    assert categorize_file("logo.png", ".png") == FileCategory.ASSET


# ============================================================================
# 4. SEARCH & SYMBOL NAVIGATION TESTS
# ============================================================================

def test_search_filenames(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "conversations.py").write_text("# conversations", encoding="utf-8")
    (tmp_path / "app" / "messages.py").write_text("# messages", encoding="utf-8")

    result = search_filenames(tmp_path, "conversations")
    assert result.total_matches == 1
    assert result.matches[0].path == "app/conversations.py"
    assert result.matches[0].match_type == "filename"


def test_search_text_plain_and_regex(tmp_path):
    target = tmp_path / "service.py"
    target.write_text("line 1\nSECRET_KEY = 'super_secret_value_123'\nline 3\nreturn 42\n", encoding="utf-8")

    # Plain text search
    summary = search_text(tmp_path, "return 42")
    assert summary.total_matches == 1
    assert summary.matches[0].line_number == 4
    assert "return 42" in summary.matches[0].line_text

    # Regex search with secret redaction
    reg_summary = search_text(tmp_path, r"SECRET_KEY\s*=\s*'[^']+'", is_regex=True)
    assert reg_summary.total_matches == 1
    assert "super_secret_value_123" not in reg_summary.matches[0].line_text
    assert "[REDACTED_SECRET: CONFIG_VALUE]" in reg_summary.matches[0].line_text


def test_search_text_with_context_lines(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("line A\nline B\nMATCH_LINE\nline C\nline D\n", encoding="utf-8")

    summary = search_text(tmp_path, "MATCH_LINE", context_lines=2)
    assert summary.total_matches == 1
    m = summary.matches[0]
    assert m.line_number == 3
    assert m.context_before == ["line A", "line B"]
    assert m.context_after == ["line C", "line D"]


def test_extract_python_symbols_and_routes(tmp_path):
    src = tmp_path / "routes.py"
    src.write_text(
        "MY_CONSTANT = 100\n\n"
        "class UserService:\n"
        "    '''Manages users.'''\n"
        "    def get_user(self, user_id: str):\n"
        "        pass\n\n"
        "@router.get('/users/{user_id}')\n"
        "async def read_user(user_id: str):\n"
        "    '''Read user endpoint.'''\n"
        "    return {}\n",
        encoding="utf-8",
    )

    symbols = extract_python_symbols(src, "routes.py")
    names = {s.name: s for s in symbols}

    assert "MY_CONSTANT" in names
    assert names["MY_CONSTANT"].symbol_type == SymbolType.CONSTANT

    assert "UserService" in names
    assert names["UserService"].symbol_type == SymbolType.CLASS_DEF
    assert names["UserService"].docstring == "Manages users."

    assert "UserService.get_user" in names
    assert names["UserService.get_user"].symbol_type == SymbolType.FUNCTION_DEF

    assert "read_user" in names
    assert names["read_user"].symbol_type == SymbolType.ROUTE_HANDLER
    assert names["read_user"].route_method == "GET"
    assert names["read_user"].route_path == "/users/{user_id}"


def test_search_symbols_query(tmp_path):
    src = tmp_path / "api.py"
    src.write_text(
        "@router.get('/conversations')\n"
        "async def list_conversations(): pass\n",
        encoding="utf-8",
    )

    results = search_symbols(tmp_path, "conversations")
    assert len(results) == 1
    assert results[0].name == "list_conversations"
    assert results[0].route_path == "/conversations"


# ============================================================================
# 5. READ-ONLY GIT INTELLIGENCE TESTS
# ============================================================================

def test_git_status_non_git_directory(tmp_path):
    summary = get_git_status(tmp_path)
    assert summary.is_repo is False
    assert summary.error is not None


def test_git_status_clean_and_modified_repo(tmp_path):
    # Initialize a temporary git repository
    try:
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Git CLI not available in test environment")

    # Initial commit
    file1 = tmp_path / "hello.py"
    file1.write_text("print('initial')", encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    # 1. Clean check
    clean_status = get_git_status(tmp_path)
    assert clean_status.is_repo is True
    assert clean_status.head_commit is not None
    assert clean_status.staged_files == []
    assert clean_status.unstaged_files == []
    assert clean_status.untracked_files == []

    # 2. Modify tracked and create untracked
    file1.write_text("print('modified')", encoding="utf-8")
    file2 = tmp_path / "new_untracked.txt"
    file2.write_text("untracked", encoding="utf-8")

    mod_status = get_git_status(tmp_path)
    assert len(mod_status.unstaged_files) == 1
    assert mod_status.unstaged_files[0].path == "hello.py"
    assert "new_untracked.txt" in mod_status.untracked_files


def test_git_cmd_ignores_hostile_git_environment_overrides(tmp_path, monkeypatch):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    file1 = tmp_path / "hello.py"
    file1.write_text("print('safe')", encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    # Set hostile Git environment overrides
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent_dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "hostile_work_tree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "hostile_index"))

    status = get_git_status(tmp_path)
    assert status.is_repo is True
    assert status.head_commit is not None


def test_git_affected_tests_mapping():
    affected = identify_affected_tests([
        "jarvis/api/routes/conversations.py",
        "jarvis/memory/ingestion.py",
        "frontend/src/App.tsx",
    ])
    assert "tests/test_api.py" in affected
    assert "tests/test_memory_ingestion.py" in affected
    assert "frontend/src/App.test.tsx" in affected


# ============================================================================
# 6. CONFIGURATION & DEPENDENCY DISCOVERY TESTS
# ============================================================================

def test_parse_pyproject_toml():
    sample = (
        "[build-system]\nrequires = ['setuptools>=68']\n\n"
        "[project]\nname = 'sample-app'\ndependencies = [\n"
        "    'fastapi>=0.110',\n"
        "    'uvicorn>=0.27',\n"
        "]\n\n"
        "[project.optional-dependencies]\n"
        "dev = [\n    'pytest>=8.0',\n]\n"
    )
    deps, fws, tfws = parse_pyproject_toml(sample)
    dep_names = {d.name for d in deps}
    assert "fastapi" in dep_names
    assert "uvicorn" in dep_names
    assert "pytest" in dep_names
    assert "FastAPI" in fws
    assert "Uvicorn" in fws
    assert "pytest" in tfws


def test_parse_package_json():
    sample = (
        '{"dependencies": {"react": "^18.2.0"},'
        '"devDependencies": {"vite": "^5.0.0", "typescript": "^5.2.0", "vitest": "^1.0.0"}}'
    )
    deps, fws, tfws, bts = parse_package_json(sample)
    assert "React" in fws
    assert "Vite" in bts
    assert "TypeScript" in bts
    assert "Vitest" in tfws


def test_discover_config_full(tmp_path):
    (tmp_path / "pyproject.toml").write_text("dependencies = ['fastapi']\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("JARVIS_PROVIDER=mock\nJARVIS_LOCAL_AUTH_TOKEN=\n", encoding="utf-8")

    cfg = discover_config(tmp_path)
    assert "Python" in cfg.languages
    assert "JARVIS_PROVIDER" in cfg.env_variable_names
    assert "JARVIS_LOCAL_AUTH_TOKEN" in cfg.env_variable_names


# ============================================================================
# 7. TEST & BUILD DISCOVERY TESTS
# ============================================================================

def test_discover_tests_and_build(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("def test_ok(): pass", encoding="utf-8")
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "sample.py").write_text("def run(): pass", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    td = discover_tests_and_build(tmp_path)
    assert "pytest" in td.test_frameworks
    assert "tests" in td.test_directories
    assert "tests/test_sample.py" in td.test_files
    assert td.module_to_test_map.get("jarvis/sample.py") == ["tests/test_sample.py"]


# ============================================================================
# 8. EVIDENCE-BASED DIAGNOSTICS TESTS
# ============================================================================

def test_diagnostics_separates_facts_and_hypotheses(tmp_path):
    routes_file = tmp_path / "routes.py"
    routes_file.write_text(
        "@router.get('/conversations')\n"
        "async def list_conversations():\n"
        "    if repository is None:\n"
        "        raise HTTPException(503, 'Persistence unavailable')\n",
        encoding="utf-8",
    )

    report = generate_diagnostic_report(
        tmp_path,
        "Why is GET /conversations returning 503?",
        target_files=["routes.py"],
    )

    assert report.problem_statement == "Why is GET /conversations returning 503?"
    assert "routes.py" in report.investigated_files
    # Check facts are factual
    assert any("routes.py" in f.description for f in report.facts)
    # Check hypotheses are labeled as hypotheses
    assert any("503" in h.description for h in report.hypotheses)
    # Check confirmation needed is populated
    assert any(h.confirmation_needed for h in report.hypotheses)
    # Check suggested commands
    assert len(report.suggested_commands) > 0


# ============================================================================
# 9. REPO INTELLIGENCE SERVICE & API ROUTE TESTS
# ============================================================================

def test_repo_service_facade(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
    service = RepoIntelligenceService(tmp_path)

    inv = service.get_inventory()
    assert inv.total_files == 1

    search_res = service.search("hello", search_type="text")
    assert search_res.total_matches == 1

    cfg = service.get_configuration()
    assert isinstance(cfg.languages, list)

    td = service.get_test_discovery()
    assert isinstance(td.test_files, list)

    diag = service.diagnose("test problem")
    assert diag.confidence in {"medium", "high"}


def test_api_routes_authentication_and_capability():
    settings = Settings(
        _env_file=None,
        environment="test",
        local_auth_token=SecretStr(AUTH_TOKEN),
        owner_id=OWNER_ID,
        persistence_enabled=False,
    )
    app = create_app(settings=settings)
    client = TestClient(app)

    # 1. Unauthenticated request to /repo/inventory -> 401
    resp_unauth = client.get("/repo/inventory")
    assert resp_unauth.status_code == 401
    assert "Bearer" in resp_unauth.headers.get("WWW-Authenticate", "")

    # 2. Authenticated request to /repo/inventory -> 200
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    resp_inv = client.get("/repo/inventory", headers=headers)
    assert resp_inv.status_code == 200
    data = resp_inv.json()
    assert "total_files" in data
    assert "languages" in data

    # 3. Authenticated request to /repo/search -> 200
    resp_search = client.post(
        "/repo/search",
        headers=headers,
        json={"query": "create_app", "search_type": "text"},
    )
    assert resp_search.status_code == 200
    search_data = resp_search.json()
    assert "matches" in search_data
    assert search_data["total_matches"] >= 1

    # 4. Authenticated request to /repo/symbols -> 200
    resp_symbols = client.get("/repo/symbols?query=create_app", headers=headers)
    assert resp_symbols.status_code == 200
    symbols_data = resp_symbols.json()
    assert any(s["name"] == "create_app" for s in symbols_data)

    # 5. Authenticated request to /repo/git -> 200
    resp_git = client.get("/repo/git", headers=headers)
    assert resp_git.status_code == 200
    git_data = resp_git.json()
    assert git_data["is_repo"] is True

    # 6. Authenticated request to /repo/config -> 200
    resp_cfg = client.get("/repo/config", headers=headers)
    assert resp_cfg.status_code == 200
    cfg_data = resp_cfg.json()
    assert "Python" in cfg_data["languages"]

    # 7. Authenticated request to /repo/tests -> 200
    resp_tests = client.get("/repo/tests", headers=headers)
    assert resp_tests.status_code == 200
    tests_data = resp_tests.json()
    assert "pytest" in tests_data["test_frameworks"]

    # 8. Authenticated request to /repo/diagnostics -> 200
    resp_diag = client.post(
        "/repo/diagnostics",
        headers=headers,
        json={"query": "conversation 503 persistence error"},
    )
    assert resp_diag.status_code == 200
    diag_data = resp_diag.json()
    assert len(diag_data["facts"]) > 0
    assert len(diag_data["hypotheses"]) > 0
