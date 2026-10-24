"""Authenticated read/advisory coding boundary. There is no apply endpoint."""

from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.coding.models import CodingReport, CodingRequest
from jarvis.coding.patches import PatchValidationError
from jarvis.coding.service import CodingOutputError, CodingService
from jarvis.models.schemas import ErrorResponse
from jarvis.orchestration.models import ExpertExecutionError, ExpertTimeoutError
from jarvis.orchestration.service import OrchestrationService
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import Capability

router = APIRouter(prefix="/coding", tags=["coding-intelligence"])


@router.post("/analyze", response_model=CodingReport,
             responses={code: {"model": ErrorResponse} for code in (401, 403, 413, 422, 502, 503, 504)})
async def analyze_coding(body: CodingRequest, request: Request,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> CodingReport:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    enforce_capability(request, owner_id, Capability.REPO_READ)
    enforce_capability(request, owner_id, Capability.CHAT_ADVISORY)
    repo = getattr(request.app.state, "repo_service", None)
    orchestration = getattr(request.app.state, "orchestration_service", None)
    provider = getattr(request.app.state, "provider", None)
    if not isinstance(repo, RepoIntelligenceService) or not isinstance(orchestration, OrchestrationService) or provider is None:
        raise HTTPException(status_code=503, detail="Coding intelligence is not configured.")
    try:
        return await CodingService(repo, orchestration, provider).analyze(
            body, deadline=monotonic() + request.app.state.settings.request_timeout_seconds,
        )
    except PatchValidationError:
        raise HTTPException(status_code=422, detail="The coding targets or proposed changes are invalid.") from None
    except (TimeoutError, ExpertTimeoutError):
        raise HTTPException(status_code=504, detail="The coding request timed out.") from None
    except (CodingOutputError, ExpertExecutionError, ValidationError):
        raise HTTPException(status_code=502, detail="The coding proposal could not be validated.") from None
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="Coding evidence is unavailable.") from None
