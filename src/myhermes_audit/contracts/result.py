"""Trial、Metric、Artifact 与 Audit 汇总结果合同。"""

from __future__ import annotations

import math
from enum import Enum

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    SafeRelativePath,
    SchemaVersion,
    Sha256Digest,
    UtcDatetime,
)
from myhermes_audit.contracts.fingerprint import AuditFingerprint, SubjectFingerprint


class TrialStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ENVIRONMENT_ERROR = "environment_error"


class MetricSource(str, Enum):
    DETERMINISTIC = "deterministic"
    RUNTIME = "runtime"
    JUDGE = "judge"
    RETRIEVAL = "retrieval"
    COMPRESSION = "compression"
    BACKGROUND_REVIEW = "background_review"


class MetricEvidence(ContractModel):
    evidence_id: Identifier
    kind: NonEmptyText
    description: NonEmptyText
    artifact_id: Identifier | None = None
    relative_path: SafeRelativePath | None = None
    metadata: JsonObject = Field(default_factory=dict)


class MetricResult(ContractModel):
    metric_name: NonEmptyText
    source: MetricSource
    value: JsonValue
    passed: StrictBool | None = None
    reason: StrictStr | None = None
    evidence: list[MetricEvidence] = Field(default_factory=list)
    evaluator_version: NonEmptyText


class ArtifactRef(ContractModel):
    artifact_id: Identifier
    kind: NonEmptyText
    relative_path: SafeRelativePath
    sha256: Sha256Digest
    size_bytes: NonNegativeInt


class TrialError(ContractModel):
    error_type: Identifier
    message: NonEmptyText
    retryable: StrictBool = False
    details: JsonObject = Field(default_factory=dict)


class TrialWarning(ContractModel):
    warning_type: Identifier
    message: NonEmptyText
    details: JsonObject = Field(default_factory=dict)


class TurnResult(ContractModel):
    turn_number: PositiveInt
    user_message: NonEmptyText
    final_output: StrictStr | None = None
    runtime_status: NonEmptyText
    error_type: Identifier | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime
    duration_ms: NonNegativeInt
    run_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_turn_result(self) -> "TurnResult":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")
        return self


class TrialRuntimeSummary(ContractModel):
    iterations: NonNegativeInt = 0
    tool_batches: NonNegativeInt = 0
    tool_call_count: NonNegativeInt = 0
    tool_names: list[NonEmptyText] = Field(default_factory=list)
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_runtime_summary(self) -> "TrialRuntimeSummary":
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("tool_names must be unique in first-seen order")
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


