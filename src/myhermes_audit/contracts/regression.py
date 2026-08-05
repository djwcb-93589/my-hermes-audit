"""Versioned, content-safe contracts for P7 baselines and comparisons.

The contracts intentionally contain projections of an ``AuditRunResult`` only.
They never carry prompts, model responses, memory text, review evidence, or
local filesystem paths.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Sequence

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    GitObjectId,
    Identifier,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    Sha256Digest,
    UtcDatetime,
)
from myhermes_audit.contracts.cost import DeepSeekCostAggregate
from myhermes_audit.contracts.result import (
    AuditSummary,
    CaseAggregate,
    DeepSeekCacheSummary,
)
from myhermes_audit.serialization import canonical_sha256


BASELINE_SCHEMA_VERSION = "baseline-v2"
REGRESSION_SCHEMA_VERSION = "regression-v2"
REGRESSION_POLICY_SCHEMA_VERSION = "regression-policy-v1"
METRIC_CONTRACT_VERSION = "p7-metrics-v1"

BaselineSchemaVersion = Literal["baseline-v2"]
RegressionSchemaVersion = Literal["regression-v2"]
RegressionPolicySchemaVersion = Literal["regression-policy-v1"]
MetricNumber = StrictInt | StrictFloat | Decimal


class RegressionStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    REGRESSED = "regressed"
    NOT_COMPARABLE = "not_comparable"
    INVALID_INPUT = "invalid_input"


class MetricDecision(str, Enum):
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"
    WARNING = "warning"
    NOT_COMPARABLE = "not_comparable"
    NOT_EVALUATED = "not_evaluated"


class PolicyMode(str, Enum):
    DISABLED = "disabled"
    WARNING = "warning"
    FAILURE = "failure"


class MetricDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class MetricAvailability(str, Enum):
    AVAILABLE = "available"
    NOT_EVALUATED = "not_evaluated"


class IdentityStatus(str, Enum):
    """Whether a comparison identity has one, no, or conflicting values."""

    AVAILABLE = "available"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class IdentityEvidence(ContractModel):
    """Safe identity evidence; conflicts are never collapsed into ``None``."""

    status: IdentityStatus
    value: NonEmptyText | None = None
    values: list[NonEmptyText] = Field(default_factory=list)
    fingerprint: Sha256Digest | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "IdentityEvidence":
        if len(self.values) != len(set(self.values)):
            raise ValueError("identity values must be unique")
        if self.values != sorted(self.values):
            raise ValueError("identity values must be sorted")
        if self.status is IdentityStatus.AVAILABLE:
            if self.value is None:
                raise ValueError("available identity requires one value")
            if self.values != [self.value]:
                raise ValueError("available identity must expose exactly one value")
            if self.fingerprint is not None:
                raise ValueError("available identity cannot expose conflict fingerprint")
        elif self.status is IdentityStatus.MISSING:
            if self.value is not None or self.values or self.fingerprint is not None:
                raise ValueError("missing identity cannot expose a value")
        else:
            if self.value is not None or len(self.values) < 2:
                raise ValueError("ambiguous identity requires multiple values")
            if self.fingerprint is None:
                raise ValueError("ambiguous identity requires a stable fingerprint")
            if self.fingerprint != canonical_sha256(self.values):
                raise ValueError("ambiguous identity fingerprint is inconsistent")
        return self


class RegressionMetricPolicy(ContractModel):
    """One explicit policy entry; no threshold is implicit in comparison code."""

    mode: PolicyMode = PolicyMode.DISABLED
    direction: MetricDirection = MetricDirection.NEUTRAL
    max_absolute_drop: StrictFloat | None = Field(default=None, ge=0)
    max_relative_increase: StrictFloat | None = Field(default=None, ge=0)
    max_absolute_increase: StrictFloat | None = Field(default=None, ge=0)
    require_pricing_match: StrictBool = False

    @model_validator(mode="after")
    def validate_thresholds(self) -> "RegressionMetricPolicy":
        if self.mode is PolicyMode.DISABLED:
            return self
        thresholds = (
            self.max_absolute_drop,
            self.max_relative_increase,
            self.max_absolute_increase,
        )
        if not any(value is not None for value in thresholds):
            raise ValueError("enabled metric policies require an explicit threshold")
        if self.direction is MetricDirection.NEUTRAL:
            raise ValueError("enabled metric policies require a comparison direction")
        return self


class RegressionPolicy(ContractModel):
    schema_version: RegressionPolicySchemaVersion = REGRESSION_POLICY_SCHEMA_VERSION
    metrics: dict[NonEmptyText, RegressionMetricPolicy] = Field(default_factory=dict)
    default_mode: PolicyMode = PolicyMode.DISABLED

    @classmethod
    def conservative_default(cls) -> "RegressionPolicy":
        """Return the documented conservative policy without hidden thresholds."""

        def failure() -> RegressionMetricPolicy:
            return RegressionMetricPolicy(
                mode=PolicyMode.FAILURE,
                direction=MetricDirection.HIGHER_IS_BETTER,
                max_absolute_drop=0.05,
            )

        def warning_lower(relative: float = 0.20) -> RegressionMetricPolicy:
            return RegressionMetricPolicy(
                mode=PolicyMode.WARNING,
                direction=MetricDirection.LOWER_IS_BETTER,
                max_relative_increase=relative,
            )

        metrics = {
            name: failure()
            for name in (
                "task_success_rate",
                "tool_correctness_rate",
                "memory_required_evidence_hit_rate",
                "background_review_decision_accuracy",
            )
        }
        metrics.update(
            {
                "memory_recall_at_k_mean": RegressionMetricPolicy(
                    mode=PolicyMode.WARNING,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    max_absolute_drop=0.05,
                ),
                "memory_mrr_mean": RegressionMetricPolicy(
                    mode=PolicyMode.WARNING,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    max_absolute_drop=0.05,
                ),
                "failure_rate": RegressionMetricPolicy(
                    mode=PolicyMode.FAILURE,
                    direction=MetricDirection.LOWER_IS_BETTER,
                    max_absolute_increase=0.02,
                ),
                "timeout_rate": RegressionMetricPolicy(
                    mode=PolicyMode.FAILURE,
                    direction=MetricDirection.LOWER_IS_BETTER,
                    max_absolute_increase=0.02,
                ),
                "environment_error_rate": RegressionMetricPolicy(
                    mode=PolicyMode.FAILURE,
                    direction=MetricDirection.LOWER_IS_BETTER,
                    max_absolute_increase=0.02,
                ),
                "cancelled_rate": RegressionMetricPolicy(
                    mode=PolicyMode.FAILURE,
                    direction=MetricDirection.LOWER_IS_BETTER,
                    max_absolute_increase=0.02,
                ),
                "deepseek_cache_hit_rate": RegressionMetricPolicy(
                    mode=PolicyMode.WARNING,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    max_absolute_drop=0.05,
                ),
                "deepseek_cost_effective_cost_per_success_usd": RegressionMetricPolicy(
                    mode=PolicyMode.WARNING,
                    direction=MetricDirection.LOWER_IS_BETTER,
                    max_relative_increase=0.20,
                    require_pricing_match=True,
                ),
                "deepseek_cost_cache_savings_usd": RegressionMetricPolicy(
                    mode=PolicyMode.WARNING,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    max_absolute_drop=0.05,
                    require_pricing_match=True,
                ),
            }
        )
        for name in (
            "conversation_turn_count_mean",
            "agent_iterations_mean",
            "duration_mean_ms",
            "prompt_tokens_mean_per_trial",
            "completion_tokens_mean_per_trial",
            "total_tokens_mean_per_trial",
            "tool_call_count_mean",
        ):
            metrics[name] = warning_lower()
        return cls(metrics=metrics)


class MetricSnapshot(ContractModel):
    metric_name: Identifier
    value: MetricNumber | None = None
    sample_count: NonNegativeInt = 0
    denominator: NonNegativeInt | None = None
    unit: NonEmptyText = "value"
    direction: MetricDirection = MetricDirection.NEUTRAL
    availability: MetricAvailability = MetricAvailability.AVAILABLE

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("metric values must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metric values must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("metric values must be finite")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "MetricSnapshot":
        if self.availability is MetricAvailability.NOT_EVALUATED:
            if self.value is not None or self.sample_count != 0:
                raise ValueError("not-evaluated metrics cannot expose samples")
        if self.denominator is not None and self.sample_count > self.denominator:
            raise ValueError("metric samples cannot exceed denominator")
        return self


class BenchmarkSummary(ContractModel):
    """Safe suite projection plus P7 distribution statistics."""

    summary: AuditSummary
    task_success_sample_count: NonNegativeInt
    task_success_passed_count: NonNegativeInt
    task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    metrics: list[MetricSnapshot] = Field(default_factory=list)
    failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_metrics(self) -> "BenchmarkSummary":
        if self.task_success_passed_count > self.task_success_sample_count:
            raise ValueError("task success passed count cannot exceed sample count")
        if self.task_success_sample_count == 0:
            if self.task_success_rate is not None:
                raise ValueError("task success rate requires explicit bool samples")
        else:
            expected = self.task_success_passed_count / self.task_success_sample_count
            if self.task_success_rate is None or not math.isclose(
                self.task_success_rate, expected, rel_tol=1e-9, abs_tol=1e-12
            ):
                raise ValueError("task success facts are inconsistent")
        if self.summary.task_success_sample_count != self.task_success_sample_count:
            raise ValueError("suite task success sample count must mirror AuditSummary")
        if self.summary.task_success_rate != self.task_success_rate:
            raise ValueError("suite task success rate must mirror AuditSummary")
        names = [item.metric_name for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric snapshots must have unique names")
        return self


class BenchmarkCaseSummary(ContractModel):
    case_id: Identifier
    summary: CaseAggregate
    declared_trial_count: PositiveInt
    task_success_sample_count: NonNegativeInt
    task_success_passed_count: NonNegativeInt
    task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    metrics: list[MetricSnapshot] = Field(default_factory=list)
    failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    background_review_decision_sample_count: NonNegativeInt
    deepseek_cache: DeepSeekCacheSummary | None = None
    deepseek_cost: DeepSeekCostAggregate | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_metrics(self) -> "BenchmarkCaseSummary":
        if self.task_success_passed_count > self.task_success_sample_count:
            raise ValueError("case task success passed count cannot exceed sample count")
        if self.task_success_sample_count == 0:
            if self.task_success_rate is not None:
                raise ValueError("case task success rate requires explicit bool samples")
        else:
            expected = self.task_success_passed_count / self.task_success_sample_count
            if self.task_success_rate is None or not math.isclose(
                self.task_success_rate, expected, rel_tol=1e-9, abs_tol=1e-12
            ):
                raise ValueError("case task success facts are inconsistent")
        if self.declared_trial_count > self.summary.trial_count:
            raise ValueError("declared case trials cannot exceed case trial count")
        if self.background_review_decision_accuracy is not None and self.background_review_decision_sample_count == 0:
            raise ValueError("review accuracy requires an explicit sample count")
        names = [item.metric_name for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("case metric snapshots must have unique names")
        if self.deepseek_cost is not None and self.deepseek_cost.trial_count != self.summary.trial_count:
            raise ValueError("case cost trial count must match case summary")
        return self


class AuditBaseline(ContractModel):
    """Immutable historical facts from one validated Audit run."""

    model_config = ConfigDict(frozen=True)

    schema_version: BaselineSchemaVersion = BASELINE_SCHEMA_VERSION
    baseline_id: Identifier
    baseline_fingerprint: Sha256Digest
    created_at: UtcDatetime
    source_run_id: Identifier
    audit_commit: GitObjectId | None = None
    subject_commit: GitObjectId
    suite_id: Identifier
    suite_fingerprint: Sha256Digest
    suite_comparison_fingerprint: Sha256Digest | None = None
    result_schema_version: NonEmptyText
    metric_contract_version: NonEmptyText = METRIC_CONTRACT_VERSION
    model_identity: IdentityEvidence
    configuration_identity: IdentityEvidence
    worker_protocol_identity: IdentityEvidence
    result_schema_identity: IdentityEvidence
    metric_contract_identity: IdentityEvidence
    worker_protocol_version: NonEmptyText | None = None
    model_identifier: NonEmptyText | None = None
    configuration_fingerprint: Sha256Digest | None = None
    pricing_fingerprint: Sha256Digest | None = None
    declared_trial_count: NonNegativeInt
    total_trial_count: NonNegativeInt
    declared_trials_per_case: NonNegativeInt | None
    declared_trial_counts_by_case: dict[Identifier, PositiveInt]
    case_ids: list[Identifier] = Field(default_factory=list)
    suite_summary: BenchmarkSummary
    case_summaries: list[BenchmarkCaseSummary] = Field(default_factory=list)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_baseline_identity(self) -> "AuditBaseline":
        if self.suite_summary.summary.trial_count != self.declared_trial_count:
            raise ValueError("legacy declared trial count must match suite summary")
        if self.total_trial_count != self.declared_trial_count:
            raise ValueError("total trial count must match suite summary")
        if self.declared_trials_per_case is not None and self.declared_trial_counts_by_case:
            if any(value != self.declared_trials_per_case for value in self.declared_trial_counts_by_case.values()):
                raise ValueError("uniform per-case count conflicts with case mapping")
        if set(self.declared_trial_counts_by_case) != set(self.case_ids):
            raise ValueError("declared trial mapping must cover the Case list")
        for identity, scalar in (
            (self.model_identity, self.model_identifier),
            (self.configuration_identity, self.configuration_fingerprint),
            (self.worker_protocol_identity, self.worker_protocol_version),
        ):
            if identity.status is IdentityStatus.AVAILABLE and scalar != identity.value:
                raise ValueError("identity scalar projection is inconsistent")
            if identity.status is not IdentityStatus.AVAILABLE and scalar is not None:
                raise ValueError("missing or ambiguous identity cannot expose scalar value")
        if self.result_schema_identity.status is IdentityStatus.AVAILABLE:
            if self.result_schema_identity.value != self.result_schema_version:
                raise ValueError("result schema identity is inconsistent")
        else:
            raise ValueError("result schema identity must be available")
        if self.metric_contract_identity.status is IdentityStatus.AVAILABLE:
            if self.metric_contract_identity.value != self.metric_contract_version:
                raise ValueError("metric contract identity is inconsistent")
        else:
            raise ValueError("metric contract identity must be available")
        summary_case_ids = [item.case_id for item in self.case_summaries]
        if summary_case_ids != self.case_ids:
            raise ValueError("case_ids must preserve Case summary order")
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("baseline Case IDs must be unique")
        payload = self.model_dump(
            mode="json",
            exclude={"baseline_id", "baseline_fingerprint", "created_at"},
        )
        expected = canonical_sha256(payload)
        if self.baseline_fingerprint != expected:
            raise ValueError("baseline_fingerprint does not match baseline facts")
        if self.baseline_id != f"baseline-{expected[:16]}":
            raise ValueError("baseline_id must be derived from baseline_fingerprint")
        return self

    @classmethod
    def from_result(cls, result) -> "AuditBaseline":
        from myhermes_audit.regression import build_baseline

        return build_baseline(result)


class MetricComparison(ContractModel):
    metric_name: Identifier
    baseline_value: MetricNumber | None = None
    current_value: MetricNumber | None = None
    absolute_delta: MetricNumber | None = None
    relative_delta: StrictFloat | None = None
    baseline_sample_count: NonNegativeInt = 0
    current_sample_count: NonNegativeInt = 0
    baseline_denominator: NonNegativeInt | None = None
    current_denominator: NonNegativeInt | None = None
    decision: MetricDecision
    policy_mode: PolicyMode
    direction: MetricDirection = MetricDirection.NEUTRAL
    max_absolute_drop: StrictFloat | None = Field(default=None, ge=0)
    max_relative_increase: StrictFloat | None = Field(default=None, ge=0)
    max_absolute_increase: StrictFloat | None = Field(default=None, ge=0)
    reason: NonEmptyText | None = None

    @field_validator("baseline_value", "current_value", "absolute_delta", mode="before")
    @classmethod
    def validate_numeric_fact(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("comparison values must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("comparison values must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("comparison values must be finite")
        return value

    @field_validator("relative_delta", mode="before")
    @classmethod
    def validate_relative_fact(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("relative delta must be finite numeric")
        return value

    @model_validator(mode="after")
    def validate_comparison(self) -> "MetricComparison":
        evaluated = {
            MetricDecision.IMPROVED,
            MetricDecision.UNCHANGED,
            MetricDecision.REGRESSED,
            MetricDecision.WARNING,
        }
        if self.decision in {MetricDecision.NOT_COMPARABLE, MetricDecision.NOT_EVALUATED}:
            if self.reason is None:
                raise ValueError("non-evaluated comparisons require a reason")
            if self.absolute_delta is not None or self.relative_delta is not None:
                raise ValueError("non-evaluated comparisons cannot carry deltas")
            return self
        if self.decision in evaluated:
            if self.baseline_value is None or self.current_value is None:
                raise ValueError("evaluated comparisons require both values")
            if self.baseline_sample_count == 0 or self.current_sample_count == 0:
                raise ValueError("evaluated comparisons require samples")
            expected_delta = float(self.current_value) - float(self.baseline_value)
            if self.absolute_delta is None or not math.isclose(float(self.absolute_delta), expected_delta, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError("absolute delta is inconsistent")
            expected_relative = None if float(self.baseline_value) == 0 else expected_delta / abs(float(self.baseline_value))
            if expected_relative is None:
                if self.relative_delta is not None:
                    raise ValueError("zero baseline requires null relative delta")
            elif self.relative_delta is None or not math.isclose(self.relative_delta, expected_relative, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError("relative delta is inconsistent")
            if self.reason is not None:
                raise ValueError("evaluated comparisons cannot carry a reason")
            if self.decision is MetricDecision.REGRESSED and self.policy_mode is not PolicyMode.FAILURE:
                raise ValueError("hard regression requires failure policy")
            if self.decision is MetricDecision.WARNING and self.policy_mode is not PolicyMode.WARNING:
                raise ValueError("warning decision requires warning policy")
            if self.decision in {MetricDecision.REGRESSED, MetricDecision.WARNING, MetricDecision.IMPROVED} and self.direction is MetricDirection.NEUTRAL:
                raise ValueError("directional decision requires a direction")
        if self.policy_mode is PolicyMode.DISABLED and self.decision is not MetricDecision.UNCHANGED:
            raise ValueError("disabled policy must produce unchanged decision")
        return self


def metric_decision_counts(metrics: Sequence[MetricComparison]) -> dict[str, int]:
    """Count decisions from the immutable comparison facts in one place."""

    return {
        "regression_count": sum(item.decision is MetricDecision.REGRESSED for item in metrics),
        "improvement_count": sum(item.decision is MetricDecision.IMPROVED for item in metrics),
        "unchanged_count": sum(item.decision is MetricDecision.UNCHANGED for item in metrics),
        "warning_count": sum(item.decision is MetricDecision.WARNING for item in metrics),
        "not_comparable_count": sum(item.decision is MetricDecision.NOT_COMPARABLE for item in metrics),
        "not_evaluated_count": sum(item.decision is MetricDecision.NOT_EVALUATED for item in metrics),
    }


class CaseRegressionSummary(ContractModel):
    case_id: Identifier
    baseline_trial_count: NonNegativeInt
    current_trial_count: NonNegativeInt
    baseline_declared_trial_count: PositiveInt
    current_declared_trial_count: PositiveInt
    baseline_task_success_sample_count: NonNegativeInt
    baseline_task_success_passed_count: NonNegativeInt
    baseline_task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    current_task_success_sample_count: NonNegativeInt
    current_task_success_passed_count: NonNegativeInt
    current_task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    task_success_rate_delta: StrictFloat | None = Field(...)
    # Deprecated compatibility alias; P7 output uses task_success_rate_delta.
    pass_rate_delta: StrictFloat | None = None
    baseline_failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    current_failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    baseline_background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    current_background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    baseline_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    current_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    metrics: list[MetricComparison] = Field(default_factory=list)
    metric_comparison_count: NonNegativeInt
    baseline_background_review_decision_sample_count: NonNegativeInt
    background_review_decision_sample_count: NonNegativeInt
    decision_reason: NonEmptyText | None = None
    decision: MetricDecision

    @model_validator(mode="after")
    def validate_case_summary(self) -> "CaseRegressionSummary":
        for sample, passed, rate, label in (
            (self.baseline_task_success_sample_count, self.baseline_task_success_passed_count, self.baseline_task_success_rate, "baseline"),
            (self.current_task_success_sample_count, self.current_task_success_passed_count, self.current_task_success_rate, "current"),
        ):
            if passed > sample:
                raise ValueError(f"{label} task success passed count exceeds sample count")
            if sample == 0:
                if rate is not None:
                    raise ValueError(f"{label} task success rate requires samples")
            else:
                expected = passed / sample
                if rate is None or not math.isclose(rate, expected, rel_tol=1e-9, abs_tol=1e-12):
                    raise ValueError(f"{label} task success facts are inconsistent")
        if self.baseline_declared_trial_count > self.baseline_trial_count or self.current_declared_trial_count > self.current_trial_count:
            raise ValueError("declared Case trials cannot exceed Trial count")
        expected_delta = None if self.baseline_task_success_rate is None or self.current_task_success_rate is None else self.current_task_success_rate - self.baseline_task_success_rate
        if expected_delta is None:
            if self.task_success_rate_delta is not None:
                raise ValueError("task success delta requires both rates")
        elif self.task_success_rate_delta is None or not math.isclose(self.task_success_rate_delta, expected_delta, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("task success rate delta is inconsistent")
        if self.pass_rate_delta is not None:
            if expected_delta is None or not math.isclose(self.pass_rate_delta, expected_delta, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError("legacy pass rate delta is inconsistent")
        if self.metric_comparison_count != len(self.metrics):
            raise ValueError("metric comparison count must match metrics")
        if sum(self.baseline_background_review_actions.values()) > self.baseline_background_review_decision_sample_count:
            raise ValueError("baseline background review action counts exceed decision samples")
        if sum(self.current_background_review_actions.values()) > self.background_review_decision_sample_count:
            raise ValueError("current background review action counts exceed decision samples")
        decisions = {item.decision for item in self.metrics}
        if MetricDecision.REGRESSED in decisions and self.decision is not MetricDecision.REGRESSED:
            raise ValueError("Case decision must expose hard regression")
        if MetricDecision.WARNING in decisions and MetricDecision.REGRESSED not in decisions and self.decision is not MetricDecision.WARNING:
            raise ValueError("Case decision must expose warning")
        if decisions and decisions <= {MetricDecision.NOT_COMPARABLE}:
            if self.decision is not MetricDecision.NOT_COMPARABLE or self.decision_reason is None:
                raise ValueError("not-comparable Case requires a reason")
        return self


class AuditRegressionReport(ContractModel):
    """Strict comparison facts; deliberately contains no weighted score."""

    model_config = ConfigDict(frozen=True)

    schema_version: RegressionSchemaVersion = REGRESSION_SCHEMA_VERSION
    baseline_id: Identifier
    current_run_id: Identifier
    status: RegressionStatus
    comparability_reasons: list[NonEmptyText] = Field(default_factory=list)
    suite_id: Identifier
    baseline_suite_fingerprint: Sha256Digest
    current_suite_fingerprint: Sha256Digest
    baseline_suite_comparison_fingerprint: Sha256Digest | None = None
    current_suite_comparison_fingerprint: Sha256Digest | None = None
    baseline_subject_commit: GitObjectId
    current_subject_commit: GitObjectId
    baseline_audit_commit: GitObjectId | None = None
    current_audit_commit: GitObjectId | None = None
    baseline_model_identifier: NonEmptyText | None = None
    current_model_identifier: NonEmptyText | None = None
    baseline_configuration_fingerprint: Sha256Digest | None = None
    current_configuration_fingerprint: Sha256Digest | None = None
    baseline_model_identity: IdentityEvidence
    current_model_identity: IdentityEvidence
    baseline_configuration_identity: IdentityEvidence
    current_configuration_identity: IdentityEvidence
    baseline_worker_protocol_identity: IdentityEvidence
    current_worker_protocol_identity: IdentityEvidence
    baseline_result_schema_identity: IdentityEvidence
    current_result_schema_identity: IdentityEvidence
    baseline_metric_contract_identity: IdentityEvidence
    current_metric_contract_identity: IdentityEvidence
    baseline_pricing_fingerprint: Sha256Digest | None = None
    current_pricing_fingerprint: Sha256Digest | None = None
    baseline_trial_count: NonNegativeInt
    current_trial_count: NonNegativeInt
    baseline_total_trial_count: NonNegativeInt
    current_total_trial_count: NonNegativeInt
    baseline_declared_trials_per_case: PositiveInt | None
    current_declared_trials_per_case: PositiveInt | None
    baseline_declared_trial_counts_by_case: dict[Identifier, PositiveInt]
    current_declared_trial_counts_by_case: dict[Identifier, PositiveInt]
    baseline_suite_task_success_sample_count: NonNegativeInt
    baseline_suite_task_success_passed_count: NonNegativeInt
    baseline_suite_task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    current_suite_task_success_sample_count: NonNegativeInt
    current_suite_task_success_passed_count: NonNegativeInt
    current_suite_task_success_rate: StrictFloat | None = Field(..., ge=0, le=1)
    suite_task_success_rate_delta: StrictFloat | None = Field(...)
    metric_contract_version: NonEmptyText = METRIC_CONTRACT_VERSION
    baseline_metric_contract_version: NonEmptyText = METRIC_CONTRACT_VERSION
    current_result_schema_version: NonEmptyText
    current_worker_protocol_version: NonEmptyText | None = None
    suite_metrics: list[MetricComparison] = Field(default_factory=list)
    case_summaries: list[CaseRegressionSummary] = Field(default_factory=list)
    regression_count: NonNegativeInt = 0
    improvement_count: NonNegativeInt = 0
    unchanged_count: NonNegativeInt = 0
    warning_count: NonNegativeInt = 0
    not_comparable_count: NonNegativeInt = 0
    not_evaluated_count: NonNegativeInt
    overall_regression_gate: StrictBool
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditRegressionReport":
        if self.baseline_trial_count != self.baseline_total_trial_count or self.current_trial_count != self.current_total_trial_count:
            raise ValueError("legacy and explicit total trial counts must match")
        case_ids = {item.case_id for item in self.case_summaries}
        if not case_ids.issubset(self.baseline_declared_trial_counts_by_case) or not case_ids.issubset(self.current_declared_trial_counts_by_case):
            raise ValueError("report repeat mappings must cover compared Case summaries")
        if self.baseline_declared_trials_per_case is not None and any(value != self.baseline_declared_trials_per_case for value in self.baseline_declared_trial_counts_by_case.values()):
            raise ValueError("baseline repeat mapping conflicts with uniform count")
        if self.current_declared_trials_per_case is not None and any(value != self.current_declared_trials_per_case for value in self.current_declared_trial_counts_by_case.values()):
            raise ValueError("current repeat mapping conflicts with uniform count")
        for identity, scalar in (
            (self.baseline_model_identity, self.baseline_model_identifier),
            (self.current_model_identity, self.current_model_identifier),
            (self.baseline_configuration_identity, self.baseline_configuration_fingerprint),
            (self.current_configuration_identity, self.current_configuration_fingerprint),
        ):
            if identity.status is IdentityStatus.AVAILABLE and scalar != identity.value:
                raise ValueError("report identity scalar projection is inconsistent")
            if identity.status is not IdentityStatus.AVAILABLE and scalar is not None:
                raise ValueError("report ambiguous identity cannot expose scalar value")
        if self.current_result_schema_identity.status is IdentityStatus.AVAILABLE and self.current_result_schema_identity.value != self.current_result_schema_version:
            raise ValueError("current Result Schema identity is inconsistent")
        if self.current_metric_contract_identity.status is IdentityStatus.AVAILABLE and self.current_metric_contract_identity.value != self.metric_contract_version:
            raise ValueError("current metric contract identity is inconsistent")
        if self.current_worker_protocol_identity.status is IdentityStatus.AVAILABLE and self.current_worker_protocol_identity.value != self.current_worker_protocol_version:
            raise ValueError("current Worker Protocol identity is inconsistent")
        if self.current_worker_protocol_identity.status is not IdentityStatus.AVAILABLE and self.current_worker_protocol_version is not None:
            raise ValueError("current ambiguous Worker Protocol cannot expose scalar value")
        for sample, passed, rate, label in (
            (self.baseline_suite_task_success_sample_count, self.baseline_suite_task_success_passed_count, self.baseline_suite_task_success_rate, "baseline"),
            (self.current_suite_task_success_sample_count, self.current_suite_task_success_passed_count, self.current_suite_task_success_rate, "current"),
        ):
            if passed > sample:
                raise ValueError(f"{label} suite task success passed count exceeds sample count")
            expected = None if sample == 0 else passed / sample
            if expected is None:
                if rate is not None:
                    raise ValueError(f"{label} suite task success rate requires samples")
            elif rate is None or not math.isclose(rate, expected, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError(f"{label} suite task success facts are inconsistent")
        expected_suite_delta = None if self.baseline_suite_task_success_rate is None or self.current_suite_task_success_rate is None else self.current_suite_task_success_rate - self.baseline_suite_task_success_rate
        if expected_suite_delta is None:
            if self.suite_task_success_rate_delta is not None:
                raise ValueError("suite task success delta requires both rates")
        elif self.suite_task_success_rate_delta is None or not math.isclose(self.suite_task_success_rate_delta, expected_suite_delta, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("suite task success delta is inconsistent")
        all_metrics = [*self.suite_metrics, *(metric for case in self.case_summaries for metric in case.metrics)]
        expected_counts = metric_decision_counts(all_metrics)
        for name, expected in expected_counts.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} does not match MetricComparison decisions")
        core_reasons = [reason for reason in self.comparability_reasons if reason != "pricing_fingerprint_mismatch"]
        hard = self.regression_count > 0
        warning = self.warning_count > 0
        if self.status is RegressionStatus.PASSED:
            if hard or warning or core_reasons or not self.overall_regression_gate:
                raise ValueError("passed status contradicts regression facts")
        elif self.status is RegressionStatus.PASSED_WITH_WARNINGS:
            if hard or not warning or core_reasons or not self.overall_regression_gate:
                raise ValueError("warning status contradicts regression facts")
        elif self.status is RegressionStatus.REGRESSED:
            if not hard or self.overall_regression_gate or core_reasons:
                raise ValueError("regressed reports must fail the regression gate")
        elif self.status is RegressionStatus.NOT_COMPARABLE:
            if not core_reasons or self.overall_regression_gate:
                raise ValueError("not-comparable reports require core reasons and a closed gate")
        elif self.status is RegressionStatus.INVALID_INPUT:
            if self.overall_regression_gate or self.regression_count or self.warning_count:
                raise ValueError("invalid input cannot carry a regression conclusion")
        if not core_reasons and self.status is RegressionStatus.NOT_COMPARABLE:
            raise ValueError("not-comparable status requires a core reason")
        return self


__all__ = (
    "AuditBaseline",
    "AuditRegressionReport",
    "BASELINE_SCHEMA_VERSION",
    "BenchmarkCaseSummary",
    "BenchmarkSummary",
    "METRIC_CONTRACT_VERSION",
    "MetricAvailability",
    "MetricComparison",
    "metric_decision_counts",
    "MetricDecision",
    "MetricDirection",
    "MetricSnapshot",
    "PolicyMode",
    "RegressionMetricPolicy",
    "RegressionPolicy",
    "REGRESSION_POLICY_SCHEMA_VERSION",
    "REGRESSION_SCHEMA_VERSION",
    "RegressionSchemaVersion",
    "RegressionStatus",
    "IdentityEvidence",
    "IdentityStatus",
    "CaseRegressionSummary",
)
