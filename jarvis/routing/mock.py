"""Deterministic scripted adapter for offline routing and evaluation tests.

It implements the ``IntelligenceProvider`` contract and is marked
``simulated`` so evaluation summaries produced with it are never mistaken for
real model measurements.
"""

from typing import Callable

from pydantic import BaseModel

from jarvis.intelligence.base import ProviderError, TransientProviderError


class ScriptedAdapter:
    is_demo = False
    simulated = True

    def __init__(self, name: str, responder: Callable[[str], str] | dict[str, str] | None = None, *,
                 fail_with: str | None = None, advance: Callable[[], None] | None = None):
        self.name = name
        self._responder = responder
        self.fail_with = fail_with  # None, "transient", "error", or "crash"
        self._advance = advance  # lets tests advance a fake clock to simulate latency
        self.calls: list[dict] = []

    async def generate_reply(self, message: str, *, system_instruction: str | None = None,
                             response_model: type[BaseModel] | None = None) -> str:
        self.calls.append({"message": message, "structured": response_model is not None})
        if self._advance:
            self._advance()
        if self.fail_with == "transient":
            raise TransientProviderError()
        if self.fail_with == "error":
            raise ProviderError()
        if self.fail_with == "crash":
            raise RuntimeError("adapter internals with secret sk-test-should-not-leak")
        if callable(self._responder):
            return self._responder(message)
        if isinstance(self._responder, dict):
            return self._responder.get(message, "I do not know.")
        return "I do not know."
