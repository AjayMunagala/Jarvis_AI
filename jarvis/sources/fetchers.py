"""Bounded, SSRF-safe fetching for event sources (v7.3).

Feeds (``HttpFeedFetcher``):
- ``https`` only, on the default port (443); no credentials in the URL; at most 2000 characters;
- the host is resolved and every address must be public (the browser's ``URLPolicy``/``is_public_address``:
  private, loopback, link-local, carrier-grade NAT, multicast, reserved and unspecified ranges are refused,
  IPv4 and IPv6, including IPv4-mapped IPv6 and numeric host spellings; ``localhost`` is refused);
- the connection goes through ``GuardedTransport``, which connects only to the validated address (DNS rebinding);
- at most 3 redirects, each hop checked again; connect timeout 5 s, total 15 s; at most 2 MB; an XML content type;
- ``Accept-Encoding: identity`` and raw reads: a server that still compresses may use one gzip or deflate layer,
  decoded incrementally under the same 2 MB cap (``jarvis.browser.bounded_body``); anything else is refused.

Folders (``scan_folder``): the folder must be inside an owner-configured root (``JARVIS_SOURCES_FOLDER_ROOTS``);
the path is first checked lexically (UNC and device paths are refused unless the root itself is UNC, so no SMB
connection is made for a path outside the roots), then real paths are compared; symbolic links and junctions
(mount points) are never followed; other reparse points such as OneDrive cloud placeholders are listed like files
but never opened (so never downloaded); at most 500 entries are looked at per poll (reported as truncated);
hidden and temporary files are ignored; file contents are never opened (name, size, mtime only).
"""

from dataclasses import dataclass
import os
from pathlib import Path
import stat as stat_module
from time import monotonic
from typing import Callable, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

from jarvis.browser.bounded_body import (
    IDENTITY_HEADERS, BodyTooLarge, UnsupportedEncoding, read_response_body,
)
from jarvis.browser.url_policy import BlockedURL, URLPolicy, _parse_numeric_host, is_public_address, system_resolver

MAX_URL_LENGTH = 2000
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 15.0
MAX_FEED_BYTES = 2 * 1024 * 1024
FEED_CONTENT_TYPES = frozenset({
    "application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml", "text/xml",
})
MAX_FOLDER_ENTRIES = 500
FOLDER_TIME_BUDGET_SECONDS = 5.0
_TEMP_SUFFIXES = (".tmp", ".crdownload", ".part", ".swp", ".partial", ".download")

Resolver = Callable[[str, int], Iterable[str]]


