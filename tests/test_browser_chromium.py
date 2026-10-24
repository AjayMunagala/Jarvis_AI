"""REAL-CHROMIUM browser tests (v2.6).

These tests launch an actual headless Chromium through the optional Python
Playwright package and drive it against a local loopback HTTP server started
here. No external website is contacted. They are skipped (not passed) when
Playwright or its Chromium build is unavailable.

The URL policy allows only the literal host ``127.0.0.1`` (``allowed_domains``)
with private-network access enabled for that local server, so any other
destination — ``localhost``, link-local metadata, private ranges — is refused,
including subresources and redirect hops.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest
from pydantic import SecretStr

pytest.importorskip("playwright.sync_api")

from jarvis.browser.models import BrowserActionRequest  # noqa: E402
from jarvis.browser.providers import PlaywrightBrowserProvider  # noqa: E402
from jarvis.browser.service import BrowserService  # noqa: E402
from jarvis.browser.url_policy import BlockedURL, URLPolicy  # noqa: E402
from jarvis.core.config import Settings  # noqa: E402

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "chromium-test-token-with-at-least-32-characters"


def _chromium_launchable() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_launchable(), reason="Real Chromium for Playwright is unavailable.")

PAGES = {
    "/": """<html><head><title>Real home</title></head><body>
<h1>Real Chromium page</h1>
<p id="static">Static paragraph.</p>
<div id="dynamic"></div>
<label for="q">Search query</label><input id="q" name="q">
<input type="password" name="pw" aria-label="Password">
<select id="lang" name="lang" aria-label="Language"><option value="en" selected>English</option><option value="fr">French</option></select>
<p id="echo">echo:none</p>
<button type="button" id="reveal" aria-label="Reveal details">Reveal</button>
<form method="post" action="/submit"><button type="submit">Send form</button></form>
<a href="/next">Next page</a>
<div style="height:3000px"></div>
<script>
document.getElementById('dynamic').textContent = 'Rendered by JavaScript';
document.getElementById('q').addEventListener('input', e => document.getElementById('echo').textContent = 'echo:' + e.target.value);
document.getElementById('lang').addEventListener('change', e => document.getElementById('echo').textContent = 'lang:' + e.target.value);
document.getElementById('reveal').addEventListener('click', () => document.getElementById('static').textContent = 'Revealed by click');
console.error('deliberate console error from page');
</script></body></html>""",
    "/next": "<html><head><title>Next real page</title></head><body><p>Second page</p></body></html>",
    "/subresources": """<html><head><title>Subresources</title></head><body><p>Subresource policy page</p>
