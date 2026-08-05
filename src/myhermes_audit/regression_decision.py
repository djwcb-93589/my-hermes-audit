"""Pure P7 decision functions.

This module intentionally has no Pydantic, CLI, filesystem, network, or Agent
dependency.  The comparison engine and contract validators both call these
functions so a serialized decision cannot disagree with a freshly computed
one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Sequence


class DecisionValue(str, Enum):
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"
    WARNING = "warning"
    NOT_COMPARABLE = "not_comparable"
    NOT_EVALUATED = "not_evaluated"


class EvaluationStatus(str, Enum):
    """Whether both sides contain enough raw facts to evaluate a metric."""

    EVALUATED = "evaluated"
    NOT_EVALUATED = "not_evaluated"


class ComparabilityStatus(str, Enum):
    """Whether an evaluated metric may be compared under the contracts."""

    COMPARABLE = "comparable"
    NOT_COMPARABLE = "not_comparable"


# These codes are deliberately finite.  They are facts rendered for humans,
# never free-form input to a decision function.
EVALUATION_REASON_CODES = frozenset(
    {
        "baseline_metric_missing",
        "current_metric_missing",
        "baseline_sample_empty",
        "current_sample_empty",
        "baseline_value_missing",
        "current_value_missing",
        "baseline_value_invalid",
        "current_value_invalid",
    }
)
COMPARABILITY_REASON_CODES = frozenset(
    {
        "model_identity_missing",
        "model_identity_ambiguous",
        "model_identity_mismatch",
        "configuration_identity_missing",
        "configuration_identity_ambiguous",
        "configuration_identity_mismatch",
        "worker_protocol_identity_missing",
        "worker_protocol_identity_ambiguous",
        "worker_protocol_identity_mismatch",
        "result_schema_identity_missing",
        "result_schema_identity_ambiguous",
        "result_schema_identity_mismatch",
        "metric_contract_identity_missing",
        "metric_contract_identity_ambiguous",
        "metric_contract_identity_mismatch",
        "suite_id_mismatch",
        "suite_fingerprint_mismatch",
        "case_set_or_order_mismatch",
        "pricing_fingerprint_missing",
        "pricing_fingerprint_mismatch",
    }
)
DECISION_REASON_CODES = frozenset(
    {
        "policy_disabled",
        "invalid_policy_mode",
        "invalid_metric_direction",
        "metric_not_evaluated",
        "core_contract_not_comparable",
    }
)
REASON_CODES = EVALUATION_REASON_CODES | COMPARABILITY_REASON_CODES | DECISION_REASON_CODES


@dataclass(frozen=True)
class MetricDecisionInput:
    baseline_value: object = None
    current_value: object = None
    direction: str = "neutral"
    policy_mode: str = "disabled"
    max_absolute_drop: object = None
    max_relative_increase: object = None
    max_absolute_increase: object = None
    evaluation_status: str = EvaluationStatus.EVALUATED.value
    comparability_status: str = ComparabilityStatus.COMPARABLE.value
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricDecisionResult:
    decision: DecisionValue
    absolute_delta: int | float | Decimal | None = None
    relative_delta: int | float | Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CaseDecisionResult:
    decision: DecisionValue
    reason: str | None = None


@dataclass(frozen=True)
class ReportDecisionResult:
    status: str
    gate: bool
    reason: str | None = None


@dataclass(frozen=True)
class MetricEvaluationInput:
    """Raw metric facts and independently computed contract facts."""

    metric_name: str
    baseline_present: bool
    current_present: bool
    baseline_value: object
    current_value: object
    baseline_sample_count: int
    current_sample_count: int
    comparability_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricEvaluationFacts:
    evaluation_status: EvaluationStatus
    comparability_status: ComparabilityStatus
    reason_codes: tuple[str, ...]
    delta_allowed: bool


def derive_metric_evaluation_facts(
    inputs: MetricEvaluationInput,
) -> MetricEvaluationFacts:
    """Derive status/reasons only from raw facts, never a saved decision."""

    reasons: list[str] = []
    for side, present, value, samples in (
        (
            "baseline",
            inputs.baseline_present,
            inputs.baseline_value,
            inputs.baseline_sample_count,
        ),
        (
            "current",
            inputs.current_present,
            inputs.current_value,
            inputs.current_sample_count,
        ),
    ):
        if not present:
            reasons.append(f"{side}_metric_missing")
        elif samples <= 0:
            reasons.append(f"{side}_sample_empty")
        elif value is None:
            reasons.append(f"{side}_value_missing")
        elif _finite_number(value) is None:
            reasons.append(f"{side}_value_invalid")

    evaluation_status = (
        EvaluationStatus.NOT_EVALUATED
        if reasons
        else EvaluationStatus.EVALUATED
    )
    structural_reasons = tuple(
        sorted(
            reason
            for reason in inputs.comparability_reasons
            if reason in COMPARABILITY_REASON_CODES
        )
    )
    all_reasons = tuple(sorted(set(reasons).union(structural_reasons)))
    comparable = evaluation_status is EvaluationStatus.EVALUATED and not structural_reasons
    return MetricEvaluationFacts(
        evaluation_status=evaluation_status,
        comparability_status=(
            ComparabilityStatus.COMPARABLE
            if comparable
            else ComparabilityStatus.NOT_COMPARABLE
        ),
        reason_codes=all_reasons,
        delta_allowed=comparable,
    )


def _finite_number(value: object) -> int | float | Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    return None


def _decimal_pair(
    baseline: int | float | Decimal,
    current: int | float | Decimal,
) -> tuple[Decimal, Decimal]:
    def as_decimal(value: int | float | Decimal) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    return as_decimal(baseline), as_decimal(current)


def _threshold_exceeded(
    delta: int | float | Decimal,
    relative: int | float | Decimal | None,
    *,
    direction: str,
    max_absolute_drop: object,
    max_relative_increase: object,
    max_absolute_increase: object,
) -> bool:
    absolute_drop = _finite_number(max_absolute_drop)
    relative_increase = _finite_number(max_relative_increase)
    absolute_increase = _finite_number(max_absolute_increase)
    decimal_semantics = isinstance(delta, Decimal) or isinstance(relative, Decimal)

    def comparable_threshold(value: int | float | Decimal | None):
        if value is None:
            return None
        return Decimal(str(value)) if decimal_semantics else float(value)

    absolute_drop = comparable_threshold(absolute_drop)
    relative_increase = comparable_threshold(relative_increase)
    absolute_increase = comparable_threshold(absolute_increase)
    if direction == "higher_is_better" and delta < 0:
        return absolute_drop is not None and -delta > absolute_drop
    if direction == "lower_is_better" and delta > 0:
        absolute = absolute_increase is not None and delta > absolute_increase
        relative_adverse = (
            relative_increase is not None
            and relative is not None
            and relative > relative_increase
        )
        return absolute or relative_adverse
    return False


def decide_metric_comparison(inputs: MetricDecisionInput) -> MetricDecisionResult:
    """Derive one Metric decision and its deltas from immutable input facts."""

    reason = inputs.reason_codes[0] if inputs.reason_codes else None
    if inputs.evaluation_status != EvaluationStatus.EVALUATED.value:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_EVALUATED,
            reason=reason or "metric_not_evaluated",
        )
    if inputs.comparability_status != ComparabilityStatus.COMPARABLE.value:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_COMPARABLE,
            reason=reason or "core_contract_not_comparable",
        )

    baseline = _finite_number(inputs.baseline_value)
    current = _finite_number(inputs.current_value)
    if baseline is None or current is None:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_EVALUATED,
            reason=reason or "metric_not_evaluated",
        )

    if isinstance(baseline, Decimal) or isinstance(current, Decimal):
        baseline_decimal, current_decimal = _decimal_pair(baseline, current)
        delta: int | float | Decimal = current_decimal - baseline_decimal
        relative: int | float | Decimal | None = (
            None
            if baseline_decimal == 0
            else delta / abs(baseline_decimal)
        )
    else:
        delta = current - baseline
        relative = None if baseline == 0 else delta / abs(baseline)

    mode = inputs.policy_mode
    direction = inputs.direction
    if mode not in {"disabled", "warning", "failure"}:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_COMPARABLE,
            reason="invalid_policy_mode",
        )
    if mode != "disabled" and direction not in {"higher_is_better", "lower_is_better"}:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_COMPARABLE,
            reason="invalid_metric_direction",
        )

    if mode == "disabled":
        if direction == "higher_is_better" and delta > 0:
            decision = DecisionValue.IMPROVED
        elif direction == "lower_is_better" and delta < 0:
            decision = DecisionValue.IMPROVED
        else:
            decision = DecisionValue.UNCHANGED
        return MetricDecisionResult(
            decision=decision,
            absolute_delta=delta,
            relative_delta=relative,
            reason="policy_disabled",
        )

    adverse = _threshold_exceeded(
        delta,
        relative,
        direction=direction,
        max_absolute_drop=inputs.max_absolute_drop,
        max_relative_increase=inputs.max_relative_increase,
        max_absolute_increase=inputs.max_absolute_increase,
    )
    if adverse:
        decision = (
            DecisionValue.REGRESSED
            if mode == "failure"
            else DecisionValue.WARNING
        )
    elif (direction == "higher_is_better" and delta > 0) or (
        direction == "lower_is_better" and delta < 0
    ):
        decision = DecisionValue.IMPROVED
    else:
        decision = DecisionValue.UNCHANGED
    return MetricDecisionResult(
        decision=decision,
        absolute_delta=delta,
        relative_delta=relative,
    )


def decide_case_regression(decisions: Sequence[str]) -> CaseDecisionResult:
    """Aggregate Metric decisions using one deterministic precedence order."""

    values = {
        item.value if isinstance(item, Enum) else str(item)
        for item in decisions
    }
    if DecisionValue.REGRESSED.value in values:
        return CaseDecisionResult(DecisionValue.REGRESSED)
    if DecisionValue.WARNING.value in values:
        return CaseDecisionResult(DecisionValue.WARNING)
    if DecisionValue.IMPROVED.value in values:
        return CaseDecisionResult(DecisionValue.IMPROVED)
    if DecisionValue.UNCHANGED.value in values or not values:
        return CaseDecisionResult(DecisionValue.UNCHANGED)
    if values == {DecisionValue.NOT_COMPARABLE.value}:
        return CaseDecisionResult(
            DecisionValue.NOT_COMPARABLE,
            "all_metrics_not_comparable",
        )
    if values == {DecisionValue.NOT_EVALUATED.value}:
        return CaseDecisionResult(
            DecisionValue.NOT_EVALUATED,
            "all_metrics_not_evaluated",
        )
    if DecisionValue.NOT_COMPARABLE.value in values:
        return CaseDecisionResult(
            DecisionValue.NOT_COMPARABLE,
            "mixed_not_comparable_and_not_evaluated",
        )
    if DecisionValue.NOT_EVALUATED.value in values:
        return CaseDecisionResult(
            DecisionValue.NOT_EVALUATED,
            "mixed_not_evaluated",
        )
    return CaseDecisionResult(DecisionValue.UNCHANGED)


def derive_comparability_reason_codes(
    *,
    baseline_suite_id: str,
    current_suite_id: str,
    baseline_suite_fingerprint: str,
    current_suite_fingerprint: str,
    baseline_suite_comparison_fingerprint: str | None,
    current_suite_comparison_fingerprint: str | None,
    baseline_case_ids: Sequence[str],
    current_case_ids: Sequence[str],
    identities: Sequence[tuple[str, str, str | None, str, str | None]],
    baseline_pricing_fingerprint: str | None,
    current_pricing_fingerprint: str | None,
) -> tuple[str, ...]:
    """Recompute report comparability reasons from immutable metadata facts."""

    reasons: list[str] = []
    if baseline_suite_id != current_suite_id:
        reasons.append("suite_id_mismatch")
    if baseline_suite_comparison_fingerprint is None or current_suite_comparison_fingerprint is None:
        if baseline_suite_fingerprint != current_suite_fingerprint:
            reasons.append("suite_fingerprint_mismatch")
    elif baseline_suite_comparison_fingerprint != current_suite_comparison_fingerprint:
        reasons.append("suite_fingerprint_mismatch")
    if tuple(baseline_case_ids) != tuple(current_case_ids):
        reasons.append("case_set_or_order_mismatch")
    for name, baseline_status, baseline_value, current_status, current_value in identities:
        if "ambiguous" in {baseline_status, current_status}:
            reasons.append(f"{name}_identity_ambiguous")
        elif "missing" in {baseline_status, current_status}:
            reasons.append(f"{name}_identity_missing")
        elif baseline_value != current_value:
            reasons.append(f"{name}_identity_mismatch")
    if (baseline_pricing_fingerprint is None) != (current_pricing_fingerprint is None):
        reasons.append("pricing_fingerprint_missing")
    elif (
        baseline_pricing_fingerprint is not None
        and baseline_pricing_fingerprint != current_pricing_fingerprint
    ):
        reasons.append("pricing_fingerprint_mismatch")
    return tuple(sorted(set(reasons)))


def decide_report_status(
    *,
    regression_count: int,
    warning_count: int,
    comparable_metric_count: int,
    core_reason_count: int,
    invalid_input: bool = False,
    warning_fails_gate: bool = False,
) -> ReportDecisionResult:
    """Derive report status and gate from counts, not persisted conclusions."""

    if invalid_input:
        return ReportDecisionResult("invalid_input", False, "invalid_input")
    if core_reason_count or comparable_metric_count == 0:
        return ReportDecisionResult(
            "not_comparable",
            False,
            "core_comparability_failure",
        )
    if regression_count > 0:
        return ReportDecisionResult("regressed", False, "hard_regression")
    if warning_count > 0:
        return ReportDecisionResult(
            "passed_with_warnings",
            not warning_fails_gate,
            "warning_observed",
        )
    return ReportDecisionResult("passed", True)


__all__ = (
    "CaseDecisionResult",
    "COMPARABILITY_REASON_CODES",
    "ComparabilityStatus",
    "DecisionValue",
    "DECISION_REASON_CODES",
    "EVALUATION_REASON_CODES",
    "EvaluationStatus",
    "MetricEvaluationFacts",
    "MetricEvaluationInput",
    "MetricDecisionInput",
    "MetricDecisionResult",
    "REASON_CODES",
    "ReportDecisionResult",
    "decide_case_regression",
    "decide_metric_comparison",
    "decide_report_status",
    "derive_comparability_reason_codes",
    "derive_metric_evaluation_facts",
)
