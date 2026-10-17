"""Bounded file, text, and symbol search across approved repository files."""

import ast
import re
from pathlib import Path

from jarvis.repo.inventory import scan_repository
from jarvis.repo.models import SearchMatch, SearchSummary, SymbolInfo, SymbolType
from jarvis.repo.path_security import safe_resolve_path, validate_repo_root
from jarvis.repo.secret_redactor import redact_secrets

MAX_LINE_LENGTH = 500
DEFAULT_MAX_RESULTS = 50
MAX_SEARCH_RESULTS = 200


def search_filenames(
    repo_root: Path | str,
    pattern: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> SearchSummary:
    """Search for files whose path or name contains the pattern."""
    root = validate_repo_root(repo_root)
    inventory = scan_repository(root)
    query_folded = pattern.casefold()

    matches: list[SearchMatch] = []
    for f in inventory.files:
        if query_folded in f.path.casefold():
            matches.append(
                SearchMatch(
                    path=f.path,
                    match_type="filename",
                    matched_text=pattern,
                    line_text=f.path,
                )
            )
            if len(matches) >= min(max_results, MAX_SEARCH_RESULTS):
                break

    matched_files = {m.path for m in matches}
    return SearchSummary(
        query=pattern,
        search_type="filename",
        total_matches=len(matches),
        matched_files_count=len(matched_files),
        matches=matches,
        truncated=len(matches) >= min(max_results, MAX_SEARCH_RESULTS),
    )


def search_text(
    repo_root: Path | str,
    query: str,
    *,
    is_regex: bool = False,
    case_sensitive: bool = False,
    path_filter: str | None = None,
    context_lines: int = 2,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_file_size_bytes: int = 512 * 1024,
) -> SearchSummary:
    """Bounded text search across non-binary repository files with secret redaction."""
    root = validate_repo_root(repo_root)
    if not query.strip():
        return SearchSummary(query=query, search_type="text", total_matches=0, matched_files_count=0)

    compiled_pattern = None
    if is_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled_pattern = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
    else:
        needle = query if case_sensitive else query.casefold()

    inventory = scan_repository(root)
    matches: list[SearchMatch] = []
    matched_files: set[str] = set()
    limit = min(max_results, MAX_SEARCH_RESULTS)
    truncated = False

    for file_info in inventory.files:
        if file_info.is_binary or file_info.is_oversized:
            continue
        if path_filter and path_filter.casefold() not in file_info.path.casefold():
            continue

        resolved = safe_resolve_path(root, file_info.path, must_exist=True)
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        for idx, line in enumerate(lines, start=1):
            line_str = line.rstrip("\r\n")
            is_match = False
            matched_substr = query

            if compiled_pattern:
                m = compiled_pattern.search(line_str)
                if m:
                    is_match = True
                    matched_substr = m.group(0)
            else:
                line_to_check = line_str if case_sensitive else line_str.casefold()
                if needle in line_to_check:
                    is_match = True

            if is_match:
                # Capture surrounding context
                start_ctx = max(0, idx - 1 - context_lines)
                end_ctx = min(len(lines), idx + context_lines)

                before = [lines[i].rstrip("\r\n")[:MAX_LINE_LENGTH] for i in range(start_ctx, idx - 1)]
                after = [lines[i].rstrip("\r\n")[:MAX_LINE_LENGTH] for i in range(idx, end_ctx)]

                # Redact secrets from matched lines and context
                safe_line, _ = redact_secrets(line_str[:MAX_LINE_LENGTH])
                safe_before = [redact_secrets(b)[0] for b in before]
                safe_after = [redact_secrets(a)[0] for a in after]
                safe_matched_substr, _ = redact_secrets(matched_substr)

                match_entry = SearchMatch(
                    path=file_info.path,
                    line_number=idx,
                    match_type="regex" if is_regex else "text",
                    matched_text=safe_matched_substr,
                    line_text=safe_line,
                    context_before=safe_before,
                    context_after=safe_after,
                    is_truncated=len(line_str) > MAX_LINE_LENGTH,
                )
                matches.append(match_entry)
                matched_files.add(file_info.path)

                if len(matches) >= limit:
                    truncated = True
                    break

        if truncated:
            break

    return SearchSummary(
        query=query,
        search_type="regex" if is_regex else "text",
        total_matches=len(matches),
        matched_files_count=len(matched_files),
        matches=matches,
        truncated=truncated,
    )


def extract_python_symbols(file_path: Path, rel_path: str) -> list[SymbolInfo]:
    """Statically parse a Python file using ast to extract symbols and route declarations."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, ValueError, OSError):
        return []

    symbols: list[SymbolInfo] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node)
            symbols.append(
                SymbolInfo(
                    name=node.name,
                    symbol_type=SymbolType.CLASS_DEF,
                    path=rel_path,
                    line_number=node.lineno,
                    end_line_number=getattr(node, "end_lineno", node.lineno),
                    docstring=doc[:200] if doc else None,
                    decorators=[ast.unparse(d) for d in node.decorator_list if hasattr(ast, "unparse")],
                )
            )
            # Inspect class methods
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_doc = ast.get_docstring(item)
                    sym_type = SymbolType.ASYNC_FUNCTION_DEF if isinstance(item, ast.AsyncFunctionDef) else SymbolType.FUNCTION_DEF
                    symbols.append(
                        SymbolInfo(
                            name=f"{node.name}.{item.name}",
                            symbol_type=sym_type,
                            path=rel_path,
                            line_number=item.lineno,
                            end_line_number=getattr(item, "end_lineno", item.lineno),
                            docstring=m_doc[:200] if m_doc else None,
                        )
                    )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            decorators = [ast.unparse(d) for d in node.decorator_list if hasattr(ast, "unparse")]
            route_method = None
            route_path = None

            # Detect FastAPI/Starlette route decorator: @router.get("/..."), @app.post("...")
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    method_name = dec.func.attr.lower()
                    if method_name in {"get", "post", "put", "delete", "patch", "options", "head"}:
                        route_method = method_name.upper()
                        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                            route_path = dec.args[0].value

            sym_type = SymbolType.ROUTE_HANDLER if route_method else (
                SymbolType.ASYNC_FUNCTION_DEF if isinstance(node, ast.AsyncFunctionDef) else SymbolType.FUNCTION_DEF
            )

            symbols.append(
                SymbolInfo(
                    name=node.name,
                    symbol_type=sym_type,
                    path=rel_path,
                    line_number=node.lineno,
                    end_line_number=getattr(node, "end_lineno", node.lineno),
                    docstring=doc[:200] if doc else None,
                    decorators=decorators,
                    route_method=route_method,
                    route_path=route_path,
                )
            )

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(
                        SymbolInfo(
                            name=target.id,
                            symbol_type=SymbolType.CONSTANT,
                            path=rel_path,
                            line_number=node.lineno,
                        )
                    )

    return symbols


def search_symbols(
    repo_root: Path | str,
    query: str,
    *,
    symbol_type: SymbolType | None = None,
    path_filter: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[SymbolInfo]:
    """Search for declared symbols in repository code files matching a query."""
    root = validate_repo_root(repo_root)
    inventory = scan_repository(root)
    query_folded = query.casefold()

    results: list[SymbolInfo] = []
    limit = min(max_results, MAX_SEARCH_RESULTS)

    for file_info in inventory.files:
        if file_info.is_binary or file_info.is_oversized:
            continue
        if path_filter and path_filter.casefold() not in file_info.path.casefold():
            continue

        resolved = safe_resolve_path(root, file_info.path, must_exist=True)
        symbols: list[SymbolInfo] = []

        if file_info.language == "Python":
            symbols = extract_python_symbols(resolved, file_info.path)

        for sym in symbols:
            if symbol_type and sym.symbol_type != symbol_type:
                continue
            name_matches = query_folded in sym.name.casefold()
            route_matches = (sym.route_path and query_folded in sym.route_path.casefold())
            if name_matches or route_matches:
                results.append(sym)
                if len(results) >= limit:
                    return results

    return results
