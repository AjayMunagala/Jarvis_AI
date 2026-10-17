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
    """Report initialized components and distinguish configured from verified dependencies.

    This endpoint never makes a provider or database network request. A
    configured remote provider or PostgreSQL adapter is therefore not_checked,
    not evidence that the dependency is reachable. Disabled persistence is
    explicitly not_required for stateless operation.
    """
    app_state = request.app.state
    settings = getattr(app_state, "settings", None)
    provider = getattr(app_state, "provider", None)
    orchestration = getattr(app_state, "orchestration_service", None)
    policy = getattr(app_state, "authorization_policy", None)

    configured_provider = getattr(settings, "provider", None)
    provider_wired = (
        callable(getattr(provider, "generate_reply", None))
        and getattr(provider, "name", None) == configured_provider
        and bool(getattr(provider, "is_demo", False)) == (configured_provider == "mock")
    )
    if not provider_wired:
        provider_status = "unavailable"
    elif configured_provider == "mock":
        # The mock adapter is an in-process deterministic implementation; it
        # does not depend on remote service availability.
        provider_status = "ready"
    elif configured_provider:
        # Adapter construction and valid configuration do not prove remote
        # provider reachability; do not make a paid/network call from /ready.
        provider_status = "not_checked"
    else:
        provider_status = "unavailable"

    components = {
        "application": "ready",
        "provider": provider_status,
        "orchestration": "ready" if callable(getattr(orchestration, "run", None)) else "unavailable",
        "authorization": "ready" if callable(getattr(policy, "allows", None)) else "unavailable",
    }
    persistence_enabled = getattr(settings, "persistence_enabled", None)
    if persistence_enabled is False:
        components["persistence"] = "not_required"
    elif persistence_enabled is True:
        repositories_present = (
            getattr(app_state, "conversation_repository", None) is not None
            and getattr(app_state, "memory_repository", None) is not None
        )
        components["persistence"] = "not_checked" if repositories_present else "unavailable"
    else:
        components["persistence"] = "unavailable"

    statuses = set(components.values())
    status = (
        "not_ready" if "unavailable" in statuses
        else "degraded" if "not_checked" in statuses
        else "ready"
    )
    response = ReadinessResponse(
        status=status,
        version=request.app.version,
        components=components,
    )
    if status == "not_ready":
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
    return response
