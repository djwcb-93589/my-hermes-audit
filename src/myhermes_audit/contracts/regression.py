"""Versioned, content-safe contracts for P7 baselines and comparisons.

The contracts intentionally contain projections of an ``AuditRunResult`` only.
They never carry prompts, model responses, memory text, review evidence, or
local filesystem paths.
"""

from __future__ import annotations

import math
import re
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
from myhermes_audit.regression_decision import (
    COMPARABILITY_REASON_CODES,
    ComparabilityStatus,
    EVALUATION_REASON_CODES,
    EvaluationStatus,
    MetricDecisionInput,
    MetricEvaluationInput,
    MetricPolicyFacts,
    PolicyEntryFacts,
    PolicySnapshotFacts,
    PRICING_REASON_CODES,
    count_comparable_metric_roles,
    decide_case_regression,
    decide_metric_comparison,
    decide_report_status,
    derive_comparability_reason_codes,
    derive_metric_evaluation_facts,
    derive_metric_role,
    derive_pricing_comparability_reason,
    finalize_report_comparability_reasons,
    resolve_metric_policy,
)


BASELINE_SCHEMA_VERSION = "baseline-v7"
REGRESSION_SCHEMA_VERSION = "regression-v10"
REGRESSION_POLICY_SCHEMA_VERSION = "regression-policy-v1"
METRIC_CONTRACT_VERSION = "p7-metrics-v1"
REQUIRED_RUNTIME_CORE_METRIC_NAMES = frozenset(
    {
        "failure_rate",
        "timeout_rate",
        "environment_error_rate",
        "cancelled_rate",
    }
)

BaselineSchemaVersion = Literal["baseline-v7"]
RegressionSchemaVersion = Literal["regression-v10"]
RegressionPolicySchemaVersion = Literal["regression-policy-v1"]
MetricNumber = StrictInt | StrictFloat | Decimal
_STRICT_DECIMAL_TEXT = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$"
)