class FetchError(Exception):
    """A sanitized fetch or scan failure; ``str()`` is safe to store and show (no URL, path or driver text)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ===================================================================== feeds

def check_feed_url(raw_url: str, resolver: Resolver | None = None) -> str:
    """Validate a feed URL and return it normalized (no fragment). Raises ``FetchError``.

    Without ``resolver`` only the static rules run (scheme, credentials, port, IP literals and localhost);
    with one, the host is resolved and every address must be public."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise FetchError("invalid_url", "A feed URL is required.")
    raw_url = raw_url.strip()
    if len(raw_url) > MAX_URL_LENGTH or any(ord(char) < 0x21 or ord(char) == 0x7f for char in raw_url):
        raise FetchError("invalid_url", "The feed URL is too long or contains spaces or control characters.")
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("invalid_url", "The feed URL could not be parsed.") from exc
    if parts.scheme.lower() != "https":
        raise FetchError("scheme_not_allowed", "Only https feed URLs are allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError("credentials_in_url", "Feed URLs must not contain credentials.")
    if port is not None and port != 443:
        raise FetchError("port_not_allowed", "Feed URLs must use the default https port (443).")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError("invalid_url", "The feed URL must include a host.")
    try:
        host = host if host.isascii() else host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FetchError("invalid_url", "The feed host name is not valid.") from exc
    if host == "localhost" or host.endswith(".localhost"):
        raise FetchError("private_network_blocked", "Local, private and internal network addresses are refused.")
    literal = _parse_numeric_host(host)
    if literal is not None and not is_public_address(literal):
        raise FetchError("private_network_blocked", "Local, private and internal network addresses are refused.")
    if resolver is not None and literal is None:
        try:
            URLPolicy(resolver=resolver).allowed_addresses(host, 443)
        except BlockedURL as exc:
            if exc.code == "dns_failure":
                raise FetchError("dns_failure", "The feed host name could not be resolved.") from None
            raise FetchError("private_network_blocked",
                             "Local, private and internal network addresses are refused.") from None
    netloc = host if literal is None or literal.version == 4 else f"[{literal}]"
    return urlunsplit(SplitResult("https", netloc, parts.path or "/", parts.query, ""))


class HttpFeedFetcher:
    """Fetch one feed document: ``fetcher(url) -> bytes``. Tests inject ``transport`` (no real network)."""

    def __init__(self, *, resolver: Resolver = system_resolver, transport=None,
                 max_bytes: int = MAX_FEED_BYTES, total_timeout_seconds: float = TOTAL_TIMEOUT_SECONDS):
        self.resolver = resolver
        self.transport = transport
        self.max_bytes = max_bytes
        self.total_timeout_seconds = total_timeout_seconds

    def __call__(self, url: str) -> bytes:
        import httpx

        from jarvis.browser.guarded_transport import GuardedTransport

        policy = URLPolicy(resolver=self.resolver)
        started = monotonic()
        timeout = httpx.Timeout(self.total_timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
        with httpx.Client(follow_redirects=False, timeout=timeout, trust_env=False,
                          transport=self.transport or GuardedTransport(policy),
                          headers={"User-Agent": "JARVIS-Sources/7.3 (+local personal assistant)",
                                   "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                                             "text/xml;q=0.8", **IDENTITY_HEADERS}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                url = check_feed_url(url, self.resolver)
                remaining = self.total_timeout_seconds - (monotonic() - started)
                if remaining <= 0:
                    raise FetchError("timeout", "The feed did not respond in time.")
                try:
                    with client.stream("GET", url, timeout=httpx.Timeout(remaining, connect=min(
                            CONNECT_TIMEOUT_SECONDS, remaining))) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError("http_error", "The feed redirected without a location.")
                            url = str(response.url.join(location))
                            continue
                        if response.status_code >= 400:
                            raise FetchError("http_error", f"The feed answered HTTP {response.status_code}.")
                        if response.status_code != 200:
                            raise FetchError("http_error", f"Unexpected HTTP status {response.status_code}.")
                        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if content_type not in FEED_CONTENT_TYPES:
                            raise FetchError("unsupported_content", "The response is not an RSS, Atom or XML document.")
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > self.max_bytes:
                            raise FetchError("response_too_large", "The feed is larger than 2 MB.")
                        def check_deadline() -> None:
                            if monotonic() - started > self.total_timeout_seconds:
                                raise FetchError("timeout", "The feed did not respond in time.")

                        try:  # v7.3 review: raw bytes, at most one bounded gzip/deflate layer (no bombs)
                            return read_response_body(response, self.max_bytes, check_deadline)
                        except BodyTooLarge:
                            raise FetchError("response_too_large", "The feed is larger than 2 MB.") from None
                        except UnsupportedEncoding as exc:
                            raise FetchError("unsupported_encoding", str(exc)) from None
                except FetchError:
                    raise
                except BlockedURL:
                    raise FetchError("private_network_blocked",
                                     "Local, private and internal network addresses are refused.") from None
                except httpx.TimeoutException:
                    raise FetchError("timeout", "The feed did not respond in time.") from None
                except httpx.HTTPError:
                    raise FetchError("network_error", "The feed could not be reached (network error).") from None
        raise FetchError("too_many_redirects", "The feed redirected more than 3 times.")


# ===================================================================== folders

@dataclass(frozen=True)
class FileEvent:
    relative_path: str  # POSIX separators, relative to the watched folder
    size: int
    mtime_ns: int


def parse_folder_roots(text: str | None) -> tuple[Path, ...]:
    """``JARVIS_SOURCES_FOLDER_ROOTS``: absolute folders separated by ``os.pathsep`` (``;`` on Windows)."""
    roots: list[Path] = []
    for part in (text or "").split(os.pathsep):
        part = part.strip()
        if part:
            path = Path(part)
            if not path.is_absolute():
                raise ValueError("Each JARVIS_SOURCES_FOLDER_ROOTS entry must be an absolute path.")
            roots.append(path)
    return tuple(roots)


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:  # different drives
        return False


def _is_unc_or_device(text: str) -> bool:
    """``\\\\server\\share``, ``\\\\?\\...`` (including ``\\\\?\\UNC\\``) and ``\\\\.\\`` device paths."""
    return text.replace("/", "\\").startswith("\\\\")


def _lexically_allowed(raw_path: str, roots: tuple[Path, ...]) -> bool:
    """A purely lexical check (no filesystem access): the normalized path is under a normalized root."""
    if _is_unc_or_device(raw_path) and not any(_is_unc_or_device(str(root)) for root in roots):
        return False
    candidate = Path(os.path.normpath(raw_path))
    return any(_within(candidate, Path(os.path.normpath(str(root)))) for root in roots)


def resolve_watched_folder(raw_path: str, roots: tuple[Path, ...]) -> tuple[Path, Path]:
    """Return (real folder, real root) for a folder inside an allowlisted root, or raise ``FetchError``."""
    if not roots:
        raise FetchError("folder_sources_disabled",
                         "Folder sources are off: set JARVIS_SOURCES_FOLDER_ROOTS to the folders JARVIS may watch.")
    if any(ord(char) < 0x20 for char in raw_path):
        raise FetchError("invalid_path", "The folder path contains control characters.")
    if not _is_unc_or_device(raw_path) and not Path(raw_path).is_absolute():
        raise FetchError("invalid_path", "The folder path must be absolute.")
    outside = FetchError("outside_allowed_roots",
                         "The folder is not inside a folder allowed by JARVIS_SOURCES_FOLDER_ROOTS.")
    if not _lexically_allowed(raw_path, roots):
        raise outside  # before any filesystem access, so a UNC path never triggers an SMB connection
    try:
        real = Path(os.path.realpath(raw_path, strict=True))
    except (OSError, RuntimeError):
        raise FetchError("folder_missing", "The folder does not exist or cannot be read.") from None
    if not real.is_dir():
        raise FetchError("folder_missing", "The path is not a folder.")
    for root in roots:
        try:
            real_root = Path(os.path.realpath(root, strict=True))
        except (OSError, RuntimeError):
            continue
        if _within(real, real_root):
            return real, real_root
    raise outside


def is_ignored_name(name: str) -> bool:
    lowered = name.lower()
    return name.startswith(".") or name.startswith("~$") or lowered.endswith(_TEMP_SUFFIXES) or name.endswith("~")


def _hidden_attribute(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0))


_LINK_TAGS = frozenset({getattr(stat_module, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
                        getattr(stat_module, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)})


def is_link_or_junction(entry, info=None) -> bool:
    """Symbolic links and junctions (mount points) are never followed. Other reparse points (for example OneDrive
    cloud placeholders) are not links: they are listed by name, size and mtime and never opened."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    if info is None:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return True
    return getattr(info, "st_reparse_tag", 0) in _LINK_TAGS


def scan_folder(raw_path: str, roots: tuple[Path, ...], *, recursive_depth: int = 0,
                max_entries: int = MAX_FOLDER_ENTRIES,
                time_budget_seconds: float = FOLDER_TIME_BUDGET_SECONDS) -> tuple[list[FileEvent], bool]:
    """(regular files with name, size and mtime; truncated). Never opens a file; reads directories lazily and
    stops at ``max_entries`` entries or the time budget."""
    folder, _root = resolve_watched_folder(raw_path, roots)
    started = monotonic()
    events: list[FileEvent] = []
    looked_at = 0
    truncated = False
    pending: list[tuple[Path, int]] = [(folder, 0)]
    while pending and not truncated:
        directory, depth = pending.pop(0)
        try:
            iterator = os.scandir(directory)
        except OSError:
            if directory == folder:
                raise FetchError("folder_unreadable", "The folder could not be read.") from None
            continue
        with iterator:
            for entry in iterator:
                looked_at += 1
                if looked_at > max_entries or monotonic() - started > time_budget_seconds:
                    truncated = True
                    break
                if is_ignored_name(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if is_link_or_junction(entry, info) or _hidden_attribute(info):
                    continue
                path = Path(entry.path)
                if stat_module.S_ISDIR(info.st_mode):
                    if depth < recursive_depth:
                        pending.append((path, depth + 1))
                    continue
                if not stat_module.S_ISREG(info.st_mode):
                    continue
                events.append(FileEvent(relative_path=path.relative_to(folder).as_posix(), size=int(info.st_size),
                                        mtime_ns=int(info.st_mtime_ns)))
    events.sort(key=lambda event: (event.relative_path.count("/"), event.relative_path))
    return events, truncated
