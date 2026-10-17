"""Small server-side capability policy for the single configured owner."""

from enum import StrEnum
from uuid import UUID


class Capability(StrEnum):
    """Server-defined operations; values are never accepted as client grants."""

    CHAT_ADVISORY = "chat.advisory"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    CONVERSATION_READ = "conversation.read"
    CONVERSATION_WRITE = "conversation.write"
    REPO_READ = "repo.read"
    VERIFY_READ = "verify.read"
    VERIFY_EXECUTE = "verify.execute"
    # Browser reading (observe, navigate, research) and non-consequential interaction.
    BROWSER_READ = "browser.read"
    BROWSER_INTERACT = "browser.interact"
    # Tool runtime (v2.7): list capabilities/audit, and invoke registered read-only tools.
    TOOLS_READ = "tools.read"
    TOOLS_INVOKE = "tools.invoke"
    EXTERNAL_ACTION_EXECUTE = "external_action.execute"


_OWNER_CAPABILITIES = frozenset({
    Capability.CHAT_ADVISORY,
    Capability.MEMORY_READ,
    Capability.MEMORY_WRITE,
    Capability.CONVERSATION_READ,
    Capability.CONVERSATION_WRITE,
    Capability.REPO_READ,
    Capability.VERIFY_READ,
    Capability.BROWSER_READ,
    Capability.BROWSER_INTERACT,
    Capability.TOOLS_READ,
    Capability.TOOLS_INVOKE,
})


class CapabilityPolicy:
    """Allow current operations to an authenticated owner; deny unknown work."""

    def __init__(self, configured_owner_id: UUID | None) -> None:
        self._configured_owner_id = (
            configured_owner_id
            if isinstance(configured_owner_id, UUID) and configured_owner_id.int != 0
            else None
        )

    def allows(
        self,
        owner_id: UUID | None,
        capability: object,
        *,
        allow_stateless_chat: bool = False,
    ) -> bool:
        # Stateless mock chat is the sole intentional anonymous capability.
        if allow_stateless_chat:
            return owner_id is None and type(capability) is Capability and capability is Capability.CHAT_ADVISORY
        if (
            not isinstance(owner_id, UUID)
            or owner_id.int == 0
            or owner_id != self._configured_owner_id
        ):
            return False
        # Exact enum type prevents arbitrary strings, model output, plan steps,
        # or tool metadata from being interpreted as a permission grant.
        return type(capability) is Capability and capability in _OWNER_CAPABILITIES
