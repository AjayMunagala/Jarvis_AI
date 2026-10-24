"""Authenticated browser-intelligence endpoints (v2.6).

Every route requires the configured owner identity and a server-side
capability: ``browser.read`` for observation, navigation, research, and
screenshots; ``browser.interact`` additionally for click/type/select/scroll;
``verify.read`` additionally for browser checks. Consequential actions are
never executed; they are recorded as ``approval_required``.
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.browser.checks import plan_checks, run_checks
from jarvis.browser.models import (
    ActionResponse, AuditEventView, BrowserActionRequest, BrowserCheckPlan, BrowserCheckRunRequest,
    BrowserSessionView, BrowserStatus, CreateSessionRequest, NavigateRequest, ResearchReport, ResearchRequest,
)
from jarvis.browser.research import (
    RESEARCH_INSTRUCTION, build_interpretation, collect_sources, finish_report, interpretation_prompt, now_iso,
)
from jarvis.browser.service import BrowserService, BrowserServiceError
from jarvis.browser.url_policy import BlockedURL
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.verification.models import VerificationReport

router = APIRouter(prefix="/browser", tags=["browser"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}
_INTERACTIONS = {"click", "type", "select", "scroll"}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _service(request: Request, *, require_enabled: bool = True) -> BrowserService:
    service = getattr(request.app.state, "browser_service", None)
    if not isinstance(service, BrowserService):
        raise HTTPException(status_code=503, detail="Browser intelligence is unavailable.")
    if require_enabled and not service.settings.browser_enabled:
        raise HTTPException(status_code=503, detail="Browser intelligence is disabled (JARVIS_BROWSER_ENABLED=false).")
    return service


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, BrowserServiceError):
        return HTTPException(status_code=exc.status_code, detail=exc.public_message)
    if isinstance(exc, BlockedURL):
        return HTTPException(status_code=422, detail=exc.public_message)
    return HTTPException(status_code=503, detail="The browser operation failed.")


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except (BrowserServiceError, BlockedURL) as exc:
        raise _translate(exc) from exc


@router.get("/status", response_model=BrowserStatus, responses=_ERRORS)
async def browser_status(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> BrowserStatus:
    enforce_capability(request, _owner(owner_id), Capability.BROWSER_READ)
    return await run_in_threadpool(_service(request, require_enabled=False).status)


@router.post("/sessions", response_model=BrowserSessionView, status_code=201, responses=_ERRORS)
async def create_session(body: CreateSessionRequest, request: Request,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> BrowserSessionView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request).create_session, owner, body.url)


@router.get("/sessions", response_model=list[BrowserSessionView], responses=_ERRORS)
async def list_sessions(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> list[BrowserSessionView]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request).list_sessions, owner)


@router.get("/sessions/{session_id}", response_model=BrowserSessionView, responses=_ERRORS)
async def get_session(session_id: str, request: Request,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> BrowserSessionView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request).get_session, owner, session_id)


@router.delete("/sessions/{session_id}", response_model=BrowserSessionView, responses=_ERRORS)
async def close_session(session_id: str, request: Request,
                        owner_id: UUID | None = Depends(authenticated_owner)) -> BrowserSessionView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request, require_enabled=False).close_session, owner, session_id)


@router.post("/sessions/{session_id}/navigate", response_model=ActionResponse, responses=_ERRORS)
async def navigate(session_id: str, body: NavigateRequest, request: Request,
                   owner_id: UUID | None = Depends(authenticated_owner)) -> ActionResponse:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request).act, owner, session_id, BrowserActionRequest(action="navigate", url=body.url))


@router.post("/sessions/{session_id}/actions", response_model=ActionResponse, responses=_ERRORS)
async def perform_action(session_id: str, body: BrowserActionRequest, request: Request,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> ActionResponse:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    if body.action in _INTERACTIONS:
        enforce_capability(request, owner, Capability.BROWSER_INTERACT)
    return await _call(_service(request).act, owner, session_id, body)


@router.get("/sessions/{session_id}/screenshots/{screenshot_id}", response_class=Response, responses=_ERRORS)
async def get_screenshot(session_id: str, screenshot_id: str, request: Request,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    image = await _call(_service(request).screenshot_bytes, owner, session_id, screenshot_id)
    return Response(content=image, media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/sessions/{session_id}/audit", response_model=list[AuditEventView], responses=_ERRORS)
async def get_audit(session_id: str, request: Request,
                    owner_id: UUID | None = Depends(authenticated_owner)) -> list[AuditEventView]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    return await _call(_service(request, require_enabled=False).audit_events, owner, session_id)


@router.post("/research", response_model=ResearchReport, responses=_ERRORS)
async def research(body: ResearchRequest, request: Request,
                   owner_id: UUID | None = Depends(authenticated_owner)) -> ResearchReport:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    if body.include_model_interpretation:
        enforce_capability(request, owner, Capability.CHAT_ADVISORY)
    service = _service(request)
    started_at = now_iso()
    sources, observations = await _call(collect_sources, service, body)
    interpretation = None
    if body.include_model_interpretation and any(item.status == "retrieved" for item in sources):
        provider = request.app.state.provider
        try:
            reply = await provider.generate_reply(
                interpretation_prompt(body.question, sources), system_instruction=RESEARCH_INSTRUCTION,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="The configured provider failed.") from exc
        interpretation = build_interpretation(reply, provider.name, bool(provider.is_demo), sources)
    return finish_report(body, started_at, sources, observations, interpretation)


@router.post("/checks/plan", response_model=BrowserCheckPlan, responses=_ERRORS)
async def plan_browser_checks(body: BrowserCheckRunRequest, request: Request,
                              owner_id: UUID | None = Depends(authenticated_owner)) -> BrowserCheckPlan:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    enforce_capability(request, owner, Capability.VERIFY_READ)
    return plan_checks(body.checks)


@router.post("/checks/run", response_model=VerificationReport, responses=_ERRORS)
async def run_browser_checks(body: BrowserCheckRunRequest, request: Request,
                             owner_id: UUID | None = Depends(authenticated_owner)) -> VerificationReport:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.BROWSER_READ)
    enforce_capability(request, owner, Capability.VERIFY_READ)
    service = _service(request)
    return await _call(run_checks, service, body.checks, execution_id=uuid4().hex)
