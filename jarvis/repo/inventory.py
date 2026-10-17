"""Bounded repository inventory and file discovery."""

import fnmatch
import os
import time
from pathlib import Path

from jarvis.repo.models import FileCategory, FileInfo, RepositoryInventory
from jarvis.repo.path_security import to_relative_posix, validate_repo_root

DEFAULT_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    ".venv-dev", "dist", "build", "out", "target", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".vite",
    ".next", "coverage", ".idea", ".vscode", "tmp", "temp",
})

DEFAULT_IGNORED_PATTERNS = frozenset({
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dll", "*.exe", ".DS_Store",
    "Thumbs.db", ".env", ".env.local", "*.log", "*.swp", "*~",
})

EXTENSION_LANGUAGE_MAP = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".json": "JSON",
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".ps1": "PowerShell",
    ".bat": "Batch",
    ".cmd": "Batch",
    ".xml": "XML",
}

MANIFEST_FILENAMES = frozenset({
    "pyproject.toml", "requirements.txt", "package.json", "package-lock.json",
    "Cargo.toml", "go.mod", "pom.xml", "setup.py", "setup.cfg",
})

BINARY_EXTENSIONS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".png",
    ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".zip", ".tar", ".gz",
    ".whl", ".pdf", ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".wav",
})


def categorize_file(rel_path: str, ext: str) -> FileCategory:
    """Classify a relative path into a FileCategory."""
    path_lower = rel_path.lower()
    base_name = Path(rel_path).name.lower()

    if base_name in MANIFEST_FILENAMES or "requirements" in base_name:
        return FileCategory.MANIFEST
    if "/tests/" in path_lower or path_lower.startswith("tests/") or "/integration_tests/" in path_lower or path_lower.startswith("integration_tests/"):
        return FileCategory.TEST
    if ".test." in path_lower or ".spec." in path_lower or base_name.startswith("test_") or base_name.endswith("_test.py"):
        return FileCategory.TEST
    if "/docs/" in path_lower or path_lower.startswith("docs/") or ext in {".md", ".markdown", ".rst"}:
        return FileCategory.DOCUMENTATION
    if ext in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"} or base_name.startswith(".env.example"):
        return FileCategory.CONFIG
    if base_name in {"makefile", "dockerfile"} or path_lower.endswith(".dockerfile"):
        return FileCategory.BUILD
    if ext in BINARY_EXTENSIONS:
        return FileCategory.ASSET
    if ext in EXTENSION_LANGUAGE_MAP:
        return FileCategory.SOURCE

    return FileCategory.UNKNOWN


def scan_repository(
    repo_root: Path | str,
    *,
    max_files: int = 5000,
    max_file_size_bytes: int = 512 * 1024,
    max_depth: int = 15,
    timeout_seconds: float = 10.0,
    extra_ignored_dirs: set[str] | None = None,
) -> RepositoryInventory:
    """Perform a bounded, read-only inventory scan of a repository directory."""
    start_time = time.monotonic()
    root = validate_repo_root(repo_root)

    ignored_dirs = DEFAULT_IGNORED_DIRS | (extra_ignored_dirs or set())
    files_collected: list[FileInfo] = []
    category_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    manifests: list[str] = []
    entry_points: list[str] = []
    test_dirs: set[str] = set()
    doc_files: list[str] = []

    total_bytes = 0
    truncated = False

    for current_root, dir_names, filenames in os.walk(root):
        if time.monotonic() - start_time > timeout_seconds:
            truncated = True
            break

        # Calculate depth
        rel_root = Path(current_root).relative_to(root)
        depth = len(rel_root.parts)
        if depth >= max_depth:
            dir_names.clear()
            continue

        # Prune ignored directories in-place
        dir_names[:] = [d for d in dir_names if d not in ignored_dirs and not d.startswith(".")]

        for filename in filenames:
            if time.monotonic() - start_time > timeout_seconds:
                truncated = True
                break
            if len(files_collected) >= max_files:
                truncated = True
                break

            # Ignore pattern check
            if any(fnmatch.fnmatch(filename, pat) for pat in DEFAULT_IGNORED_PATTERNS):
                continue

            file_path = Path(current_root) / filename
            try:
                # Do not follow symlinks pointing outside
                stat = file_path.stat()
                file_size = stat.st_size
            except (OSError, ValueError):
                continue

            rel_posix = to_relative_posix(root, file_path)
            ext = file_path.suffix.lower()
            lang = EXTENSION_LANGUAGE_MAP.get(ext)
            category = categorize_file(rel_posix, ext)
            is_bin = ext in BINARY_EXTENSIONS or (file_size > 0 and ext not in EXTENSION_LANGUAGE_MAP and ext not in {".txt", ".json", ".md", ".xml", ".html", ".css", ".csv"})
            is_oversized = file_size > max_file_size_bytes

            info = FileInfo(
                path=rel_posix,
                size_bytes=file_size,
                language=lang,
                category=category,
                is_binary=is_bin,
                is_oversized=is_oversized,
            )
            files_collected.append(info)
            total_bytes += file_size

            # Aggregate stats
            category_counts[category.value] = category_counts.get(category.value, 0) + 1
            if lang:
                language_counts[lang] = language_counts.get(lang, 0) + 1

            if category == FileCategory.MANIFEST:
                manifests.append(rel_posix)
            if category == FileCategory.TEST:
                test_dirs.add(Path(rel_posix).parent.as_posix())
            if category == FileCategory.DOCUMENTATION:
                doc_files.append(rel_posix)

            # Detect common entrypoints
            if filename in {"main.py", "app.py", "server.py", "index.ts", "index.tsx", "main.tsx", "index.html"}:
                entry_points.append(rel_posix)

    scan_duration_ms = round((time.monotonic() - start_time) * 1000, 2)

    return RepositoryInventory(
        root_path=root.as_posix(),
        total_files=len(files_collected),
        total_size_bytes=total_bytes,
        categories=category_counts,
        languages=language_counts,
        manifests=sorted(manifests),
        entry_points=sorted(entry_points),
        test_directories=sorted(test_dirs),
        documentation_files=sorted(doc_files),
        files=files_collected,
        truncated=truncated,
        scan_duration_ms=scan_duration_ms,
    )
