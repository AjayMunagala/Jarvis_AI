"""Typed provider contract used by orchestration and API layers."""

from typing import Protocol
from pydantic import BaseModel


class IntelligenceProvider(Protocol):
    name: str
    is_demo: bool

    async def generate_reply(
        self,
        message: str,
        *,
        system_instruction: str | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str:
        """Return provider content, optionally constrained to a Pydantic schema."""


class ProviderError(Exception):
    """Sanitized, non-retryable provider failure."""

    def __init__(self, message: str = "The configured provider request failed.") -> None:
        super().__init__(message)


class TransientProviderError(ProviderError):
    """Sanitized provider failure eligible for bounded orchestration retries."""

    def __init__(self) -> None:
        super().__init__("The configured provider is temporarily unavailable.")
