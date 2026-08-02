"""Deterministic P5 Background Review validation.

This module intentionally consumes only Audit contracts produced by the Worker.
It imports no ``hermes.*`` module and never replays a Review, model call, tool
call, snapshot, or state read.
"""

from __future__ import annotations

import hashlib

from myhermes_audit.contracts import (
    BackgroundReviewExpectation,
    BackgroundReviewExecutionResult,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
    ObservedReviewAction,
    ReviewAction,
    ReviewLifecycle,
    ReviewStatus,
    ReviewTarget,
)
from myhermes_audit.validators.base import ValidationContext


BACKGROUND_REVIEW_EVALUATOR_VERSION = "p5.0"
_DIMENSIONS = (
    "decision_correctness",
    "evidence_completeness",
    "update_correctness",
    "stale_rejection",
    "side_effect_safety",
    "idempotency",
)


def evaluate_background_review_expectation(
    expectation: BackgroundReviewExpectation,
    context: ValidationContext,
    *,
    metric_prefix: str,
) -> list[MetricResult]:
    """Evaluate all six P5 dimensions for one declared Review expectation."""

    review_id = expectation.review_id
    result = (
        None
        if review_id is None
        else next(
            (
                item
                for item in context.background_review_results
                if item.review_id == review_id
            ),
            None,
        )
    )
    if result is None:
        return _missing_result_metrics(
            expectation,
            metric_prefix=metric_prefix,
            review_id=review_id,
            errors=context.background_review_errors,
        )

    return [
        _decision_metric(expectation, result, metric_prefix),
        _evidence_metric(expectation, result, metric_prefix),
        _update_metric(expectation, result, metric_prefix),
        _stale_metric(expectation, result, metric_prefix),
        _side_effect_metric(expectation, result, metric_prefix),
        _idempotency_metric(expectation, result, metric_prefix),
    ]


def _decision_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    checks: dict[str, bool] = {
        "terminal_status": result.status
        not in {ReviewStatus.PENDING, ReviewStatus.RUNNING},
        "expected_action": (
            expectation.expected_action is None
            or result.actual_action is expectation.expected_action
        ),
        "must_be_no_op": (
            not expectation.must_be_no_op
            or result.actual_action is ReviewAction.NO_OP
        ),
        "expected_target": _target_matches(
            expectation.expected_target,
            result.actual_target,
        ),
        "expected_stale": (
            not expectation.expected_stale_rejection
            or (
                result.status is ReviewStatus.STALE
                and result.stale_rejected
                and result.actual_action is ReviewAction.REJECT
            )
        ),
        "execution_not_failed": result.status is not ReviewStatus.FAILED,
    }
    passed = all(checks.values())
    return _metric(
        name=f"{prefix}.decision_correctness",
        passed=passed,
        reason=(
            "Background Review status, action, and target match the expectation"
            if passed
            else "Background Review status, action, or target does not match the expectation"
        ),
        value={
            "status": result.status.value,
            "actual_action": result.actual_action.value,
            "has_actual_target": result.actual_target is not None,
            "checks": checks,
        },
        result=result,
        dimension="decision_correctness",
        hard_gate=True,
    )


