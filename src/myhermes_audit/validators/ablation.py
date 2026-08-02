"""Deterministic P4 required-fact and checkpoint evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from myhermes_audit.contracts import (
    CheckpointResult,
    DiagnosticStatus,
    DistortionResult,
    DistortionType,
    FactRetentionResult,
    FactRetentionStatus,
    MemorySnapshotPhase,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
    RequiredFact,
    RequiredFactExpectation,
    RequiredFactLossResult,
    RequiredFactScope,
)
from myhermes_audit.fact_matching import (
    fact_projection,
    match_distortion_candidate,
    match_required_fact,
    matches_fact_value,
)
from myhermes_audit.validators.base import ValidationContext


@dataclass(frozen=True, slots=True)
class AblationEvaluation:
    metrics: tuple[MetricResult, ...]
    checkpoint_results: tuple[CheckpointResult, ...]
    fact_retention_results: tuple[FactRetentionResult, ...]
    required_fact_loss: RequiredFactLossResult
    distortion_results: tuple[DistortionResult, ...]


def evaluate_ablation(
    expectations: list[RequiredFactExpectation],
    context: ValidationContext,
    *,
    evaluator_id: str,
) -> AblationEvaluation:
    retention: list[FactRetentionResult] = []
    distortions: list[DistortionResult] = []
    metrics: list[MetricResult] = []
    fact_by_id: dict[str, RequiredFact] = {}
    expectation_by_fact: dict[str, RequiredFactExpectation] = {}

    selected_expectations = [
        item
        for item in expectations
        if not item.applicable_variant_ids
        or context.variant_id in item.applicable_variant_ids
    ]
    for expectation in selected_expectations:
        for fact in expectation.facts:
            fact_by_id[fact.fact_id] = fact
            expectation_by_fact[fact.fact_id] = expectation
            fact_result, distortion = _evaluate_fact(fact, expectation, context)
            retention.append(fact_result)
            metrics.append(
                _fact_metric(
                    fact_result,
                    evaluator_id=evaluator_id,
                )
            )
            distortion_metric, distortion_result = _distortion_metric(
                fact,
                expectation,
                fact_result,
                distortion,
                evaluator_id=evaluator_id,
            )
            metrics.append(distortion_metric)
            if distortion_result is not None:
                distortions.append(distortion_result)

    retention_by_id = {item.fact_id: item for item in retention}
    checkpoint_specs = []
    if context.ablation_plan is not None:
        for checkpoint in context.ablation_plan.checkpoints:
            required_ids = [
                item for item in checkpoint.required_fact_ids if item in fact_by_id
            ]
            if not required_ids and checkpoint.expected_answer is None:
                continue
            checkpoint_specs.append(
                checkpoint.model_copy(update={"required_fact_ids": required_ids})
            )
    checkpoints = [
        _evaluate_checkpoint(checkpoint, context, retention_by_id)
        for checkpoint in checkpoint_specs
    ]
    metrics.extend(
        _checkpoint_metric(
            item,
            evaluator_id=evaluator_id,
            hard_gate=checkpoint.required,
        )
        for checkpoint, item in zip(checkpoint_specs, checkpoints, strict=True)
    )
    loss = _required_fact_loss(retention, expectation_by_fact)
    metrics.append(_loss_metric(loss, evaluator_id=evaluator_id))
    return AblationEvaluation(
        metrics=tuple(metrics),
        checkpoint_results=tuple(checkpoints),
        fact_retention_results=tuple(retention),
        required_fact_loss=loss,
        distortion_results=tuple(distortions),
    )


def _evaluate_fact(
    fact: RequiredFact,
    expectation: RequiredFactExpectation,
    context: ValidationContext,
) -> tuple[FactRetentionResult, tuple[DistortionType, object | None] | None]:
    expected = fact_projection(fact.canonical_value, include_value=False)
    hard_gate = expectation.required
    evidence_source = fact.scope.value
    actual = None
    distortion = None
    matched: bool | None
    error_type: str | None = None

    if fact.scope is RequiredFactScope.SUBJECT_CONTEXT:
        observation = next(
            (
                item
                for item in context.fact_context_observations
                if item.fact_id == fact.fact_id
                and item.checkpoint_id == fact.checkpoint_id
            ),
            None,
        )
        if observation is None:
            matched = None
            error_type = "fact_observation_unavailable"
        elif observation.matched is None:
            matched = None
            error_type = observation.error_type or "fact_observation_error"
        elif fact.must_survive_compression and observation.compression_applied is not True:
            matched = None
            error_type = "compression_observation_unavailable"
        elif (
            fact.must_survive_session_change
            and not observation.session_changed
        ):
            matched = None
            error_type = "session_change_not_observed"
        else:
            matched = observation.matched
            actual = observation.matched_projection
            if observation.distortion_type is not None:
                distortion = (
                    observation.distortion_type,
                    observation.distortion_projection,
                )
    elif (
        fact.must_survive_compression
        and not any(
            item.compression_applied is True
            for item in context.context_diagnostics
        )
    ):
        matched = None
        error_type = "compression_observation_unavailable"
    elif fact.must_survive_session_change and not any(
        item.session_changed for item in context.context_diagnostics
    ):
        matched = None
        error_type = "session_change_not_observed"
    else:
        evidence, available = _fact_evidence(fact, context)
        if not available:
            matched = None
            error_type = "fact_evidence_unavailable"
        else:
            actual = match_required_fact(evidence, fact, include_value=False)
            matched = actual is not None
            candidate = (
                None
                if matched
                else match_distortion_candidate(
                    evidence,
                    fact,
                    include_value=False,
                )
            )
            if candidate is not None:
                distortion = (candidate[0].distortion_type, candidate[1])

    if matched is None:
        status = FactRetentionStatus.NOT_EVALUABLE
    elif fact.must_be_absent:
        status = (
            FactRetentionStatus.PRESENT_WHEN_FORBIDDEN
            if matched
            else FactRetentionStatus.ABSENT
        )
    else:
        status = FactRetentionStatus.RETAINED if matched else FactRetentionStatus.LOST

    result = FactRetentionResult(
        expectation_id=expectation.expectation_id,
        fact_id=fact.fact_id,
        status=status,
        scope=fact.scope,
        checkpoint_id=fact.checkpoint_id,
        evidence_source=evidence_source,
        expected_projection=expected,
        actual_projection=actual,
        hard_gate=hard_gate,
    )
    if error_type is not None:
        # NOT_EVALUABLE is deliberately separate from validator ERROR. The
        # corresponding metric below records the structured unavailable cause.
        return result, (DistortionType.NOT_EVALUABLE, None)
    return result, distortion


def _fact_evidence(
    fact: RequiredFact,
    context: ValidationContext,
) -> tuple[list[str], bool]:
    if fact.scope is RequiredFactScope.FINAL_ANSWER:
        if context.final_output is None:
            return [], False
        return [context.final_output], True
    if fact.scope is RequiredFactScope.LONG_TERM_MEMORY:
        snapshots = [
            item
            for item in context.memory_snapshots
            if item.phase is MemorySnapshotPhase.AFTER_CONVERSATION
        ]
        if snapshots:
            return [item.content for item in snapshots[-1].items], True
        configuration = context.effective_subject_configuration
        if configuration is not None and not configuration.include_memory:
            return [], True
        return [], False
    return [], False


def _evaluate_checkpoint(
    checkpoint,
    context: ValidationContext,
    retention_by_id: dict[str, FactRetentionResult],
) -> CheckpointResult:
    selected = [retention_by_id[item] for item in checkpoint.required_fact_ids]
    fact_gate = (
        None
        if not selected
        else all(
            item.status
            in {FactRetentionStatus.RETAINED, FactRetentionStatus.ABSENT}
            for item in selected
        )
    )
    answer_gate = None
    if checkpoint.expected_answer is not None:
        answer_gate = False
        turn = next(
            (
                item
                for item in context.turns
                if item.turn_number == checkpoint.after_turn
            ),
            None,
        )
        if turn is not None and turn.final_output is not None:
            answer_gate = matches_fact_value(
                turn.final_output,
                checkpoint.expected_answer,
                checkpoint.answer_match,
            )
    diagnostic = next(
        (
            item
            for item in context.context_diagnostics
            if item.turn_index == checkpoint.after_turn
        ),
        None,
    )
    return CheckpointResult(
        checkpoint_id=checkpoint.checkpoint_id,
        after_turn=checkpoint.after_turn,
        required_fact_ids=checkpoint.required_fact_ids,
        fact_gate_passed=fact_gate,
        answer_gate_passed=answer_gate,
        context_diagnostic_available=(
            diagnostic is not None
            and diagnostic.status
            in {DiagnosticStatus.AVAILABLE, DiagnosticStatus.PARTIAL}
        ),
        compression_applied=(
            None if diagnostic is None else diagnostic.compression_applied
        ),
    )


def _required_fact_loss(
    results: list[FactRetentionResult],
    expectation_by_fact: dict[str, RequiredFactExpectation],
) -> RequiredFactLossResult:
    required = [
        item
        for item in results
        if expectation_by_fact[item.fact_id].required
    ]
    if not required:
        return RequiredFactLossResult(status=DiagnosticStatus.NOT_APPLICABLE)
    if any(
        item.status
        in {FactRetentionStatus.NOT_EVALUABLE, FactRetentionStatus.ERROR}
        for item in required
    ):
        return RequiredFactLossResult(
            status=DiagnosticStatus.ERROR,
            error_type="required_fact_not_evaluable",
        )
    successful = {
        FactRetentionStatus.RETAINED,
        FactRetentionStatus.ABSENT,
    }
    lost = [item.fact_id for item in required if item.status not in successful]
    retained_count = len(required) - len(lost)
    return RequiredFactLossResult(
        status=DiagnosticStatus.AVAILABLE,
        required_fact_count=len(required),
        retained_required_fact_count=retained_count,
        lost_required_fact_ids=lost,
        required_fact_loss_count=len(lost),
        required_fact_loss_rate=len(lost) / len(required),
    )


def _fact_metric(
    result: FactRetentionResult,
    *,
    evaluator_id: str,
) -> MetricResult:
    name = f"{evaluator_id}.fact.{result.fact_id}"
    passed = result.status in {
        FactRetentionStatus.RETAINED,
        FactRetentionStatus.ABSENT,
    }
    metadata = {
        "metric_type": "required_fact_retention",
        "fact_id": result.fact_id,
        "hard_gate": result.hard_gate,
        "expected_sha256": result.expected_projection.sha256,
        "expected_length": result.expected_projection.length,
    }
    evidence_item = MetricEvidence(
        evidence_id=f"evidence-fact-{result.fact_id}",
        kind="fact_projection",
        description=f"fact_status={result.status.value}",
        metadata={
            "scope": result.scope.value,
            "status": result.status.value,
        },
    )
    if result.status is FactRetentionStatus.NOT_EVALUABLE:
        return MetricResult(
            metric_name=name,
            source=MetricSource.COMPRESSION,
            status=MetricStatus.ERROR,
            value=None,
            passed=None,
            reason="required fact could not be evaluated from public observations",
            evidence=[evidence_item],
            evaluator_version="p4.0",
            error=MetricError(
                error_type="required_fact_not_evaluable",
                message="required fact observation is unavailable",
            ),
            metadata=metadata,
        )
    return MetricResult(
        metric_name=name,
        source=MetricSource.COMPRESSION,
        value=passed,
        passed=passed,
        reason=f"required fact status is {result.status.value}",
        evidence=[evidence_item],
        evaluator_version="p4.0",
        metadata=metadata,
    )


def _distortion_metric(
    fact: RequiredFact,
    expectation: RequiredFactExpectation,
    retention: FactRetentionResult,
    observed: tuple[DistortionType, object | None] | None,
    *,
    evaluator_id: str,
) -> tuple[MetricResult, DistortionResult | None]:
    successful = retention.status in {
        FactRetentionStatus.RETAINED,
        FactRetentionStatus.ABSENT,
    }
    if successful:
        distortion_type = None
        projection = None
    elif observed is not None:
        distortion_type, projection = observed
    elif retention.status is FactRetentionStatus.PRESENT_WHEN_FORBIDDEN:
        distortion_type = DistortionType.UNSUPPORTED_ADDITION
        projection = retention.actual_projection
    else:
        distortion_type = DistortionType.MISSING
        projection = None
    hard_gate = expectation.distortion_hard_gate
    name = f"{evaluator_id}.distortion.{fact.fact_id}"
    common = {
        "metric_name": name,
        "source": MetricSource.COMPRESSION,
        "evidence": [
            MetricEvidence(
                evidence_id=f"evidence-distortion-{fact.fact_id}",
                kind="distortion_projection",
                description=(
                    "distortion=none"
                    if distortion_type is None
                    else f"distortion={distortion_type.value}"
                ),
            )
        ],
        "evaluator_version": "p4.0",
        "metadata": {
            "metric_type": "distortion",
            "fact_id": fact.fact_id,
            "hard_gate": hard_gate,
        },
    }
    if distortion_type is DistortionType.NOT_EVALUABLE:
        metric_result = MetricResult(
            **common,
            status=MetricStatus.ERROR,
            value=None,
            passed=None,
            reason="distortion could not be determined from public observations",
            error=MetricError(
                error_type="distortion_not_evaluable",
                message="distortion could not be evaluated without guessing",
            ),
        )
    else:
        metric_result = MetricResult(
            **common,
            value=distortion_type is None,
            passed=distortion_type is None,
            reason=(
                "no deterministic distortion detected"
                if distortion_type is None
                else f"deterministic distortion={distortion_type.value}"
            ),
        )
    if distortion_type is None:
        return metric_result, None
    return metric_result, DistortionResult(
        expectation_id=expectation.expectation_id,
        fact_id=fact.fact_id,
        distortion_type=distortion_type,
        expected_projection=retention.expected_projection,
        actual_projection=projection,
        evidence_source=retention.evidence_source,
        hard_gate=hard_gate,
    )


def _checkpoint_metric(
    checkpoint: CheckpointResult,
    *,
    evaluator_id: str,
    hard_gate: bool,
) -> MetricResult:
    evaluated = [
        value
        for value in (checkpoint.fact_gate_passed, checkpoint.answer_gate_passed)
        if value is not None
    ]
    passed = (
        bool(evaluated)
        and all(evaluated)
        and checkpoint.context_diagnostic_available
    )
    return MetricResult(
        metric_name=f"{evaluator_id}.checkpoint.{checkpoint.checkpoint_id}",
        source=MetricSource.COMPRESSION,
        value=passed,
        passed=passed,
        reason=(
            "checkpoint gates passed"
            if passed
            else "checkpoint has a failed or unavailable gate"
        ),
        evidence=[
            MetricEvidence(
                evidence_id=f"evidence-checkpoint-{checkpoint.checkpoint_id}",
                kind="checkpoint",
                description=f"after_turn={checkpoint.after_turn}",
                metadata={
                    "context_diagnostic_available": (
                        checkpoint.context_diagnostic_available
                    ),
                    "compression_applied": checkpoint.compression_applied,
                },
            )
        ],
        evaluator_version="p4.0",
        metadata={
            "metric_type": "checkpoint",
            "checkpoint_id": checkpoint.checkpoint_id,
            "hard_gate": hard_gate,
        },
    )


def _loss_metric(
    result: RequiredFactLossResult,
    *,
    evaluator_id: str,
) -> MetricResult:
    name = f"{evaluator_id}.required_fact_loss"
    if result.status is DiagnosticStatus.ERROR:
        return MetricResult(
            metric_name=name,
            source=MetricSource.COMPRESSION,
            status=MetricStatus.ERROR,
            value=None,
            passed=None,
            reason="required fact loss is unavailable",
            evaluator_version="p4.0",
            error=MetricError(
                error_type=result.error_type or "required_fact_loss_error",
                message="required fact loss could not be computed without guessing",
            ),
            metadata={"metric_type": "required_fact_loss", "hard_gate": False},
        )
    value = (
        None
        if result.status is DiagnosticStatus.NOT_APPLICABLE
        else result.required_fact_loss_rate
    )
    if value is None:
        return MetricResult(
            metric_name=name,
            source=MetricSource.COMPRESSION,
            status=MetricStatus.NOT_APPLICABLE,
            value=None,
            passed=None,
            reason="no required facts were declared",
            evaluator_version="p4.0",
            metadata={"metric_type": "required_fact_loss", "hard_gate": False},
        )
    return MetricResult(
        metric_name=name,
        source=MetricSource.COMPRESSION,
        value=value,
        passed=value == 0,
        reason=f"required_fact_loss_rate={value:.6f}",
        evaluator_version="p4.0",
        metadata={"metric_type": "required_fact_loss", "hard_gate": False},
    )


__all__ = ("AblationEvaluation", "evaluate_ablation")
