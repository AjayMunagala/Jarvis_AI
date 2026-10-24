"""Test and build infrastructure discovery and test-to-module mapping."""

import json
from pathlib import Path

from jarvis.repo.inventory import scan_repository
from jarvis.repo.models import TestDiscovery
from jarvis.repo.path_security import read_repository_text, validate_repo_root


def discover_tests_and_build(repo_root: Path | str) -> TestDiscovery:
    """Discover test frameworks, test files, build commands, and module-to-test mappings."""
    root = validate_repo_root(repo_root)
    inventory = scan_repository(root)

    test_frameworks: set[str] = set()
    test_directories: set[str] = set()
    test_files: list[str] = []
    test_commands: list[str] = []
    module_to_test_map: dict[str, list[str]] = {}

    # 1. Identify test files and directories
    for f in inventory.files:
        path_lower = f.path.lower()
        if (
            path_lower.startswith("tests/")
            or path_lower.startswith("integration_tests/")
            or "/tests/" in path_lower
            or "/__tests__/" in path_lower
            or ".test." in path_lower
            or ".spec." in path_lower
            or Path(f.path).name.startswith("test_")
            or Path(f.path).name.endswith("_test.py")
        ):
            test_files.append(f.path)
            test_directories.add(Path(f.path).parent.as_posix())

    # 2. Check pytest configuration
    content = read_repository_text(root, "pyproject.toml")
    if content is not None:
        if "pytest" in content or any(f.endswith(".py") for f in test_files):
            test_frameworks.add("pytest")
            test_commands.append("python -m pytest -v")
            test_commands.append("python -m pytest tests/")

    # 3. Check npm test scripts
    for pkg_rel in ["package.json", "frontend/package.json"]:
        content = read_repository_text(root, pkg_rel)
        if content is not None:
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    continue
                scripts = data.get("scripts", {})
                scripts = scripts if isinstance(scripts, dict) else {}
                scripts = {name: value for name, value in scripts.items() if isinstance(value, str)}
                prefix = f"npm --prefix {Path(pkg_rel).parent.as_posix()} " if Path(pkg_rel).parent.name else "npm "
                if "test" in scripts:
                    test_commands.append(f"{prefix}run test" if prefix != "npm " else "npm test")
                if "test:e2e" in scripts:
                    test_frameworks.add("Playwright")
                    test_commands.append(f"{prefix}run test:e2e")
                dev_deps = data.get("devDependencies", {})
                dev_deps = dev_deps if isinstance(dev_deps, dict) else {}
                if "vitest" in dev_deps or "vitest" in scripts.get("test", ""):
                    test_frameworks.add("Vitest")
            except (json.JSONDecodeError, OSError):
                pass

    # 4. Infer module-to-test mapping based on naming conventions and imports
    all_source_files = [f.path for f in inventory.files if f.category.value == "source"]

    for src in all_source_files:
        src_path = Path(src)
        src_stem = src_path.stem  # e.g., 'conversations' or 'App'
        matching_tests: list[str] = []

        for tf in test_files:
            tf_stem = Path(tf).stem
            # Direct name matching e.g. test_conversations.py or App.test.tsx
            if tf_stem == f"test_{src_stem}" or tf_stem == f"{src_stem}.test":
                matching_tests.append(tf)
            elif src_stem in tf_stem and len(src_stem) > 3:
                matching_tests.append(tf)

        if matching_tests:
            module_to_test_map[src] = sorted(set(matching_tests))

    # Add core known subsystem mappings for Project JARVIS
    core_mappings = {
        "jarvis/api/routes/conversations.py": ["tests/test_api.py", "integration_tests/test_postgres_integration.py"],
        "jarvis/api/routes/chat.py": ["tests/test_api.py", "tests/test_e2e_workflows.py"],
        "jarvis/api/routes/health.py": ["tests/test_health.py", "tests/test_api.py"],
        "jarvis/api/routes/memories.py": ["tests/test_api.py", "tests/test_memory_ingestion.py"],
        "jarvis/api/dependencies.py": ["tests/test_auth.py", "tests/test_policy.py"],
        "jarvis/persistence/postgres.py": ["tests/test_persistence.py", "integration_tests/test_postgres_integration.py"],
        "jarvis/security/policy.py": ["tests/test_policy.py"],
        "jarvis/memory/ingestion.py": ["tests/test_memory_ingestion.py"],
        "frontend/src/App.tsx": ["frontend/src/App.test.tsx", "frontend/e2e/jarvis.e2e.mjs"],
        "frontend/src/components/MemoryManager.tsx": ["frontend/src/components/MemoryManager.test.tsx"],
        "frontend/src/services/api.ts": ["frontend/src/services/api.test.ts"],
    }
    for mod, tests in core_mappings.items():
        if any(f.path == mod for f in inventory.files):
            existing = module_to_test_map.setdefault(mod, [])
            for t in tests:
                if t not in existing and any(f.path == t for f in inventory.files):
                    existing.append(t)

    return TestDiscovery(
        test_frameworks=sorted(test_frameworks),
        test_directories=sorted(test_directories),
        test_files=sorted(test_files),
        test_commands=sorted(test_commands),
        module_to_test_map=module_to_test_map,
    )
