"""Read-only capability checks for the supported public Langfuse SDK surface."""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import re
from typing import Any

from myhermes_audit.contracts import ExperimentStrategy, LangfuseCapabilityReport
from myhermes_audit.errors import (
    LangfuseCapabilityError,
    LangfuseDependencyError,
    UnsupportedLangfuseVersionError,
)


LANGFUSE_MINIMUM_VERSION = "4.7.0"
LANGFUSE_MAXIMUM_MAJOR = 5
LANGFUSE_VERSION_RANGE = f">={LANGFUSE_MINIMUM_VERSION},<{LANGFUSE_MAXIMUM_MAJOR}"
SCORE_IDEMPOTENCY_STRATEGY = (
    "stable_score_id_submission_with_uncertain_remote_confirmation"
)
_MINIMUM_VERSION_PARTS = (4, 7, 0)
_FINAL_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:\.post\d+)?(?:\+[A-Za-z0-9.-]+)?$"
)


def probe_langfuse_capabilities() -> LangfuseCapabilityReport:
    """Inspect documented class methods without creating a client or remote resource."""

    try:
        version = importlib.metadata.version("langfuse")
    except importlib.metadata.PackageNotFoundError:
        capabilities = _capability_flags(None, None)
        return LangfuseCapabilityReport(
            installed=False,
            version=None,
            compatible=False,
            required_minimum_version=LANGFUSE_MINIMUM_VERSION,
            capabilities=capabilities,
            missing_capabilities=sorted(capabilities),
            warnings=[
                "Langfuse SDK is not installed; local Audit commands remain available."
            ],
            experiment_strategy=ExperimentStrategy.RUNNER_REPLAY,
            score_idempotency_strategy=SCORE_IDEMPOTENCY_STRATEGY,
            score_submission_supported=False,
            score_confirmation_supported=False,
        )

    try:
        langfuse = importlib.import_module("langfuse")
    except Exception as exc:
        capabilities = _capability_flags(None, None)
        return LangfuseCapabilityReport(
            installed=True,
            version=version,
            compatible=False,
            required_minimum_version=LANGFUSE_MINIMUM_VERSION,
            capabilities=capabilities,
            missing_capabilities=sorted(capabilities),
            warnings=[
                "Installed Langfuse SDK cannot be imported: "
                f"{type(exc).__name__}."
            ],
            experiment_strategy=ExperimentStrategy.RUNNER_REPLAY,
            score_idempotency_strategy=SCORE_IDEMPOTENCY_STRATEGY,
            score_submission_supported=False,
            score_confirmation_supported=False,
        )

    client_type = getattr(langfuse, "Langfuse", None)
    capabilities = _capability_flags(client_type, langfuse)
    version_parts = _final_version_parts(version)
    version_compatible = (
        version_parts is not None
        and version_parts >= _MINIMUM_VERSION_PARTS
        and version_parts[0] < LANGFUSE_MAXIMUM_MAJOR
    )
    missing = sorted(name for name, available in capabilities.items() if not available)
    warnings: list[str] = []
    if version_parts is None:
        warnings.append("Installed Langfuse SDK version is not a final semantic release.")
    elif not version_compatible:
        warnings.append(
            f"Installed Langfuse SDK {version} is outside {LANGFUSE_VERSION_RANGE}."
        )
    if missing:
        warnings.append("Required public Langfuse SDK capabilities are missing.")
    warnings.append(
        "Experiment publication uses the official runner with a local-result replay task."
    )
    score_submission_supported = capabilities.get(
        "score_submission_supported",
        False,
    )
    if score_submission_supported:
        warnings.append(
            "The supported high-level SDK can submit Scores but does not provide a "
            "public remote-confirmation method; submitted Scores remain uncertain."
        )
    else:
        warnings.append(
            "The installed SDK does not expose the required public Score submission "
            "method."
        )
    return LangfuseCapabilityReport(
        installed=True,
        version=version,
        compatible=version_compatible and not missing,
        required_minimum_version=LANGFUSE_MINIMUM_VERSION,
        capabilities=capabilities,
        missing_capabilities=missing,
        warnings=warnings,
        experiment_strategy=ExperimentStrategy.RUNNER_REPLAY,
        score_idempotency_strategy=SCORE_IDEMPOTENCY_STRATEGY,
        score_submission_supported=score_submission_supported,
        score_confirmation_supported=False,
    )


