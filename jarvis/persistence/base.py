"""Database-independent conversation repository contracts."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


MessageRole = Literal["user", "assistant"]


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    conversation_id: UUID
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

    def get_messages(self, conversation_id: UUID) -> tuple[ConversationMessage, ...]: ...

    def save_exchange(self, user_content: str, assistant_content: str) -> UUID:
        """Atomically create a one-request conversation and store its two messages."""
        ...


class PersistenceError(Exception):
    """Sanitized repository failure safe to cross the API boundary."""

    def __init__(self) -> None:
        super().__init__("Conversation persistence is unavailable.")
