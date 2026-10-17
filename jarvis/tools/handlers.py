"""Fixed allowlist of read-only tool handlers (v2.7).

Each handler wraps an existing JARVIS service; none reimplements it. Inputs and
outputs are strict Pydantic models (``extra="forbid"``) whose JSON schemas are
published in ``tools.json`` and checked by conformance tests. Configuration
cannot add handlers: a tool id in ``tools.json`` without an entry here is
reported unavailable and never runs.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from jarvis.security.policy import Capability
from jarvis.tools.calculator import evaluate
from jarvis.tools.models import RiskClass

EXCERPT_CHARS = 400


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class ToolContext:
    owner_id: UUID
    settings: Any
    memory_repository: Any = None
    repo_service: Any = None
    verification_service: Any = None
    browser_service: Any = None
    assistant_service: Any = None


def build_tool_context(state, owner_id: UUID) -> "ToolContext":
    """Collect the existing services a tool may wrap from FastAPI app state."""
    return ToolContext(
        owner_id=owner_id, settings=state.settings,
        memory_repository=getattr(state, "memory_repository", None),
        repo_service=getattr(state, "repo_service", None),
        verification_service=getattr(state, "verification_service", None),
        browser_service=getattr(state, "browser_service", None),
        assistant_service=getattr(state, "assistant_service", None),
    )


@dataclass(frozen=True)
class HandlerResult:
    output: Output
    evidence: tuple[str, ...] = ()
    untrusted_content: bool = False


@dataclass(frozen=True)
class ToolHandler:
    tool_id: str
    version: str
    title: str
    description: str
    args_model: type[Args]
    output_model: type[Output]
    required: tuple[Capability, ...]
    run: Callable[[ToolContext, Args], HandlerResult]
    available: Callable[[ToolContext], tuple[bool, str]]
    risk: RiskClass = RiskClass.LOW
    read_only: bool = True
    timeout_seconds: float = 10.0


def _clip(text: str | None, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _always(_: ToolContext) -> tuple[bool, str]:
    return True, ""


# ------------------------------------------------------------- calculator

class CalculatorArgs(Args):
    expression: StrictStr = Field(min_length=1, max_length=200)


class CalculatorOutput(Output):
    expression: StrictStr
    result: int | float
    result_type: Literal["integer", "decimal"]


def _calculate(context: ToolContext, args: CalculatorArgs) -> HandlerResult:
    value = evaluate(args.expression)
    return HandlerResult(CalculatorOutput(
        expression=args.expression, result=value,
        result_type="integer" if isinstance(value, int) else "decimal",
    ), evidence=("computed:calculator",))


# ---------------------------------------------------------- memory search

class MemorySearchArgs(Args):
    query: StrictStr = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=10)


class MemoryHit(Output):
    id: UUID
    source_kind: Literal["memory", "document"]
    category: StrictStr | None
    source_reference: StrictStr | None
    excerpt: StrictStr
    updated_at: StrictStr


class MemorySearchOutput(Output):
    query: StrictStr
    total: int
    hits: tuple[MemoryHit, ...] = Field(max_length=10)


def _memory_available(context: ToolContext) -> tuple[bool, str]:
    if context.memory_repository is None:
        return False, "Memory storage requires JARVIS_PERSISTENCE_ENABLED=true."
    return True, ""


def _memory_search(context: ToolContext, args: MemorySearchArgs) -> HandlerResult:
    page = context.memory_repository.search(context.owner_id, args.query, limit=args.limit, offset=0)
    hits = tuple(MemoryHit(
        id=item.id, source_kind=getattr(item, "source_kind", "memory"), category=item.category,
        source_reference=item.source_reference, excerpt=_clip(item.content),
        updated_at=item.updated_at.isoformat(),
    ) for item in page.items[: args.limit])
    return HandlerResult(
        MemorySearchOutput(query=args.query, total=page.total, hits=hits),
        evidence=tuple(f"memory:{hit.id}" for hit in hits),
        untrusted_content=any(hit.source_kind == "document" for hit in hits),
    )


# ------------------------------------------------------------ repository

class RepoSearchArgs(Args):
    query: StrictStr = Field(min_length=1, max_length=200)
    search_type: Literal["text", "filename"] = "text"
    path_filter: StrictStr | None = Field(default=None, max_length=200)
    max_results: int = Field(default=20, ge=1, le=50)


class RepoMatch(Output):
    path: StrictStr
    line_number: int | None
    matched_text: StrictStr
    line_text: StrictStr | None


class RepoSearchOutput(Output):
    query: StrictStr
    search_type: StrictStr
    total_matches: int
    matched_files_count: int
    truncated: bool
    matches: tuple[RepoMatch, ...] = Field(max_length=50)


def _repo_available(context: ToolContext) -> tuple[bool, str]:
    if context.repo_service is None:
        return False, "Repository intelligence is not configured."
    return True, ""


def _repo_search(context: ToolContext, args: RepoSearchArgs) -> HandlerResult:
    # Regex search is deliberately not exposed through the tool runtime.
    summary = context.repo_service.search(
        args.query, search_type=args.search_type, is_regex=False, path_filter=args.path_filter,
        max_results=args.max_results,
    )
    matches = tuple(RepoMatch(
        path=item.path, line_number=item.line_number, matched_text=_clip(item.matched_text, 200),
        line_text=_clip(item.line_text, 300) if item.line_text is not None else None,
    ) for item in summary.matches[: args.max_results])
    return HandlerResult(RepoSearchOutput(
        query=args.query, search_type=summary.search_type, total_matches=summary.total_matches,
        matched_files_count=summary.matched_files_count, truncated=summary.truncated, matches=matches,
    ), evidence=tuple(f"repo:{item.path}:{item.line_number or 0}" for item in matches))


class RepoSymbolsArgs(Args):
    query: StrictStr = Field(min_length=1, max_length=120)
    path_filter: StrictStr | None = Field(default=None, max_length=200)
    max_results: int = Field(default=20, ge=1, le=50)


class RepoSymbol(Output):
    name: StrictStr
    symbol_type: StrictStr
    path: StrictStr
    line_number: int
    signature: StrictStr | None


class RepoSymbolsOutput(Output):
    query: StrictStr
    symbols: tuple[RepoSymbol, ...] = Field(max_length=50)


def _repo_symbols(context: ToolContext, args: RepoSymbolsArgs) -> HandlerResult:
    symbols = context.repo_service.find_symbols(args.query, path_filter=args.path_filter, max_results=args.max_results)
    items = tuple(RepoSymbol(
        name=_clip(item.name, 120), symbol_type=str(item.symbol_type), path=item.path,
        line_number=item.line_number, signature=_clip(item.signature, 300) if item.signature else None,
    ) for item in symbols[: args.max_results])
    return HandlerResult(RepoSymbolsOutput(query=args.query, symbols=items),
                         evidence=tuple(f"repo:{item.path}:{item.line_number}" for item in items))


class NoArgs(Args):
    pass


class GitFile(Output):
    path: StrictStr
    status: StrictStr


class GitStatusOutput(Output):
    is_repo: bool
    branch: StrictStr | None
    head_commit: StrictStr | None
    staged: tuple[GitFile, ...] = Field(max_length=100)
    unstaged: tuple[GitFile, ...] = Field(max_length=100)
    untracked: tuple[StrictStr, ...] = Field(max_length=100)
    error: StrictStr | None


def _git_status(context: ToolContext, args: NoArgs) -> HandlerResult:
    status = context.repo_service.get_git_status(max_diff_entries=0)
    output = GitStatusOutput(
        is_repo=status.is_repo, branch=status.branch, head_commit=status.head_commit,
        staged=tuple(GitFile(path=item.path, status=item.status_code) for item in status.staged_files[:100]),
        unstaged=tuple(GitFile(path=item.path, status=item.status_code) for item in status.unstaged_files[:100]),
        untracked=tuple(status.untracked_files[:100]), error=_clip(status.error, 300) if status.error else None,
    )
    return HandlerResult(output, evidence=(f"git:HEAD:{status.head_commit or 'unknown'}",))


class GitDiffEntryOut(Output):
    path: StrictStr
    status: StrictStr
    insertions: int
    deletions: int
    is_binary: bool


class GitDiffSummaryOutput(Output):
    branch: StrictStr | None
    head_commit: StrictStr | None
    files: tuple[GitDiffEntryOut, ...] = Field(max_length=100)
    total_insertions: int
    total_deletions: int
    content_included: Literal[False] = False


def _git_diff_summary(context: ToolContext, args: NoArgs) -> HandlerResult:
    # Line counts only: diff content is never returned through the tool runtime.
    status = context.repo_service.get_git_status(max_diff_entries=100)
    files = tuple(GitDiffEntryOut(path=item.path, status=item.status, insertions=item.insertions,
                                  deletions=item.deletions, is_binary=item.is_binary)
                  for item in status.diff_entries[:100])
    return HandlerResult(GitDiffSummaryOutput(
        branch=status.branch, head_commit=status.head_commit, files=files,
        total_insertions=sum(item.insertions for item in files), total_deletions=sum(item.deletions for item in files),
    ), evidence=(f"git:HEAD:{status.head_commit or 'unknown'}",))


# ---------------------------------------------------------- verification

class VerificationPlanArgs(Args):
    target_paths: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    reason: StrictStr = Field(default="Verification plan requested through the tool runtime", min_length=1, max_length=500)


class PlannedCheck(Output):
    check_id: StrictStr
    name: StrictStr
    scope: StrictStr
    target: StrictStr
    profile_id: StrictStr


class VerificationPlanOutput(Output):
    plan_id: StrictStr
    checks: tuple[PlannedCheck, ...] = Field(max_length=10)
    executed: Literal[False] = False


def _verification_available(context: ToolContext) -> tuple[bool, str]:
    if context.verification_service is None:
        return False, "Verification planning is not configured."
    return True, ""


def _verification_plan(context: ToolContext, args: VerificationPlanArgs) -> HandlerResult:
    from jarvis.verification.models import VerificationPlanRequest  # noqa: PLC0415 - avoid import cycles

    plan = context.verification_service.create_plan(
        VerificationPlanRequest(target_paths=args.target_paths, reason=args.reason)
    )
    checks = tuple(PlannedCheck(check_id=item.check_id, name=item.name, scope=str(item.scope), target=item.target,
                                profile_id=item.profile_id) for item in plan.checks)
    return HandlerResult(VerificationPlanOutput(plan_id=plan.plan_id, checks=checks),
                         evidence=(f"verification-plan:{plan.plan_id}",))


# --------------------------------------------------------------- browser

class BrowserObserveArgs(Args):
    url: StrictStr = Field(min_length=1, max_length=2000)


class BrowserLink(Output):
    label: StrictStr
    href: StrictStr


class BrowserObserveOutput(Output):
    url: StrictStr
    title: StrictStr
    http_status: int | None
    retrieved_at: StrictStr
    content_sha256: StrictStr
    text_excerpt: StrictStr
    text_truncated: bool
    links: tuple[BrowserLink, ...] = Field(max_length=20)
    javascript_executed: bool
    untrusted: Literal[True] = True


def _browser_available(context: ToolContext) -> tuple[bool, str]:
    service = context.browser_service
    if service is None or not getattr(service.settings, "browser_enabled", False):
        return False, "Browser access requires JARVIS_BROWSER_ENABLED=true."
    available, detail = service.provider.availability()
    return (True, "") if available else (False, detail)


def _browser_observe(context: ToolContext, args: BrowserObserveArgs) -> HandlerResult:
    page = context.browser_service.fetch_observation(args.url)
    links = tuple(BrowserLink(label=_clip(item.label or item.text, 120), href=item.href)
                  for item in page.elements if item.kind == "link" and item.href)[:20]
    text = page.text[:4000]
    return HandlerResult(BrowserObserveOutput(
        url=page.url, title=page.title, http_status=page.http_status, retrieved_at=page.retrieved_at,
        content_sha256=page.content_sha256, text_excerpt=text, text_truncated=page.text_truncated or len(page.text) > 4000,
        links=links, javascript_executed=page.javascript_executed,
    ), evidence=(f"url:{page.url}", f"sha256:{page.content_sha256}"), untrusted_content=True)


# ----------------------------------------------------------- assistant (v3.1)

class TasksListArgs(Args):
    status: StrictStr | None = None
    limit: int = Field(default=20, ge=1, le=100)


class TaskSummaryItem(Output):
    id: StrictStr
    title: StrictStr
    status: StrictStr
    priority: StrictStr
    due_date: StrictStr | None = None


class TasksListOutput(Output):
    tasks: tuple[TaskSummaryItem, ...]
    total: int


class NotesSearchArgs(Args):
    query: StrictStr | None = None
    tag: StrictStr | None = None
    limit: int = Field(default=10, ge=1, le=50)


class NoteSummaryItem(Output):
    id: StrictStr
    title: StrictStr
    content_excerpt: StrictStr
    tags: tuple[StrictStr, ...]


class NotesSearchOutput(Output):
    notes: tuple[NoteSummaryItem, ...]
    total: int


class CalendarListArgs(Args):
    start_range: StrictStr
    end_range: StrictStr
    limit: int = Field(default=20, ge=1, le=100)


class CalendarEventSummaryItem(Output):
    id: StrictStr
    title: StrictStr
    start_time: StrictStr
    end_time: StrictStr
    location: StrictStr | None = None


class CalendarListOutput(Output):
    events: tuple[CalendarEventSummaryItem, ...]
    total: int


def _assistant_available(context: ToolContext) -> tuple[bool, str]:
    if context.assistant_service is None:
        return False, "Personal assistant service is not available."
    return True, ""


def _tasks_list(context: ToolContext, args: TasksListArgs) -> HandlerResult:
    from jarvis.assistant.models import TaskStatus
    st = TaskStatus(args.status) if args.status else None
    page = context.assistant_service.list_tasks(context.owner_id, status=st, limit=args.limit)
    items = tuple(
        TaskSummaryItem(
            id=str(t.id), title=t.title, status=t.status.value, priority=t.priority.value,
            due_date=t.due_date.isoformat() if t.due_date else None,
        ) for t in page.items
    )
    return HandlerResult(TasksListOutput(tasks=items, total=page.total), evidence=("assistant:tasks",))


def _notes_search(context: ToolContext, args: NotesSearchArgs) -> HandlerResult:
    page = context.assistant_service.list_notes(context.owner_id, query=args.query, tag=args.tag, limit=args.limit)
    items = tuple(
        NoteSummaryItem(
            id=str(n.id), title=n.title, content_excerpt=_clip(n.content, 200), tags=tuple(n.tags),
        ) for n in page.items
    )
    return HandlerResult(NotesSearchOutput(notes=items, total=page.total), evidence=("assistant:notes",))


def _calendar_list(context: ToolContext, args: CalendarListArgs) -> HandlerResult:
    try:
        start_dt = datetime.fromisoformat(args.start_range)
        end_dt = datetime.fromisoformat(args.end_range)
    except ValueError:
        return HandlerResult(CalendarListOutput(events=(), total=0))
    page = context.assistant_service.list_calendar_events(context.owner_id, start_range=start_dt, end_range=end_dt, limit=args.limit)
    items = tuple(
        CalendarEventSummaryItem(
            id=str(e.id), title=e.title, start_time=e.start_time.isoformat(),
            end_time=e.end_time.isoformat(), location=e.location,
        ) for e in page.items
    )
    return HandlerResult(CalendarListOutput(events=items, total=page.total), evidence=("assistant:calendar",))


HANDLERS: dict[str, ToolHandler] = {item.tool_id: item for item in (
    ToolHandler("calculator", "1", "Calculator", "Evaluates bounded arithmetic (numbers, + - * / // % **, abs/min/max/round/sqrt).",
                CalculatorArgs, CalculatorOutput, (Capability.CHAT_ADVISORY,), _calculate, _always, timeout_seconds=2.0),
    ToolHandler("memory.search", "1", "Memory search", "Lexical search over the owner's saved memories and imported documents.",
                MemorySearchArgs, MemorySearchOutput, (Capability.MEMORY_READ,), _memory_search, _memory_available),
    ToolHandler("repo.search", "1", "Repository search", "Bounded text or filename search in the configured repository (no regex).",
                RepoSearchArgs, RepoSearchOutput, (Capability.REPO_READ,), _repo_search, _repo_available),
    ToolHandler("repo.symbols", "1", "Repository symbols", "Finds classes, functions, and routes by name in the configured repository.",
                RepoSymbolsArgs, RepoSymbolsOutput, (Capability.REPO_READ,), _repo_symbols, _repo_available),
    ToolHandler("git.status", "1", "Git status", "Reads branch, HEAD, and working-tree file status (read-only).",
                NoArgs, GitStatusOutput, (Capability.REPO_READ,), _git_status, _repo_available),
    ToolHandler("git.diff_summary", "1", "Git diff summary", "Per-file insertion/deletion counts for uncommitted changes; no diff content.",
                NoArgs, GitDiffSummaryOutput, (Capability.REPO_READ,), _git_diff_summary, _repo_available),
    ToolHandler("verification.plan", "1", "Verification plan", "Plans verification checks for paths; never executes them.",
                VerificationPlanArgs, VerificationPlanOutput, (Capability.VERIFY_READ,), _verification_plan, _verification_available),
    ToolHandler("browser.observe", "1", "Browser observe", "Fetches and observes one policy-checked page as untrusted content.",
                BrowserObserveArgs, BrowserObserveOutput, (Capability.BROWSER_READ,), _browser_observe, _browser_available,
                timeout_seconds=20.0),
    ToolHandler("tasks.list", "1", "List tasks", "Lists personal assistant tasks by status and due date.",
                TasksListArgs, TasksListOutput, (Capability.ASSISTANT_READ,), _tasks_list, _assistant_available),
    ToolHandler("notes.search", "1", "Search notes", "Searches personal notes by title, tag, or content.",
                NotesSearchArgs, NotesSearchOutput, (Capability.ASSISTANT_READ,), _notes_search, _assistant_available),
    ToolHandler("calendar.list", "1", "List calendar events", "Lists local calendar events within a date range.",
                CalendarListArgs, CalendarListOutput, (Capability.ASSISTANT_READ,), _calendar_list, _assistant_available),
)}
