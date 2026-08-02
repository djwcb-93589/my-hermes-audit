"""Deterministic retrieval and Memory-state evaluation."""

from __future__ import annotations

import unicodedata
import hashlib

from myhermes_audit.contracts import (
    MemoryContentExpectation,
    MemoryContentMatchMode,
    MemoryExpectation,
    MemoryOperationError,
    MemorySnapshotPhase,
    MemoryStateChange,
    MemoryStateChangeType,
    MemoryStateExpectation,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
)
from myhermes_audit.validators.base import ValidationContext


MEMORY_EVALUATOR_VERSION = "p3.0"


def evaluate_memory_expectation(
    expectation: MemoryExpectation,
    context: ValidationContext,
    *,
    metric_prefix: str,
) -> list[MetricResult]:
    result = next(
        (
            item
            for item in context.memory_query_results
            if item.query_id == expectation.query_id
        ),
        None,
    )
    if result is None:
        operation_error = next(
            (
                item
                for item in context.memory_errors
                if item.query_id == expectation.query_id
            ),
            None,
        )
        if operation_error is None and context.memory_errors:
            operation_error = context.memory_errors[0]
        return [
            _memory_error_metric(
                name=f"{metric_prefix}.required_evidence",
                operation_error=operation_error,
                fallback_type="memory_query_error",
                query_id=expectation.query_id,
            )
        ]

    retrieved_ids = [item.memory_id for item in result.items]
    retrieved_id_set = set(retrieved_ids)
    required_ids = list(expectation.required_memory_ids)
    found_required = [item for item in required_ids if item in retrieved_id_set]
    missing_required = [item for item in required_ids if item not in retrieved_id_set]
    found_forbidden = [
        item
        for item in expectation.forbidden_memory_ids
        if item in retrieved_id_set
    ]
    matched_kinds = list(dict.fromkeys(item.kind.value for item in result.items))
    missing_kinds = [
        kind.value
        for kind in expectation.required_kinds
        if kind.value not in matched_kinds
    ]
    match_count = len(found_required)
    required_evidence_found = (
        match_count >= expectation.minimum_matches
        and not missing_kinds
        and not found_forbidden
    )
    evidence_metric = MetricResult(
        metric_name=f"{metric_prefix}.required_evidence",
        source=MetricSource.RETRIEVAL,
        value={
            "query_id": expectation.query_id,
            "found_required_ids": found_required,
            "missing_required_ids": missing_required,
            "found_forbidden_ids": found_forbidden,
            "matched_kinds": matched_kinds,
            "missing_required_kinds": missing_kinds,
            "match_count": match_count,
            "minimum_matches": expectation.minimum_matches,
            "required_evidence_found": required_evidence_found,
        },
        passed=required_evidence_found,
        reason=(
            "required Memory exposure gate passed"
            if required_evidence_found
            else "required Memory exposure gate failed"
        ),
        evidence=[
            MetricEvidence(
                evidence_id=_stable_evidence_id("memory-query", expectation.query_id),
                kind="memory_query_result",
                description="structured Memory query evidence",
                metadata={
                    "query_id": expectation.query_id,
                    "phase": result.phase.value,
                    "provider": result.provider,
                    "strategy": result.strategy.value,
                    "retrieved_item_count": len(result.items),
                    "duration_ms": result.duration_ms,
                },
            )
        ],
        evaluator_version=MEMORY_EVALUATOR_VERSION,
        metadata={
            "metric_type": "required_evidence",
            "query_id": expectation.query_id,
            "hard_gate": True,
        },
    )

    if required_ids:
        recall = len(found_required) / len(required_ids)
        recall_passed = (
            None
            if expectation.minimum_recall_at_k is None
            else recall >= expectation.minimum_recall_at_k
        )
        recall_metric = MetricResult(
            metric_name=f"{metric_prefix}.recall_at_k",
            source=MetricSource.RETRIEVAL,
            value=float(recall),
            passed=recall_passed,
            reason=f"Recall@{expectation.query.top_k} diagnostic",
            evaluator_version=MEMORY_EVALUATOR_VERSION,
            metadata={
                "metric_type": "recall_at_k",
                "query_id": expectation.query_id,
                "k": expectation.query.top_k,
                "required_count": len(required_ids),
                "hit_count": len(found_required),
                "threshold": expectation.minimum_recall_at_k,
                "hard_gate": expectation.minimum_recall_at_k is not None,
            },
        )
        required_ranks = [
            item.rank for item in result.items if item.memory_id in set(required_ids)
        ]
        mrr = 0.0 if not required_ranks else 1.0 / min(required_ranks)
        mrr_passed = (
            None
            if expectation.minimum_mrr is None
            else mrr >= expectation.minimum_mrr
        )
        mrr_metric = MetricResult(
            metric_name=f"{metric_prefix}.mrr",
            source=MetricSource.RETRIEVAL,
            value=float(mrr),
            passed=mrr_passed,
            reason="reciprocal rank of the first required Memory",
            evaluator_version=MEMORY_EVALUATOR_VERSION,
            metadata={
                "metric_type": "mrr",
                "query_id": expectation.query_id,
                "threshold": expectation.minimum_mrr,
                "hard_gate": expectation.minimum_mrr is not None,
            },
        )
    else:
        recall_metric = _not_applicable_metric(
            f"{metric_prefix}.recall_at_k",
            "Recall@K is not applicable without required Memory IDs",
            metric_type="recall_at_k",
            identity_name="query_id",
            identity=expectation.query_id,
        )
        mrr_metric = _not_applicable_metric(
            f"{metric_prefix}.mrr",
            "MRR is not applicable without required Memory IDs",
            metric_type="mrr",
            identity_name="query_id",
            identity=expectation.query_id,
        )
    return [evidence_metric, recall_metric, mrr_metric]


