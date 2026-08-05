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


@dataclass(frozen=True)
class MetricDecisionInput:
    baseline_value: object = None
    current_value: object = None
    direction: str = "neutral"
    policy_mode: str = "disabled"
    max_absolute_drop: object = None
    max_relative_increase: object = None
    max_absolute_increase: object = None
    comparable: bool = True
    sufficient_samples: bool = True
    reason: str | None = None


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

    if not inputs.comparable:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_COMPARABLE,
            reason=inputs.reason or "core_contract_not_comparable",
        )

    baseline = _finite_number(inputs.baseline_value)
    current = _finite_number(inputs.current_value)
    if not inputs.sufficient_samples or baseline is None or current is None:
        return MetricDecisionResult(
            decision=DecisionValue.NOT_EVALUATED,
            reason=inputs.reason or "metric_not_evaluated",
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
    "DecisionValue",
    "MetricDecisionInput",
    "MetricDecisionResult",
    "ReportDecisionResult",
    "decide_case_regression",
    "decide_metric_comparison",
    "decide_report_status",
)
