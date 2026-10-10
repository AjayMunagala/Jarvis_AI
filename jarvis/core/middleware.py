"""Bound HTTP request bodies before JSON validation allocates parsed objects."""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self._reject(scope, send)
                    return
            except ValueError:
                # Let the ASGI server/framework handle malformed lengths.
                pass

        bytes_received = 0
        body_too_large = False
        rejection_sent = False

        async def limited_receive() -> Message:
            nonlocal bytes_received, body_too_large
            if body_too_large:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                bytes_received += len(message.get("body", b""))
                if bytes_received > self.max_body_bytes:
                    body_too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal rejection_sent
            if body_too_large:
                if message["type"] == "http.response.start" and not rejection_sent:
                    rejection_sent = True
                    await self._reject(scope, send)
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not body_too_large:
                raise
        if body_too_large and not rejection_sent:
            await self._reject(scope, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            {
                "error": "request_too_large",
                "detail": "Request body exceeds the configured size limit.",
            }
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if scope.get("http_version") in {"1.0", "1.1"}:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
