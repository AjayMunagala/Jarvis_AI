"""Closed, bounded contracts for browser observation, interaction, and research.

Every observation is marked untrusted: page text, labels, and links are
evidence about a page, never instructions or permission grants.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

Text = Annotated[StrictStr, Field(max_length=1000)]
ShortText = Annotated[StrictStr, Field(max_length=200)]
UrlText = Annotated[StrictStr, Field(min_length=1, max_length=2000)]

ProviderName = Literal["mock", "http", "playwright"]
ElementKind = Literal[
    "link", "button", "submit", "text_input", "textarea", "select", "checkbox", "radio", "other",
]
ActionName = Literal["navigate", "back", "reload", "observe", "click", "type", "select", "scroll", "screenshot"]
ActionClass = Literal["observe", "navigate", "interact", "consequential", "blocked"]
ActionOutcome = Literal["performed", "blocked", "approval_required", "failed", "unsupported", "timed_out"]
Transition = Literal["none", "same_page", "navigated", "unexpected_navigation"]
SessionState = Literal["active", "closed", "expired", "failed"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderCapabilities(Contract):
    javascript: StrictBool
    interaction: StrictBool
    screenshots: StrictBool
    console: StrictBool
    network_diagnostics: StrictBool


class SelectOption(Contract):
    value: ShortText
    label: ShortText
    selected: StrictBool = False


class PageElement(Contract):
    """One interactive element with a server-assigned id.

    Values of sensitive fields (passwords, hidden inputs, payment or one-time
    codes) are never read; ``sensitive`` marks them so typing is refused.
    """

    element_id: StrictStr = Field(pattern=r"^el-[0-9]{1,4}$")
    kind: ElementKind
    tag: ShortText
    label: ShortText = ""
    text: ShortText = ""
    href: UrlText | None = None
    input_type: ShortText | None = None
    name: ShortText | None = None
    form_id: StrictStr | None = Field(default=None, pattern=r"^form-[0-9]{1,3}$")
    required: StrictBool = False
    disabled: StrictBool = False
    sensitive: StrictBool = False
    options: tuple[SelectOption, ...] = Field(default=(), max_length=30)


class PageForm(Contract):
    form_id: StrictStr = Field(pattern=r"^form-[0-9]{1,3}$")
    action: UrlText | None = None
    method: Literal["get", "post"] = "get"
    label: ShortText = ""
    element_ids: tuple[StrictStr, ...] = Field(default=(), max_length=60)


class ConsoleEntry(Contract):
    level: Literal["error", "warning"]
    message: Text


class NetworkFailure(Contract):
    """Request metadata only: no bodies, headers, cookies, or query values."""

    url: UrlText
    method: ShortText
    status: int | None = None
    error: ShortText = ""
    resource_type: ShortText = ""


class PageObservation(Contract):
    observation_id: StrictStr = Field(pattern=r"^OBS-[a-f0-9]{12}$")
    session_id: StrictStr | None = None
    sequence: int = Field(ge=0)
    url: UrlText
    title: ShortText = ""
    retrieved_at: StrictStr
    http_status: int | None = None
    content_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    text: StrictStr = Field(default="", max_length=50000)
    text_truncated: StrictBool = False
    headings: tuple[ShortText, ...] = Field(default=(), max_length=40)
    elements: tuple[PageElement, ...] = Field(default=(), max_length=300)
    elements_truncated: StrictBool = False
    forms: tuple[PageForm, ...] = Field(default=(), max_length=20)
    console_errors: tuple[ConsoleEntry, ...] = Field(default=(), max_length=20)
    network_failures: tuple[NetworkFailure, ...] = Field(default=(), max_length=20)
    provider: ProviderName
    javascript_executed: StrictBool
    redaction_count: int = Field(default=0, ge=0)
    limitations: tuple[Text, ...] = Field(default=(), max_length=10)
    untrusted: Literal[True] = True


class ScreenshotRecord(Contract):
    screenshot_id: StrictStr = Field(pattern=r"^SHOT-[a-f0-9]{12}$")
    observation_id: StrictStr | None = None
    captured_at: StrictStr
    mime_type: Literal["image/png"] = "image/png"
    byte_size: int = Field(ge=1)
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class ActionRecord(Contract):
    action_id: StrictStr = Field(pattern=r"^ACT-[a-f0-9]{12}$")
    sequence: int = Field(ge=1)
    action: ActionName
    classification: ActionClass
    outcome: ActionOutcome
    reason: Text
    element_id: StrictStr | None = None
    element_label: ShortText = ""
    started_at: StrictStr
    duration_ms: float = Field(ge=0)
    url_before: UrlText | None = None
    url_after: UrlText | None = None
    transition: Transition = "none"
    resulting_observation_id: StrictStr | None = None
    screenshot_id: StrictStr | None = None


class SessionLimits(Contract):
    max_navigations: int
    max_actions: int
    navigation_timeout_seconds: float
    session_ttl_seconds: int
    max_text_chars: int
    max_elements: int


class BrowserSessionView(Contract):
    session_id: StrictStr = Field(pattern=r"^BS-[a-f0-9]{24}$")
    state: SessionState
    provider: ProviderName
    capabilities: ProviderCapabilities
    created_at: StrictStr
    expires_at: StrictStr
    current_url: UrlText | None = None
    navigations_used: int = Field(ge=0)
    actions_used: int = Field(ge=0)
    limits: SessionLimits
    last_observation: PageObservation | None = None
    actions: tuple[ActionRecord, ...] = Field(default=(), max_length=60)
    screenshots: tuple[ScreenshotRecord, ...] = Field(default=(), max_length=5)
    pending_approvals: tuple[ActionRecord, ...] = Field(default=(), max_length=60)
    failure_reason: Text | None = None
    execution_of_consequential_actions: Literal["disabled"] = "disabled"


class AuditEventView(Contract):
    sequence: int = Field(ge=1)
    timestamp: StrictStr
    event: ShortText
    outcome: ShortText
    detail: Text = ""


class BrowserStatus(Contract):
    enabled: StrictBool
    provider: ProviderName
    provider_available: StrictBool
    provider_detail: Text
    capabilities: ProviderCapabilities
    limits: SessionLimits
    private_network_allowed: StrictBool
    allowed_domains: tuple[ShortText, ...] = Field(default=(), max_length=50)
    consequential_actions: Literal["approval_required"] = "approval_required"


# ----------------------------------------------------------------- requests

class CreateSessionRequest(Contract):
    url: UrlText | None = None


class NavigateRequest(Contract):
    url: UrlText


class BrowserActionRequest(Contract):
    action: ActionName
    element_id: StrictStr | None = Field(default=None, pattern=r"^el-[0-9]{1,4}$")
    # Element actions must name the observation they were planned against, so a
    # changed page is detected as stale instead of acting on the wrong element.
    observation_id: StrictStr | None = Field(default=None, pattern=r"^OBS-[a-f0-9]{12}$")
    text: StrictStr | None = Field(default=None, max_length=1000)
    option: StrictStr | None = Field(default=None, max_length=200)
    direction: Literal["up", "down"] | None = None
    url: UrlText | None = None


class ActionResponse(Contract):
    record: ActionRecord
    session: BrowserSessionView


# ----------------------------------------------------------------- research

class ResearchRequest(Contract):
    question: StrictStr = Field(min_length=1, max_length=500)
    urls: tuple[UrlText, ...] = Field(min_length=1, max_length=5)
    include_model_interpretation: StrictBool = False


class ResearchSource(Contract):
    source_id: StrictStr = Field(pattern=r"^W[1-5]$")
    requested_url: UrlText
    final_url: UrlText | None = None
    title: ShortText = ""
    status: Literal["retrieved", "failed", "blocked", "timed_out"]
    failure_reason: Text | None = None
    retrieved_at: StrictStr
    http_status: int | None = None
    content_sha256: StrictStr | None = None
    excerpt: StrictStr = Field(default="", max_length=2100)
    excerpt_truncated: StrictBool = False
    matched_terms: tuple[ShortText, ...] = Field(default=(), max_length=8)
    javascript_executed: StrictBool = False
    provider: ProviderName


class ResearchObservation(Contract):
    """A statement taken verbatim from a retrieved source (no model involved)."""

    source_id: StrictStr = Field(pattern=r"^W[1-5]$")
    quote: StrictStr = Field(min_length=1, max_length=600)
    origin: Literal["page_text"] = "page_text"


class ModelInterpretation(Contract):
    text: StrictStr = Field(max_length=8000)
    origin: Literal["model"] = "model"
    provider: ShortText
    demo: StrictBool
    cited_source_ids: tuple[StrictStr, ...] = Field(default=(), max_length=5)
    unknown_citation_count: int = Field(default=0, ge=0)
    verified: Literal[False] = False


class ResearchReport(Contract):
    research_id: StrictStr = Field(pattern=r"^RS-[a-f0-9]{12}$")
    question: StrictStr
    started_at: StrictStr
    completed_at: StrictStr
    sources: tuple[ResearchSource, ...] = Field(max_length=5)
    observations: tuple[ResearchObservation, ...] = Field(default=(), max_length=15)
    interpretation: ModelInterpretation | None = None
    limitations: tuple[Text, ...] = Field(default=(), max_length=10)
    content_untrusted: Literal[True] = True


# ----------------------------------------------------------- browser checks

AssertionKind = Literal[
    "url_equals", "url_contains", "title_contains", "text_present", "text_absent",
    "element_present", "status_code", "no_console_errors", "no_network_failures",
]


class BrowserAssertion(Contract):
    kind: AssertionKind
    expected: StrictStr = Field(default="", max_length=500)


class BrowserCheckRequest(Contract):
    name: StrictStr = Field(min_length=1, max_length=200)
    url: UrlText
    assertions: tuple[BrowserAssertion, ...] = Field(min_length=1, max_length=10)


class BrowserCheckPlanItem(Contract):
    check_id: StrictStr = Field(pattern=r"^CHK-[a-f0-9]{6,16}$")
    name: StrictStr
    url: UrlText
    assertions: tuple[BrowserAssertion, ...]
    state: Literal["not_checked"] = "not_checked"


class BrowserCheckPlan(Contract):
    plan_id: StrictStr = Field(pattern=r"^VP-[a-f0-9]{8,16}$")
    checks: tuple[BrowserCheckPlanItem, ...] = Field(max_length=5)
    executed: Literal[False] = False
    note: Text


class BrowserCheckRunRequest(Contract):
    checks: tuple[BrowserCheckRequest, ...] = Field(min_length=1, max_length=5)


class AssertionResult(Contract):
    kind: AssertionKind
    expected: StrictStr
    observed: StrictStr = Field(max_length=500)
    outcome: Literal["pass", "fail", "not_evaluated"]
