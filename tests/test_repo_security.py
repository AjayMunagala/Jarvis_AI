"""Service and HTTP regressions for repository file-access boundaries.

Native-link cases intentionally skip where Windows denies link creation. The
mocked-lstat cases exercise the same callers without claiming native coverage.
"""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.repo.config_discovery import parse_package_json
from jarvis.repo.git_intel import get_git_status
from jarvis.repo.path_security import PathSecurityError, read_repository_text, safe_resolve_path, to_relative_posix
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "synthetic-repository-regression-token-0123456789"


def make_client(root):
    app = create_app(settings=Settings(
        _env_file=None, environment="test", persistence_enabled=False,
        local_auth_token=SecretStr(TOKEN), owner_id=OWNER,
    ))
    app.state.repo_service = RepoIntelligenceService(root)
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def install_link(monkeypatch, alias, target, mode, *, directory=False):
    alias.parent.mkdir(parents=True, exist_ok=True)
    if mode == "native":
        try:
            alias.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            pytest.skip(f"Native symlink unavailable: winerror={getattr(exc, 'winerror', None)}, errno={exc.errno}")
    else:
        if directory:
            alias.mkdir()
            (alias / "unsafe.py").write_text("UNSAFE_TARGET", encoding="utf-8")
        else:
            alias.write_text("UNSAFE_TARGET", encoding="utf-8")
        original = Path.lstat

        def linked_lstat(path):
            if path == alias:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return original(path)

        monkeypatch.setattr(Path, "lstat", linked_lstat)

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path != target and path != alias and alias not in path.parents, "Prohibited target was opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)


@pytest.mark.parametrize("mode", ["mocked_lstat", "native"])
@pytest.mark.parametrize("scope", ["internal", "external"])
@pytest.mark.parametrize("relative", [
    "pyproject.toml", "package.json", "frontend/package.json", ".env.example", "jarvis/core/config.py",
])
def test_manifest_links_never_read_by_services_or_api(tmp_path, monkeypatch, mode, scope, relative):
    root = tmp_path / "repo"
    root.mkdir()
    target_dir = root / ".private" if scope == "internal" else tmp_path / "outside"
    target_dir.mkdir()
    target = target_dir / "manifest.txt"
    target.write_text('UNSAFE_TARGET', encoding="utf-8")
    install_link(monkeypatch, root / relative, target, mode)
    service = RepoIntelligenceService(root)
    for result in (service.get_configuration(), service.get_test_discovery()):
        assert "UNSAFE_TARGET" not in result.model_dump_json()
    assert relative not in service.get_configuration().manifests
    with make_client(root) as client:
        for endpoint in ("/repo/config", "/repo/tests"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert "UNSAFE_TARGET" not in response.text


@pytest.mark.parametrize("mode", ["mocked_lstat", "native"])
@pytest.mark.parametrize("target_kind", ["internal", "external", "protected", "directory"])
def test_inventory_search_and_diagnostics_reject_aliases(tmp_path, monkeypatch, mode, target_kind):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ordinary.py").write_text("def permitted():\n    return 'ORDINARY_MARKER'\n", encoding="utf-8")
    target = {
        "internal": root / ".private" / "target.py",
        "external": tmp_path / "outside.py",
        "protected": root / ".env",
        "directory": tmp_path / "outside-directory",
    }[target_kind]
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = target_kind == "directory"
    if directory:
        target.mkdir()
        (target / "unsafe.py").write_text("UNSAFE_TARGET", encoding="utf-8")
    else:
        target.write_text("UNSAFE_TARGET", encoding="utf-8")
    alias = root / ("alias_dir" if directory else "alias.py")
    install_link(monkeypatch, alias, target, mode, directory=directory)
    service = RepoIntelligenceService(root)
    assert [item.path for item in service.get_inventory().files] == ["ordinary.py"]
    assert service.search("alias", search_type="filename").total_matches == 0
    assert service.search("UNSAFE_TARGET").total_matches == 0
    assert service.find_symbols("unsafe") == []
    assert service.search("ORDINARY_MARKER").total_matches == 1
    assert service.find_symbols("permitted")[0].path == "ordinary.py"
    assert service.diagnose("inspect", target_files=[alias.name, ".env", "../outside.py", "C:unsafe", "bad\x00path"]).investigated_files == []
    with make_client(root) as client:
        response = client.get("/repo/inventory")
        assert response.status_code == 200
        assert [item["path"] for item in response.json()["files"]] == ["ordinary.py"]
        response = client.post("/repo/search", json={"query": "UNSAFE_TARGET"})
        assert response.status_code == 200 and response.json()["matches"] == []
        assert "UNSAFE_TARGET" not in client.get("/repo/symbols?query=unsafe").text
        response = client.post("/repo/diagnostics", json={"query": "inspect", "target_files": [alias.name, ".env", "../outside.py"]})
        assert response.status_code == 200 and response.json()["investigated_files"] == []
        assert "UNSAFE_TARGET" not in response.text
        response = client.post("/repo/search", json={"query": "ORDINARY_MARKER"})
        assert response.status_code == 200 and response.json()["total_matches"] == 1


def test_ordinary_manifests_remain_available(tmp_path):
    (tmp_path / "pyproject.toml").write_text("dependencies = [\n 'fastapi>=0.110',\n]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18"}, "scripts": {"test": "vitest"},
        "devDependencies": {"vitest": "^5"},
    }), encoding="utf-8")
    (tmp_path / ".env.example").write_text("JARVIS_PROVIDER=mock\n", encoding="utf-8")
    service = RepoIntelligenceService(tmp_path)
    assert {"pyproject.toml", "package.json"} <= set(service.get_configuration().manifests)
    assert "JARVIS_PROVIDER" in service.get_configuration().env_variable_names
    assert "Vitest" in service.get_test_discovery().test_frameworks
    with make_client(tmp_path) as client:
        assert "React" in client.get("/repo/config").json()["frameworks"]
        assert "Vitest" in client.get("/repo/tests").json()["test_frameworks"]


