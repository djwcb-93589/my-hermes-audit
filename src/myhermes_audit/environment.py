"""Central environment allowlists for isolated MyHermes workers."""

from __future__ import annotations

import re
from collections.abc import Mapping


SUITE_ENVIRONMENT_ALLOWLIST = frozenset({
    "MODEL",
    "MAX_ITERATIONS",
    "MODEL_TIMEOUT_SECONDS",
    "MODEL_MAX_OUTPUT_TOKENS",
})

WORKER_INHERITED_ENVIRONMENT_ALLOWLIST = frozenset({
    "ALL_PROXY",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "OPENSSL_CONF",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TZ",
    "USERPROFILE",
    "WINDIR",
})

MODEL_ENVIRONMENT_ALLOWLIST = frozenset({
    "FALLBACK_API_KEY",
    "FALLBACK_BASE_URL",
    "FALLBACK_MAX_OUTPUT_TOKENS",
    "FALLBACK_MODEL",
    "MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
})

AUDIT_OWNED_ENVIRONMENT_NAMES = frozenset({
    "DB_PATH",
    "HERMES_HOME",
    "HERMES_WORKSPACE",
    "MYHERMES_AUDIT_ARTIFACTS_DIR",
    "MYHERMES_AUDIT_TRIAL_ID",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "PYTHONSAFEPATH",
})

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_NAME_PARTS = (
    "API_KEY",
    "TOKEN",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
    "PROXY",
)
_NON_SECRET_TOKEN_COUNT_NAMES = frozenset({
    "FALLBACK_MAX_OUTPUT_TOKENS",
    "MODEL_MAX_OUTPUT_TOKENS",
})


def is_sensitive_environment_name(name: str) -> bool:
    upper = name.upper()
    if upper in _NON_SECRET_TOKEN_COUNT_NAMES:
        return False
    return any(part in upper for part in _SENSITIVE_NAME_PARTS)


def validate_suite_environment_overrides(
    value: Mapping[str, str],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not _ENVIRONMENT_NAME.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        upper = key.upper()
        if upper in normalized:
            raise ValueError(
                f"environment override names collide case-insensitively: {key!r}"
            )
        if upper in AUDIT_OWNED_ENVIRONMENT_NAMES:
            raise ValueError(f"environment override {key!r} is owned by Audit")
        if is_sensitive_environment_name(upper):
            raise ValueError(f"sensitive environment override is forbidden: {key!r}")
        if upper not in SUITE_ENVIRONMENT_ALLOWLIST:
            allowed = ", ".join(sorted(SUITE_ENVIRONMENT_ALLOWLIST))
            raise ValueError(
                f"environment override {key!r} is outside the allowlist: {allowed}"
            )
        normalized[upper] = item
    return normalized


__all__ = (
    "AUDIT_OWNED_ENVIRONMENT_NAMES",
    "MODEL_ENVIRONMENT_ALLOWLIST",
    "SUITE_ENVIRONMENT_ALLOWLIST",
    "WORKER_INHERITED_ENVIRONMENT_ALLOWLIST",
    "is_sensitive_environment_name",
    "validate_suite_environment_overrides",
)
