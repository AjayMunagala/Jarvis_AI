"""Bounded static evidence assembled through existing repository safeguards."""

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic

from jarvis.coding.models import CodingRequest, Evidence, Workflow
from jarvis.coding.patches import EDITABLE_SUFFIXES, MAX_SOURCE_BYTES, PatchValidationError, authorized_path, digest, private_coding_path
from jarvis.repo.path_security import read_repository_text
from jarvis.repo.search import extract_python_symbols
from jarvis.repo.service import RepoIntelligenceService
from jarvis.repo.git_intel import identify_affected_tests
from jarvis.repo.secret_redactor import redact_secrets


def classify_request(body: CodingRequest) -> Workflow:
    if body.workflow != "auto":
        return body.workflow
    if body.changes:
        return "review_patch"
    query = body.request.casefold()
    for workflow, terms in (
        ("review_patch", ("review patch", "explain patch", "review diff")),
        ("patch", ("patch", "generate code", "implement")),
        ("tests", ("test", "coverage")),
        ("trace", ("trace", "call path", "relationship")),
        ("locate", ("find", "locate", "where")),
        ("diagnose", ("error", "debug", "defect", "diagnos", "failure")),
        ("recommend", ("recommend", "suggest", "fix")),
    ):
        if any((bool(re.search(r"\bimplement\b", query)) if term == "implement" else term in query) for term in terms):
            return workflow
    return "explain"


@dataclass(frozen=True)
class CollectedEvidence:
    items: tuple[Evidence, ...]
    snapshots: dict[str, str]
    requested_paths: set[str]
    tests: tuple[str, ...]
    limitations: tuple[str, ...]


def collect_evidence(repo: RepoIntelligenceService, body: CodingRequest, *, deadline: float) -> CollectedEvidence:
    def check_time():
        if monotonic() >= deadline:
            raise TimeoutError()

    check_time()
    # Validate every supplied target before any inventory, file read or provider call.
    requested = {authorized_path(repo.repo_root, path) for path in body.target_paths}
    changed = {authorized_path(repo.repo_root, change.path) for change in body.changes}
    explicit = sorted(requested | changed)
    if any(PurePosixPath(path).suffix.lower() not in EDITABLE_SUFFIXES for path in explicit):
        raise PatchValidationError()
    if len(explicit) > 8:
        raise PatchValidationError()
    inventory = repo.get_inventory(max_files=500, max_depth=12, timeout_seconds=min(2.0, max(0.01, deadline - monotonic())))
    terms = tuple(dict.fromkeys(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{2,63}", body.request.casefold())))[:12]
    paths = [item.path for item in inventory.files if PurePosixPath(item.path).suffix.lower() in EDITABLE_SUFFIXES
             and len(item.path) <= 240 and not item.is_binary and item.size_bytes <= MAX_SOURCE_BYTES
             and not private_coding_path(item.path)]
    paths.sort(key=lambda path: (-sum(term in path.casefold() for term in terms), path))
    # Explicit targets win; automatic discovery reads at most 24 files and returns at most 8.
    candidates = explicit if explicit else paths[:24]
    selected = []
    for path in candidates:
        check_time()
        path = authorized_path(repo.repo_root, path)
        raw = read_repository_text(repo.repo_root, path, max_bytes=MAX_SOURCE_BYTES)
        if raw is None or "\x00" in raw or "\ufffd" in raw:
            continue
        score = sum(term in (path + "\n" + raw).casefold() for term in terms)
        selected.append((score, path, raw))
    if not explicit:
        selected.sort(key=lambda item: (-item[0], item[1]))
    snapshots = {}
    items = []
    for _, path, raw in selected[:8]:
        check_time()
        snapshots[path] = raw
        safe, _ = redact_secrets(raw)
        # Source locations count LF lines, not Unicode separators inside literals.
        lines = safe.split("\n")
        hit = next((index for index, line in enumerate(lines) if any(term in line.casefold() for term in terms)), 0)
        start = max(0, hit - 3)
        excerpt = "\n".join(lines[start:])[:1600]
        symbols = ()
        relationships = []
        if path.endswith(".py"):
            symbols = tuple(f"{symbol.name[:200]} at line {symbol.line_number}" for symbol in
                            extract_python_symbols(repo.repo_root / path, path, repo_root=repo.repo_root)[:20])
            try:
                for node in ast.walk(ast.parse(safe)):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        relationships.append(f"Import at line {node.lineno}: {ast.unparse(node)[:240]}")
                    elif isinstance(node, ast.Call):
                        relationships.append(f"Syntactic call at line {node.lineno}: {ast.unparse(node.func)[:240]}")
                    if len(relationships) >= 20:
                        break
            except (SyntaxError, ValueError, RecursionError):
                relationships = []
        items.append(Evidence(id=f"E{len(items)+1}", path=path, sha256=digest(raw), excerpt=excerpt,
                              excerpt_start_line=start+1, truncated=start > 0 or len(excerpt) < len(safe),
                              symbols=symbols, relationships=tuple(relationships)))
    tests = set(identify_affected_tests(list(snapshots)))
    for path in paths:
        name = PurePosixPath(path).name
        if name.startswith("test_") or ".test." in name or ".spec." in name:
            if any(PurePosixPath(source).stem in name for source in snapshots) or any(term in path.casefold() for term in terms):
                tests.add(path)
    existing = []
    for path in sorted(tests)[:20]:
        check_time()
        if len(path) <= 240 and read_repository_text(repo.repo_root, path, max_bytes=MAX_SOURCE_BYTES) is not None:
            existing.append(path)
    limitations = ["Static inspection only; no code, tests, commands or patches were executed.",
                   "Imports and calls are syntactic evidence; runtime resolution and causality are unverified.",
                   "Automatic discovery inspects at most 500 inventory entries, 24 source files and returns 8 excerpts; it is not exhaustive.",
                   "Secret redaction is heuristic. Review context before permitting transmission to the configured provider.",
                   "Filesystem changes can race inspection; review hashes against current files before manual application."]
    if inventory.truncated:
        limitations.append("The bounded repository inventory was truncated.")
    if len(items) < len(explicit) or not items:
        limitations.append("Some requested evidence is missing, excluded, oversized, binary or unreadable.")
    return CollectedEvidence(tuple(items), snapshots, requested, tuple(existing), tuple(limitations))