def evaluate_memory_state_expectation(
    expectation: MemoryStateExpectation,
    context: ValidationContext,
    *,
    metric_name: str,
) -> MetricResult:
    state_error = next(
        (
            item
            for item in context.memory_errors
            if item.error_type.value == "memory_state_validation_error"
        ),
        None,
    )
    if state_error is not None:
        return _memory_error_metric(
            name=metric_name,
            operation_error=state_error,
            fallback_type="memory_state_validation_error",
            state_id=expectation.state_id,
        )
    before = next(
        (
            item
            for item in context.memory_snapshots
            if item.phase is MemorySnapshotPhase.BEFORE_CONVERSATION
        ),
        None,
    )
    after = next(
        (
            item
            for item in context.memory_snapshots
            if item.phase is MemorySnapshotPhase.AFTER_CONVERSATION
        ),
        None,
    )
    if before is None or after is None:
        operation_error = next(
            (
                item
                for item in context.memory_errors
                if item.error_type.value == "memory_snapshot_error"
            ),
            None,
        )
        if operation_error is None and context.memory_errors:
            operation_error = context.memory_errors[0]
        return _memory_error_metric(
            name=metric_name,
            operation_error=operation_error,
            fallback_type="memory_snapshot_error",
            state_id=expectation.state_id,
        )

    before_by_id = {item.memory_id: item for item in before.items}
    after_by_id = {item.memory_id: item for item in after.items}
    change_by_id = {item.memory_id: item for item in context.memory_state_changes}
    missing_present = [
        memory_id
        for memory_id in expectation.required_present_memory_ids
        if memory_id not in after_by_id
    ]
    still_present = [
        memory_id
        for memory_id in expectation.required_absent_memory_ids
        if memory_id in after_by_id
    ]
    missing_removed = [
        memory_id
        for memory_id in expectation.required_removed_memory_ids
        if memory_id not in before_by_id or memory_id in after_by_id
    ]
    changed_unchanged = [
        memory_id
        for memory_id in expectation.unchanged_memory_ids
        if (
            memory_id not in change_by_id
            or change_by_id[memory_id].change_type
            is not MemoryStateChangeType.UNCHANGED
        )
    ]
    added_changes = [
        item
        for item in context.memory_state_changes
        if item.change_type is MemoryStateChangeType.ADDED and item.after is not None
    ]
    missing_added_content = [
        index
        for index, content_expectation in enumerate(
            expectation.required_added_content,
            start=1,
        )
        if not any(
            _content_matches(change, content_expectation)
            for change in added_changes
        )
    ]
    forbidden_added_content = [
        index
        for index, content_expectation in enumerate(
            expectation.forbidden_added_content,
            start=1,
        )
        if any(
            _content_matches(change, content_expectation)
            for change in added_changes
        )
    ]
    unexpected_changes = (
        []
        if expectation.allow_other_changes
        else [
            change.memory_id
            for change in context.memory_state_changes
            if change.change_type is not MemoryStateChangeType.UNCHANGED
            and not _change_is_declared(change, expectation)
        ]
    )
    passed = not any(
        (
            missing_present,
            still_present,
            missing_removed,
            changed_unchanged,
            missing_added_content,
            forbidden_added_content,
            unexpected_changes,
        )
    )
    return MetricResult(
        metric_name=metric_name,
        source=MetricSource.RETRIEVAL,
        value={
            "state_id": expectation.state_id,
            "required_present_missing": missing_present,
            "required_absent_still_present": still_present,
            "required_removed_missing": missing_removed,
            "required_unchanged_changed": changed_unchanged,
            "required_added_content_missing": missing_added_content,
            "forbidden_added_content_found": forbidden_added_content,
            "unexpected_change_ids": unexpected_changes,
            "change_count": sum(
                item.change_type is not MemoryStateChangeType.UNCHANGED
                for item in context.memory_state_changes
            ),
        },
        passed=passed,
        reason=(
            "Memory state gate passed" if passed else "Memory state gate failed"
        ),
        evidence=[
            MetricEvidence(
                evidence_id=_stable_evidence_id("memory-state", expectation.state_id),
                kind="memory_state_diff",
                description="structured before/after Memory state diff",
                metadata={
                    "state_id": expectation.state_id,
                    "before_snapshot_id": before.snapshot_id,
                    "after_snapshot_id": after.snapshot_id,
                    "change_record_count": len(context.memory_state_changes),
                },
            )
        ],
        evaluator_version=MEMORY_EVALUATOR_VERSION,
        metadata={
            "metric_type": "memory_state_gate",
            "state_id": expectation.state_id,
            "hard_gate": True,
        },
    )


