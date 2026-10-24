"""Shared API authentication dependencies."""

import secrets
from uuid import UUID

from fastapi import HTTPException, Request

from jarvis.security.policy import Capability, CapabilityPolicy


def authenticated_owner(request: Request) -> UUID | None:
    """Authenticate persistence-backed requests as the configured local owner.

    Anonymous operation is limited to stateless demo-provider chat. Persistence
    and any non-demo provider require the configured local bearer identity.
    """
    settings = request.app.state.settings
    provider = getattr(request.app.state, "provider", None)
    anonymous_demo = getattr(provider, "is_demo", False) is True
    has_auth_header = bool(request.headers.get("authorization"))
    if not settings.persistence_enabled and anonymous_demo and not has_auth_header:
        return None

    configured_token = settings.local_auth_token.get_secret_value() if settings.local_auth_token else ""
    configured_owner = settings.owner_id
    if not configured_token or configured_owner is None or configured_owner.int == 0:
        # Defensive fail-closed behavior for nonstandard settings/application setup.
        raise HTTPException(status_code=503, detail="Authenticated access is not configured.")

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
    request.state.audit_owner = configured_owner  # v4.2: lets the failure-audit middleware attribute refusals
    return configured_owner


def enforce_capability(
    request: Request,
    owner_id: UUID | None,
    capability: Capability,
    *,
    allow_stateless_chat: bool = False,
) -> None:
    """Apply the app's trusted policy after authentication, failing closed."""
    policy = getattr(request.app.state, "authorization_policy", None)
    if not isinstance(policy, CapabilityPolicy):
        raise HTTPException(status_code=403, detail="The operation is not permitted.")
    stateless_chat = (
        allow_stateless_chat
        and not request.app.state.settings.persistence_enabled
        and owner_id is None
        and getattr(getattr(request.app.state, "provider", None), "is_demo", False) is True
    )
    try:
        allowed = policy.allows(
            owner_id,
            capability,
            allow_stateless_chat=stateless_chat,
        )
    except Exception:
        # Policy errors are not permission. Keep response and logs free of
        # exception details, which could contain sensitive request data.
        allowed = False
    if allowed is not True:
        raise HTTPException(status_code=403, detail="The operation is not permitted.")