@pytest.mark.parametrize("endpoint,body", [
    ("/repo/inventory", None), ("/repo/config", None), ("/repo/tests", None),
    ("/repo/git", None), ("/repo/symbols?query=ordinary", None),
    ("/repo/search", {"query": "ordinary"}), ("/repo/diagnostics", {"query": "ordinary"}),
])
def test_repository_capability_denial_happens_before_file_access(tmp_path, monkeypatch, endpoint, body):
    class DenyRepository(CapabilityPolicy):
        def allows(self, owner_id, capability, **kwargs):
            return False if capability is Capability.REPO_READ else super().allows(owner_id, capability, **kwargs)

    with make_client(tmp_path) as client:
        client.app.state.authorization_policy = DenyRepository(OWNER)

        def forbidden_open(*args, **kwargs):
            raise AssertionError("Denied operation reached repository file access")

        monkeypatch.setattr(Path, "open", forbidden_open)
        response = client.get(endpoint) if body is None else client.post(endpoint, json=body)
        assert response.status_code == 403
        client.headers.pop("Authorization")
        response = client.get(endpoint) if body is None else client.post(endpoint, json=body)
        assert response.status_code == 401


def test_reader_bounds_exclusions_and_unsafe_paths(tmp_path, monkeypatch):
    (tmp_path / "ordinary.txt").write_text("12345", encoding="utf-8")
    assert read_repository_text(tmp_path, "ordinary.txt", max_bytes=5) == "12345"
    assert read_repository_text(tmp_path, "ordinary.txt", max_bytes=4) is None
    (tmp_path / ".env").write_text("SYNTHETIC_PRIVATE", encoding="utf-8")
    assert read_repository_text(tmp_path, ".env") is None
    for candidate in ("../outside", "C:relative", "file:stream", "bad\x00path", "con.py", "missing.txt"):
        assert read_repository_text(tmp_path, candidate) is None
    with pytest.raises(PathSecurityError):
        to_relative_posix(tmp_path, tmp_path.parent / "outside")

    # Simulate growth after a successful small stat: bytes still cannot exceed the cap.
    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, **kw: SimpleNamespace(st_mode=stat.S_IFREG, st_size=1)
                        if path == tmp_path / "ordinary.txt" else original_stat(path, **kw))
    assert read_repository_text(tmp_path, "ordinary.txt", max_bytes=4) is None


def test_reparse_directory_is_rejected_before_traversal(tmp_path, monkeypatch):
    directory = tmp_path / "junction"
    directory.mkdir()
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                        if path == directory else original(path))
    with pytest.raises(PathSecurityError, match="reparse"):
        safe_resolve_path(tmp_path, "junction/file.txt")
    assert RepoIntelligenceService(tmp_path).get_inventory().total_files == 0


@pytest.mark.parametrize("payload", ['[]', 'null', '{"dependencies": [], "devDependencies": []}', '{"scripts": [], "devDependencies": null}'])
def test_malformed_manifest_shapes_fail_safely(tmp_path, payload):
    (tmp_path / "package.json").write_text(payload, encoding="utf-8")
    parse_package_json(payload)
    with make_client(tmp_path) as client:
        assert client.get("/repo/config").status_code == 200
        assert client.get("/repo/tests").status_code == 200


def test_symbol_and_manifest_metadata_redacts_known_secrets(tmp_path):
    synthetic_key = "sk-" + "a" * 40
    (tmp_path / "ordinary.py").write_text(
        f'@app.get("/ordinary", token="{synthetic_key}")\ndef ordinary():\n    """{synthetic_key}"""\n    pass\n', encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"synthetic": synthetic_key}}), encoding="utf-8")
    service = RepoIntelligenceService(tmp_path)
    assert synthetic_key not in service.find_symbols("ordinary")[0].model_dump_json()
    assert synthetic_key not in service.get_configuration().model_dump_json()
    with make_client(tmp_path) as client:
        assert synthetic_key not in client.get("/repo/symbols?query=ordinary").text
        assert synthetic_key not in client.get("/repo/config").text


