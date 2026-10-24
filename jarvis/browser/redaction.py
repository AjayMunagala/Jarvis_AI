"""Redaction for browser text, URLs, and diagnostics before they leave the provider."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jarvis.repo.secret_redactor import redact_secrets

_SENSITIVE_PARAM = re.compile(
    r"(token|key|secret|pass|pwd|auth|session|sid|code|sig|signature|credential|cookie|otp|jwt)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = re.compile(
    r"(pass|pwd|token|secret|otp|one.?time|2fa|mfa|cvv|cvc|card|ccnum|iban|ssn|pin\b|security.?code)",
    re.IGNORECASE,
)
_SENSITIVE_AUTOCOMPLETE = re.compile(
    r"(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp)", re.IGNORECASE,
)


def redact_text(text: str) -> tuple[str, int]:
    return redact_secrets(text or "")


def sanitize_url(url: str, *, keep_query: bool = True) -> str:
    """Drop fragments and mask sensitive query values; optionally drop the query."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unparseable-url]"
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    query = ""
    if keep_query and parts.query:
        pairs = [
            (key, "[REDACTED]" if _SENSITIVE_PARAM.search(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        query = urlencode(pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))[:2000]


def is_sensitive_field(*, input_type: str | None, name: str | None, label: str | None, autocomplete: str | None) -> bool:
    if (input_type or "").lower() in {"password", "hidden"}:
        return True
    if autocomplete and _SENSITIVE_AUTOCOMPLETE.search(autocomplete):
        return True
    return any(value and _SENSITIVE_FIELD.search(value) for value in (name, label))


def contains_secret(text: str) -> bool:
    return redact_secrets(text or "")[1] > 0
