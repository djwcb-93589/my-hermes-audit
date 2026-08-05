"""Trial、Metric、Artifact 与 Audit 汇总结果合同。"""

from __future__ import annotations

import math
from decimal import Decimal
from enum import Enum
from typing import Literal

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
    Sha256Digest,
    UtcDatetime,
)
from myhermes_audit.contracts.cost import (
    DeepSeekCostAggregate,
    DeepSeekCostStatus,
    DeepSeekCostSummary,
)
from myhermes_audit.contracts.ablation import (
    AblationComparisonResult,
    CheckpointResult,
    CompressionEvent,
    CompressionEventStatus,
    CompressionMode,
    ContextDiagnostic,
    DistortionResult,
    DurationDiagnostics,
    EffectiveSubjectConfiguration,
    FactContextObservation,
    FactRetentionResult,
    MemoryMode,
    RequiredFactLossResult,
    TokenDiagnostics,
    TrialIdentity,
)
from myhermes_audit.contracts.fingerprint import AuditFingerprint, SubjectFingerprint
from myhermes_audit.contracts.judge import JudgeResult, JudgeRunSummary
from myhermes_audit.contracts.langfuse import (
    LangfuseExperimentIdentity,
    LangfusePublishError,
    LangfusePublishResult,
    LangfusePublishStatus,
)
from myhermes_audit.contracts.memory import (
    MemoryOperationError,
    MemoryQueryResult,
    MemorySnapshotPhase,
    MemoryStateChange,
    MemoryStateSnapshot,
)
from myhermes_audit.contracts.background_review import (
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
)
from myhermes_audit.contracts.scenario import (
    E2EScenarioKind,
    ProcessScenarioExecutionResult,
    ScenarioError,
    ScenarioExecutionResult,
    ToolchainScenarioExecutionResult,
)
from myhermes_audit.serialization import canonical_sha256


RESULT_SCHEMA_VERSION = "1.4"
ResultSchemaVersion = Literal["1.4"]


class TrialStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ENVIRONMENT_ERROR = "environment_error"


