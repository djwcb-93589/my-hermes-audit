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
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    GitObjectId,
    Identifier,
    NonEmptyText,
    NonNegativeInt,
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


BASELINE_SCHEMA_VERSION = "baseline-v1"
REGRESSION_SCHEMA_VERSION = "regression-v1"
REGRESSION_POLICY_SCHEMA_VERSION = "regression-policy-v1"
METRIC_CONTRACT_VERSION = "p7-metrics-v1"

BaselineSchemaVersion = Literal["baseline-v1"]
RegressionSchemaVersion = Literal["regression-v1"]
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
    DISABLED = "disabled"


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
    metrics: list[MetricSnapshot] = Field(default_factory=list)
    failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_metrics(self) -> "BenchmarkSummary":
        names = [item.metric_name for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric snapshots must have unique names")
        return self


class BenchmarkCaseSummary(ContractModel):
    case_id: Identifier
    summary: CaseAggregate
    metrics: list[MetricSnapshot] = Field(default_factory=list)
    failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    background_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    deepseek_cache: DeepSeekCacheSummary | None = None
    deepseek_cost: DeepSeekCostAggregate | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_metrics(self) -> "BenchmarkCaseSummary":
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
    worker_protocol_version: NonEmptyText | None = None
    model_identifier: NonEmptyText | None = None
    configuration_fingerprint: Sha256Digest | None = None
    pricing_fingerprint: Sha256Digest | None = None
    declared_trial_count: NonNegativeInt
    case_ids: list[Identifier] = Field(default_factory=list)
    suite_summary: BenchmarkSummary
    case_summaries: list[BenchmarkCaseSummary] = Field(default_factory=list)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_baseline_identity(self) -> "AuditBaseline":
        if self.suite_summary.summary.trial_count != self.declared_trial_count:
            raise ValueError("declared trial count must match suite summary")
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
    reason: NonEmptyText | None = None


class CaseRegressionSummary(ContractModel):
    case_id: Identifier
    baseline_trial_count: NonNegativeInt
    current_trial_count: NonNegativeInt
    baseline_pass_rate: StrictFloat = Field(ge=0, le=1)
    current_pass_rate: StrictFloat = Field(ge=0, le=1)
    pass_rate_delta: StrictFloat
    baseline_failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    current_failure_categories: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    baseline_background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    current_background_review_actions: dict[NonEmptyText, NonNegativeInt] = Field(default_factory=dict)
    baseline_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    current_review_decision_accuracy: StrictFloat | None = Field(default=None, ge=0, le=1)
    metrics: list[MetricComparison] = Field(default_factory=list)
    decision: MetricDecision


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
    baseline_pricing_fingerprint: Sha256Digest | None = None
    current_pricing_fingerprint: Sha256Digest | None = None
    baseline_trial_count: NonNegativeInt
    current_trial_count: NonNegativeInt
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
    overall_regression_gate: StrictBool
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditRegressionReport":
        if self.status is RegressionStatus.REGRESSED and self.overall_regression_gate:
            raise ValueError("regressed reports must fail the regression gate")
        if self.status is RegressionStatus.NOT_COMPARABLE and self.overall_regression_gate:
            raise ValueError("not-comparable reports must fail the regression gate")
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
    "CaseRegressionSummary",
)