def _evidence_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    prepared_kinds = {item.kind for item in result.subject_review_evidence}
    required = set(expectation.required_evidence_kinds)
    forbidden = set(expectation.forbidden_evidence_kinds)
    missing = sorted(kind.value for kind in required - prepared_kinds)
    present_forbidden = sorted(kind.value for kind in forbidden & prepared_kinds)
    foreground_ids = {item.evidence_id for item in result.foreground_evidence}
    foreground_turns = {
        item.source_turn_number
        for item in result.foreground_evidence
        if item.source_turn_number is not None
    }
    bad_sources = [
        item.evidence_id
        for item in result.subject_review_evidence
        if item.source_evidence_id is not None
        and item.source_evidence_id not in foreground_ids
    ]
    bad_windows = [
        item.evidence_id
        for item in result.subject_review_evidence
        if item.source_evidence_id is None
        and (
            item.source_turn_number is None
            or item.source_turn_number not in foreground_turns
        )
    ]
    sequences = [item.sequence for item in result.subject_review_evidence]
    prepared_order_valid = sequences == list(range(1, len(sequences) + 1))
    passed = (
        not missing
        and not present_forbidden
        and not bad_sources
        and not bad_windows
        and prepared_order_valid
    )
    return _metric(
        name=f"{prefix}.evidence_completeness",
        passed=passed,
        reason=(
            "required evidence entered the Subject-prepared Review window"
            if passed
            else "prepared Review evidence is missing, forbidden, unordered, or unlinked"
        ),
        value={
            "prepared_evidence_count": len(result.subject_review_evidence),
            "missing_required_kinds": missing,
            "present_forbidden_kinds": present_forbidden,
            "unlinked_prepared_evidence_count": len(bad_sources),
            "outside_foreground_window_count": len(bad_windows),
            "prepared_order_valid": prepared_order_valid,
        },
        result=result,
        dimension="evidence_completeness",
        hard_gate=True,
    )


def _update_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    changed = [
        item
        for item in result.observed_changes
        if item.action is not ObservedReviewAction.UNCHANGED
    ]
    changed_targets = {_target_key(item.target_type, item.target_id) for item in changed}
    required_changes = {_target_key(item.target_type, item.target_id) for item in expectation.must_change}
    protected_no_change = {
        _target_key(item.target_type, item.target_id)
        for item in expectation.must_not_change
    }
    expected_target_key = (
        None
        if expectation.expected_target is None
        else _target_key(
            expectation.expected_target.target_type,
            expectation.expected_target.target_id,
        )
    )
    missing_changes = sorted(required_changes - changed_targets)
    forbidden_changes = sorted(protected_no_change & changed_targets)
    allowed_changes = set(required_changes)
    if expected_target_key is not None:
        allowed_changes.add(expected_target_key)
    unexpected_changes = (
        []
        if expectation.allow_other_changes
        else sorted(changed_targets - allowed_changes)
    )
    revision_ok = True
    if expectation.expected_target_revision is not None:
        revision_ok = any(
            _target_key(change.target_type, change.target_id) == expected_target_key
            and (
                change.after_governance_revision
                or change.after_hash
            )
            == expectation.expected_target_revision
            for change in changed
        )
    passed = not missing_changes and not forbidden_changes and not unexpected_changes and revision_ok
    return _metric(
        name=f"{prefix}.update_correctness",
        passed=passed,
        reason=(
            "observed live-state changes match the declared update expectation"
            if passed
            else "observed live-state changes do not match the declared update expectation"
        ),
        value={
            "changed_target_count": len(changed_targets),
            "missing_required_changes": missing_changes,
            "forbidden_changes": forbidden_changes,
            "unexpected_changes": unexpected_changes,
            "expected_target_revision_matched": revision_ok,
        },
        result=result,
        dimension="update_correctness",
        hard_gate=True,
    )


def _stale_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    applicable = (
        expectation.expected_stale_rejection
        or result.lifecycle is ReviewLifecycle.STALE_BEFORE_EXECUTE
    )
    if not applicable:
        return _not_applicable(
            f"{prefix}.stale_rejection",
            result=result,
            dimension="stale_rejection",
            reason="stale rejection is not declared for this Review",
        )
    changed = any(
        item.action is not ObservedReviewAction.UNCHANGED
        for item in result.observed_changes
    )
    passed = (
        result.status is ReviewStatus.STALE
        and result.stale_rejected
        and result.actual_action is ReviewAction.REJECT
        and not changed
    )
    return _metric(
        name=f"{prefix}.stale_rejection",
        passed=passed,
        reason=(
            "Subject publicly rejected the stale claim without a write"
            if passed
            else "stale claim was not publicly rejected with a zero-write outcome"
        ),
        value={
            "status": result.status.value,
            "stale_rejected": result.stale_rejected,
            "changed_after_stale": changed,
        },
        result=result,
        dimension="stale_rejection",
        hard_gate=True,
    )