def require_langfuse_capabilities() -> LangfuseCapabilityReport:
    """Return a compatible report or fail before any client-side remote write."""

    report = probe_langfuse_capabilities()
    if not report.installed:
        raise LangfuseDependencyError(
            "Langfuse SDK is unavailable; install my-hermes-audit[langfuse]",
            required_version=LANGFUSE_VERSION_RANGE,
        )
    version_parts = _final_version_parts(report.version)
    if (
        version_parts is None
        or version_parts < _MINIMUM_VERSION_PARTS
        or version_parts[0] >= LANGFUSE_MAXIMUM_MAJOR
    ):
        raise UnsupportedLangfuseVersionError(
            f"Langfuse SDK {report.version or 'unknown'} is unsupported; "
            f"required range is {LANGFUSE_VERSION_RANGE}",
            installed_version=report.version,
            required_version=LANGFUSE_VERSION_RANGE,
        )
    if report.missing_capabilities:
        raise LangfuseCapabilityError(
            "installed Langfuse SDK lacks required public capabilities",
            installed_version=report.version,
            missing_capabilities=list(report.missing_capabilities),
        )
    return report


def _capability_flags(client_type: Any, langfuse_module: Any) -> dict[str, bool]:
    span_type = getattr(langfuse_module, "LangfuseSpan", None)
    propagate_attributes = getattr(langfuse_module, "propagate_attributes", None)
    not_found_error = getattr(
        getattr(langfuse_module, "api", None),
        "NotFoundError",
        None,
    )
    return {
        "client_initialization": _has_callable_parameters(
            client_type,
            {"public_key", "secret_key", "base_url", "timeout", "sample_rate"},
        ),
        "dataset_read": _has_method(client_type, "get_dataset", {"name"}),
        "dataset_create": _has_method(client_type, "create_dataset", {"name"}),
        "dataset_not_found_mapping": (
            isinstance(not_found_error, type)
            and issubclass(not_found_error, BaseException)
        ),
        "dataset_item_upsert": _has_method(
            client_type,
            "create_dataset_item",
            {"dataset_name", "id", "input", "expected_output", "metadata"},
        ),
        "experiment_runner": _has_method(
            client_type,
            "run_experiment",
            {
                "name",
                "run_name",
                "description",
                "data",
                "task",
                "evaluators",
                "run_evaluators",
                "max_concurrency",
                "metadata",
            },
        ),
        "experiment_item_association": _has_method(
            client_type,
            "run_experiment",
            {"run_name", "data", "task"},
        ),
        "trace_observation": all(
            _has_method(client_type, name, parameters)
            for name, parameters in (
                (
                    "start_as_current_observation",
                    {"name", "as_type", "input", "output", "metadata", "version"},
                ),
                ("get_current_trace_id", set()),
                ("get_current_observation_id", set()),
            )
        ),
        "child_observation": all(
            (
                _has_method(
                    span_type,
                    "start_observation",
                    {
                        "name",
                        "as_type",
                        "input",
                        "output",
                        "metadata",
                        "model",
                        "usage_details",
                        "version",
                    },
                ),
                _has_method(span_type, "end", set()),
            )
        ),
        "attribute_propagation": _has_callable_parameters(
            propagate_attributes,
            {"session_id", "metadata", "tags", "trace_name"},
        ),
        "score_submission_supported": _has_method(
            client_type,
            "create_score",
            {
                "name",
                "value",
                "dataset_run_id",
                "trace_id",
                "score_id",
                "data_type",
                "comment",
                "metadata",
                "timestamp",
            },
        ),
        "flush": _has_method(client_type, "flush", set()),
        "shutdown": _has_method(client_type, "shutdown", set()),
        "authentication_check": _has_method(client_type, "auth_check", set()),
    }


def _has_method(
    client_type: Any,
    name: str,
    required_parameters: set[str],
) -> bool:
    if client_type is None:
        return False
    method = getattr(client_type, name, None)
    if not callable(method):
        return False
    try:
        parameters = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return False
    return required_parameters <= parameters


def _has_callable_parameters(
    candidate: Any,
    required_parameters: set[str],
) -> bool:
    if not callable(candidate):
        return False
    try:
        parameters = set(inspect.signature(candidate).parameters)
    except (TypeError, ValueError):
        return False
    return required_parameters <= parameters


def _final_version_parts(version: str | None) -> tuple[int, int, int] | None:
    if version is None:
        return None
    match = _FINAL_VERSION_RE.fullmatch(version)
    if match is None:
        return None
    return tuple(
        int(match.group(name)) for name in ("major", "minor", "patch")
    )


__all__ = (
    "LANGFUSE_MAXIMUM_MAJOR",
    "LANGFUSE_MINIMUM_VERSION",
    "LANGFUSE_VERSION_RANGE",
    "SCORE_IDEMPOTENCY_STRATEGY",
    "probe_langfuse_capabilities",
    "require_langfuse_capabilities",
)
