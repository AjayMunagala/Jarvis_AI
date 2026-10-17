"""Destination policy for every browser request (SSRF and local-file protection).

Rules, applied before each navigation, each redirect hop, and (for engines that
support request interception) each subresource request:

- only ``http`` and ``https``; ``file:``, ``data:``, ``javascript:``, ``chrome:``
  and every other scheme are refused, so local files cannot be read;
- no credentials in the URL;
- the host is resolved and every resolved address must be a public unicast
  address: loopback, private, link-local (including cloud metadata
  169.254.169.254), carrier-grade NAT, multicast, reserved, and unspecified
  ranges are refused, including IPv4-mapped IPv6 and numeric host spellings;
- optional allowed/blocked domain lists (suffix match on label boundaries).

Loopback/private destinations can be enabled only outside production for
controlled local test pages (``JARVIS_BROWSER_ALLOW_PRIVATE_NETWORK``).

DNS rebinding: the HTTP provider additionally enforces ``allowed_addresses`` at
connection time (``guarded_transport.py``) and connects only to the validated
IP, so a hostname that re-resolves after ``check`` cannot reach a disallowed
address. Chromium (Playwright provider) resolves DNS itself; there the policy is
applied per request URL, which does not pin the connected address.
"""

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

MAX_URL_LENGTH = 2000
ALLOWED_SCHEMES = frozenset({"http", "https"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

Resolver = Callable[[str, int], Iterable[str]]


class BlockedURL(ValueError):
    """A destination refused by policy; ``code`` is safe to return to clients."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _parse_numeric_host(host: str) -> ipaddress._BaseAddress | None:
    """Recognize IP literals, including decimal/hex/octal IPv4 spellings."""
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    try:
        # inet_aton accepts 2130706433, 0x7f000001, 0177.0.0.1, 127.1, ...
        packed = socket.inet_aton(candidate)
    except OSError:
        return None
    if all(part.isalnum() for part in candidate.split(".")):
        return ipaddress.ip_address(packed)
    return None


def is_public_address(address: ipaddress._BaseAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT:
        return False
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
        or not address.is_global
    )


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class URLPolicy:
    allow_private_network: bool = False
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    resolver: Resolver = field(default=system_resolver, compare=False)

    def check(self, raw_url: str) -> str:
        """Return a normalized URL or raise ``BlockedURL``."""
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise BlockedURL("invalid_url", "A URL is required.")
        raw_url = raw_url.strip()
        if len(raw_url) > MAX_URL_LENGTH or any(ord(char) < 0x21 for char in raw_url):
            raise BlockedURL("invalid_url", "The URL is too long or contains control characters.")
        try:
            parts = urlsplit(raw_url)
            port = parts.port
        except ValueError as exc:
            raise BlockedURL("invalid_url", "The URL could not be parsed.") from exc
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise BlockedURL("scheme_not_allowed", "Only http and https URLs are allowed.")
        if parts.username is not None or parts.password is not None:
            raise BlockedURL("credentials_in_url", "URLs must not contain credentials.")
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise BlockedURL("invalid_url", "The URL must include a host.")
        try:
            host = host.encode("idna").decode("ascii") if not host.isascii() else host
        except UnicodeError as exc:
            raise BlockedURL("invalid_url", "The host name is not valid.") from exc
        port = port or (443 if scheme == "https" else 80)

        if any(_domain_matches(host, domain) for domain in self.blocked_domains):
            raise BlockedURL("domain_blocked", "The destination domain is blocked by policy.")
        if self.allowed_domains and not any(_domain_matches(host, domain) for domain in self.allowed_domains):
            raise BlockedURL("domain_not_allowed", "The destination domain is not in the allowed list.")

        self.allowed_addresses(host, port)
        literal = _parse_numeric_host(host)
        netloc = host if literal is None or literal.version == 4 else f"[{literal}]"
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit(SplitResult(scheme, netloc, parts.path or "/", parts.query, ""))

    def allowed_addresses(self, host: str, port: int) -> list[ipaddress._BaseAddress]:
        """Resolve ``host`` once and return its addresses only if every one is permitted.

        Used both by ``check`` and, at connection time, by the guarded network
        backend, so the address that is validated is the address connected to.
        """
        literal = _parse_numeric_host(host)
        if literal is not None:
            addresses = [literal]
        elif host == "localhost" or host.endswith(".localhost"):
            addresses = [ipaddress.ip_address("127.0.0.1")]
        else:
            try:
                addresses = [ipaddress.ip_address(item) for item in self.resolver(host, port)]
            except (OSError, ValueError) as exc:
                raise BlockedURL("dns_failure", "The host name could not be resolved.") from exc
            if not addresses:
                raise BlockedURL("dns_failure", "The host name could not be resolved.")
        if not self.allow_private_network and not all(is_public_address(item) for item in addresses):
            raise BlockedURL(
                "private_network_blocked",
                "Local, private, and internal network destinations are blocked.",
            )
        return addresses