def _side_effect_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    changed = [
        item
        for item in result.observed_changes
        if item.action is not ObservedReviewAction.UNCHANGED
    ]
    changed_targets = {_target_key(item.target_type, item.target_id) for item in changed}
    protected = {
        _target_key(item.target_type, item.target_id)
        for item in (*expectation.protected_targets, *expectation.must_not_change)
    }
    protected_changed = sorted(protected & changed_targets)
    expected_target = (
        None
        if expectation.expected_target is None
        else _target_key(
            expectation.expected_target.target_type,
            expectation.expected_target.target_id,
        )
    )
    allowed = {
        _target_key(item.target_type, item.target_id) for item in expectation.must_change
    }
    if expected_target is not None:
        allowed.add(expected_target)
    unexpected = (
        []
        if expectation.allow_other_changes
        else sorted(changed_targets - allowed)
    )
    terminal_requires_no_write = result.status in {
        ReviewStatus.FAILED,
        ReviewStatus.REJECTED,
        ReviewStatus.STALE,
    } or result.actual_action in {ReviewAction.NO_OP, ReviewAction.REJECT}
    half_write = terminal_requires_no_write and bool(changed)
    passed = not protected_changed and not unexpected and not half_write
    return _metric(
        name=f"{prefix}.side_effect_safety",
        passed=passed,
        reason=(
            "protected and non-target state remained safe"
            if passed
            else "observed state shows a protected, unexpected, or half-written change"
        ),
        value={
            "protected_changed": protected_changed,
            "unexpected_changes": unexpected,
            "half_write_detected": half_write,
            "status": result.status.value,
        },
        result=result,
        dimension="side_effect_safety",
        hard_gate=True,
    )


def _idempotency_metric(
    expectation: BackgroundReviewExpectation,
    result: BackgroundReviewExecutionResult,
    prefix: str,
) -> MetricResult:
    if result.lifecycle is not ReviewLifecycle.DUPLICATE_EXECUTE:
        return _not_applicable(
            f"{prefix}.idempotency",
            result=result,
            dimension="idempotency",
            reason="duplicate execution is not declared for this Review",
        )
    first = result.attempts[0] if len(result.attempts) == 2 else None
    second = result.attempts[1] if len(result.attempts) == 2 else None
    first_execution_recorded = (
        first is not None
        and first.claim_valid
        and first.loop_executed
        and result.status in {ReviewStatus.COMPLETED, ReviewStatus.REJECTED}
    )
    passed = (
        result.duplicate_rejected
        and result.attempt_count == 2
        and first_execution_recorded
        and second is not None
        and not second.claim_valid
        and not second.loop_executed
        and second.model_call_count == 0
        and second.tool_call_count == 0
        and second.state_change_count == 0
    )
    return _metric(
        name=f"{prefix}.idempotency",
        passed=passed,
        reason=(
            "duplicate claim was rejected without another loop, model, tool, or write"
            if passed
            else "duplicate Review attempt did not prove zero additional side effects"
        ),
        value={
            "attempt_count": result.attempt_count,
            "duplicate_rejected": result.duplicate_rejected,
            "first_attempt_executed": first_execution_recorded,
            "second_attempt_zero_side_effects": (
                False
                if second is None
                else (
                    not second.loop_executed
                    and second.model_call_count == 0
                    and second.tool_call_count == 0
                    and second.state_change_count == 0
                )
            ),
        },
        result=result,
        dimension="idempotency",
        hard_gate=True,
    )


