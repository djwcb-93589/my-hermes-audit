"""Subject-neutral validator context, evidence, and path safety."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from myhermes_audit.contracts import (
    AblationPlan,
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
    ContextDiagnostic,
    EffectiveSubjectConfiguration,
    FactContextObservation,
    MemoryOperationError,
    MemoryQueryResult,
    MemoryStateChange,
    MemoryStateSnapshot,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
    TurnResult,
)
from myhermes_audit.contracts.common import validate_relative_path
from myhermes_audit.errors import UnsafePathError, ValidatorError


EVALUATOR_VERSION = "p1.0"


@dataclass(frozen=True, slots=True)
class ToolTraceEntry:
    tool_call_id: str
    tool_name: str
    status: str
    success: bool
    error_type: str | None
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ValidationContext:
    workspace: Path
    hermes_home: Path
    final_output: str | None
    tool_calls: tuple[ToolTraceEntry, ...] | None = None
    tool_trace_complete: bool = True
    memory_query_results: tuple[MemoryQueryResult, ...] = ()
    memory_snapshots: tuple[MemoryStateSnapshot, ...] = ()
    memory_state_changes: tuple[MemoryStateChange, ...] = ()
    memory_errors: tuple[MemoryOperationError, ...] = ()
    turns: tuple[TurnResult, ...] = ()
    effective_subject_configuration: EffectiveSubjectConfiguration | None = None
    ablation_plan: AblationPlan | None = None
    context_diagnostics: tuple[ContextDiagnostic, ...] = ()
    fact_context_observations: tuple[FactContextObservation, ...] = ()
    variant_id: str | None = None
    background_review_results: tuple[BackgroundReviewExecutionResult, ...] = ()
    background_review_errors: tuple[BackgroundReviewExecutionError, ...] = ()


def resolve_validation_path(context: ValidationContext, declared: str) -> Path:
    try:
        normalized = validate_relative_path(
            declared,
            allowed_roots=frozenset({"workspace", "hermes_home"}),
        )
    except ValueError as exc:
        raise UnsafePathError(declared, reason=str(exc)) from exc
    parts = normalized.split("/")
    root = context.workspace if parts[0] == "workspace" else context.hermes_home
    resolved_root = root.resolve(strict=True)
    current = root
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(
                declared,
                reason="validator paths cannot traverse symbolic links",
            )
    candidate = current.resolve(strict=False)
    if not candidate.is_relative_to(resolved_root):
        raise UnsafePathError(declared, reason="validator path escaped its root")
    return candidate


def evidence(
    *,
    kind: str,
    description: str,
    relative_path: str | None = None,
    metadata: dict | None = None,
) -> MetricEvidence:
    return MetricEvidence(
        evidence_id=f"evidence-{uuid.uuid4().hex}",
        kind=kind,
        description=description,
        relative_path=relative_path,
        metadata=dict(metadata or {}),
    )


def metric(
    *,
    name: str,
    source: MetricSource,
    passed: bool,
    reason: str,
    evidence_items: list[MetricEvidence] | None = None,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=source,
        value=passed,
        passed=passed,
        reason=reason,
        evidence=list(evidence_items or []),
        evaluator_version=EVALUATOR_VERSION,
    )


def validator_error_metric(
    *,
    name: str,
    source: MetricSource,
    error: Exception,
) -> MetricResult:
    error_type = type(error).__name__
    return MetricResult(
        metric_name=name,
        source=source,
        status=MetricStatus.ERROR,
        value=None,
        passed=None,
        reason=f"evaluator error: {error_type}",
        evidence=[
            evidence(
                kind="validator_error",
                description=f"validator_error={error_type}",
            )
        ],
        evaluator_version=EVALUATOR_VERSION,
        error=MetricError(
            error_type="validator_error",
            message=f"validator raised {error_type}",
            retryable=False,
            details={"exception_type": error_type},
        ),
    )


def require_text_output(context: ValidationContext) -> str:
    if context.final_output is None:
        raise ValidatorError("final output is unavailable")
    return context.final_output


__all__ = (
    "EVALUATOR_VERSION",
    "ToolTraceEntry",
    "ValidationContext",
    "evidence",
    "metric",
    "require_text_output",
    "resolve_validation_path",
    "validator_error_metric",
)
