"""Static configuration, dependency, and environment variable discovery."""

import json
import re
from pathlib import Path

from jarvis.repo.models import ConfigDiscovery, DependencyDeclaration
from jarvis.repo.path_security import safe_resolve_path, validate_repo_root

# Regex for environment variables in Python settings and .env.example
ENV_VAR_REGEX = re.compile(r"\b(JARVIS_[A-Z0-9_]+)\b")
ENV_ASSIGNMENT_REGEX = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)


def parse_pyproject_toml(content: str) -> tuple[list[DependencyDeclaration], list[str], list[str]]:
    """Lightweight parser for pyproject.toml dependencies without external TOML libraries."""
    dependencies: list[DependencyDeclaration] = []
    frameworks: list[str] = []
    test_frameworks: list[str] = []

    in_dependencies = False
    in_dev = False

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("dependencies = ["):
            in_dependencies = True
            in_dev = False
            continue
        elif line.startswith("dev = ["):
            in_dependencies = False
            in_dev = True
            continue
        elif line.startswith("persistence = ["):
            in_dependencies = True
            in_dev = False
            continue
        elif line == "]" or line == "],":
            in_dependencies = False
            in_dev = False
            continue

        if (in_dependencies or in_dev) and line.startswith(('"', "'")):
            dep_str = line.strip('"\', ')
            # Extract package name and specifier
            match = re.match(r"^([a-zA-Z0-9_\-\[\]]+)(.*)$", dep_str)
            if match:
                pkg_name = match.group(1).strip()
                spec = match.group(2).strip() or None
                dependencies.append(
                    DependencyDeclaration(
                        name=pkg_name,
                        specifier=spec,
                        source_manifest="pyproject.toml",
                        is_dev=in_dev,
                    )
                )
                pkg_base = pkg_name.split("[")[0].lower()
                if pkg_base == "fastapi":
                    frameworks.append("FastAPI")
                elif pkg_base == "uvicorn":
                    frameworks.append("Uvicorn")
                elif pkg_base == "pytest":
                    test_frameworks.append("pytest")

    return dependencies, frameworks, test_frameworks


def parse_package_json(content: str) -> tuple[list[DependencyDeclaration], list[str], list[str], list[str]]:
    """Parse package.json dependencies, frameworks, and npm scripts."""
    dependencies: list[DependencyDeclaration] = []
    frameworks: list[str] = []
    test_frameworks: list[str] = []
    build_tools: list[str] = []

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return dependencies, frameworks, test_frameworks, build_tools

    deps = data.get("dependencies", {})
    dev_deps = data.get("devDependencies", {})

    for name, spec in deps.items():
        dependencies.append(
            DependencyDeclaration(
                name=name,
                specifier=str(spec),
                source_manifest="package.json",
                is_dev=False,
            )
        )
        if name.lower() == "react":
            frameworks.append("React")

    for name, spec in dev_deps.items():
        dependencies.append(
            DependencyDeclaration(
                name=name,
                specifier=str(spec),
                source_manifest="package.json",
                is_dev=True,
            )
        )
        if name.lower() == "vite":
            build_tools.append("Vite")
        elif name.lower() == "typescript":
            build_tools.append("TypeScript")
        elif "vitest" in name.lower():
            test_frameworks.append("Vitest")
        elif "playwright" in name.lower():
            test_frameworks.append("Playwright")

    return dependencies, frameworks, test_frameworks, build_tools


def discover_config(repo_root: Path | str) -> ConfigDiscovery:
    """Statically discover configuration, dependencies, and environment variable names."""
    root = validate_repo_root(repo_root)

    languages: set[str] = set()
    frameworks: set[str] = set()
    manifests: list[str] = []
    all_dependencies: list[DependencyDeclaration] = []
    env_vars: set[str] = set()
    build_tools: set[str] = set()
    test_frameworks: set[str] = set()
    startup_commands: list[str] = []
    known_services: set[str] = set()

    # 1. pyproject.toml
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists() and pyproject_path.is_file():
        manifests.append("pyproject.toml")
        languages.add("Python")
        build_tools.add("setuptools")
        try:
            content = pyproject_path.read_text(encoding="utf-8", errors="replace")
            deps, fws, tfws = parse_pyproject_toml(content)
            all_dependencies.extend(deps)
            frameworks.update(fws)
            test_frameworks.update(tfws)
        except OSError:
            pass

    # 2. package.json in root or frontend/
    for pkg_rel in ["package.json", "frontend/package.json"]:
        pkg_path = root / pkg_rel
        if pkg_path.exists() and pkg_path.is_file():
            manifests.append(pkg_rel)
            languages.add("TypeScript" if (root / "tsconfig.json").exists() or (root / "frontend/tsconfig.json").exists() else "JavaScript")
            try:
                content = pkg_path.read_text(encoding="utf-8", errors="replace")
                deps, fws, tfws, bts = parse_package_json(content)
                all_dependencies.extend(deps)
                frameworks.update(fws)
                test_frameworks.update(tfws)
                build_tools.update(bts)
            except OSError:
                pass

    # 3. Environment Variable Discovery (statically from .env.example and config.py)
    # NEVER read .env or values; only collect declared variable names
    for env_source_rel in [".env.example", "jarvis/core/config.py"]:
        env_source = root / env_source_rel
        if env_source.exists() and env_source.is_file():
            try:
                text = env_source.read_text(encoding="utf-8", errors="replace")
                # Find all declared JARVIS_* variable names
                matches = ENV_VAR_REGEX.findall(text)
                env_vars.update(matches)

                # For .env.example, also find any line-starting assignments
                if env_source_rel == ".env.example":
                    for line in text.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            var_name = line.split("=", 1)[0].strip()
                            if var_name.isidentifier():
                                env_vars.add(var_name)
            except OSError:
                pass

    # 4. Service dependencies
    if any("psycopg" in d.name.lower() or "JARVIS_PERSISTENCE" in var for var in env_vars for d in all_dependencies):
        known_services.add("PostgreSQL")
    if any("OPENAI" in var for var in env_vars):
        known_services.add("OpenAI API")

    # 5. Documented startup commands
    if "FastAPI" in frameworks:
        startup_commands.append("uvicorn jarvis.main:app --reload")
    if "Vite" in build_tools:
        startup_commands.append("npm run dev (in frontend/)")
        startup_commands.append("npm run build (in frontend/)")

    return ConfigDiscovery(
        languages=sorted(languages),
        frameworks=sorted(frameworks),
        manifests=sorted(manifests),
        dependencies=all_dependencies,
        env_variable_names=sorted(env_vars),
        build_tools=sorted(build_tools),
        test_frameworks=sorted(test_frameworks),
        startup_commands=startup_commands,
        known_services=sorted(known_services),
    )
