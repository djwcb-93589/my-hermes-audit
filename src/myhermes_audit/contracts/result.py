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


class TrialResult(ContractModel):
    trial_id: Identifier
    run_id: Identifier
    case_id: Identifier
    trial_number: NonNegativeInt
    status: TrialStatus
    final_output: StrictStr | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt | None = None
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
            if self.error is not None or self.final_output is not None:
                raise ValueError(
                    "pending and running trials cannot contain terminal results"
                )
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
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditSummary":
        if self.passed_count > self.trial_count:
            raise ValueError("passed_count cannot exceed trial_count")
        if not math.isfinite(self.pass_rate):
            raise ValueError("pass_rate must be finite")
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
        return self
