"""Request correlation IDs (v4.2).

Every HTTP request gets one correlation ID. A client may supply it in the
``X-Request-ID`` header, as long as it is 1-64 characters from
``[A-Za-z0-9._-]``. Otherwise a fresh ``req-<16 hex>`` ID is generated, so a
client can never inject log or header content. The ID is:

- stored in a context variable, so code anywhere in the request can read it
  with ``get_correlation_id()`` without passing it through every call (the
  thread pools used for blocking work copy the context, so it also reaches
  services running there);
- returned to the client in the ``X-Request-ID`` response header;
- added to every JSON log line and to every audit record that does not set
  its own correlation ID.
"""

import re
import secrets
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "x-request-id"
_VALID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_current: ContextVar[str | None] = ContextVar("jarvis_correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _current.get()


def new_correlation_id() -> str:
    return "req-" + secrets.token_hex(8)


def accept_or_generate(candidate: str | None) -> str:
    if candidate is not None and _VALID.fullmatch(candidate):
        return candidate
    return new_correlation_id()


class CorrelationIdMiddleware:
    """Pure ASGI middleware: sets the context variable and echoes the response header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((value.decode("latin-1") for name, value in scope.get("headers", [])
                         if name.lower() == HEADER.encode()), None)
        correlation_id = accept_or_generate(supplied)
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        token = _current.set(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() != HEADER.encode()]
                headers.append((HEADER.encode(), correlation_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _current.reset(token)
