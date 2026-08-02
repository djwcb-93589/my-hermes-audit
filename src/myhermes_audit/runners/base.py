"""Subject-neutral Trial runner port consumed by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence

from myhermes_audit.contracts import (
    AblationVariant,
    AuditCase,
    MemoryOperationError,
    MemoryQueryResult,
    MemoryStateChange,
    MemoryStateSnapshot,
    CompressionEvent,
    ContextDiagnostic,
    EffectiveSubjectConfiguration,
    FactContextObservation,
    TrialObservationSummary,
    TrialRuntimeSummary,
    TrialWarning,
    TurnResult,
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
)
from myhermes_audit.sandbox import AuditSandbox
from myhermes_audit.validators.base import ToolTraceEntry


class RunnerStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ENVIRONMENT_ERROR = "environment_error"


@dataclass(frozen=True, slots=True)
class TrialRunnerOutcome:
    status: RunnerStatus
    runtime_status: str | None
    duration_ms: int
    final_output: str | None
    turns: tuple[TurnResult, ...]
    runtime: TrialRuntimeSummary | None
    observations: TrialObservationSummary | None
    tool_calls: tuple[ToolTraceEntry, ...] | None
    tool_trace_complete: bool
    artifact_paths: dict[str, Path] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool = False
    warnings: tuple[TrialWarning, ...] = ()
    memory_query_results: tuple[MemoryQueryResult, ...] = ()
    memory_snapshots: tuple[MemoryStateSnapshot, ...] = ()
    memory_state_changes: tuple[MemoryStateChange, ...] = ()
    memory_errors: tuple[MemoryOperationError, ...] = ()
    variant_id: str | None = None
    effective_subject_configuration: EffectiveSubjectConfiguration | None = None
    compression_events: tuple[CompressionEvent, ...] = ()
    context_diagnostics: tuple[ContextDiagnostic, ...] = ()
    fact_context_observations: tuple[FactContextObservation, ...] = ()
    background_review_results: tuple[BackgroundReviewExecutionResult, ...] = ()
    background_review_errors: tuple[BackgroundReviewExecutionError, ...] = ()
    review_gate_passed: bool | None = None


class TrialRunnerPort(Protocol):
    def preflight(self, cases: Sequence[AuditCase]) -> None:
        """Reject unsupported cases before any Trial is started."""

    def run_trial(
        self,
        case: AuditCase,
        sandbox: AuditSandbox,
        *,
        trial_id: str,
        timeout_seconds: int,
        variant: AblationVariant | None = None,
    ) -> TrialRunnerOutcome:
        """Execute one Trial in its already-created Sandbox."""


__all__ = (
    "RunnerStatus",
    "ToolTraceEntry",
    "TrialRunnerOutcome",
    "TrialRunnerPort",
)
