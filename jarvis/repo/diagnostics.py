"""Evidence-based coding diagnostics combining static code, Git, and test analysis."""

from pathlib import Path

from jarvis.repo.config_discovery import discover_config
from jarvis.repo.git_intel import get_git_status
from jarvis.repo.models import (
    DiagnosticFact,
    DiagnosticHypothesis,
    DiagnosticReport,
    SymbolType,
)
from jarvis.repo.path_security import safe_resolve_path, validate_repo_root
from jarvis.repo.search import extract_python_symbols, search_filenames
from jarvis.repo.test_discovery import discover_tests_and_build


def generate_diagnostic_report(
    repo_root: Path | str,
    query: str,
    *,
    target_files: list[str] | None = None,
) -> DiagnosticReport:
    """Generate an evidence-based diagnostic report separating confirmed facts from hypotheses."""
    root = validate_repo_root(repo_root)
    query_clean = query.strip()
    query_folded = query_clean.casefold()

    investigated_files: list[str] = []
    facts: list[DiagnosticFact] = []
    hypotheses: list[DiagnosticHypothesis] = []
    suggested_steps: list[str] = []
    suggested_commands: list[str] = []
    limitations: list[str] = [
        "Analysis is strictly static and read-only; no application code or tests were executed.",
        "Hypotheses are potential explanations and must be verified with focused testing.",
    ]

    # 1. Identify files to investigate
    if target_files:
        for tf in target_files:
            try:
                resolved = safe_resolve_path(root, tf, must_exist=True)
                investigated_files.append(tf)
            except Exception:
                continue
    else:
        # Infer relevant files from search terms in query
        terms = [t for t in query_clean.replace("/", " ").replace(".", " ").split() if len(t) > 2]
        candidate_paths: set[str] = set()
        for term in terms[:5]:
            matches = search_filenames(root, term, max_results=5)
            for m in matches.matches:
                candidate_paths.add(m.path)
        investigated_files = sorted(candidate_paths)[:10]

    # 2. Gather Facts from Investigated Files
    git_status = get_git_status(root)
    test_disc = discover_tests_and_build(root)
    cfg_disc = discover_config(root)

    for rel_path in investigated_files:
        try:
            resolved = safe_resolve_path(root, rel_path, must_exist=True)
            stat = resolved.stat()
            facts.append(
                DiagnosticFact(
                    description=f"File '{rel_path}' exists ({stat.st_size} bytes).",
                    source_file=rel_path,
                )
            )

            if rel_path.endswith(".py"):
                symbols = extract_python_symbols(resolved, rel_path)
                for sym in symbols:
                    if sym.symbol_type == SymbolType.ROUTE_HANDLER:
                        facts.append(
                            DiagnosticFact(
                                description=f"Declares API route handler '{sym.name}' [{sym.route_method} {sym.route_path}] at line {sym.line_number}.",
                                source_file=rel_path,
                                line_number=sym.line_number,
                            )
                        )
                    elif sym.symbol_type == SymbolType.CLASS_DEF and ("repository" in sym.name.lower() or "service" in sym.name.lower()):
                        facts.append(
                            DiagnosticFact(
                                description=f"Defines component class '{sym.name}' at line {sym.line_number}.",
                                source_file=rel_path,
                                line_number=sym.line_number,
                            )
                        )
        except Exception:
            continue

    # 3. Check Git Status for Investigated Files
    if git_status.is_repo:
        all_changed = {f.path: f for f in git_status.staged_files + git_status.unstaged_files}
        for rel_path in investigated_files:
            if rel_path in all_changed:
                ch = all_changed[rel_path]
                state = "staged" if ch.is_staged else "unstaged"
                facts.append(
                    DiagnosticFact(
                        description=f"File '{rel_path}' has uncommitted {state} changes (status: {ch.status_code}).",
                        source_file=rel_path,
                    )
                )

    # 4. Relate Tests
    for rel_path in investigated_files:
        mapped_tests = test_disc.module_to_test_map.get(rel_path, [])
        for t in mapped_tests:
            facts.append(
                DiagnosticFact(
                    description=f"Relevant test coverage identified in '{t}'.",
                    source_file=t,
                )
            )
            suggested_commands.append(f"python -m pytest {t} -v")

    # 5. Formulate Hypotheses based on Query Indicators
    if "503" in query_folded or "persistence" in query_folded or "database" in query_folded:
        hypotheses.append(
            DiagnosticHypothesis(
                description="Endpoint may be returning HTTP 503 because conversation/memory persistence is disabled or unavailable.",
                rationale="Routes check 'repository is None' or catch PersistenceError to return sanitized 503.",
                likelihood="high" if "503" in query_folded else "plausible",
                confirmation_needed="Verify JARVIS_PERSISTENCE_ENABLED setting and database connectivity.",
            )
        )
        suggested_steps.append("Check application settings to confirm if JARVIS_PERSISTENCE_ENABLED=true.")
        suggested_steps.append("Check database connectivity logs for connection timeout or authentication error.")

    if "401" in query_folded or "unauthorized" in query_folded or "token" in query_folded:
        hypotheses.append(
            DiagnosticHypothesis(
                description="Endpoint returned HTTP 401 due to missing or invalid Bearer token.",
                rationale="authenticated_owner requires a valid Bearer token matching JARVIS_LOCAL_AUTH_TOKEN.",
                likelihood="high",
                confirmation_needed="Check if the request carries 'Authorization: Bearer <token>' matching the configured secret.",
            )
        )
        suggested_steps.append("Verify request headers include the required Bearer token.")

    if "504" in query_folded or "timeout" in query_folded:
        hypotheses.append(
            DiagnosticHypothesis(
                description="Operation exceeded monotonic execution deadline or database operation timeout.",
                rationale="Chat and database operations enforce strict bounded monotonic deadlines.",
                likelihood="high",
                confirmation_needed="Check JARVIS_REQUEST_TIMEOUT_SECONDS and PERSISTENCE_OPERATION_TIMEOUT_SECONDS.",
            )
        )
        suggested_steps.append("Inspect provider response time and database query latency.")

    # Generic hypothesis if modified files exist
    modified_in_query = [f for f in investigated_files if git_status.is_repo and any(c.path == f for c in git_status.unstaged_files + git_status.staged_files)]
    if modified_in_query:
        hypotheses.append(
            DiagnosticHypothesis(
                description=f"Working-tree modifications in {modified_in_query} may have altered runtime behavior.",
                rationale="Uncommitted changes in active modules directly influence application logic.",
                likelihood="plausible",
                confirmation_needed="Inspect git diff for the modified files and run the corresponding test suite.",
            )
        )
        suggested_steps.append("Review uncommitted diffs in the modified files.")

    # Add general suggested steps
    suggested_steps.append("Execute discovered unit tests for the investigated modules.")

    # Deduplicate suggested commands
    unique_commands = list(dict.fromkeys(suggested_commands))[:5]
    if "python -m pytest tests/ -v" not in unique_commands:
        unique_commands.append("python -m pytest tests/ -v")

    return DiagnosticReport(
        problem_statement=query_clean,
        investigated_files=investigated_files,
        facts=facts,
        hypotheses=hypotheses,
        confirmed_issues=[],
        suggested_steps=suggested_steps,
        suggested_commands=unique_commands,
        confidence="high" if facts and hypotheses else "medium",
        limitations=limitations,
    )