def test_git_inspection_disables_configured_helpers_and_optional_writes(tmp_path, monkeypatch):
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        if "--is-inside-work-tree" in command:
            output = "true\n"
        elif "--numstat" in command:
            output = "1\t0\tordinary.py\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("jarvis.repo.git_intel.shutil.which", lambda _: "git")
    monkeypatch.setattr("jarvis.repo.git_intel.subprocess.run", run)
    get_git_status(tmp_path)
    assert observed
    for command, options in observed:
        assert command[1:4] == ["--no-optional-locks", "-c", "core.fsmonitor=false"]
        assert options["shell"] is False
        assert not any(key.upper().startswith("GIT_") for key in options["env"])
        if "diff" in command:
            assert "--no-ext-diff" in command and "--no-textconv" in command


def test_search_redacts_multiline_keys_before_truncation_and_context_selection(tmp_path):
    private_body = "SYNTHETIC_PRIVATE_MATERIAL_" + "x" * 600
    (tmp_path / "example.txt").write_text(
        f"before\n-----BEGIN PRIVATE KEY-----\n{private_body}\n-----END PRIVATE KEY-----\nafter\n",
        encoding="utf-8",
    )
    service = RepoIntelligenceService(tmp_path)
    for query in ("PRIVATE", "SYNTHETIC_PRIVATE_MATERIAL", "before", "after"):
        results = service.search(query)
        assert results.total_matches > 0
        assert private_body[:100] not in results.model_dump_json()
        assert not any(private_body[:100] in match.matched_text for match in results.matches)
    assert service.search("after").matches[0].line_number == 5
    with make_client(tmp_path) as client:
        response = client.post("/repo/search", json={"query": "PRIVATE"})
        assert response.status_code == 200
        assert private_body[:100] not in response.text


def test_git_diff_redacts_whole_private_key_before_output_truncation(tmp_path, monkeypatch):
    body = "SYNTHETIC_PRIVATE_MATERIAL" * 5000

    def command(root, args):
        if "--is-inside-work-tree" in args:
            return 0, "true\n", ""
        if "--numstat" in args:
            return 0, "1\t0\tordinary.txt\n", ""
        if args[0] == "diff":
            return 0, f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----", ""
        return 0, "", ""

    monkeypatch.setattr("jarvis.repo.git_intel._run_git_cmd", command)
    result = get_git_status(tmp_path)
    assert "SYNTHETIC_PRIVATE_MATERIAL" not in result.model_dump_json()
    assert "[REDACTED_SECRET: PRIVATE_KEY]" in result.diff_entries[0].diff_content


def test_symbol_search_contract_and_sanitized_regex_errors(tmp_path):
    (tmp_path / "ordinary.py").write_text(
        "def declared_symbol():\n    pass\n# comment_only_marker\n", encoding="utf-8",
    )
    service = RepoIntelligenceService(tmp_path)
    assert service.search("comment_only_marker", search_type="symbol").matches == []
    assert service.search("declared_symbol", search_type="symbol").matches[0].matched_text == "declared_symbol"
    with make_client(tmp_path) as client:
        response = client.post("/repo/search", json={"query": "declared_symbol", "search_type": "symbol"})
        assert response.status_code == 200 and response.json()["search_type"] == "symbol"
        assert response.json()["matches"][0]["match_type"] == "symbol"
        response = client.post("/repo/search", json={"query": "(?P<SYNTHETIC_PRIVATE", "is_regex": True})
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid regular expression."
        assert "SYNTHETIC_PRIVATE" not in response.text


def test_sensitive_variants_and_windows_alias_paths_are_excluded(tmp_path):
    for name in (".env", ".ENV.LOCAL", ".env.production", ".env.backup"):
        (tmp_path / name).write_text("SYNTHETIC_PRIVATE", encoding="utf-8")
        assert read_repository_text(tmp_path, name) is None
    directory = tmp_path / "NODE_MODULES"
    directory.mkdir()
    (directory / "private.txt").write_text("SYNTHETIC_PRIVATE", encoding="utf-8")
    assert read_repository_text(tmp_path, "NODE_MODULES/private.txt") is None
    assert RepoIntelligenceService(tmp_path).get_inventory().files == []
    for candidate in ("ordinary. /private.txt", "ordinary./private.txt", "con .txt"):
        assert read_repository_text(tmp_path, candidate) is None


def test_off_root_absolute_path_rejected_before_target_resolution(tmp_path, monkeypatch):
    original_resolve = Path.resolve

    def guarded_resolve(path, **kwargs):
        assert path == tmp_path, "Off-root target resolution was attempted"
        return original_resolve(path, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(PathSecurityError):
        safe_resolve_path(tmp_path, tmp_path.parent / "outside", allow_symlinks=True)
    if os.name == "nt":
        with pytest.raises(PathSecurityError):
            safe_resolve_path(tmp_path, r"\\example.invalid\share\file", allow_symlinks=True)