class LocalExecutionStatus(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"


class MetricSource(str, Enum):
    DETERMINISTIC = "deterministic"
    RUNTIME = "runtime"
    JUDGE = "judge"
    RETRIEVAL = "retrieval"
    COMPRESSION = "compression"
    BACKGROUND_REVIEW = "background_review"


class MetricStatus(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"


class DeepSeekCacheStatus(str, Enum):
    """Availability of the exact DeepSeek cache token observation."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    NOT_EVALUATED = "not_evaluated"
    INVALID = "invalid"


class DeepSeekCacheSummary(ContractModel):
    """Strict, token-weighted cache facts for a Trial collection."""

    status: DeepSeekCacheStatus
    model_call_count: NonNegativeInt
    evaluated_model_call_count: NonNegativeInt
    trial_count: NonNegativeInt
    model_call_trial_count: NonNegativeInt
    evaluated_trial_count: NonNegativeInt
    prompt_cache_hit_tokens: NonNegativeInt | None = None
    prompt_cache_miss_tokens: NonNegativeInt | None = None
    deepseek_cache_evaluated_prompt_tokens: NonNegativeInt | None = None
    cache_hit_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    model_call_coverage_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    trial_coverage_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    invalid_trial_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_cache_summary(self) -> "DeepSeekCacheSummary":
        if self.evaluated_model_call_count > self.model_call_count:
            raise ValueError("evaluated model calls cannot exceed model calls")
        if self.evaluated_trial_count > self.trial_count:
            raise ValueError("evaluated trials cannot exceed trial count")
        if self.model_call_trial_count > self.trial_count:
            raise ValueError("model-call trials cannot exceed trial count")
        if self.evaluated_trial_count > self.model_call_trial_count:
            raise ValueError("evaluated trials cannot exceed model-call trials")
        if self.invalid_trial_count > self.trial_count:
            raise ValueError("invalid trials cannot exceed trial count")
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("cache token totals must be paired")
        if (self.prompt_cache_hit_tokens is None) != (
            self.deepseek_cache_evaluated_prompt_tokens is None
        ):
            raise ValueError("cache totals and evaluated prompt tokens must be paired")
        if self.status is DeepSeekCacheStatus.INVALID:
            if self.invalid_trial_count == 0:
                raise ValueError("invalid cache summary requires invalid trials")
            if any(
                value is not None
                for value in (
                    self.prompt_cache_hit_tokens,
                    self.prompt_cache_miss_tokens,
                    self.deepseek_cache_evaluated_prompt_tokens,
                    self.cache_hit_rate,
                )
            ):
                raise ValueError("invalid cache summaries cannot expose totals")
        elif self.status in (
            DeepSeekCacheStatus.AVAILABLE,
            DeepSeekCacheStatus.PARTIAL,
        ) and (
            self.prompt_cache_hit_tokens is None
            or self.prompt_cache_miss_tokens is None
            or self.deepseek_cache_evaluated_prompt_tokens is None
        ):
            raise ValueError("evaluated cache summaries require cache totals")
        elif self.prompt_cache_hit_tokens is not None:
            total = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
            if total != self.deepseek_cache_evaluated_prompt_tokens:
                raise ValueError("cache totals must equal evaluated prompt tokens")
            expected = (
                None
                if self.deepseek_cache_evaluated_prompt_tokens == 0
                else self.prompt_cache_hit_tokens
                / self.deepseek_cache_evaluated_prompt_tokens
            )
            if self.cache_hit_rate != expected:
                raise ValueError("cache_hit_rate must match token totals")
        elif self.cache_hit_rate is not None:
            raise ValueError("cache_hit_rate requires cache token totals")
        if self.status is DeepSeekCacheStatus.NOT_EVALUATED and self.evaluated_model_call_count:
            raise ValueError("not evaluated cache cannot have evaluated calls")
        if self.status is DeepSeekCacheStatus.NOT_EVALUATED:
            if self.invalid_trial_count != 0:
                raise ValueError("not evaluated cache cannot have invalid trials")
            if any(
                value is not None
                for value in (
                    self.prompt_cache_hit_tokens,
                    self.prompt_cache_miss_tokens,
                    self.deepseek_cache_evaluated_prompt_tokens,
                    self.cache_hit_rate,
                )
            ):
                raise ValueError("not evaluated cache cannot expose totals")
        if self.status is DeepSeekCacheStatus.AVAILABLE and (
            self.model_call_count == 0
            or self.evaluated_model_call_count != self.model_call_count
            or self.model_call_trial_count != self.trial_count
            or self.evaluated_trial_count != self.model_call_trial_count
        ):
            raise ValueError("available cache requires every model call")
        if self.status is DeepSeekCacheStatus.PARTIAL and not self.evaluated_model_call_count:
            raise ValueError("partial cache requires evaluated calls")
        if self.status is DeepSeekCacheStatus.PARTIAL and (
            self.evaluated_model_call_count == self.model_call_count
            and self.evaluated_trial_count == self.model_call_trial_count
            and self.model_call_trial_count == self.trial_count
        ):
            raise ValueError("partial cache requires a partial or unevaluated Trial")
        if self.invalid_trial_count > 0 and self.status is not DeepSeekCacheStatus.INVALID:
            raise ValueError("invalid trials require invalid cache status")
        if self.status is not DeepSeekCacheStatus.INVALID and self.invalid_trial_count != 0:
            raise ValueError("non-invalid cache summaries cannot contain invalid trials")
        if self.model_call_trial_count == 0 and self.trial_coverage_rate is not None:
            raise ValueError("trial coverage requires model-call trials")
        if self.model_call_trial_count > 0:
            expected_coverage = self.evaluated_trial_count / self.model_call_trial_count
            if self.trial_coverage_rate != expected_coverage:
                raise ValueError("trial coverage must use model-call trials")
        elif self.trial_coverage_rate is not None:
            raise ValueError("trial coverage must be unavailable without model-call trials")
        if self.model_call_count == 0:
            if self.model_call_coverage_rate is not None:
                raise ValueError("model coverage must be unavailable without model calls")
        else:
            expected_model_coverage = (
                self.evaluated_model_call_count / self.model_call_count
            )
            if self.model_call_coverage_rate != expected_model_coverage:
                raise ValueError("model coverage must match evaluated calls")
        for rate in (self.model_call_coverage_rate, self.trial_coverage_rate):
            if rate is not None and not math.isfinite(rate):
                raise ValueError("cache coverage rates must be finite")
        return self


class MetricError(ContractModel):
    error_type: Identifier
    message: NonEmptyText
    retryable: StrictBool = False
    details: JsonObject = Field(default_factory=dict)


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
    status: MetricStatus = MetricStatus.COMPLETED
    value: JsonValue | None = None
    passed: StrictBool | None = None
    reason: StrictStr | None = None
    evidence: list[MetricEvidence] = Field(default_factory=list)
    evaluator_version: NonEmptyText
    error: MetricError | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metric_result(self) -> "MetricResult":
        if self.status is MetricStatus.COMPLETED:
            if self.error is not None:
                raise ValueError("completed metrics cannot contain error")
            if self.value is None:
                raise ValueError("completed metrics require value")
            return self
        if self.value is not None:
            raise ValueError("non-completed metrics cannot contain value")
        if self.passed is not None:
            raise ValueError("non-completed metrics must set passed to null")
        if self.status is MetricStatus.ERROR:
            if self.error is None:
                raise ValueError("error metrics require structured error")
        elif self.error is not None:
            raise ValueError("skipped and not_applicable metrics cannot contain error")
        return self


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
    session_id: Identifier | None = None
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
    subject_model: NonEmptyText | None = None
    iterations: NonNegativeInt = 0
    tool_batches: NonNegativeInt = 0
    tool_call_count: NonNegativeInt = 0
    model_call_count: NonNegativeInt = 0
    tool_names: list[NonEmptyText] = Field(default_factory=list)
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    prompt_cache_hit_tokens: NonNegativeInt | None = None
    prompt_cache_miss_tokens: NonNegativeInt | None = None
    deepseek_cache_evaluated_prompt_tokens: NonNegativeInt | None = None
    deepseek_cache_hit_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    deepseek_cache_status: DeepSeekCacheStatus = DeepSeekCacheStatus.NOT_EVALUATED
    deepseek_cache_evaluated_model_call_count: NonNegativeInt = 0
    deepseek_cache_invalid_model_call_count: NonNegativeInt = 0

    @property
    def cache_evaluated_model_call_count(self) -> int:
        """Compatibility alias without creating a second serialized fact."""

        return self.deepseek_cache_evaluated_model_call_count

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
        if self.deepseek_cache_evaluated_model_call_count > self.model_call_count:
            raise ValueError("evaluated model calls cannot exceed model_call_count")
        if self.deepseek_cache_invalid_model_call_count > self.model_call_count:
            raise ValueError("invalid cache calls cannot exceed model_call_count")
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("cache token totals must be paired")
        if self.deepseek_cache_status is DeepSeekCacheStatus.INVALID:
            if self.deepseek_cache_invalid_model_call_count == 0:
                raise ValueError("invalid cache runtime requires invalid calls")
            if any(
                value is not None
                for value in (
                    self.prompt_cache_hit_tokens,
                    self.prompt_cache_miss_tokens,
                    self.deepseek_cache_evaluated_prompt_tokens,
                    self.deepseek_cache_hit_rate,
                )
            ):
                raise ValueError("invalid cache runtime cannot expose totals")
        elif self.prompt_cache_hit_tokens is not None:
            if self.prompt_tokens is None:
                raise ValueError("cache runtime requires prompt_tokens")
            if self.deepseek_cache_evaluated_prompt_tokens is None:
                raise ValueError("cache runtime requires evaluated prompt tokens")
            if (
                self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
                != self.deepseek_cache_evaluated_prompt_tokens
            ):
                raise ValueError("cache runtime totals must equal evaluated prompt tokens")
            if (
                self.deepseek_cache_status is DeepSeekCacheStatus.AVAILABLE
                and self.deepseek_cache_evaluated_prompt_tokens != self.prompt_tokens
            ):
                raise ValueError("available cache must cover all prompt tokens")
            if self.deepseek_cache_evaluated_prompt_tokens > self.prompt_tokens:
                raise ValueError("evaluated prompt tokens cannot exceed prompt tokens")
            expected = (
                None
                if self.deepseek_cache_evaluated_prompt_tokens == 0
                else self.prompt_cache_hit_tokens
                / self.deepseek_cache_evaluated_prompt_tokens
            )
            if self.deepseek_cache_hit_rate != expected:
                raise ValueError("cache rate must match runtime totals")
        elif self.deepseek_cache_hit_rate is not None:
            raise ValueError("cache rate requires cache totals")
        if (
            self.deepseek_cache_status is DeepSeekCacheStatus.NOT_EVALUATED
            and self.deepseek_cache_evaluated_model_call_count != 0
        ):
            raise ValueError("not evaluated cache cannot have evaluated calls")
        if (
            self.deepseek_cache_status in (
                DeepSeekCacheStatus.AVAILABLE,
                DeepSeekCacheStatus.PARTIAL,
            )
            and self.deepseek_cache_evaluated_model_call_count == 0
        ):
            raise ValueError("evaluated cache status requires evaluated calls")
        if self.deepseek_cache_status is DeepSeekCacheStatus.PARTIAL and (
            self.deepseek_cache_evaluated_model_call_count >= self.model_call_count
        ):
            raise ValueError("partial cache requires unevaluated model calls")
        if self.deepseek_cache_status is DeepSeekCacheStatus.NOT_EVALUATED:
            if self.deepseek_cache_invalid_model_call_count != 0:
                raise ValueError("not evaluated cache cannot have invalid calls")
            if any(
                value is not None
                for value in (
                    self.prompt_cache_hit_tokens,
                    self.prompt_cache_miss_tokens,
                    self.deepseek_cache_evaluated_prompt_tokens,
                    self.deepseek_cache_hit_rate,
                )
            ):
                raise ValueError("not evaluated cache cannot expose totals")
        elif self.deepseek_cache_status is not DeepSeekCacheStatus.INVALID:
            if self.deepseek_cache_invalid_model_call_count != 0:
                raise ValueError("non-invalid cache cannot contain invalid calls")
            if (
                self.prompt_cache_hit_tokens is None
                or self.prompt_cache_miss_tokens is None
                or self.deepseek_cache_evaluated_prompt_tokens is None
            ):
                raise ValueError("evaluated cache status requires cache totals")
        if self.deepseek_cache_invalid_model_call_count > 0 and (
            self.deepseek_cache_status is not DeepSeekCacheStatus.INVALID
        ):
            raise ValueError("invalid calls require invalid cache status")
        if (
            self.deepseek_cache_status is DeepSeekCacheStatus.AVAILABLE
            and self.deepseek_cache_evaluated_model_call_count != self.model_call_count
        ):
            raise ValueError("available cache requires every model call")
        return self


class RunObservationSummary(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    status: NonEmptyText
    stop_reason: NonEmptyText
    iterations: NonNegativeInt
    tool_call_count: NonNegativeInt
    has_final_reply: StrictBool
    duration_ms: NonNegativeInt | None = None


class ModelObservationSummary(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    finish_reason: StrictStr | None = None
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    prompt_cache_hit_tokens: NonNegativeInt | None = None
    prompt_cache_miss_tokens: NonNegativeInt | None = None
    duration_ms: NonNegativeInt
    tool_call_count: NonNegativeInt
    error_category: Identifier | None = None

    @model_validator(mode="after")
    def validate_tokens(self) -> "ModelObservationSummary":
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("model total_tokens must equal prompt plus completion")
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("cache observation fields must be paired")
        if self.prompt_cache_hit_tokens is not None:
            if self.prompt_tokens is None:
                raise ValueError("cache observations require prompt_tokens")
            if (
                self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
                != self.prompt_tokens
            ):
                raise ValueError("cache observations must sum to prompt_tokens")
        return self


class ToolObservationSummary(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    tool_call_id: Identifier
    tool_name: NonEmptyText
    status: NonEmptyText
    success: StrictBool
    error_type: Identifier | None = None
    duration_ms: NonNegativeInt


class TrialObservationSummary(ContractModel):
    worker_protocol_version: NonEmptyText
    runs: list[RunObservationSummary] = Field(default_factory=list)
    model_calls: list[ModelObservationSummary] = Field(default_factory=list)
    tool_calls: list[ToolObservationSummary] = Field(default_factory=list)
    truncated: StrictBool = False
    cache_invalid_model_call_count: NonNegativeInt = 0
    deepseek_cache_evaluated_prompt_tokens: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_observations(self) -> "TrialObservationSummary":
        run_ids = [item.run_id for item in self.runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run observations must have unique run_id values")
        tool_call_ids = [item.tool_call_id for item in self.tool_calls]
        if len(tool_call_ids) != len(set(tool_call_ids)):
            raise ValueError("tool observations must have unique tool_call_id values")
        if self.cache_invalid_model_call_count > len(self.model_calls):
            raise ValueError("invalid cache model calls cannot exceed observations")
        evaluated_prompt_tokens = sum(
            item.prompt_tokens
            for item in self.model_calls
            if item.prompt_cache_hit_tokens is not None
            and item.prompt_cache_miss_tokens is not None
            and item.prompt_tokens is not None
        )
        has_valid_cache_observation = any(
            item.prompt_cache_hit_tokens is not None
            and item.prompt_cache_miss_tokens is not None
            and item.prompt_tokens is not None
            for item in self.model_calls
        )
        if self.cache_invalid_model_call_count:
            if self.deepseek_cache_evaluated_prompt_tokens is not None:
                raise ValueError(
                    "invalid cache observations cannot expose evaluated prompt tokens"
                )
        elif has_valid_cache_observation:
            if self.deepseek_cache_evaluated_prompt_tokens is None:
                raise ValueError(
                    "cache observations require evaluated prompt tokens"
                )
            if self.deepseek_cache_evaluated_prompt_tokens != evaluated_prompt_tokens:
                raise ValueError(
                    "observation evaluated prompt tokens must match model calls"
                )
        elif self.deepseek_cache_evaluated_prompt_tokens is not None:
            raise ValueError(
                "unevaluated observations cannot expose evaluated prompt tokens"
            )
        return self


class TrialResult(ContractModel):
    trial_id: Identifier
    run_id: Identifier
    case_id: Identifier
    trial_number: NonNegativeInt
    trial_identity: TrialIdentity | None = None
    variant_id: Identifier | None = None
    memory_mode: MemoryMode | None = None
    compression_mode: CompressionMode | None = None
    configuration_fingerprint: Sha256Digest | None = None
    comparison_basis_fingerprint: Sha256Digest | None = None
    scenario_fingerprint: Sha256Digest | None = None
    effective_subject_configuration: EffectiveSubjectConfiguration | None = None
    status: TrialStatus
    task_passed: StrictBool | None = None
    passed: StrictBool | None = None
    final_output: StrictStr | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt | None = None
    turns: list[TurnResult] = Field(default_factory=list)
    runtime: TrialRuntimeSummary | None = None
    deepseek_cost: DeepSeekCostSummary | None = None
    deepseek_cache_evaluated_prompt_tokens: NonNegativeInt | None = None
    observations: TrialObservationSummary | None = None
    memory_query_results: list[MemoryQueryResult] = Field(default_factory=list)
    memory_snapshots: list[MemoryStateSnapshot] = Field(default_factory=list)
    memory_state_changes: list[MemoryStateChange] = Field(default_factory=list)
    memory_errors: list[MemoryOperationError] = Field(default_factory=list)
    compression_events: list[CompressionEvent] = Field(default_factory=list)
    context_diagnostics: list[ContextDiagnostic] = Field(default_factory=list)
    fact_context_observations: list[FactContextObservation] = Field(
        default_factory=list
    )
    checkpoint_results: list[CheckpointResult] = Field(default_factory=list)
    fact_retention_results: list[FactRetentionResult] = Field(default_factory=list)
    required_fact_loss: RequiredFactLossResult | None = None
    distortion_results: list[DistortionResult] = Field(default_factory=list)
    token_diagnostics: TokenDiagnostics | None = None
    duration_diagnostics: DurationDiagnostics | None = None
    retrieval_gate_passed: StrictBool | None = None
    final_answer_gate_passed: StrictBool | None = None
    memory_state_gate_passed: StrictBool | None = None
    required_fact_gate_passed: StrictBool | None = None
    background_review_results: list[BackgroundReviewExecutionResult] = Field(
        default_factory=list
    )
    background_review_errors: list[BackgroundReviewExecutionError] = Field(
        default_factory=list
    )
    review_gate_passed: StrictBool | None = None
    scenario_results: list[ScenarioExecutionResult] = Field(default_factory=list)
    process_errors: list[ScenarioError] = Field(default_factory=list)
    toolchain_gate_passed: StrictBool | None = None
    process_gate_passed: StrictBool | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    judge_result: JudgeResult | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[TrialWarning] = Field(default_factory=list)
    error: TrialError | None = None

    @property
    def toolchain_scenario_results(self) -> list[ToolchainScenarioExecutionResult]:
        """Return Toolchain facts without duplicating the canonical result list."""

        return [
            item
            for item in self.scenario_results
            if item.kind is E2EScenarioKind.TOOLCHAIN
        ]

    @property
    def process_scenario_results(self) -> list[ProcessScenarioExecutionResult]:
        """Return Process facts without creating a second source of truth."""

        return [
            item
            for item in self.scenario_results
            if item.kind is E2EScenarioKind.PROCESS_BACKGROUND
        ]

    @property
    def evaluated_prompt_tokens(self) -> int | None:
        """Return the canonical Trial cache prompt projection."""

        return self.deepseek_cache_evaluated_prompt_tokens

    @model_validator(mode="after")
    def validate_trial_result(self) -> "TrialResult":
        if self.trial_number < 1:
            raise ValueError("trial_number must start at 1")
        if self.runtime is None:
            if self.deepseek_cache_evaluated_prompt_tokens is not None:
                raise ValueError(
                    "cache evaluated prompt tokens require runtime summary"
                )
        elif (
            self.deepseek_cache_evaluated_prompt_tokens
            != self.runtime.deepseek_cache_evaluated_prompt_tokens
        ):
            raise ValueError(
                "Trial cache evaluated prompt tokens must mirror runtime summary"
            )
        if (
            self.observations is not None
            and self.observations.deepseek_cache_evaluated_prompt_tokens
            != self.deepseek_cache_evaluated_prompt_tokens
        ):
            raise ValueError(
                "Trial cache evaluated prompt tokens must mirror observations"
            )
        p4_identity_fields = (
            self.trial_identity,
            self.variant_id,
            self.memory_mode,
            self.compression_mode,
            self.configuration_fingerprint,
            self.comparison_basis_fingerprint,
            self.effective_subject_configuration,
        )
        p4_result_present = any(
            (
                self.compression_events,
                self.context_diagnostics,
                self.fact_context_observations,
                self.checkpoint_results,
                self.fact_retention_results,
                self.required_fact_loss is not None,
                self.distortion_results,
                self.token_diagnostics is not None,
                self.duration_diagnostics is not None,
                self.required_fact_gate_passed is not None,
            )
        )
        if all(item is None for item in p4_identity_fields):
            if p4_result_present:
                raise ValueError("P4 Trial facts require a complete Variant identity")
        elif any(item is None for item in p4_identity_fields):
            raise ValueError("P4 Trial identity fields must be all present")
        else:
            assert self.trial_identity is not None
            assert self.variant_id is not None
            assert self.memory_mode is not None
            assert self.compression_mode is not None
            assert self.configuration_fingerprint is not None
            assert self.effective_subject_configuration is not None
            if (
                self.trial_identity.case_id != self.case_id
                or self.trial_identity.variant_id != self.variant_id
                or self.trial_identity.trial_ordinal != self.trial_number
                or self.trial_identity.configuration_sha256
                != self.configuration_fingerprint
                or self.trial_identity.model_identifier
                != self.effective_subject_configuration.model_identifier
            ):
                raise ValueError("Trial identity must match its P4 Trial fields")
            if (
                self.effective_subject_configuration.memory_mode
                is not self.memory_mode
                or self.effective_subject_configuration.requested_compression_mode
                is not self.compression_mode
            ):
                raise ValueError("effective Subject configuration must match Variant")
            if (
                self.runtime is not None
                and self.runtime.subject_model
                != self.trial_identity.model_identifier
            ):
                raise ValueError(
                    "runtime model identifier must match the P4 Trial identity"
                )
            if (
                len(self.compression_events)
                > self.effective_subject_configuration.maximum_compression_events
            ):
                raise ValueError("compression event count exceeds the declared limit")
            if (
                self.status is TrialStatus.COMPLETED
                and self.effective_subject_configuration.compression_events_observable
                and sum(
                    item.status is CompressionEventStatus.COMPLETED
                    for item in self.compression_events
                )
                < self.effective_subject_configuration.minimum_compression_events
            ):
                raise ValueError(
                    "completed Trial did not observe the required Compression events"
                )
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
                or self.task_passed is not None
            ):
                raise ValueError(
                    "pending and running trials cannot contain terminal results"
                )
        if self.status is not TrialStatus.COMPLETED and self.passed is True:
            raise ValueError("only completed trials can pass")
        if self.status is not TrialStatus.COMPLETED and self.task_passed is True:
            raise ValueError("only completed trials can have task_passed=true")
        if self.retrieval_gate_passed is False and self.task_passed is True:
            raise ValueError("failed retrieval gate cannot have task_passed=true")
        if self.memory_state_gate_passed is False and self.task_passed is True:
            raise ValueError("failed Memory state gate cannot have task_passed=true")
        if self.final_answer_gate_passed is False and self.task_passed is True:
            raise ValueError("failed final-answer gate cannot have task_passed=true")
        if self.required_fact_gate_passed is False and self.task_passed is True:
            raise ValueError("failed required-fact gate cannot have task_passed=true")
        if self.review_gate_passed is False and self.task_passed is True:
            raise ValueError("failed Review gate cannot have task_passed=true")
        if self.process_gate_passed is False and self.task_passed is True:
            raise ValueError("failed Process gate cannot have task_passed=true")
        if self.toolchain_gate_passed is False and self.task_passed is True:
            raise ValueError("failed Toolchain gate cannot have task_passed=true")
        if self.task_passed is False and self.passed is True:
            raise ValueError("failed task cannot have passed=true")
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
        review_ids = [item.review_id for item in self.background_review_results]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("Background Review result IDs must be unique")
        scenario_ids = [item.scenario_id for item in self.scenario_results]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("scenario result IDs must be unique")
        if self.scenario_results and self.scenario_fingerprint is None:
            raise ValueError("scenario results require a scenario fingerprint")
        process_ids = [
            item.scenario_id
            for item in self.scenario_results
            if item.kind is E2EScenarioKind.PROCESS_BACKGROUND
        ]
        if self.process_gate_passed is not None and not process_ids:
            raise ValueError("process_gate_passed requires Process scenario results")
        toolchain_ids = [
            item.scenario_id
            for item in self.scenario_results
            if item.kind is E2EScenarioKind.TOOLCHAIN
        ]
        if self.toolchain_gate_passed is not None and not toolchain_ids:
            raise ValueError(
                "toolchain_gate_passed requires Toolchain scenario results"
            )
        review_snapshot_ids = [
            snapshot.snapshot_id
            for item in self.background_review_results
            for snapshot in (item.before_snapshot, item.after_snapshot)
            if snapshot is not None
        ]
        if len(review_snapshot_ids) != len(set(review_snapshot_ids)):
            raise ValueError("Background Review snapshot IDs must be unique")
        observed_review_targets = [
            (item.review_id, change.target_type, change.target_id)
            for item in self.background_review_results
            for change in item.observed_changes
        ]
        if len(observed_review_targets) != len(set(observed_review_targets)):
            raise ValueError(
                "Background Review observed targets must be unique within a Review"
            )
        turn_numbers = [turn.turn_number for turn in self.turns]
        if turn_numbers != list(range(1, len(turn_numbers) + 1)):
            raise ValueError("turn numbers must be contiguous from 1")
        query_ids = [item.query_id for item in self.memory_query_results]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("memory_query_results must have unique query_id values")
        snapshot_ids = [item.snapshot_id for item in self.memory_snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("memory_snapshots must have unique snapshot_id values")
        snapshot_phases = [item.phase for item in self.memory_snapshots]
        if len(snapshot_phases) != len(set(snapshot_phases)):
            raise ValueError("memory_snapshots must have unique phase values")
        if any(
            item.phase is None
            or item.strategy is None
            or item.provider is None
            for item in self.memory_snapshots
        ):
            raise ValueError(
                "TrialResult Memory snapshots require phase, strategy, and provider"
            )
        query_strategies = {item.strategy for item in self.memory_query_results}
        query_providers = {item.provider for item in self.memory_query_results}
        snapshot_strategies = {
            item.strategy for item in self.memory_snapshots
        }
        snapshot_providers = {item.provider for item in self.memory_snapshots}
        if (
            len(query_strategies) > 1
            or len(query_providers) > 1
            or len(snapshot_strategies) > 1
            or len(snapshot_providers) > 1
        ):
            raise ValueError("TrialResult Memory facts use inconsistent semantics")
        if (
            query_strategies
            and snapshot_strategies
            and snapshot_strategies != query_strategies
        ):
            raise ValueError("Memory query and snapshot strategies must agree")
        if self.memory_state_changes and set(snapshot_phases) != {
            MemorySnapshotPhase.BEFORE_CONVERSATION,
            MemorySnapshotPhase.AFTER_CONVERSATION,
        }:
            raise ValueError("Memory state changes require before/after snapshots")
        change_ids = [item.change_id for item in self.memory_state_changes]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("memory_state_changes must have unique change_id values")
        changed_memory_ids = [
            item.memory_id for item in self.memory_state_changes
        ]
        if len(changed_memory_ids) != len(set(changed_memory_ids)):
            raise ValueError("memory_state_changes must have unique memory_id values")
        compression_event_ids = [item.event_id for item in self.compression_events]
        if len(compression_event_ids) != len(set(compression_event_ids)):
            raise ValueError("compression_events must have unique event_id values")
        context_keys = [
            (item.session_id, item.turn_index) for item in self.context_diagnostics
        ]
        if len(context_keys) != len(set(context_keys)):
            raise ValueError("context diagnostics must have unique session/turn values")
        fact_observation_keys = [
            (item.fact_id, item.checkpoint_id)
            for item in self.fact_context_observations
        ]
        if len(fact_observation_keys) != len(set(fact_observation_keys)):
            raise ValueError("fact context observations must be unique")
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoint_results]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("checkpoint_results must have unique checkpoint_id values")
        retention_ids = [item.fact_id for item in self.fact_retention_results]
        if len(retention_ids) != len(set(retention_ids)):
            raise ValueError("fact_retention_results must have unique fact_id values")
        distortion_keys = [
            (item.fact_id, item.distortion_type) for item in self.distortion_results
        ]
        if len(distortion_keys) != len(set(distortion_keys)):
            raise ValueError("distortion_results must be unique by fact and type")
        if self.duration_diagnostics is not None and (
            self.duration_ms is not None
            and self.duration_diagnostics.trial_duration_ms != self.duration_ms
        ):
            raise ValueError("duration diagnostics must match Trial duration")
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
    agent_iterations_mean: StrictFloat | None = Field(default=None, ge=0)
    duration_mean_ms: StrictFloat | None = Field(default=None, ge=0)
    total_tokens_mean_per_trial: StrictFloat | None = Field(default=None, ge=0)
    tool_call_count_mean: StrictFloat | None = Field(default=None, ge=0)
    deepseek_cache_status: DeepSeekCacheStatus = DeepSeekCacheStatus.NOT_EVALUATED
    deepseek_cache_hit_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    deepseek_cache_hit_tokens: NonNegativeInt | None = None
    deepseek_cache_miss_tokens: NonNegativeInt | None = None
    deepseek_cache_evaluated_prompt_tokens: NonNegativeInt | None = None
    deepseek_cache_model_call_coverage_rate: StrictFloat | None = Field(
        default=None, ge=0, le=1
    )
    deepseek_cache_trial_coverage_rate: StrictFloat | None = Field(
        default=None, ge=0, le=1
    )
    deepseek_cache: DeepSeekCacheSummary | None = None
    deepseek_cost: DeepSeekCostAggregate | None = None
    deepseek_cost_status: DeepSeekCostStatus = DeepSeekCostStatus.NOT_EVALUATED
    deepseek_cost_mean_per_evaluated_trial_usd: Decimal | None = None
    deepseek_cost_mean_per_successful_trial_usd: Decimal | None = None
    deepseek_cost_effective_cost_per_success_usd: Decimal | None = None
    deepseek_cost_cache_savings_usd: Decimal | None = None
    deepseek_cost_coverage_rate: Decimal | None = None

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
        if self.deepseek_cache is not None:
            if self.deepseek_cache_status is not self.deepseek_cache.status:
                raise ValueError("Case cache status must mirror DeepSeek summary")
            if self.deepseek_cache_hit_rate != self.deepseek_cache.cache_hit_rate:
                raise ValueError("Case cache rate must mirror DeepSeek summary")
            if (
                self.deepseek_cache_hit_tokens
                != self.deepseek_cache.prompt_cache_hit_tokens
            ):
                raise ValueError("Case cache hit tokens must mirror DeepSeek summary")
            if (
                self.deepseek_cache_miss_tokens
                != self.deepseek_cache.prompt_cache_miss_tokens
            ):
                raise ValueError("Case cache miss tokens must mirror DeepSeek summary")
            if (
                self.deepseek_cache_evaluated_prompt_tokens
                != self.deepseek_cache.deepseek_cache_evaluated_prompt_tokens
            ):
                raise ValueError(
                    "Case evaluated prompt tokens must mirror DeepSeek summary"
                )
            if (
                self.deepseek_cache_model_call_coverage_rate
                != self.deepseek_cache.model_call_coverage_rate
            ):
                raise ValueError("Case model coverage must mirror DeepSeek summary")
            if (
                self.deepseek_cache_trial_coverage_rate
                != self.deepseek_cache.trial_coverage_rate
            ):
                raise ValueError("Case trial coverage must mirror DeepSeek summary")
        if self.deepseek_cost is not None:
            if self.deepseek_cost.trial_count != self.trial_count:
                raise ValueError("Case cost trial count must mirror Case aggregate")
            if self.deepseek_cost_status is not self.deepseek_cost.status:
                raise ValueError("Case cost status must mirror DeepSeek aggregate")
            if self.deepseek_cost_mean_per_evaluated_trial_usd != self.deepseek_cost.mean_cost_per_evaluated_trial_usd:
                raise ValueError("Case evaluated cost mean must mirror DeepSeek aggregate")
            if self.deepseek_cost_mean_per_successful_trial_usd != self.deepseek_cost.mean_cost_per_successful_trial_usd:
                raise ValueError("Case successful cost mean must mirror DeepSeek aggregate")
            if self.deepseek_cost_effective_cost_per_success_usd != self.deepseek_cost.effective_cost_per_success_usd:
                raise ValueError("Case effective cost must mirror DeepSeek aggregate")
            if self.deepseek_cost_cache_savings_usd != self.deepseek_cost.cache_savings_usd:
                raise ValueError("Case cost savings must mirror DeepSeek aggregate")
            if self.deepseek_cost_coverage_rate != self.deepseek_cost.cost_coverage_rate:
                raise ValueError("Case cost coverage must mirror DeepSeek aggregate")
        elif self.deepseek_cost_status is not DeepSeekCostStatus.NOT_EVALUATED or any(
            value is not None
            for value in (
                self.deepseek_cost_mean_per_evaluated_trial_usd,
                self.deepseek_cost_mean_per_successful_trial_usd,
                self.deepseek_cost_effective_cost_per_success_usd,
                self.deepseek_cost_cache_savings_usd,
                self.deepseek_cost_coverage_rate,
            )
        ):
            raise ValueError("Case cost projections require DeepSeek aggregate")
        return self


class AuditSummary(ContractModel):
    case_count: NonNegativeInt
    trial_count: NonNegativeInt
    passed_count: NonNegativeInt
    pass_rate: StrictFloat = Field(ge=0, le=1)
    task_success_sample_count: NonNegativeInt = 0
    task_success_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    tool_correctness_sample_count: NonNegativeInt = 0
    tool_correctness_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    memory_required_evidence_sample_count: NonNegativeInt = 0
    memory_required_evidence_hit_rate: StrictFloat | None = Field(
        default=None, ge=0, le=1
    )
    memory_recall_at_k_sample_count: NonNegativeInt = 0
    memory_recall_at_k_mean: StrictFloat | None = Field(default=None, ge=0, le=1)
    memory_mrr_sample_count: NonNegativeInt = 0
    memory_mrr_mean: StrictFloat | None = Field(default=None, ge=0, le=1)
    background_review_decision_sample_count: NonNegativeInt = 0
    background_review_decision_accuracy: StrictFloat | None = Field(
        default=None, ge=0, le=1
    )
    conversation_turn_count_total: NonNegativeInt = 0
    conversation_turn_count_mean: StrictFloat | None = Field(default=None, ge=0)
    agent_iterations_total: NonNegativeInt = 0
    agent_iterations_mean: StrictFloat | None = Field(default=None, ge=0)
    agent_iterations_p50: NonNegativeInt | None = None
    agent_iterations_p95: NonNegativeInt | None = None
    duration_mean_ms: StrictFloat | None = Field(default=None, ge=0)
    duration_sample_count: NonNegativeInt = 0
    duration_p50_ms: NonNegativeInt | None = None
    duration_p95_ms: NonNegativeInt | None = None
    prompt_tokens_total: NonNegativeInt | None = None
    prompt_tokens_mean_per_trial: StrictFloat | None = Field(default=None, ge=0)
    prompt_tokens_sample_count: NonNegativeInt = 0
    completion_tokens_total: NonNegativeInt | None = None
    completion_tokens_mean_per_trial: StrictFloat | None = Field(default=None, ge=0)
    completion_tokens_sample_count: NonNegativeInt = 0
    total_tokens: NonNegativeInt | None = None
    total_tokens_mean_per_trial: StrictFloat | None = Field(default=None, ge=0)
    total_tokens_mean_per_success: StrictFloat | None = Field(default=None, ge=0)
    total_tokens_sample_count: NonNegativeInt = 0
    total_tokens_success_sample_count: NonNegativeInt = 0
    tool_call_count_total: NonNegativeInt = 0
    tool_call_count_mean: StrictFloat | None = Field(default=None, ge=0)
    tool_call_count_p50: NonNegativeInt | None = None
    tool_call_count_p95: NonNegativeInt | None = None
    tool_call_count_mean_per_success: StrictFloat | None = Field(default=None, ge=0)
    tool_call_sample_count: NonNegativeInt = 0
    tool_call_success_sample_count: NonNegativeInt = 0
    failure_count: NonNegativeInt = 0
    failure_rate: StrictFloat = Field(default=0.0, ge=0, le=1)
    timeout_count: NonNegativeInt = 0
    timeout_rate: StrictFloat = Field(default=0.0, ge=0, le=1)
    environment_error_count: NonNegativeInt = 0
    cancelled_count: NonNegativeInt = 0
    deepseek_cache: DeepSeekCacheSummary | None = None
    deepseek_cost: DeepSeekCostAggregate | None = None
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
        if self.task_success_sample_count == 0 and self.task_success_rate is not None:
            raise ValueError("task success rate requires samples")
        if self.tool_correctness_sample_count == 0 and self.tool_correctness_rate is not None:
            raise ValueError("tool correctness rate requires samples")
        for rate_name, sample_count in (
            ("memory_required_evidence_hit_rate", self.memory_required_evidence_sample_count),
            ("memory_recall_at_k_mean", self.memory_recall_at_k_sample_count),
            ("memory_mrr_mean", self.memory_mrr_sample_count),
            ("background_review_decision_accuracy", self.background_review_decision_sample_count),
            ("conversation_turn_count_mean", self.trial_count),
            ("agent_iterations_mean", self.trial_count),
            ("duration_mean_ms", self.duration_sample_count),
            ("prompt_tokens_mean_per_trial", self.prompt_tokens_sample_count),
            ("completion_tokens_mean_per_trial", self.completion_tokens_sample_count),
            ("total_tokens_mean_per_trial", self.total_tokens_sample_count),
            ("total_tokens_mean_per_success", self.total_tokens_success_sample_count),
            ("tool_call_count_mean", self.tool_call_sample_count),
            ("tool_call_count_mean_per_success", self.tool_call_success_sample_count),
        ):
            if getattr(self, rate_name) is not None and sample_count == 0:
                raise ValueError(f"{rate_name} requires samples")
        if self.failure_count > self.trial_count:
            raise ValueError("failure_count cannot exceed trial_count")
        if self.timeout_count > self.trial_count:
            raise ValueError("timeout_count cannot exceed trial_count")
        if self.environment_error_count > self.trial_count:
            raise ValueError("environment_error_count cannot exceed trial_count")
        if self.cancelled_count > self.trial_count:
            raise ValueError("cancelled_count cannot exceed trial_count")
        if self.deepseek_cost is not None and self.deepseek_cost.trial_count != self.trial_count:
            raise ValueError("Summary cost trial count must mirror summary trials")
        return self


class AuditRunResult(ContractModel):
    schema_version: ResultSchemaVersion = Field(
        description="Required top-level Audit result schema version."
    )
    run_id: Identifier
    suite_id: Identifier
    subject_fingerprint: SubjectFingerprint
    audit_fingerprint: AuditFingerprint
    deepseek_pricing_fingerprint: Sha256Digest | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    trials: list[TrialResult] = Field(default_factory=list)
    cases: list[CaseAggregate] = Field(default_factory=list)
    summary: AuditSummary
    judge_summary: JudgeRunSummary = Field(default_factory=JudgeRunSummary)
    ablation_comparisons: list[AblationComparisonResult] = Field(
        default_factory=list
    )
    local_execution_status: LocalExecutionStatus | None = None
    remote_publication_status: LangfusePublishStatus | None = None
    experiment_identity: LangfuseExperimentIdentity | None = None
    langfuse_publish_result: LangfusePublishResult | None = None
    integration_errors: list[LangfusePublishError] = Field(default_factory=list)

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
        comparison_case_ids = [
            item.case_id for item in self.ablation_comparisons
        ]
        if len(comparison_case_ids) != len(set(comparison_case_ids)):
            raise ValueError("ablation comparisons must have unique case_id values")
        if any(case_id not in set(case_ids) for case_id in comparison_case_ids):
            raise ValueError("ablation comparisons require a matching case aggregate")
        p4_case_ids = {
            trial.case_id for trial in self.trials if trial.variant_id is not None
        }
        subject_identity_sha256 = canonical_sha256(
            {
                "git_commit": self.subject_fingerprint.git_commit,
                "tree_hash": self.subject_fingerprint.tree_hash,
                "dirty": self.subject_fingerprint.dirty,
                "python_requirement": self.subject_fingerprint.python_requirement,
            }
        )
        for trial in self.trials:
            if trial.trial_identity is None:
                continue
            if (
                trial.trial_identity.suite_sha256
                != self.audit_fingerprint.suite_sha256
                or trial.trial_identity.subject_commit
                != self.subject_fingerprint.git_commit
                or trial.trial_identity.subject_fingerprint_sha256
                != subject_identity_sha256
            ):
                raise ValueError(
                    "P4 Trial identity must match Audit Suite and Subject fingerprints"
                )
        cost_fingerprints = {
            trial.deepseek_cost.pricing_fingerprint
            for trial in self.trials
            if trial.deepseek_cost is not None
            and trial.deepseek_cost.pricing_fingerprint is not None
        }
        aggregate_cost_fingerprints = {
            aggregate.deepseek_cost.pricing_fingerprint
            for aggregate in [*self.cases, self.summary]
            if aggregate.deepseek_cost is not None
            and aggregate.deepseek_cost.pricing_fingerprint is not None
        }
        all_cost_fingerprints = cost_fingerprints | aggregate_cost_fingerprints
        if len(all_cost_fingerprints) > 1:
            raise ValueError("Trials must use one DeepSeek pricing fingerprint")
        if all_cost_fingerprints and self.deepseek_pricing_fingerprint != next(
            iter(all_cost_fingerprints)
        ):
            raise ValueError("deepseek_pricing_fingerprint must match cost summaries")
        if set(comparison_case_ids) != p4_case_ids:
            raise ValueError(
                "ablation comparisons must cover every and only P4 Case"
            )
        comparison_by_case = {
            item.case_id: item for item in self.ablation_comparisons
        }
        for case_id in p4_case_ids:
            comparison = comparison_by_case[case_id]
            case_trials = [
                trial for trial in self.trials if trial.case_id == case_id
            ]
            result_by_variant = {
                item.variant_id: item for item in comparison.variant_results
            }
            trial_variant_ids = {
                trial.variant_id for trial in case_trials
            }
            if set(result_by_variant) != trial_variant_ids:
                raise ValueError(
                    "ablation comparison Variants must match local Trials"
                )
            for variant_id, variant_result in result_by_variant.items():
                variant_trials = [
                    trial
                    for trial in case_trials
                    if trial.variant_id == variant_id
                ]
                if variant_result.trial_ids != [
                    trial.trial_id for trial in variant_trials
                ]:
                    raise ValueError(
                        "ablation comparison Trial IDs must replay local order"
                    )
                if {
                    trial.configuration_fingerprint
                    for trial in variant_trials
                } != {variant_result.configuration_sha256}:
                    raise ValueError(
                        "ablation comparison configuration must match Trials"
                    )
        if self.summary.case_count != len(self.cases):
            raise ValueError("summary.case_count must match case aggregates")
        if self.summary.trial_count != len(self.trials):
            raise ValueError("summary.trial_count must match trials")
        if self.summary.deepseek_cost is not None and cost_fingerprints:
            if self.summary.deepseek_cost.pricing_fingerprint not in cost_fingerprints:
                raise ValueError("summary cost fingerprint must match Trial costs")
        expected_local_status = (
            LocalExecutionStatus.COMPLETED
            if self.summary.passed_count == self.summary.trial_count
            else LocalExecutionStatus.COMPLETED_WITH_FAILURES
        )
        if self.local_execution_status is None:
            object.__setattr__(self, "local_execution_status", expected_local_status)
        elif self.local_execution_status is not expected_local_status:
            raise ValueError("local_execution_status must match local Trial results")
        if self.langfuse_publish_result is None and (
            self.remote_publication_status is not None
            or self.experiment_identity is not None
            or self.integration_errors
        ):
            raise ValueError(
                "remote publication status/identity/errors require a publication result"
            )
        if self.langfuse_publish_result is not None:
            expected_remote_status = self.langfuse_publish_result.status
            if self.remote_publication_status is None:
                object.__setattr__(
                    self,
                    "remote_publication_status",
                    expected_remote_status,
                )
            elif self.remote_publication_status is not expected_remote_status:
                raise ValueError(
                    "remote_publication_status must match the publication result"
                )
            if self.experiment_identity != self.langfuse_publish_result.experiment:
                raise ValueError(
                    "experiment_identity must match the Langfuse publication result"
                )
            if self.integration_errors != self.langfuse_publish_result.errors:
                raise ValueError(
                    "integration_errors must mirror Langfuse publication errors"
                )
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
