"""Connection-time destination enforcement for the HTTP browser provider.

``URLPolicy.check`` validates a URL before a request, but a hostname can
resolve differently when the socket is actually opened (DNS rebinding). This
module closes that gap: a custom ``httpcore`` network backend resolves the
host itself, requires every resolved address to pass ``URLPolicy``, and then
opens the TCP connection to one of those *validated* IP addresses. The address
that is checked is the address that is connected to; nothing is re-resolved
in between.

TLS is unaffected: ``httpcore`` performs the handshake with the original
hostname as SNI/``server_hostname``, so certificates are still verified
against the requested host, not the IP. Only public ``httpx``/``httpcore``
APIs are used. Connection reuse is safe because a pooled connection was
validated when it was opened.
"""

import contextlib
from typing import Iterable, Iterator

import httpcore
import httpx

from jarvis.browser.url_policy import URLPolicy

# Map httpcore failures to the httpx exception types callers already handle.
_EXCEPTIONS: tuple[tuple[type[Exception], type[Exception]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
)


@contextlib.contextmanager
def _mapped(request: httpx.Request) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        # The table lists subclasses before their bases. BlockedURL is not
        # listed, so policy refusals propagate unchanged.
        for source, target in _EXCEPTIONS:
            if isinstance(exc, source):
                raise target(str(exc), request=request) from exc
        raise


class GuardedNetworkBackend(httpcore.NetworkBackend):
    """Resolve, validate, and connect to the same address."""

    def __init__(self, policy: URLPolicy, backend: httpcore.NetworkBackend | None = None):
        self.policy = policy
        self.backend = backend or httpcore.SyncBackend()
        self.connections: list[tuple[str, str, int]] = []  # (requested host, connected ip, port) for audit/tests

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.NetworkStream:
        # Raises BlockedURL if any resolved address is disallowed.
        addresses = self.policy.allowed_addresses(host.lower().rstrip("."), port)
        last_error: Exception | None = None
        for address in addresses:
            ip = str(address)
            try:
                stream = self.backend.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
                continue
            self.connections.append((host, ip, port))
            return stream
        raise last_error or httpcore.ConnectError("No validated address accepted the connection.")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix socket connections are not permitted.")

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream, request: httpx.Request):
        self._stream = stream
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        with _mapped(self._request):
            for part in self._stream:
                yield part

    def close(self) -> None:
        if hasattr(self._stream, "close"):
            self._stream.close()


class GuardedTransport(httpx.BaseTransport):
    """An ``httpx`` transport whose every TCP connection passes ``URLPolicy``.

    No proxy support by design: a proxy would make the proxy, not the
    destination, the connected address.
    """

    def __init__(self, policy: URLPolicy, *, backend: httpcore.NetworkBackend | None = None):
        self.network_backend = GuardedNetworkBackend(policy, backend)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(), http1=True, http2=False, retries=0,
            max_connections=10, network_backend=self.network_backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _mapped(request):
            response = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=response.status, headers=response.headers,
            stream=_ResponseStream(response.stream, request), extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()