<p id="marker">marker:</p>
<img src="http://169.254.169.254/latest/meta-data.png">
<script src="http://localhost:{port}/evil.js"></script>
<script src="/ok.js"></script></body></html>""",
    "/ok.js": "document.getElementById('marker').textContent += 'ok-script-ran';",
    "/evil.js": "document.getElementById('marker').textContent += 'EVIL-SCRIPT-RAN';",
    "/cookie": "<html><head><title>Cookie</title></head><body><p id='c'></p><script>document.getElementById('c').textContent = 'cookie=' + document.cookie;</script></body></html>",
}


class _Server:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="text/html; charset=utf-8", extra=()):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                owner.requests.append(("GET", self.path))
                port = self.server.server_address[1]
                if self.path == "/redirect-ok":
                    self.send_response(302)
                    self.send_header("Location", "/next")
                    self.end_headers()
                elif self.path == "/redirect-out":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{port}/next")
                    self.end_headers()
                elif self.path == "/slow":
                    time.sleep(6)
                    self._send(200, "<p>too late</p>")
                elif self.path == "/set-cookie":
                    self._send(200, "<title>Set</title><p>set</p>", extra=(("Set-Cookie", "sid=session-a; Path=/"),))
                elif self.path in PAGES:
                    body = PAGES[self.path].replace("{port}", str(port))
                    self._send(200, body, "application/javascript" if self.path.endswith(".js") else "text/html; charset=utf-8")
                else:
                    self._send(404, "<p>missing</p>")

            def do_POST(self):  # noqa: N802
                owner.requests.append(("POST", self.path))
                self._send(200, "<p>posted</p>")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    try:
        instance = _Server()
    except OSError as exc:
        pytest.skip(f"Loopback sockets are unavailable: {exc}")
    yield instance
    instance.close()


@pytest.fixture
def service():
    settings = Settings(
        _env_file=None, environment="test", log_level="ERROR", browser_enabled=True, browser_provider="playwright",
        browser_allow_private_network=True, browser_navigation_timeout_seconds=3,
        local_auth_token=SecretStr(TOKEN), owner_id=OWNER,
    )
    policy = URLPolicy(allow_private_network=True, allowed_domains=("127.0.0.1",))
    instance = BrowserService(settings, PlaywrightBrowserProvider(), policy=policy)
    yield instance
    instance.close_all()


def _act(service, session_id, **kwargs):
    return service.act(OWNER, session_id, BrowserActionRequest(**kwargs))


def _element(observation, label):
    return next(item for item in observation.elements if item.label == label)


def test_real_chromium_startup_observation_and_shutdown(service, server):
    view = service.create_session(OWNER, server.base + "/")
    observation = view.last_observation
    assert view.provider == "playwright" and observation.javascript_executed is True
    assert observation.title == "Real home" and observation.http_status == 200
    assert "Rendered by JavaScript" in observation.text  # proves real JS execution
    assert _element(observation, "Search query").kind == "text_input"
    assert _element(observation, "Password").sensitive is True
    assert _element(observation, "Reveal details").kind == "button"
    assert _element(observation, "Send form").kind == "submit"
    assert _element(observation, "Language").options[1].value == "fr"
    assert any("deliberate console error" in item.message for item in observation.console_errors)
    context = service._sessions[view.session_id].context
    closed = service.close_session(OWNER, view.session_id)
    assert closed.state == "closed" and closed.screenshots == ()
    assert context.page.is_closed()


def test_real_chromium_navigation_and_redirects(service, server):
    view = service.create_session(OWNER, server.base + "/redirect-ok")
    assert view.last_observation.url == server.base + "/next"
    assert view.last_observation.title == "Next real page"
    blocked = _act(service, view.session_id, action="navigate", url=server.base + "/redirect-out")
    assert blocked.record.outcome == "blocked"  # redirect hop to localhost refused
    assert ("GET", "/next") in server.requests
    direct = _act(service, view.session_id, action="navigate", url="http://169.254.169.254/latest/meta-data")
    assert direct.record.outcome == "blocked"
    back = _act(service, view.session_id, action="back")
    assert back.record.outcome == "performed"


def test_real_chromium_interactions_and_screenshot(service, server):
    view = service.create_session(OWNER, server.base + "/")
    observation = view.last_observation
    typed = _act(service, view.session_id, action="type", element_id=_element(observation, "Search query").element_id,
                 observation_id=observation.observation_id, text="hello chromium")
    assert typed.record.outcome == "performed" and "echo:hello chromium" in typed.session.last_observation.text
    observation = typed.session.last_observation
    selected = _act(service, view.session_id, action="select", element_id=_element(observation, "Language").element_id,
                    observation_id=observation.observation_id, option="fr")
    assert selected.record.outcome == "performed" and "lang:fr" in selected.session.last_observation.text
    observation = selected.session.last_observation
    clicked = _act(service, view.session_id, action="click", element_id=_element(observation, "Reveal details").element_id,
                   observation_id=observation.observation_id)
    assert clicked.record.outcome == "performed" and clicked.record.transition == "same_page"
    assert "Revealed by click" in clicked.session.last_observation.text
    scrolled = _act(service, view.session_id, action="scroll", direction="down")
    assert scrolled.record.outcome == "performed"
    shot = _act(service, view.session_id, action="screenshot")
    image = service.screenshot_bytes(OWNER, view.session_id, shot.record.screenshot_id)
    assert image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) > 1000
    observation = shot.session.last_observation  # scroll produced the latest observation
    link = _act(service, view.session_id, action="click", element_id=_element(observation, "Next page").element_id,
                observation_id=observation.observation_id)
    assert link.record.transition == "navigated" and link.session.last_observation.title == "Next real page"


def test_real_chromium_never_submits_consequential_controls(service, server):
    view = service.create_session(OWNER, server.base + "/")
    observation = view.last_observation
    result = _act(service, view.session_id, action="click", element_id=_element(observation, "Send form").element_id,
                  observation_id=observation.observation_id)
    assert result.record.outcome == "approval_required"
    password = _act(service, view.session_id, action="type", element_id=_element(observation, "Password").element_id,
                    observation_id=result.session.last_observation.observation_id if result.session.last_observation else observation.observation_id,
                    text="hunter2")
    assert password.record.outcome == "blocked"
    assert not any(method == "POST" for method, _ in server.requests)


def test_real_chromium_subresource_policy(service, server):
    view = service.create_session(OWNER, server.base + "/subresources")
    observation = view.last_observation
    assert "ok-script-ran" in observation.text
    assert "EVIL-SCRIPT-RAN" not in observation.text  # localhost script aborted before it ran
    blocked = [item for item in observation.network_failures if item.error == "Blocked by JARVIS URL policy"]
    assert {item.url.split("/")[2] for item in blocked} >= {"169.254.169.254", f"localhost:{server.server.server_address[1]}"}
    assert ("GET", "/evil.js") not in server.requests


def test_real_chromium_timeout_and_disconnect(service, server):
    view = service.create_session(OWNER, server.base + "/next")
    slow = _act(service, view.session_id, action="navigate", url=server.base + "/slow")
    assert slow.record.outcome == "timed_out"
    context = service._sessions[view.session_id].context
    context._call(lambda: context.browser.close())  # simulate the browser going away
    after = _act(service, view.session_id, action="navigate", url=server.base + "/next")
    assert after.record.outcome == "failed" and after.session.state == "failed"


def test_real_chromium_sessions_are_isolated(service, server):
    first = service.create_session(OWNER, server.base + "/set-cookie")
    _act(service, first.session_id, action="navigate", url=server.base + "/cookie")
    assert "cookie=sid=session-a" in service.get_session(OWNER, first.session_id).last_observation.text
    second = service.create_session(OWNER, server.base + "/cookie")
    assert "cookie=sid" not in second.last_observation.text
    service.close_session(OWNER, first.session_id)
    still = _act(service, second.session_id, action="reload")
    assert still.record.outcome == "performed"


def test_real_chromium_blocks_disallowed_start_url(service):
    with pytest.raises(BlockedURL):
        service.create_session(OWNER, "http://localhost:1/")
