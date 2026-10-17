"""Database-independent conversation repository contracts."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from jarvis.models.schemas import CONVERSATION_LIST_DEFAULT_LIMIT, ConversationListResponse


MessageRole = Literal["user", "assistant"]


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    conversation_id: UUID
    sequence_number: int = Field(ge=0)
    role: MessageRole
    content: StrictStr = Field(min_length=1, max_length=40000)
    created_at: datetime

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content must not be blank.")
        return value


class ConversationRepository(Protocol):
    """Small persistence boundary; adapters must not persist provider internals."""

    def create_conversation(self) -> UUID: ...

    def append_message(
        self, conversation_id: UUID, role: MessageRole, content: str
    ) -> ConversationMessage: ...

    def get_messages(
        self, conversation_id: UUID, *, before_sequence: int | None = None, limit: int = 51
    ) -> tuple[ConversationMessage, ...]: ...

    def list_conversations(
        self, *, limit: int = CONVERSATION_LIST_DEFAULT_LIMIT, offset: int = 0
    ) -> ConversationListResponse: ...

    def save_exchange(
        self, user_content: str, assistant_content: str, conversation_id: UUID | None = None
    ) -> UUID:
        """Atomically create or continue a conversation and store one exchange."""
        ...


class PersistenceError(Exception):
    """Sanitized repository failure safe to cross the API boundary."""

    def __init__(self) -> None:
        super().__init__("Conversation persistence is unavailable.")


class ConversationNotFound(Exception):
    """The caller supplied an identifier that does not exist."""
