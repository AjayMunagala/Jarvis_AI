"""Typed provider contract used by orchestration and API layers."""

from typing import Protocol


class IntelligenceProvider(Protocol):
    name: str
    is_demo: bool

    async def generate_reply(self, message: str) -> str:
        """Return a response to a user message."""
