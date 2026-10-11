"""Shared API authentication dependencies."""

import secrets
from uuid import UUID

from fastapi import HTTPException, Request


def authenticated_owner(request: Request) -> UUID | None:
    """Authenticate persistence-backed requests as the configured local owner.

    Stateless operation deliberately has no owner or credential requirement.
    Settings validation guarantees that persistent operation has both values.
    """
    settings = request.app.state.settings
    if not settings.persistence_enabled:
        return None

    configured_token = settings.local_auth_token.get_secret_value() if settings.local_auth_token else ""
    configured_owner = settings.owner_id
    if not configured_token or configured_owner is None or configured_owner.int == 0:
        # Defensive fail-closed behavior for nonstandard settings/application setup.
        raise HTTPException(status_code=503, detail="Persistent access is not configured.")

    authorization = request.headers.get("authorization", "")
    scheme, separator, candidate = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not candidate
        or candidate != candidate.strip()
        or any(char.isspace() for char in candidate)
        or not secrets.compare_digest(candidate.encode("utf-8"), configured_token.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return configured_owner