def parse_metric_number(value: object) -> int | float | Decimal | None:
    """Parse the finite numeric forms used by versioned P7 Metric contracts.

    Pydantic's JSON mode serializes ``Decimal`` as text.  The parser therefore
    accepts only a strict decimal grammar for strings and never falls back to
    ``float`` or ``str(value)`` coercion.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("metric numbers must not be booleans")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metric numbers must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("metric numbers must be finite")
        return value
    if isinstance(value, str):
        if _STRICT_DECIMAL_TEXT.fullmatch(value) is None:
            raise ValueError("metric number text is not a strict decimal")
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise ValueError("metric numbers must be finite")
        return parsed
    raise ValueError("metric numbers must be int, float, Decimal, or decimal text")


class RegressionStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    REGRESSED = "regressed"
    NOT_COMPARABLE = "not_comparable"


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
        if self.direction is MetricDirection.HIGHER_IS_BETTER and (
            self.max_relative_increase is not None
            or self.max_absolute_increase is not None
        ):
            raise ValueError("higher-is-better policies require max_absolute_drop")
        if self.direction is MetricDirection.LOWER_IS_BETTER and self.max_absolute_drop is not None:
            raise ValueError("lower-is-better policies cannot use max_absolute_drop")
        return self


class RegressionPolicy(ContractModel):
    schema_version: RegressionPolicySchemaVersion = REGRESSION_POLICY_SCHEMA_VERSION
    metrics: dict[NonEmptyText, RegressionMetricPolicy] = Field(default_factory=dict)
    default_mode: PolicyMode = PolicyMode.DISABLED

    @model_validator(mode="after")
    def validate_default_policy(self) -> "RegressionPolicy":
        # A default policy has no direction or threshold fields of its own;
        # only the disabled default can therefore be a complete policy fact.
        if self.default_mode is not PolicyMode.DISABLED:
            raise ValueError("non-disabled default_mode requires an explicit metric policy")
        return self

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


class RegressionPolicySnapshotEntry(ContractModel):
    """One canonical, content-safe entry in a Report policy snapshot."""

    metric_name: Identifier
    mode: PolicyMode
    direction: MetricDirection
    max_absolute_drop: StrictFloat | None = Field(default=None, ge=0)
    max_relative_increase: StrictFloat | None = Field(default=None, ge=0)
    max_absolute_increase: StrictFloat | None = Field(default=None, ge=0)
    require_pricing_match: StrictBool = False

    @model_validator(mode="after")
    def validate_entry_policy(self) -> "RegressionPolicySnapshotEntry":
        RegressionMetricPolicy(
            mode=self.mode,
            direction=self.direction,
            max_absolute_drop=self.max_absolute_drop,
            max_relative_increase=self.max_relative_increase,
            max_absolute_increase=self.max_absolute_increase,
            require_pricing_match=self.require_pricing_match,
        )
        return self


class RegressionPolicySnapshot(ContractModel):
    """Immutable policy facts used by one complete Regression Report."""

    model_config = ConfigDict(frozen=True)

    schema_version: RegressionPolicySchemaVersion = REGRESSION_POLICY_SCHEMA_VERSION
    default_mode: PolicyMode
    metrics: list[RegressionPolicySnapshotEntry]
    policy_fingerprint: Sha256Digest

    @staticmethod
    def _fingerprint_payload(
        schema_version: str,
        default_mode: PolicyMode,
        metrics: Sequence[RegressionPolicySnapshotEntry],
    ) -> dict[str, object]:
        return {
            "schema_version": schema_version,
            "default_mode": default_mode.value,
            "metrics": [item.model_dump(mode="json") for item in metrics],
        }

    @model_validator(mode="after")
    def validate_snapshot(self) -> "RegressionPolicySnapshot":
        if self.default_mode is not PolicyMode.DISABLED:
            raise ValueError("non-disabled default_mode has no complete default policy facts")
        names = [item.metric_name for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("policy snapshot metric names must be unique")
        if names != sorted(names):
            raise ValueError("policy snapshot metric names must be sorted")
        expected = canonical_sha256(
            self._fingerprint_payload(self.schema_version, self.default_mode, self.metrics)
        )
        if self.policy_fingerprint != expected:
            raise ValueError("policy snapshot fingerprint is inconsistent")
        return self

    @classmethod
    def from_policy(cls, policy: RegressionPolicy) -> "RegressionPolicySnapshot":
        metrics = [
            RegressionPolicySnapshotEntry(
                metric_name=name,
                mode=item.mode,
                direction=item.direction,
                max_absolute_drop=item.max_absolute_drop,
                max_relative_increase=item.max_relative_increase,
                max_absolute_increase=item.max_absolute_increase,
                require_pricing_match=item.require_pricing_match,
            )
            for name, item in sorted(policy.metrics.items())
        ]
        payload = cls._fingerprint_payload(policy.schema_version, policy.default_mode, metrics)
        return cls(
            schema_version=policy.schema_version,
            default_mode=policy.default_mode,
            metrics=metrics,
            policy_fingerprint=canonical_sha256(payload),
        )

    def to_facts(self) -> PolicySnapshotFacts:
        return PolicySnapshotFacts(
            schema_version=self.schema_version,
            default_mode=self.default_mode.value,
            metrics=tuple(
                PolicyEntryFacts(
                    metric_name=item.metric_name,
                    mode=item.mode.value,
                    direction=item.direction.value,
                    max_absolute_drop=item.max_absolute_drop,
                    max_relative_increase=item.max_relative_increase,
                    max_absolute_increase=item.max_absolute_increase,
                    require_pricing_match=item.require_pricing_match,
                )
                for item in self.metrics
            ),
        )


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
        return parse_metric_number(value)

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

    schema_version: BaselineSchemaVersion
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
    run_configuration_identity: IdentityEvidence
    worker_protocol_identity: IdentityEvidence
    result_schema_identity: IdentityEvidence
    metric_contract_identity: IdentityEvidence
    worker_protocol_version: NonEmptyText | None = None
    model_identifier: NonEmptyText | None = None
    run_configuration_fingerprint: Sha256Digest
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
        if self.declared_trial_count <= 0 or self.total_trial_count <= 0:
            raise ValueError("Baseline requires at least one Trial")
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
            (
                self.run_configuration_identity,
                self.run_configuration_fingerprint,
            ),
            (self.worker_protocol_identity, self.worker_protocol_version),
        ):
            if identity.status is not IdentityStatus.AVAILABLE:
                raise ValueError("Baseline core identities must be available")
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
        suite_metrics = {
            item.metric_name: item for item in self.suite_summary.metrics
        }
        missing_runtime = sorted(
            name
            for name in REQUIRED_RUNTIME_CORE_METRIC_NAMES
            if name not in suite_metrics
            or suite_metrics[name].sample_count <= 0
            or suite_metrics[name].value is None
        )
        if missing_runtime:
            raise ValueError(
                "Baseline is missing required runtime core Metrics: "
                + ", ".join(missing_runtime)
            )
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
    baseline_metric_present: StrictBool
    current_metric_present: StrictBool
    baseline_value: MetricNumber | None = None
    current_value: MetricNumber | None = None
    absolute_delta: MetricNumber | None = None
    relative_delta: MetricNumber | None = None
    baseline_sample_count: NonNegativeInt = 0
    current_sample_count: NonNegativeInt = 0
    baseline_denominator: NonNegativeInt | None = None
    current_denominator: NonNegativeInt | None = None
    comparability_fact_codes: list[NonEmptyText] = Field(...)
    requires_pricing_match: StrictBool
    comparability_facts_verified: Literal[False]
    policy_facts_verified: Literal[False]
    evaluation_status: EvaluationStatus
    comparability_status: ComparabilityStatus
    reason_codes: list[NonEmptyText] = Field(...)
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
        return parse_metric_number(value)

    @field_validator("relative_delta", mode="before")
    @classmethod
    def validate_relative_fact(cls, value: object) -> object:
        return parse_metric_number(value)

    @model_validator(mode="after")
    def validate_comparison(self) -> "MetricComparison":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("comparison reason codes must be unique")
        if self.reason_codes != sorted(self.reason_codes):
            raise ValueError("comparison reason codes must be sorted")
        allowed = COMPARABILITY_REASON_CODES | EVALUATION_REASON_CODES
        if any(reason not in allowed for reason in self.reason_codes):
            raise ValueError("comparison reason code is not recognized")
        if len(self.comparability_fact_codes) != len(set(self.comparability_fact_codes)):
            raise ValueError("comparability fact codes must be unique")
        if self.comparability_fact_codes != sorted(self.comparability_fact_codes):
            raise ValueError("comparability fact codes must be sorted")
        if any(code not in COMPARABILITY_REASON_CODES for code in self.comparability_fact_codes):
            raise ValueError("comparability fact code is not recognized")
        facts = derive_metric_evaluation_facts(
            MetricEvaluationInput(
                metric_name=self.metric_name,
                baseline_present=self.baseline_metric_present,
                current_present=self.current_metric_present,
                baseline_value=self.baseline_value,
                current_value=self.current_value,
                baseline_sample_count=self.baseline_sample_count,
                current_sample_count=self.current_sample_count,
                comparability_fact_codes=tuple(self.comparability_fact_codes),
            )
        )
        if self.evaluation_status is not facts.evaluation_status:
            raise ValueError("evaluation status is inconsistent with metric facts")
        if self.comparability_status is not facts.comparability_status:
            raise ValueError("comparability status is inconsistent with metric facts")
        if self.comparability_fact_codes != list(facts.comparability_fact_codes):
            raise ValueError("comparability fact codes are inconsistent with metric facts")
        if self.reason_codes != list(facts.reason_codes):
            raise ValueError("comparison reason codes are inconsistent with metric facts")
        if self.comparability_status is ComparabilityStatus.COMPARABLE and self.reason_codes:
            raise ValueError("comparable metrics cannot carry comparability reasons")
        expected = decide_metric_comparison(
            MetricDecisionInput(
                baseline_value=self.baseline_value,
                current_value=self.current_value,
                direction=self.direction.value,
                policy_mode=self.policy_mode.value,
                max_absolute_drop=self.max_absolute_drop,
                max_relative_increase=self.max_relative_increase,
                max_absolute_increase=self.max_absolute_increase,
                evaluation_status=self.evaluation_status.value,
                comparability_status=self.comparability_status.value,
                reason_codes=facts.reason_codes,
            )
        )
        if self.decision.value != expected.decision.value:
            raise ValueError("Metric decision does not match the decision helper")
        if self.absolute_delta != expected.absolute_delta:
            raise ValueError("absolute delta is inconsistent with the decision helper")
        if self.relative_delta != expected.relative_delta:
            raise ValueError("relative delta is inconsistent with the decision helper")
        if self.reason != expected.reason:
            raise ValueError("comparison reason is inconsistent with the decision helper")
        return self


def derive_metric_decision(
    item: MetricComparison,
    policy_facts: MetricPolicyFacts | None = None,
):
    facts = derive_metric_evaluation_facts(
        MetricEvaluationInput(
            metric_name=item.metric_name,
            baseline_present=item.baseline_metric_present,
            current_present=item.current_metric_present,
            baseline_value=item.baseline_value,
            current_value=item.current_value,
            baseline_sample_count=item.baseline_sample_count,
            current_sample_count=item.current_sample_count,
            comparability_fact_codes=tuple(item.comparability_fact_codes),
        )
    )
    effective_policy = policy_facts or MetricPolicyFacts(
        metric_name=item.metric_name,
        mode=item.policy_mode.value,
        direction=item.direction.value,
        max_absolute_drop=item.max_absolute_drop,
        max_relative_increase=item.max_relative_increase,
        max_absolute_increase=item.max_absolute_increase,
        requires_pricing_match=item.requires_pricing_match,
    )
    return decide_metric_comparison(
        MetricDecisionInput(
            baseline_value=item.baseline_value,
            current_value=item.current_value,
            direction=effective_policy.direction,
            policy_mode=effective_policy.mode,
            max_absolute_drop=effective_policy.max_absolute_drop,
            max_relative_increase=effective_policy.max_relative_increase,
            max_absolute_increase=effective_policy.max_absolute_increase,
            evaluation_status=facts.evaluation_status.value,
            comparability_status=facts.comparability_status.value,
            reason_codes=facts.reason_codes,
        )
    ).decision


def metric_decision_counts(
    metrics: Sequence[MetricComparison],
    policy_facts: PolicySnapshotFacts | None = None,
) -> dict[str, int]:
    """Count freshly derived decisions, never the serialized decision field."""

    decisions = [
        derive_metric_decision(
            item,
            None if policy_facts is None else resolve_metric_policy(item.metric_name, policy_facts),
        )
        for item in metrics
    ]

    return {
        "regression_count": sum(decision == MetricDecision.REGRESSED.value for decision in decisions),
        "improvement_count": sum(decision == MetricDecision.IMPROVED.value for decision in decisions),
        "unchanged_count": sum(decision == MetricDecision.UNCHANGED.value for decision in decisions),
        "warning_count": sum(decision == MetricDecision.WARNING.value for decision in decisions),
        "not_comparable_count": sum(decision == MetricDecision.NOT_COMPARABLE.value for decision in decisions),
        "not_evaluated_count": sum(decision == MetricDecision.NOT_EVALUATED.value for decision in decisions),
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
    comparability_facts_verified: Literal[False]
    policy_facts_verified: Literal[False]
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
        if self.decision_reason is not None and (
            not self.decision_reason.replace("_", "").isalnum()
            or self.decision_reason.lower() != self.decision_reason
        ):
            raise ValueError("Case decision reason must be a stable reason code")
        return self


def pricing_applicability_fingerprint(
    policy_fingerprint: str,
    suite_metrics: Sequence[MetricComparison],
    case_summaries: Sequence[CaseRegressionSummary],
) -> Sha256Digest:
    """Fingerprint effective pricing facts with policy and scope identity."""

    metrics: list[dict[str, object]] = [
        {
            "scope": "suite",
            "case_id": None,
            "metric_name": item.metric_name,
            "requires_pricing_match": item.requires_pricing_match,
        }
        for item in suite_metrics
    ]
    metrics.extend(
        {
            "scope": "case",
            "case_id": case.case_id,
            "metric_name": item.metric_name,
            "requires_pricing_match": item.requires_pricing_match,
        }
        for case in case_summaries
        for item in case.metrics
    )
    return canonical_sha256(
        {
            "policy_fingerprint": policy_fingerprint,
            "metrics": metrics,
        }
    )


class AuditRegressionReport(ContractModel):
    """Strict comparison facts; deliberately contains no weighted score."""

    model_config = ConfigDict(frozen=True)

    schema_version: RegressionSchemaVersion
    regression_policy: RegressionPolicySnapshot
    regression_policy_fingerprint: Sha256Digest
    policy_facts_verified: Literal[True]
    comparability_facts_verified: Literal[True]
    baseline_id: Identifier
    current_run_id: Identifier
    status: RegressionStatus
    comparability_reasons: list[NonEmptyText] = Field(default_factory=list)
    baseline_suite_id: Identifier
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
    baseline_run_configuration_fingerprint: Sha256Digest
    current_run_configuration_fingerprint: Sha256Digest
    baseline_model_identity: IdentityEvidence
    current_model_identity: IdentityEvidence
    baseline_run_configuration_identity: IdentityEvidence
    current_run_configuration_identity: IdentityEvidence
    baseline_worker_protocol_identity: IdentityEvidence
    current_worker_protocol_identity: IdentityEvidence
    baseline_result_schema_identity: IdentityEvidence
    current_result_schema_identity: IdentityEvidence
    baseline_metric_contract_identity: IdentityEvidence
    current_metric_contract_identity: IdentityEvidence
    baseline_pricing_fingerprint: Sha256Digest | None = None
    current_pricing_fingerprint: Sha256Digest | None = None
    pricing_applicability_fingerprint: Sha256Digest
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
    baseline_result_schema_version: NonEmptyText
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
    comparable_core_metric_count: NonNegativeInt
    comparable_local_metric_count: NonNegativeInt
    overall_regression_gate: StrictBool
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "AuditRegressionReport":
        if any(
            not reason.replace("_", "").isalnum() or reason.lower() != reason
            for reason in self.comparability_reasons
        ):
            raise ValueError("comparability reasons must be stable reason codes")
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
            (
                self.baseline_run_configuration_identity,
                self.baseline_run_configuration_fingerprint,
            ),
            (
                self.current_run_configuration_identity,
                self.current_run_configuration_fingerprint,
            ),
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
        if self.baseline_result_schema_identity.status is IdentityStatus.AVAILABLE and self.baseline_result_schema_identity.value != self.baseline_result_schema_version:
            raise ValueError("baseline Result Schema identity is inconsistent")
        if self.baseline_metric_contract_identity.status is IdentityStatus.AVAILABLE and self.baseline_metric_contract_identity.value != self.baseline_metric_contract_version:
            raise ValueError("baseline metric contract identity is inconsistent")
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
        if self.regression_policy_fingerprint != self.regression_policy.policy_fingerprint:
            raise ValueError("Report policy fingerprint does not match its snapshot")
        all_metrics = [*self.suite_metrics, *(metric for case in self.case_summaries for metric in case.metrics)]
        suite_metric_map = {item.metric_name: item for item in self.suite_metrics}
        missing_runtime = sorted(
            name
            for name in REQUIRED_RUNTIME_CORE_METRIC_NAMES
            if name not in suite_metric_map
            or not suite_metric_map[name].baseline_metric_present
            or not suite_metric_map[name].current_metric_present
            or suite_metric_map[name].baseline_sample_count <= 0
            or suite_metric_map[name].current_sample_count <= 0
            or suite_metric_map[name].baseline_value is None
            or suite_metric_map[name].current_value is None
        )
        if missing_runtime:
            raise ValueError(
                "Regression input is missing required runtime core Metrics: "
                + ", ".join(missing_runtime)
            )
        policy_facts = self.regression_policy.to_facts()
        expected_pricing_applicability = pricing_applicability_fingerprint(
            self.regression_policy_fingerprint,
            self.suite_metrics,
            self.case_summaries,
        )
        if self.pricing_applicability_fingerprint != expected_pricing_applicability:
            raise ValueError("pricing applicability facts are inconsistent")
        expected_reasons = list(
            derive_comparability_reason_codes(
                baseline_suite_id=self.baseline_suite_id,
                current_suite_id=self.suite_id,
                baseline_suite_fingerprint=self.baseline_suite_fingerprint,
                current_suite_fingerprint=self.current_suite_fingerprint,
                baseline_suite_comparison_fingerprint=self.baseline_suite_comparison_fingerprint,
                current_suite_comparison_fingerprint=self.current_suite_comparison_fingerprint,
                baseline_case_ids=tuple(self.baseline_declared_trial_counts_by_case),
                current_case_ids=tuple(self.current_declared_trial_counts_by_case),
                identities=tuple(
                    (
                        name,
                        baseline_identity.status.value,
                        baseline_identity.value,
                        current_identity.status.value,
                        current_identity.value,
                    )
                    for name, baseline_identity, current_identity in (
                        ("model", self.baseline_model_identity, self.current_model_identity),
                        (
                            "run_configuration",
                            self.baseline_run_configuration_identity,
                            self.current_run_configuration_identity,
                        ),
                        ("worker_protocol", self.baseline_worker_protocol_identity, self.current_worker_protocol_identity),
                        ("result_schema", self.baseline_result_schema_identity, self.current_result_schema_identity),
                        ("metric_contract", self.baseline_metric_contract_identity, self.current_metric_contract_identity),
                    )
                ),
                baseline_pricing_fingerprint=self.baseline_pricing_fingerprint,
                current_pricing_fingerprint=self.current_pricing_fingerprint,
                pricing_required=False,
            )
        )
        base_core_reasons = [
            reason
            for reason in expected_reasons
            if reason not in PRICING_REASON_CODES
        ]
        for item in all_metrics:
            expected_policy = resolve_metric_policy(
                item.metric_name,
                policy_facts,
            )
            if item.policy_mode.value != expected_policy.mode:
                raise ValueError("Metric policy mode does not match Report policy snapshot")
            if item.direction.value != expected_policy.direction:
                raise ValueError("Metric policy direction does not match Report policy snapshot")
            if item.max_absolute_drop != expected_policy.max_absolute_drop:
                raise ValueError("Metric absolute threshold does not match Report policy snapshot")
            if item.max_relative_increase != expected_policy.max_relative_increase:
                raise ValueError("Metric relative threshold does not match Report policy snapshot")
            if item.max_absolute_increase != expected_policy.max_absolute_increase:
                raise ValueError("Metric increase threshold does not match Report policy snapshot")
            if item.requires_pricing_match != expected_policy.requires_pricing_match:
                raise ValueError("Metric pricing applicability does not match Report policy snapshot")
            applicable_fact_codes = [
                reason
                for reason in base_core_reasons
                if reason in COMPARABILITY_REASON_CODES
            ]
            pricing_reason = derive_pricing_comparability_reason(
                item.requires_pricing_match,
                self.baseline_pricing_fingerprint,
                self.current_pricing_fingerprint,
            )
            if pricing_reason is not None:
                applicable_fact_codes.append(pricing_reason)
            applicable_fact_codes = sorted(set(applicable_fact_codes))
            expected_facts = derive_metric_evaluation_facts(
                MetricEvaluationInput(
                    metric_name=item.metric_name,
                    baseline_present=item.baseline_metric_present,
                    current_present=item.current_metric_present,
                    baseline_value=item.baseline_value,
                    current_value=item.current_value,
                    baseline_sample_count=item.baseline_sample_count,
                    current_sample_count=item.current_sample_count,
                    comparability_fact_codes=tuple(applicable_fact_codes),
                )
            )
            if item.evaluation_status is not expected_facts.evaluation_status:
                raise ValueError("Metric evaluation status does not match report facts")
            if item.comparability_status is not expected_facts.comparability_status:
                raise ValueError("Metric comparability status does not match report facts")
            if item.comparability_fact_codes != list(expected_facts.comparability_fact_codes):
                raise ValueError("Metric comparability facts do not match report facts")
            if item.reason_codes != list(expected_facts.reason_codes):
                raise ValueError("Metric reason codes do not match report facts")
        expected_counts = metric_decision_counts(all_metrics, policy_facts)
        for name, expected in expected_counts.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} does not match MetricComparison decisions")
        metric_decisions = []
        metric_roles = []
        for item in all_metrics:
            effective_policy = resolve_metric_policy(item.metric_name, policy_facts)
            metric_decisions.append(derive_metric_decision(item, effective_policy))
            metric_roles.append(derive_metric_role(effective_policy))
        comparable_counts = count_comparable_metric_roles(metric_decisions, metric_roles)
        if self.comparable_core_metric_count != comparable_counts.comparable_core_metric_count:
            raise ValueError("comparable core Metric count does not match derived facts")
        if self.comparable_local_metric_count != comparable_counts.comparable_local_metric_count:
            raise ValueError("comparable local Metric count does not match derived facts")
        report_comparability = finalize_report_comparability_reasons(
            expected_reasons,
            [
                reason
                for item in all_metrics
                if (
                    reason := derive_pricing_comparability_reason(
                        item.requires_pricing_match,
                        self.baseline_pricing_fingerprint,
                        self.current_pricing_fingerprint,
                    )
                ) is not None
            ],
            comparable_counts.comparable_core_metric_count,
            comparable_counts.comparable_local_metric_count,
        )
        expected_reasons = list(report_comparability.reasons)
        saved_reasons = list(self.comparability_reasons)
        if len(saved_reasons) != len(set(saved_reasons)):
            raise ValueError("comparability reasons must be unique")
        if sorted(saved_reasons) != expected_reasons:
            raise ValueError("comparability reasons do not match derived facts")
        for case in self.case_summaries:
            expected_case = decide_case_regression(
                [
                    derive_metric_decision(
                        item,
                        resolve_metric_policy(item.metric_name, policy_facts),
                    ).value
                    for item in case.metrics
                ]
            )
            if case.decision.value != expected_case.decision.value:
                raise ValueError("Case decision does not match report-derived facts")
            if case.decision_reason != expected_case.reason:
                raise ValueError("Case decision reason does not match report-derived facts")
        expected_status = decide_report_status(
            regression_count=expected_counts["regression_count"],
            warning_count=expected_counts["warning_count"],
            comparable_core_metric_count=comparable_counts.comparable_core_metric_count,
            core_reason_count=len(report_comparability.core_reasons),
        )
        if self.status.value != expected_status.status or self.overall_regression_gate != expected_status.gate:
            raise ValueError("report status or gate does not match the decision helper")
        if not report_comparability.core_reasons and self.status is RegressionStatus.NOT_COMPARABLE:
            raise ValueError("not-comparable status requires a core reason")
        return self


__all__ = (
    "AuditBaseline",
    "AuditRegressionReport",
    "BASELINE_SCHEMA_VERSION",
    "REQUIRED_RUNTIME_CORE_METRIC_NAMES",
    "BenchmarkCaseSummary",
    "BenchmarkSummary",
    "METRIC_CONTRACT_VERSION",
    "MetricAvailability",
    "MetricComparison",
    "EvaluationStatus",
    "ComparabilityStatus",
    "metric_decision_counts",
    "derive_metric_decision",
    "MetricDecision",
    "MetricDirection",
    "MetricSnapshot",
    "parse_metric_number",
    "pricing_applicability_fingerprint",
    "PolicyMode",
    "RegressionMetricPolicy",
    "RegressionPolicy",
    "RegressionPolicySnapshot",
    "RegressionPolicySnapshotEntry",
    "REGRESSION_POLICY_SCHEMA_VERSION",
    "REGRESSION_SCHEMA_VERSION",
    "RegressionSchemaVersion",
    "RegressionStatus",
    "IdentityEvidence",
    "IdentityStatus",
    "CaseRegressionSummary",
)
