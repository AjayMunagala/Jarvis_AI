"""DNS-rebinding and redirect regression tests for the HTTP browser provider.

The guarded transport must validate the address it actually connects to. Two
kinds of evidence are used:

- a fake ``httpcore`` network backend that records which IP each connection
  targets (no real network traffic), for public-looking addresses and
  cross-host redirects;
- a real loopback HTTP server, to prove that a rebound hostname never produces
  a request on the local service and that legitimate connections still work.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import pytest

from jarvis.browser.guarded_transport import GuardedNetworkBackend, GuardedTransport
from jarvis.browser.providers import ContextLimits, HttpBrowserProvider, ProviderError
from jarvis.browser.url_policy import BlockedURL, URLPolicy

LIMITS = ContextLimits(navigation_timeout_seconds=5, max_page_bytes=200_000)


class SequenceResolver:
    """Returns the next answer for each lookup of a host (simulates rebinding)."""

    def __init__(self, answers: dict[str, list[list[str]]]):
        self.answers = {host: list(values) for host, values in answers.items()}
        self.calls: list[str] = []

    def __call__(self, host, port):
        self.calls.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise OSError("unknown host")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class _FakeStream(httpcore.NetworkStream):
    def __init__(self, payload: bytes, log: list):
        self.payload = payload
        self.log = log

    def read(self, max_bytes, timeout=None):
        chunk, self.payload = self.payload[:max_bytes], self.payload[max_bytes:]
        return chunk

    def write(self, buffer, timeout=None):
        self.log.append(("write", bytes(buffer)))

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log.append(("start_tls", server_hostname))
        return self

    def get_extra_info(self, info):
        return None


class FakeBackend(httpcore.NetworkBackend):
    """Serves canned HTTP/1.1 responses per connected IP and records connections."""

    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.connected: list[tuple[str, int]] = []
        self.log: list = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        if host not in self.responses:
            raise httpcore.ConnectError("refused")
        return _FakeStream(self.responses[host], self.log)

    def sleep(self, seconds):
        pass


def http_response(status: int, body: str = "", headers: dict[str, str] | None = None) -> bytes:
    lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body.encode())}", "Content-Type: text/html", "Connection: close"]
    lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


def context_with(policy, backend):
    transport = GuardedTransport(policy, backend=backend)
    return HttpBrowserProvider(transport=transport).open_context(policy, LIMITS), transport


def test_rebinding_after_check_is_blocked_at_connection_time():
    resolver = SequenceResolver({"rebind.example": [["93.184.216.34"], ["10.0.0.5"]]})
    backend = FakeBackend({"93.184.216.34": http_response(200, "<p>public</p>")})
    context, _ = context_with(URLPolicy(resolver=resolver), backend)
    with pytest.raises(BlockedURL) as raised:
        context.navigate("http://rebind.example/")
    assert raised.value.code == "private_network_blocked"
    assert resolver.calls == ["rebind.example", "rebind.example"]  # checked, then re-resolved at connect
    assert backend.connected == []  # no socket was opened to any address


def test_connection_uses_the_validated_address_and_preserves_host_and_sni():
    resolver = SequenceResolver({"public.example": [["93.184.216.34"]]})
    backend = FakeBackend({"93.184.216.34": http_response(200, "<title>ok</title>")})
    context, transport = context_with(URLPolicy(resolver=resolver), backend)
    page = context.navigate("https://public.example/path")
    assert page.status == 200 and "ok" in page.html
    assert backend.connected == [("93.184.216.34", 443)]
    assert transport.network_backend.connections == [("public.example", "93.184.216.34", 443)]
    assert ("start_tls", "public.example") in backend.log  # certificate checked against the host name
    request_bytes = next(data for kind, data in backend.log if kind == "write")
    assert b"Host: public.example" in request_bytes


def test_any_disallowed_answer_blocks_the_connection():
    resolver = SequenceResolver({"mixed.example": [["93.184.216.34", "127.0.0.1"]]})
    backend = FakeBackend({"93.184.216.34": http_response(200, "x")})
    context, _ = context_with(URLPolicy(resolver=resolver), backend)
    with pytest.raises(BlockedURL):
        context.navigate("http://mixed.example/")
    assert backend.connected == []


def test_redirect_to_a_rebinding_host_is_blocked_before_connecting():
    resolver = SequenceResolver({
        "start.example": [["93.184.216.34"]],
        "next.example": [["93.184.216.35"], ["169.254.169.254"]],
    })
    backend = FakeBackend({
        "93.184.216.34": http_response(302, headers={"Location": "http://next.example/landing"}),
        "93.184.216.35": http_response(200, "<p>should not be reached</p>"),
    })
    context, _ = context_with(URLPolicy(resolver=resolver), backend)
    with pytest.raises(BlockedURL):
        context.navigate("http://start.example/")
    assert backend.connected == [("93.184.216.34", 80)]  # the rebound redirect target was never connected


def test_redirect_to_literal_internal_address_is_blocked_at_check_time():
    resolver = SequenceResolver({"start.example": [["93.184.216.34"]]})
    backend = FakeBackend({"93.184.216.34": http_response(301, headers={"Location": "http://169.254.169.254/latest"})})
    context, _ = context_with(URLPolicy(resolver=resolver), backend)
    with pytest.raises(BlockedURL):
        context.navigate("http://start.example/")
    assert backend.connected == [("93.184.216.34", 80)]


def test_legitimate_redirect_chain_between_public_hosts_succeeds():
    resolver = SequenceResolver({"a.example": [["93.184.216.34"]], "b.example": [["93.184.216.35"]]})
    backend = FakeBackend({
        "93.184.216.34": http_response(302, headers={"Location": "http://b.example/final"}),
        "93.184.216.35": http_response(200, "<title>Final</title>"),
    })
    context, _ = context_with(URLPolicy(resolver=resolver), backend)
    page = context.navigate("http://a.example/start")
    assert page.url == "http://b.example/final" and "Final" in page.html
    assert backend.connected == [("93.184.216.34", 80), ("93.184.216.35", 80)]


def test_backend_refuses_literal_private_addresses_and_unix_sockets():
    guarded = GuardedNetworkBackend(URLPolicy(), FakeBackend({"127.0.0.1": b""}))
    with pytest.raises(BlockedURL):
        guarded.connect_tcp("127.0.0.1", 80)
    with pytest.raises(BlockedURL):
        guarded.connect_tcp("2130706433", 80)
    with pytest.raises(httpcore.ConnectError):
        guarded.connect_unix_socket("/var/run/docker.sock")


def test_unreachable_validated_address_is_a_network_failure_not_a_bypass():
    resolver = SequenceResolver({"down.example": [["93.184.216.34"]]})
    context, _ = context_with(URLPolicy(resolver=resolver), FakeBackend({}))
    with pytest.raises(ProviderError) as raised:
        context.navigate("http://down.example/")
    assert raised.value.code == "navigation_failed"


# ------------------------------------------------- real loopback evidence

class _CountingHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.headers.get("Host", ""))
        body = b"<title>Local service</title><p>internal data</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_service():
    _CountingHandler.hits = []
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    except OSError as exc:
        pytest.skip(f"Loopback sockets are unavailable: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_real_rebinding_to_a_local_service_never_reaches_it(local_service):
    resolver = SequenceResolver({"rebind.example": [["93.184.216.34"], ["127.0.0.1"]]})
    context = HttpBrowserProvider().open_context(URLPolicy(resolver=resolver), LIMITS)
    with pytest.raises(BlockedURL):
        context.navigate(f"http://rebind.example:{local_service}/")
    assert _CountingHandler.hits == []  # the local service received no request
    context.close()


def test_real_connection_goes_to_the_validated_ip_with_the_original_host(local_service):
    resolver = SequenceResolver({"app.example": [["127.0.0.1"]]})
    context = HttpBrowserProvider().open_context(URLPolicy(allow_private_network=True, resolver=resolver), LIMITS)
    page = context.navigate(f"http://app.example:{local_service}/")
    assert page.status == 200 and "Local service" in page.html
    assert _CountingHandler.hits == [f"app.example:{local_service}"]
    context.close()
