"""Structured models for repository intelligence and coding diagnostics."""

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class FileCategory(StrEnum):
    """Broad classification of repository files."""

    SOURCE = "source"
    TEST = "test"
    CONFIG = "config"
    DOCUMENTATION = "documentation"
    MANIFEST = "manifest"
    BUILD = "build"
    ASSET = "asset"
    DATA = "data"
    UNKNOWN = "unknown"


class FileInfo(BaseModel):
    """Metadata for one discovered repository file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size_bytes: int
    language: str | None = None
    category: FileCategory = FileCategory.UNKNOWN
    is_binary: bool = False
    is_oversized: bool = False


class RepositoryInventory(BaseModel):
    """Bounded inventory of repository structure and contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_path: str
    total_files: int
    total_size_bytes: int
    categories: dict[str, int] = Field(default_factory=dict)
    languages: dict[str, int] = Field(default_factory=dict)
    manifests: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    test_directories: list[str] = Field(default_factory=list)
    documentation_files: list[str] = Field(default_factory=list)
    files: list[FileInfo] = Field(default_factory=list)
    truncated: bool = False
    scan_duration_ms: float = 0.0


class SearchMatch(BaseModel):
    """One search hit with location and bounded excerpt context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line_number: int | None = None
    column: int | None = None
    match_type: str = "text"
    matched_text: str
    line_text: str | None = None
    context_before: list[str] = Field(default_factory=list)
    context_after: list[str] = Field(default_factory=list)
    is_truncated: bool = False


class SearchSummary(BaseModel):
    """Collection of search matches with bounds and pagination metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    search_type: str
    total_matches: int
    matched_files_count: int
    matches: list[SearchMatch] = Field(default_factory=list)
    truncated: bool = False


class SymbolType(StrEnum):
    """Categorization of extracted code symbols."""

    CLASS_DEF = "class_def"
    FUNCTION_DEF = "function_def"
    ASYNC_FUNCTION_DEF = "async_function_def"
    ROUTE_HANDLER = "route_handler"
    CONSTANT = "constant"
    IMPORT_STATEMENT = "import_statement"
    INTERFACE_DEF = "interface_def"
    TYPE_ALIAS = "type_alias"


class SymbolInfo(BaseModel):
    """Parsed code symbol with location and signature metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    symbol_type: SymbolType
    path: str
    line_number: int
    end_line_number: int | None = None
    signature: str | None = None
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    route_method: str | None = None
    route_path: str | None = None


class GitFileStatus(BaseModel):
    """Status entry for one file in the Git working tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    status_code: str
    is_staged: bool = False
    is_untracked: bool = False
    is_modified: bool = False
    is_deleted: bool = False


class GitDiffEntry(BaseModel):
    """Summary of changes for one file in a Git diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    status: str
    insertions: int = 0
    deletions: int = 0
    diff_content: str | None = None
    is_binary: bool = False
    is_truncated: bool = False


class GitStatusSummary(BaseModel):
    """Safe read-only overview of Git working tree and changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_repo: bool
    branch: str | None = None
    head_commit: str | None = None
    staged_files: list[GitFileStatus] = Field(default_factory=list)
    unstaged_files: list[GitFileStatus] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    diff_entries: list[GitDiffEntry] = Field(default_factory=list)
    changed_modules: list[str] = Field(default_factory=list)
    potentially_affected_tests: list[str] = Field(default_factory=list)
    error: str | None = None


class DependencyDeclaration(BaseModel):
    """Discovered package dependency specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    specifier: str | None = None
    source_manifest: str = ""
    is_dev: bool = False


class ConfigDiscovery(BaseModel):
    """Static discovery of configuration, dependencies, and environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    dependencies: list[DependencyDeclaration] = Field(default_factory=list)
    env_variable_names: list[str] = Field(default_factory=list)
    build_tools: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    startup_commands: list[str] = Field(default_factory=list)
    known_services: list[str] = Field(default_factory=list)


class TestDiscovery(BaseModel):
    """Discovery of test infrastructure and module relationships."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_frameworks: list[str] = Field(default_factory=list)
    test_directories: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    module_to_test_map: dict[str, list[str]] = Field(default_factory=dict)


class DiagnosticFact(BaseModel):
    """Verified fact backed by direct repository evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    source_file: str | None = None
    line_number: int | None = None
    evidence_excerpt: str | None = None


class DiagnosticHypothesis(BaseModel):
    """Plausible explanation of an observed issue requiring confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    rationale: str
    likelihood: str = "plausible"
    confirmation_needed: str = ""


class DiagnosticReport(BaseModel):
    """Evidence-based diagnostic analysis separating facts from hypotheses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    problem_statement: str
    investigated_files: list[str] = Field(default_factory=list)
    facts: list[DiagnosticFact] = Field(default_factory=list)
    hypotheses: list[DiagnosticHypothesis] = Field(default_factory=list)
    confirmed_issues: list[str] = Field(default_factory=list)
    suggested_steps: list[str] = Field(default_factory=list)
    suggested_commands: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    limitations: list[str] = Field(default_factory=list)