def _content_matches(
    change: MemoryStateChange,
    expectation: MemoryContentExpectation,
) -> bool:
    item = change.after
    if item is None or (
        expectation.kind is not None and item.kind is not expectation.kind
    ):
        return False
    if expectation.match is MemoryContentMatchMode.EXACT:
        return item.content == expectation.content
    if expectation.match is MemoryContentMatchMode.CONTAINS:
        return expectation.content in item.content
    return _normalize(item.content) == _normalize(expectation.content)


def _change_is_declared(
    change: MemoryStateChange,
    expectation: MemoryStateExpectation,
) -> bool:
    if change.change_type is MemoryStateChangeType.ADDED:
        return (
            change.memory_id in expectation.required_present_memory_ids
            or any(
                _content_matches(change, item)
                for item in expectation.required_added_content
            )
        )
    if change.change_type is MemoryStateChangeType.REMOVED:
        return change.memory_id in {
            *expectation.required_removed_memory_ids,
            *expectation.required_absent_memory_ids,
        }
    return False


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _not_applicable_metric(
    name: str,
    reason: str,
    *,
    metric_type: str,
    identity_name: str,
    identity: str,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.RETRIEVAL,
        status=MetricStatus.NOT_APPLICABLE,
        value=None,
        passed=None,
        reason=reason,
        evaluator_version=MEMORY_EVALUATOR_VERSION,
        metadata={
            "metric_type": metric_type,
            identity_name: identity,
            "hard_gate": False,
        },
    )


def _memory_error_metric(
    *,
    name: str,
    operation_error: MemoryOperationError | None,
    fallback_type: str,
    query_id: str | None = None,
    state_id: str | None = None,
) -> MetricResult:
    error_type = (
        fallback_type
        if operation_error is None
        else operation_error.error_type.value
    )
    identity = query_id or state_id or "memory"
    return MetricResult(
        metric_name=name,
        source=MetricSource.RETRIEVAL,
        status=MetricStatus.ERROR,
        value=None,
        passed=None,
        reason=f"Memory evaluator error: {error_type}",
        evidence=[
            MetricEvidence(
                evidence_id=_stable_evidence_id("memory-error", identity),
                kind="memory_error",
                description=f"structured Memory failure: {error_type}",
                metadata={
                    "query_id": query_id,
                    "state_id": state_id,
                },
            )
        ],
        evaluator_version=MEMORY_EVALUATOR_VERSION,
        error=MetricError(
            error_type=error_type,
            message=(
                f"Memory operation failed: {error_type}"
                if operation_error is None
                else operation_error.message
            ),
            retryable=(
                False if operation_error is None else operation_error.retryable
            ),
            details={
                "query_id": query_id,
                "state_id": state_id,
            },
        ),
        metadata={
            "metric_type": (
                "required_evidence" if query_id is not None else "memory_state_gate"
            ),
            "query_id": query_id,
            "state_id": state_id,
            "hard_gate": True,
        },
    )


def _stable_evidence_id(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


__all__ = (
    "MEMORY_EVALUATOR_VERSION",
    "evaluate_memory_expectation",
    "evaluate_memory_state_expectation",
)
