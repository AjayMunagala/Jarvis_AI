"""Multi-pattern secret redaction for repository text, diffs, and diagnostics."""

import re

# Compiled patterns for known credential types
SECRET_PATTERNS = [
    # Private Key blocks
    (
        re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----", re.IGNORECASE),
        "[REDACTED_SECRET: PRIVATE_KEY]",
    ),
    # Anthropic and OpenAI API keys
    (
        re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{20,})\b"),
        "[REDACTED_SECRET: ANTHROPIC_KEY]",
    ),
    (
        re.compile(r"\b(sk-[A-Za-z0-9_\-]{20,})\b"),
        "[REDACTED_SECRET: OPENAI_KEY]",
    ),
    # GitHub personal access tokens
    (
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,})\b"),
        "[REDACTED_SECRET: GITHUB_TOKEN]",
    ),
    (
        re.compile(r"\b(github_pat_[A-Za-z0-9_]{50,})\b"),
        "[REDACTED_SECRET: GITHUB_PAT]",
    ),
    # AWS Access Key IDs
    (
        re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
        "[REDACTED_SECRET: AWS_ACCESS_KEY]",
    ),
    # Database URLs with user:password
    (
        re.compile(r"(postgres(?:ql)?:\/\/[^:\s]+:)([^@\s]+)(@)", re.IGNORECASE),
        r"\1[REDACTED_SECRET: DB_PASSWORD]\3",
    ),
    # Authorization: Bearer headers
    (
        re.compile(r"(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+\/-]+=*", re.IGNORECASE),
        r"\1[REDACTED_SECRET: BEARER_TOKEN]",
    ),
    # Common variable assignments for secrets / passwords / tokens
    (
        re.compile(r"((?:API_KEY|AUTH_TOKEN|SECRET_KEY|DATABASE_PASSWORD|PASSWORD|ACCESS_TOKEN|PRIVATE_KEY|CLIENT_SECRET)\s*[:=]\s*['\"])(\S+?)(['\"])", re.IGNORECASE),
        r"\1[REDACTED_SECRET: CONFIG_VALUE]\3",
    ),
    (
        re.compile(r"((?:api_key|auth_token|secret_key|database_password|password|access_token|private_key|client_secret)\s*[:=]\s*['\"])(\S+?)(['\"])", re.IGNORECASE),
        r"\1[REDACTED_SECRET: CONFIG_VALUE]\3",
    ),
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Redact known secret patterns from text, returning (redacted_text, count).

    Note: Secret redaction is heuristic and defense-in-depth; it does not guarantee
    complete detection of all high-entropy strings or custom credential formats.
    """
    if not text:
        return text, 0

    total_redacted = 0
    current_text = text

    for pattern, replacement in SECRET_PATTERNS:
        # Count matches before substitution
        matches = len(pattern.findall(current_text))
        if matches > 0:
            total_redacted += matches
            if replacement == "[REDACTED_SECRET: PRIVATE_KEY]":
                # Preserve line positions for repository search context/results.
                current_text = pattern.sub(
                    lambda match: replacement + "\n" * match.group(0).count("\n"), current_text,
                )
            else:
                current_text = pattern.sub(replacement, current_text)

    return current_text, total_redacted
