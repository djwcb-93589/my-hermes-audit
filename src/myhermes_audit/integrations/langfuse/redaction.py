"""Data-classification-aware projections for Langfuse content fields."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from myhermes_audit.contracts import DataClassification
from myhermes_audit.errors import ContentRedactionError
from myhermes_audit.security import redact_text, truncate_text_head_tail
from myhermes_audit.serialization import canonical_json, canonical_sha256


_SYNTHETIC_TEXT_LIMIT = 8_000
_INTERNAL_TEXT_LIMIT = 2_000


def project_remote_content(
    value: Any,
    *,
    classification: DataClassification,
    no_content: bool,
    sensitive_values: Iterable[str],
) -> Any:
    try:
        sanitized = _sanitize_value(
            value,
            text_limit=(
                _SYNTHETIC_TEXT_LIMIT
                if classification is DataClassification.SYNTHETIC
                else _INTERNAL_TEXT_LIMIT
            ),
            sensitive_values=sensitive_values,
        )
        if no_content or classification is DataClassification.SENSITIVE:
            serialized = canonical_json(sanitized)
            return {
                "content_omitted": True,
                "data_classification": classification.value,
                "sha256": canonical_sha256(sanitized),
                "serialized_length": len(serialized),
                "serialized_size_bytes": len(serialized.encode("utf-8")),
            }
        return sanitized
    except Exception as exc:
        if isinstance(exc, ContentRedactionError):
            raise
        raise ContentRedactionError(
            "content could not be projected for remote publication",
            exception_type=type(exc).__name__,
        ) from exc


def _sanitize_value(
    value: Any,
    *,
    text_limit: int,
    sensitive_values: Iterable[str],
) -> Any:
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return truncate_text_head_tail(
            redact_text(value, sensitive_values),
            limit=text_limit,
        )
    if isinstance(value, list):
        return [
            _sanitize_value(
                item,
                text_limit=text_limit,
                sensitive_values=sensitive_values,
            )
            for item in value
        ]
    if isinstance(value, dict):
        sanitized_items: dict[str, Any] = {}
        for key, item in value.items():
            sanitized_key = truncate_text_head_tail(
                redact_text(str(key), sensitive_values),
                limit=200,
            )
            if sanitized_key in sanitized_items:
                raise ContentRedactionError(
                    "remote content keys collide after redaction"
                )
            sanitized_items[sanitized_key] = _sanitize_value(
                item,
                text_limit=text_limit,
                sensitive_values=sensitive_values,
            )
        return sanitized_items
    raise ContentRedactionError(
        "remote content contains a non-JSON value",
        value_type=type(value).__name__,
    )


__all__ = ("project_remote_content",)
