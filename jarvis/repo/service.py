"""High-level repository intelligence service orchestrating repository capabilities."""

from pathlib import Path

from jarvis.repo.config_discovery import discover_config
from jarvis.repo.diagnostics import generate_diagnostic_report
from jarvis.repo.git_intel import get_git_status
from jarvis.repo.inventory import scan_repository
from jarvis.repo.models import (
    ConfigDiscovery,
    DiagnosticReport,
    GitStatusSummary,
    RepositoryInventory,
    SearchSummary,
    SearchMatch,
    SymbolInfo,
    SymbolType,
    TestDiscovery,
)
from jarvis.repo.path_security import validate_repo_root
from jarvis.repo.search import search_filenames, search_symbols, search_text
from jarvis.repo.test_discovery import discover_tests_and_build


class RepoIntelligenceService:
    """Bounded, read-only service providing repository intelligence and diagnostics."""

    def __init__(self, repo_root: Path | str | None = None) -> None:
        self._repo_root = validate_repo_root(repo_root or Path.cwd())

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    def get_inventory(
        self,
        *,
        max_files: int = 5000,
        max_depth: int = 15,
        timeout_seconds: float = 10.0,
    ) -> RepositoryInventory:
        """Scan and return repository structure and metadata."""
        return scan_repository(
            self._repo_root,
            max_files=max_files,
            max_depth=max_depth,
            timeout_seconds=timeout_seconds,
        )

    def search(
        self,
        query: str,
        *,
        search_type: str = "text",
        is_regex: bool = False,
        case_sensitive: bool = False,
        path_filter: str | None = None,
        max_results: int = 50,
    ) -> SearchSummary:
        """Execute a bounded search over filenames or file contents."""
        if search_type == "filename":
            return search_filenames(
                self._repo_root,
                query,
                max_results=max_results,
            )
        if search_type == "symbol":
            symbols = self.find_symbols(query, path_filter=path_filter, max_results=max_results)
            return SearchSummary(
                query=query, search_type="symbol", total_matches=len(symbols),
                matched_files_count=len({symbol.path for symbol in symbols}),
                matches=[SearchMatch(
                    path=symbol.path, line_number=symbol.line_number,
                    match_type="symbol", matched_text=symbol.name,
                ) for symbol in symbols],
                truncated=len(symbols) >= min(max_results, 200),
            )
        return search_text(
            self._repo_root,
            query,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            path_filter=path_filter,
            max_results=max_results,
        )

    def find_symbols(
        self,
        query: str,
        *,
        symbol_type: SymbolType | None = None,
        path_filter: str | None = None,
        max_results: int = 50,
    ) -> list[SymbolInfo]:
        """Search for code symbols (functions, classes, route declarations)."""
        return search_symbols(
            self._repo_root,
            query,
            symbol_type=symbol_type,
            path_filter=path_filter,
            max_results=max_results,
        )

    def get_git_status(self, *, max_diff_entries: int = 50) -> GitStatusSummary:
        """Inspect the current Git branch, working tree status, and diffs."""
        return get_git_status(self._repo_root, max_diff_entries=max_diff_entries)

    def get_configuration(self) -> ConfigDiscovery:
        """Discover declared dependencies, frameworks, and configuration."""
        return discover_config(self._repo_root)

    def get_test_discovery(self) -> TestDiscovery:
        """Discover test frameworks, test files, and test mappings."""
        return discover_tests_and_build(self._repo_root)

    def diagnose(
        self,
        query: str,
        *,
        target_files: list[str] | None = None,
    ) -> DiagnosticReport:
        """Produce an evidence-based diagnostic report for an issue or query."""
        return generate_diagnostic_report(
            self._repo_root,
            query,
            target_files=target_files,
        )