class TrialResult(ContractModel):
    trial_id: Identifier
    run_id: Identifier
    case_id: Identifier
    trial_number: NonNegativeInt
    status: TrialStatus
    passed: StrictBool | None = None
    final_output: StrictStr | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt | None = None
    turns: list[TurnResult] = Field(default_factory=list)
    runtime: TrialRuntimeSummary | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[TrialWarning] = Field(default_factory=list)
    error: TrialError | None = None

    @model_validator(mode="after")
    def validate_trial_result(self) -> "TrialResult":
        if self.trial_number < 1:
            raise ValueError("trial_number must start at 1")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot be before started_at")
        if self.status is TrialStatus.COMPLETED and self.error is not None:
            raise ValueError("completed trials must not contain error")
        if self.status in {
            TrialStatus.FAILED,
            TrialStatus.ENVIRONMENT_ERROR,
        } and self.error is None:
            raise ValueError("failed and environment_error trials require error")
        if self.status in {TrialStatus.PENDING, TrialStatus.RUNNING}:
            if (
                self.error is not None
                or self.final_output is not None
                or self.passed is not None
            ):
                raise ValueError(
                    "pending and running trials cannot contain terminal results"
                )
        if self.status is not TrialStatus.COMPLETED and self.passed is True:
            raise ValueError("only completed trials can pass")
        if self.status is TrialStatus.TIMEOUT:
            if self.error is None or self.error.error_type != "timeout":
                raise ValueError("timeout trials require a stable timeout error")
            if self.final_output is not None:
                raise ValueError("timeout trials cannot contain final_output")
        if self.status is TrialStatus.CANCELLED:
            if self.error is None or self.error.error_type != "cancelled":
                raise ValueError("cancelled trials require a cancelled error")
            if self.final_output is not None or any(
                metric.passed is True for metric in self.metrics
            ):
                raise ValueError(
                    "cancelled trials cannot contain successful run semantics"
                )
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact_id must be unique within a TrialResult")
        artifact_paths = [artifact.relative_path for artifact in self.artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("artifact paths must be unique within a TrialResult")
        metric_names = [metric.metric_name for metric in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("metric names must be unique within a TrialResult")
        turn_numbers = [turn.turn_number for turn in self.turns]
        if turn_numbers != list(range(1, len(turn_numbers) + 1)):
            raise ValueError("turn numbers must be contiguous from 1")
        return self


class MetricSummary(ContractModel):
    metric_name: NonEmptyText
    sample_count: NonNegativeInt
    passed_count: NonNegativeInt
    mean: StrictInt | StrictFloat | None = None
    minimum: StrictInt | StrictFloat | None = None
    maximum: StrictInt | StrictFloat | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> "MetricSummary":
        if self.passed_count > self.sample_count:
            raise ValueError("passed_count cannot exceed sample_count")
        for value in (self.mean, self.minimum, self.maximum):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("metric summary values must be finite")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum cannot exceed maximum")
        return self


class CaseAggregate(ContractModel):
    case_id: Identifier
    trial_count: PositiveInt
    passed_count: NonNegativeInt
    pass_rate: StrictFloat = Field(ge=0, le=1)
    metric_summaries: list[MetricSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "CaseAggregate":
        if self.passed_count > self.trial_count:
            raise ValueError("passed_count cannot exceed trial_count")
        if not math.isfinite(self.pass_rate):
            raise ValueError("pass_rate must be finite")
        expected_rate = self.passed_count / self.trial_count
        if not math.isclose(
            self.pass_rate,
            expected_rate,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("pass_rate must equal passed_count / trial_count")
        metric_names = [item.metric_name for item in self.metric_summaries]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("metric_summaries must have unique metric_name values")
        return self


class AuditSummary(ContractModel):
    case_count: NonNegativeInt
    trial_count: NonNegativeInt
    passed_count: NonNegativeInt
    pass_rate: StrictFloat = Field(ge=0, le=1)
    tool_correctness_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    duration_p50_ms: NonNegativeInt | None = None
    duration_p95_ms: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditSummary":
        if self.passed_count > self.trial_count:
            raise ValueError("passed_count cannot exceed trial_count")
        if not math.isfinite(self.pass_rate):
            raise ValueError("pass_rate must be finite")
        expected_rate = (
            self.passed_count / self.trial_count
            if self.trial_count
            else 0.0
        )
        if not math.isclose(
            self.pass_rate,
            expected_rate,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("pass_rate must equal passed_count / trial_count")
        if self.case_count == 0 and self.trial_count != 0:
            raise ValueError("trials require at least one case")
        return self


class AuditRunResult(ContractModel):
    schema_version: SchemaVersion = Field(
        description="Required top-level Audit result schema version."
    )
    run_id: Identifier
    suite_id: Identifier
    subject_fingerprint: SubjectFingerprint
    audit_fingerprint: AuditFingerprint
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    trials: list[TrialResult] = Field(default_factory=list)
    cases: list[CaseAggregate] = Field(default_factory=list)
    summary: AuditSummary

    @model_validator(mode="after")
    def validate_run_result(self) -> "AuditRunResult":
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case aggregates must have unique case_id values")
        trial_ids = [trial.trial_id for trial in self.trials]
        if len(trial_ids) != len(set(trial_ids)):
            raise ValueError("trial_id must be unique within an AuditRunResult")
        run_ids = [trial.run_id for trial in self.trials]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("Trial run_id must be unique within an AuditRunResult")
        if self.summary.case_count != len(self.cases):
            raise ValueError("summary.case_count must match case aggregates")
        if self.summary.trial_count != len(self.trials):
            raise ValueError("summary.trial_count must match trials")
        passed_count = sum(trial.passed is True for trial in self.trials)
        if self.summary.passed_count != passed_count:
            raise ValueError("summary.passed_count must match passed trials")
        aggregate_by_id = {case.case_id: case for case in self.cases}
        if any(trial.case_id not in aggregate_by_id for trial in self.trials):
            raise ValueError("every trial must belong to a case aggregate")
        for case_id, aggregate in aggregate_by_id.items():
            case_trials = [trial for trial in self.trials if trial.case_id == case_id]
            if aggregate.trial_count != len(case_trials):
                raise ValueError("case trial_count must match its trials")
            case_passed = sum(trial.passed is True for trial in case_trials)
            if aggregate.passed_count != case_passed:
                raise ValueError("case passed_count must match its trials")
        return self