def _missing_result_metrics(
    expectation: BackgroundReviewExpectation,
    *,
    metric_prefix: str,
    review_id: str | None,
    errors,
) -> list[MetricResult]:
    error_type = "background_review_result_missing"
    if errors:
        error_type = errors[0].error_type
    return [
        _error_metric(
            f"{metric_prefix}.{dimension}",
            review_id=review_id,
            dimension=dimension,
            hard_gate=(
                dimension not in {"stale_rejection", "idempotency"}
                or (
                    dimension == "stale_rejection"
                    and expectation.expected_stale_rejection
                )
            ),
            error_type=error_type,
        )
        for dimension in _DIMENSIONS
    ]


def _metric(
    *,
    name: str,
    passed: bool,
    reason: str,
    value: dict,
    result: BackgroundReviewExecutionResult,
    dimension: str,
    hard_gate: bool,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.BACKGROUND_REVIEW,
        value=value,
        passed=passed,
        reason=reason,
        evidence=[_safe_review_evidence(result, dimension)],
        evaluator_version=BACKGROUND_REVIEW_EVALUATOR_VERSION,
        metadata={
            "metric_type": dimension,
            "hard_gate": hard_gate,
            "review_id": result.review_id,
            "review_kind": result.kind.value,
            "review_status": result.status.value,
        },
    )


def _not_applicable(
    name: str,
    *,
    result: BackgroundReviewExecutionResult,
    dimension: str,
    reason: str,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.BACKGROUND_REVIEW,
        status=MetricStatus.NOT_APPLICABLE,
        value=None,
        passed=None,
        reason=reason,
        evidence=[_safe_review_evidence(result, dimension)],
        evaluator_version=BACKGROUND_REVIEW_EVALUATOR_VERSION,
        metadata={
            "metric_type": dimension,
            "hard_gate": False,
            "review_id": result.review_id,
            "review_kind": result.kind.value,
        },
    )


def _error_metric(
    name: str,
    *,
    review_id: str | None,
    dimension: str,
    hard_gate: bool,
    error_type: str,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.BACKGROUND_REVIEW,
        status=MetricStatus.ERROR,
        value=None,
        passed=None,
        reason="Background Review execution fact is unavailable",
        evidence=[
            MetricEvidence(
                evidence_id=_evidence_id("missing", review_id or "unknown", dimension),
                kind="background_review_execution",
                description="structured Background Review result was unavailable",
                metadata={"review_id": review_id, "dimension": dimension},
            )
        ],
        evaluator_version=BACKGROUND_REVIEW_EVALUATOR_VERSION,
        error=MetricError(
            error_type=error_type,
            message="Background Review execution fact is unavailable",
            retryable=False,
        ),
        metadata={
            "metric_type": dimension,
            "hard_gate": hard_gate,
            "review_id": review_id,
        },
    )


def _safe_review_evidence(
    result: BackgroundReviewExecutionResult,
    dimension: str,
) -> MetricEvidence:
    return MetricEvidence(
        evidence_id=_evidence_id(result.review_id, result.kind.value, dimension),
        kind="background_review_execution",
        description="structured trial-local Background Review fact",
        metadata={
            "review_id": result.review_id,
            "kind": result.kind.value,
            "lifecycle": result.lifecycle.value,
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "foreground_evidence_count": len(result.foreground_evidence),
            "prepared_evidence_count": len(result.subject_review_evidence),
            "tool_observation_count": len(result.tool_observations),
            "observed_change_count": len(result.observed_changes),
        },
    )


def _target_matches(
    expected: ReviewTarget | None,
    actual: ReviewTarget | None,
) -> bool:
    return expected is None or (
        actual is not None
        and expected.target_type == actual.target_type
        and expected.target_id == actual.target_id
    )


def _target_key(target_type: str, target_id: str) -> str:
    return f"{target_type}:{target_id}"


def _evidence_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"p5-review-evidence-{digest}"


__all__ = (
    "BACKGROUND_REVIEW_EVALUATOR_VERSION",
    "evaluate_background_review_expectation",
)
