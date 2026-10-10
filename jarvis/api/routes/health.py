"""Health and readiness endpoints."""

from fastapi import APIRouter, Request

from jarvis import __version__
from jarvis.models.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse:
    # Configuration and provider construction occur before the app is served.
    return HealthResponse(status="ready", version=request.app.version)
