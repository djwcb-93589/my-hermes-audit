"""Resolve the effective MyHermes model once for execution and Audit identity."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass

from myhermes_audit.contracts import ModelIdentifierSource
from myhermes_audit.errors import ConfigBuildError


_MAX_RUNTIME_MODEL_LENGTH = 4_096
_MAX_RECORDED_MODEL_LENGTH = 256
_ENDPOINT_MATERIAL = re.compile(r"https?://\S+", re.IGNORECASE)
_CREDENTIAL_MATERIAL = re.compile(
    r"(?:"
    r"\bBearer\s+\S{12,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bAIza[A-Za-z0-9_-]{20,}"
    r"|\b(?:gh[opusr]|xox[aboprs])[-_][A-Za-z0-9_-]{16,}"
    r"|\b(?:api[_-]?key|access[_-]?token)\s*[:=]\s*\S{8,}"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EffectiveModelIdentifier:
    model_identifier: str
    source: ModelIdentifierSource
    worker_model_value: str | None


def _normalized_candidate(value: object, *, source: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigBuildError(
            "effective model configuration must be a string",
            model_source=source,
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_RUNTIME_MODEL_LENGTH:
        raise ConfigBuildError(
            "effective model identifier exceeds the safe length limit",
            model_source=source,
        )
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ConfigBuildError(
            "effective model identifier contains control characters",
            model_source=source,
        )
    return normalized


def _safe_recorded_identifier(
    value: str,
    *,
    sensitive_values: Iterable[str],
) -> str:
    contains_sensitive_material = any(
        secret and len(secret) >= 4 and secret in value
        for secret in sensitive_values
    )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if (
        contains_sensitive_material
        or _ENDPOINT_MATERIAL.search(value) is not None
        or _CREDENTIAL_MATERIAL.search(value) is not None
    ):
        return f"model-sha256:{digest}"
    if len(value) <= _MAX_RECORDED_MODEL_LENGTH:
        return value
    suffix = f"...#sha256:{digest}"
    return value[: _MAX_RECORDED_MODEL_LENGTH - len(suffix)] + suffix


def resolve_effective_model_identifier(
    *,
    case_environment: Mapping[str, object],
    parent_environment: Mapping[str, object],
    subject_configuration: Mapping[str, object],
    sensitive_values: Iterable[str] = (),
) -> EffectiveModelIdentifier:
    """Apply the exact Worker precedence while keeping the recorded value safe."""

    candidates = (
        (
            ModelIdentifierSource.CASE_ENVIRONMENT_OVERRIDE,
            case_environment.get("MODEL"),
        ),
        (
            ModelIdentifierSource.PARENT_ENVIRONMENT,
            parent_environment.get("MODEL"),
        ),
        (
            ModelIdentifierSource.SUBJECT_CONFIGURATION,
            subject_configuration.get("model"),
        ),
    )
    for source, candidate in candidates:
        normalized = _normalized_candidate(candidate, source=source.value)
        if normalized is None:
            continue
        return EffectiveModelIdentifier(
            model_identifier=_safe_recorded_identifier(
                normalized,
                sensitive_values=sensitive_values,
            ),
            source=source,
            worker_model_value=normalized,
        )
    return EffectiveModelIdentifier(
        model_identifier="subject-default",
        source=ModelIdentifierSource.SUBJECT_DEFAULT,
        worker_model_value=None,
    )


def apply_effective_model_to_worker_environment(
    environment: MutableMapping[str, str],
    resolution: EffectiveModelIdentifier,
) -> None:
    """Make the Worker environment obey the same non-empty precedence decision."""

    if resolution.source in {
        ModelIdentifierSource.CASE_ENVIRONMENT_OVERRIDE,
        ModelIdentifierSource.PARENT_ENVIRONMENT,
    }:
        if resolution.worker_model_value is None:
            raise ConfigBuildError("environment model resolution lost its runtime value")
        environment["MODEL"] = resolution.worker_model_value
    else:
        environment.pop("MODEL", None)


__all__ = (
    "EffectiveModelIdentifier",
    "apply_effective_model_to_worker_environment",
    "resolve_effective_model_identifier",
)
