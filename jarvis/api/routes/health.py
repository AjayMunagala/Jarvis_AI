"""Health and readiness endpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from jarvis import __version__
from jarvis.models.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def ready(request: Request) -> ReadinessResponse | JSONResponse:
    """Report fixed, non-sensitive in-process readiness checks.

    Persistence status means repository adapters are configured, not that a
    database connection was attempted or that PostgreSQL is reachable.
    """
    app_state = request.app.state
    settings = getattr(app_state, "settings", None)
    provider = getattr(app_state, "provider", None)
    orchestration = getattr(app_state, "orchestration_service", None)
    policy = getattr(app_state, "authorization_policy", None)

    repo_service = getattr(app_state, "repo_service", None)

    components = {
        "application": "ready",
        "provider": "ready" if callable(getattr(provider, "generate_reply", None)) else "unavailable",
        "orchestration": "ready" if callable(getattr(orchestration, "run", None)) else "unavailable",
        "authorization": "ready" if callable(getattr(policy, "allows", None)) else "unavailable",
        "repository_intelligence": (
            "ready" if callable(getattr(repo_service, "get_inventory", None)) else "unavailable"
        ),
    }
    persistence_enabled = getattr(settings, "persistence_enabled", None)
    if persistence_enabled is False:
        components["persistence"] = "disabled"
    elif persistence_enabled is True:
        repositories_present = (
            getattr(app_state, "conversation_repository", None) is not None
            and getattr(app_state, "memory_repository", None) is not None
        )
        components["persistence"] = "configured" if repositories_present else "unavailable"
    else:
        components["persistence"] = "unavailable"

    status = "not_ready" if "unavailable" in components.values() else "ready"
    response = ReadinessResponse(
        status=status,
        version=request.app.version,
        components=components,
    )
    if status == "not_ready":
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
    return response
