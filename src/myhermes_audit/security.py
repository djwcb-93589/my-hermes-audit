"""Small, deterministic redaction helpers for local Audit artifacts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from myhermes_audit.environment import is_sensitive_environment_name


REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "\n...[truncated by my-hermes-audit]...\n"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_AUTHORIZATION = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9_-]{12,}|"
    r"glpat-[A-Za-z0-9_-]{12,}"
    r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)


def sensitive_environment_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        value
        for name, value in environment.items()
        if is_sensitive_environment_name(name)
        and isinstance(value, str)
        and len(value) >= 4
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(text: str, sensitive_values: Iterable[str] = ()) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    redacted = text
    for value in sorted(
        {item for item in sensitive_values if isinstance(item, str) and len(item) >= 4},
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(value, REDACTION_MARKER)
    redacted = _PRIVATE_KEY.sub(REDACTION_MARKER, redacted)
    redacted = _AUTHORIZATION.sub(REDACTION_MARKER, redacted)
    redacted = _KNOWN_TOKEN.sub(REDACTION_MARKER, redacted)
    return _JWT.sub(REDACTION_MARKER, redacted)


def truncate_text_head_tail(text: str, *, limit: int) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if len(text) <= limit:
        return text
    marker_budget = len(TRUNCATION_MARKER)
    if limit <= marker_budget + 2:
        return text[:limit]
    available = limit - marker_budget
    head = available // 2
    tail = available - head
    return text[:head] + TRUNCATION_MARKER + text[-tail:]


__all__ = (
    "REDACTION_MARKER",
    "TRUNCATION_MARKER",
    "redact_text",
    "sensitive_environment_values",
    "truncate_text_head_tail",
)
