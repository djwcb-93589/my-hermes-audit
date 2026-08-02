"""Replay local Audit facts as children of an Experiment Runner task trace."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

from myhermes_audit.contracts import MemorySnapshotPhase, MetricSource
from myhermes_audit.integrations.langfuse.redaction import project_remote_content
from myhermes_audit.ports.langfuse import LangfuseTrialRequest


TRACE_NAME = "myhermes.audit.trial"
TURN_NAME = "myhermes.audit.turn"
MODEL_NAME = "myhermes.agent.model"
TOOL_NAME = "myhermes.agent.tool"
VALIDATOR_NAME = "myhermes.audit.validator"
JUDGE_NAME = "myhermes.audit.judge"
MEMORY_SEED_NAME = "myhermes.audit.memory.seed"
MEMORY_QUERY_NAME = "myhermes.audit.memory.query"
MEMORY_SNAPSHOT_NAME = "myhermes.audit.memory.snapshot"
MEMORY_EVALUATOR_NAME = "myhermes.audit.memory.evaluator"
COMPRESSION_NAME = "myhermes.audit.compression"
CHECKPOINT_NAME = "myhermes.audit.checkpoint"
FACT_RETENTION_NAME = "myhermes.audit.fact_retention"
DISTORTION_NAME = "myhermes.audit.distortion"
ABLATION_COMPARISON_NAME = "myhermes.audit.ablation.comparison"


def publish_replay_observations(
    client: Any,
    propagate_attributes: Callable[..., Any],
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    trial = request.trial
    runtime = trial.runtime
    memory_enabled = _has_memory_facts(request)
    p4_enabled = trial.variant_id is not None
    metadata = {
        "suite_id": request.suite_id,
        "suite_sha256": request.suite_sha256,
        "case_id": request.case.case_id,
        "case_sha256": request.dataset_item.case_sha256,
        "trial_id": trial.trial_id,
        "audit_run_id": request.experiment.audit_run_id,
        "trial_run_id": trial.run_id,
        "trial_number": trial.trial_number,
        "subject_commit": request.subject_commit,
        "subject_dirty": request.subject_dirty,
        "audit_commit": request.audit_commit,
        "audit_version": request.audit_version,
        "worker_protocol_version": (
            trial.observations.worker_protocol_version
            if trial.observations is not None
            else "unavailable"
        ),
        "subject_model": (
            trial.trial_identity.model_identifier
            if trial.trial_identity is not None
            else (
                runtime.subject_model
                if runtime is not None and runtime.subject_model is not None
                else "unavailable"
            )
        ),
        "judge_model": (
            trial.judge_result.judge_model
            if trial.judge_result is not None
            else "not_evaluated"
        ),
        "judge_prompt_version": (
            trial.judge_result.prompt_version
            if trial.judge_result is not None
            else "not_evaluated"
        ),
        "case_mode": request.case.mode.value,
        "tags": list(request.case.tags),
        "runtime_status": trial.status.value,
        "data_classification": request.data_classification.value,
        "content_omitted": request.no_content
        or request.data_classification.value == "sensitive",
        "runtime_duration_ms": trial.duration_ms,
        "runtime_iterations": None if runtime is None else runtime.iterations,
        "runtime_tool_call_count": (
            None if runtime is None else runtime.tool_call_count
        ),
        "runtime_total_tokens": None if runtime is None else runtime.total_tokens,
        **(
            {
                "memory_query_count": len(trial.memory_query_results),
                "memory_error_count": len(trial.memory_errors),
                "retrieval_gate_passed": trial.retrieval_gate_passed,
                "final_answer_gate_passed": trial.final_answer_gate_passed,
                "memory_state_gate_passed": trial.memory_state_gate_passed,
            }
            if memory_enabled
            else {}
        ),
        **(
            {
                "variant_id": trial.variant_id,
                "memory_mode": trial.memory_mode.value,
                "requested_compression_mode": (
                    trial.compression_mode.value
                ),
                "configuration_fingerprint": trial.configuration_fingerprint,
                "comparison_basis_fingerprint": (
                    trial.comparison_basis_fingerprint
                ),
                "required_fact_gate_passed": trial.required_fact_gate_passed,
                "compression_event_count": (
                    len(trial.compression_events)
                    if trial.effective_subject_configuration.compression_events_observable
                    else None
                ),
                "distortion_count": len(trial.distortion_results),
                "effective_subject_configuration": (
                    trial.effective_subject_configuration.model_dump(
                        mode="json",
                        exclude={"schema_version"},
                    )
                ),
            }
            if p4_enabled
            else {}
        ),
        "post_hoc_publication": True,
        "runtime_timestamps_not_replayed": True,
        "experiment_runner_replay": True,
    }
    root_input = project_remote_content(
        _case_input(request),
        classification=request.data_classification,
        no_content=request.no_content,
        sensitive_values=sensitive_values,
    )
    root_output = project_remote_content(
        trial.final_output,
        classification=request.data_classification,
        no_content=request.no_content,
        sensitive_values=sensitive_values,
    )
    session_id = f"audit:{request.experiment.audit_run_id}:{trial.trial_id}"
    version = "p4" if p4_enabled else ("p3" if memory_enabled else "p2")
    with propagate_attributes(
        session_id=session_id[:200],
        metadata={
            "audit_suite_id": request.suite_id,
            "audit_case_id": request.case.case_id,
            "audit_trial_id": trial.trial_id,
        },
        tags=["myhermes-audit", version, request.case.mode.value],
        trace_name=TRACE_NAME,
    ):
        with client.start_as_current_observation(
            name=TRACE_NAME,
            as_type="span",
            input=root_input,
            output=root_output,
            metadata=metadata,
            version=version,
        ) as root:
            _publish_turns(root, request, sensitive_values=sensitive_values)
            _publish_memory(root, request, sensitive_values=sensitive_values)
            _publish_evaluators(root, request, sensitive_values=sensitive_values)
            _publish_ablation(root, request)


def _publish_turns(
    root: Any,
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    observations = request.trial.observations
    omit_content = (
        request.no_content
        or request.data_classification.value == "sensitive"
    )
    model_calls = [] if observations is None else list(observations.model_calls)
    tool_calls = [] if observations is None else list(observations.tool_calls)
    handled_runs: set[str] = set()
    for turn in request.trial.turns:
        turn_span = root.start_observation(
            name=TURN_NAME,
            as_type="span",
            input=project_remote_content(
                turn.user_message,
                classification=request.data_classification,
                no_content=request.no_content,
                sensitive_values=sensitive_values,
            ),
            output=project_remote_content(
                turn.final_output,
                classification=request.data_classification,
                no_content=request.no_content,
                sensitive_values=sensitive_values,
            ),
            metadata={
                "turn_number": turn.turn_number,
                **(
                    {}
                    if turn.session_id is None
                    else {
                        "logical_session_id": (
                            None if omit_content else turn.session_id
                        ),
                        "logical_session_declared": True,
                    }
                ),
                "run_id": turn.run_id,
                "runtime_status": turn.runtime_status,
                "runtime_started_at": turn.started_at.isoformat(),
                "runtime_finished_at": turn.finished_at.isoformat(),
                "runtime_duration_ms": turn.duration_ms,
                "post_hoc_publication": True,
            },
            version="p2",
        )
        try:
            if turn.run_id is not None:
                handled_runs.add(turn.run_id)
                _publish_model_calls(
                    turn_span,
                    [item for item in model_calls if item.run_id == turn.run_id],
                    request,
                )
                _publish_tool_calls(
                    turn_span,
                    [item for item in tool_calls if item.run_id == turn.run_id],
                )
        finally:
            turn_span.end()
    _publish_model_calls(
        root,
        [item for item in model_calls if item.run_id not in handled_runs],
        request,
    )
    _publish_tool_calls(
        root,
        [item for item in tool_calls if item.run_id not in handled_runs],
    )


def _publish_model_calls(parent: Any, items: list, request: LangfuseTrialRequest) -> None:
    subject_model = (
        request.trial.runtime.subject_model
        if request.trial.runtime is not None
        else None
    )
    for index, item in enumerate(items, start=1):
        usage = {
            key: value
            for key, value in {
                "input": item.prompt_tokens,
                "output": item.completion_tokens,
                "total": item.total_tokens,
            }.items()
            if value is not None
        }
        generation = parent.start_observation(
            name=MODEL_NAME,
            as_type="generation",
            input={
                "content_omitted": True,
                "reason": "worker protocol excludes hidden prompts and model request bodies",
            },
            output={
                "content_omitted": True,
                "reason": "worker protocol stores only public model-call projections",
            },
            metadata={
                "runtime_order_within_type": index,
                "runtime_cross_type_order_available": False,
                "run_id": item.run_id,
                "parent_run_id": item.parent_run_id,
                "finish_reason": item.finish_reason,
                "runtime_duration_ms": item.duration_ms,
                "tool_call_count": item.tool_call_count,
                "error_category": item.error_category,
                "post_hoc_publication": True,
            },
            model=subject_model,
            usage_details=usage or None,
            version="p2",
        )
        generation.end()


def _publish_tool_calls(parent: Any, items: list) -> None:
    for index, item in enumerate(items, start=1):
        tool = parent.start_observation(
            name=TOOL_NAME,
            as_type="tool",
            input={
                "content_omitted": True,
                "reason": "tool arguments are outside the Audit publication contract",
            },
            output={
                "content_omitted": True,
                "success": item.success,
                "status": item.status,
            },
            metadata={
                "runtime_order_within_type": index,
                "runtime_cross_type_order_available": False,
                "run_id": item.run_id,
                "parent_run_id": item.parent_run_id,
                "tool_call_id": item.tool_call_id,
                "tool_name": item.tool_name,
                "runtime_duration_ms": item.duration_ms,
                "error_type": item.error_type,
                "post_hoc_publication": True,
            },
            version="p2",
        )
        tool.end()


def _publish_evaluators(
    root: Any,
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    for metric in request.trial.metrics:
        if (
            metric.source is MetricSource.JUDGE
            and request.trial.judge_result is not None
        ):
            _publish_judge_generation(
                root,
                request,
                sensitive_values=sensitive_values,
            )
            continue
        name = (
            JUDGE_NAME
            if metric.source is MetricSource.JUDGE
            else (
                MEMORY_EVALUATOR_NAME
                if metric.source is MetricSource.RETRIEVAL
                else (
                    FACT_RETENTION_NAME
                    if metric.source is MetricSource.COMPRESSION
                    else VALIDATOR_NAME
                )
            )
        )
        evaluator = root.start_observation(
            name=name,
            as_type="evaluator",
            input={"metric_name": metric.metric_name},
            output=project_remote_content(
                {
                    "status": metric.status.value,
                    "value": metric.value,
                    "passed": metric.passed,
                    "reason": metric.reason,
                },
                classification=request.data_classification,
                no_content=request.no_content,
                sensitive_values=sensitive_values,
            ),
            metadata={
                "metric_source": metric.source.value,
                "evaluator_version": metric.evaluator_version,
                "post_hoc_publication": True,
            },
            version=(
                "p4"
                if metric.source is MetricSource.COMPRESSION
                else (
                    "p3" if metric.source is MetricSource.RETRIEVAL else "p2"
                )
            ),
        )
        evaluator.end()


def _publish_ablation(root: Any, request: LangfuseTrialRequest) -> None:
    trial = request.trial
    if trial.variant_id is None:
        return
    compression = root.start_observation(
        name=COMPRESSION_NAME,
        as_type="span",
        input={
            "variant_id": trial.variant_id,
            "requested_compression_mode": (
                trial.compression_mode.value
            ),
            "effective_compression_semantics": (
                trial.effective_subject_configuration
                .effective_compression_semantics.value
            ),
            "control": (
                trial.effective_subject_configuration.compression_control.value
            ),
            "threshold": (
                trial.effective_subject_configuration.compression_threshold
            ),
        },
        output={
            "event_count": (
                len(trial.compression_events)
                if trial.effective_subject_configuration.compression_events_observable
                else None
            ),
            "events": (
                [
                    item.model_dump(mode="json", exclude={"schema_version"})
                    for item in trial.compression_events
                ]
                if trial.effective_subject_configuration.compression_events_observable
                else None
            ),
            "context_diagnostics": [
                item.model_dump(mode="json", exclude={"schema_version"})
                for item in trial.context_diagnostics
            ],
            "token_diagnostics": (
                None
                if trial.token_diagnostics is None
                else trial.token_diagnostics.model_dump(
                    mode="json",
                    exclude={"schema_version"},
                )
            ),
            "duration_diagnostics": (
                None
                if trial.duration_diagnostics is None
                else trial.duration_diagnostics.model_dump(
                    mode="json",
                    exclude={"schema_version"},
                )
            ),
        },
        metadata={
            "compression_threshold_control": (
                trial.effective_subject_configuration.compression_threshold_control
            ),
            "emergency_overflow_compression_disable_supported": (
                trial.effective_subject_configuration
                .emergency_overflow_compression_disable_supported
            ),
            "emergency_overflow_compression_may_still_occur": (
                trial.effective_subject_configuration.emergency_compression_possible
            ),
            "compression_events_observable": (
                trial.effective_subject_configuration.compression_events_observable
            ),
            "post_hoc_publication": True,
            "content_uploaded": False,
        },
        version="p4",
    )
    compression.end()

    for checkpoint in trial.checkpoint_results:
        observation = root.start_observation(
            name=CHECKPOINT_NAME,
            as_type="evaluator",
            input={
                "checkpoint_id": checkpoint.checkpoint_id,
                "after_turn": checkpoint.after_turn,
                "required_fact_ids": checkpoint.required_fact_ids,
            },
            output={
                "fact_gate_passed": checkpoint.fact_gate_passed,
                "answer_gate_passed": checkpoint.answer_gate_passed,
                "context_diagnostic_available": (
                    checkpoint.context_diagnostic_available
                ),
                "compression_applied": checkpoint.compression_applied,
            },
            metadata={"post_hoc_publication": True, "content_uploaded": False},
            version="p4",
        )
        observation.end()

    for fact in trial.fact_retention_results:
        observation = root.start_observation(
            name=FACT_RETENTION_NAME,
            as_type="evaluator",
            input={
                "expectation_id": fact.expectation_id,
                "fact_id": fact.fact_id,
                "scope": fact.scope.value,
                "expected_projection": _safe_fact_projection(
                    fact.expected_projection
                ),
            },
            output={
                "status": fact.status.value,
                "actual_projection": _safe_fact_projection(
                    fact.actual_projection
                ),
                "hard_gate": fact.hard_gate,
                "error_type": fact.error_type,
            },
            metadata={
                "checkpoint_id": fact.checkpoint_id,
                "evidence_source": fact.evidence_source,
                "post_hoc_publication": True,
                "content_uploaded": False,
            },
            version="p4",
        )
        observation.end()

    for distortion in trial.distortion_results:
        observation = root.start_observation(
            name=DISTORTION_NAME,
            as_type="evaluator",
            input={
                "fact_id": distortion.fact_id,
                "expected_projection": _safe_fact_projection(
                    distortion.expected_projection
                ),
            },
            output={
                "distortion_type": distortion.distortion_type.value,
                "actual_projection": _safe_fact_projection(
                    distortion.actual_projection
                ),
                "hard_gate": distortion.hard_gate,
            },
            metadata={
                "expectation_id": distortion.expectation_id,
                "evidence_source": distortion.evidence_source,
                "post_hoc_publication": True,
                "content_uploaded": False,
            },
            version="p4",
        )
        observation.end()

    comparison = request.ablation_comparison
    if (
        comparison is not None
        and trial.variant_id == comparison.reference_variant_id
        and trial.trial_number == 1
    ):
        observation = root.start_observation(
            name=ABLATION_COMPARISON_NAME,
            as_type="span",
            input={
                "case_id": comparison.case_id,
                "reference_variant_id": comparison.reference_variant_id,
            },
            output=comparison.model_dump(mode="json", exclude={"schema_version"}),
            metadata={"post_hoc_publication": True, "content_uploaded": False},
            version="p4",
        )
        observation.end()


def _safe_fact_projection(projection) -> dict | None:
    if projection is None:
        return None
    return {
        "sha256": projection.sha256,
        "length": projection.length,
        "content_omitted": True,
    }


def _publish_memory(
    root: Any,
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    trial = request.trial
    if not _has_memory_facts(request):
        return
    omit_content = (
        request.no_content
        or request.data_classification.value == "sensitive"
    )
    required_by_query = {
        item.query_id: set(item.required_memory_ids)
        for item in request.case.expected.memories
    }
    before = next(
        (
            item
            for item in trial.memory_snapshots
            if item.phase is MemorySnapshotPhase.BEFORE_CONVERSATION
        ),
        None,
    )
    seed = root.start_observation(
        name=MEMORY_SEED_NAME,
        as_type="span",
        input={
            "declared_fixture_count": (
                0
                if request.case.fixture.memory is None
                else len(request.case.fixture.memory.items)
            ),
            "content_omitted": omit_content,
        },
        output={
            "snapshot_item_count": 0 if before is None else len(before.items),
            "items": (
                []
                if before is None
                else [
                    _memory_item_projection(
                        item,
                        omit_content=omit_content,
                        classification=request.data_classification,
                        sensitive_values=sensitive_values,
                    )
                    for item in before.items
                ]
            ),
        },
        metadata={
            "strategy": (
                "unavailable"
                if before is None or before.strategy is None
                else before.strategy.value
            ),
            "provider": (
                "unavailable"
                if before is None or before.provider is None
                else before.provider
            ),
            "post_hoc_publication": True,
        },
        version="p3",
    )
    seed.end()

    for result in trial.memory_query_results:
        required_ids = required_by_query.get(result.query_id, set())
        query = root.start_observation(
            name=MEMORY_QUERY_NAME,
            as_type="span",
            input={
                "query_id": result.query_id,
                "query": _text_projection(
                    result.query.query,
                    omit_content=omit_content,
                    classification=request.data_classification,
                    sensitive_values=sensitive_values,
                ),
                "top_k": result.query.top_k,
            },
            output={
                "item_count": len(result.items),
                "items": [
                    {
                        **_memory_item_projection(
                            item,
                            omit_content=omit_content,
                            classification=request.data_classification,
                            sensitive_values=sensitive_values,
                        ),
                        "rank": item.rank,
                        "score": item.score,
                        "required_hit": item.memory_id in required_ids,
                    }
                    for item in result.items
                ],
            },
            metadata={
                "query_id": result.query_id,
                "phase": result.phase.value,
                "strategy": result.strategy.value,
                "provider": result.provider,
                "duration_ms": result.duration_ms,
                "query_used": result.metadata.get("query_used"),
                "score_semantics": result.metadata.get("score_semantics"),
                "post_hoc_publication": True,
            },
            version="p3",
        )
        query.end()

    for snapshot in trial.memory_snapshots:
        if (
            snapshot.phase is None
            or snapshot.strategy is None
            or snapshot.provider is None
        ):
            raise ValueError("P3 Memory snapshot semantics are incomplete")
        span = root.start_observation(
            name=MEMORY_SNAPSHOT_NAME,
            as_type="span",
            input={"phase": snapshot.phase.value},
            output={
                "item_count": len(snapshot.items),
                "items": [
                    _memory_item_projection(
                        item,
                        omit_content=omit_content,
                        classification=request.data_classification,
                        sensitive_values=sensitive_values,
                    )
                    for item in snapshot.items
                ],
            },
            metadata={
                "snapshot_id": snapshot.snapshot_id,
                "phase": snapshot.phase.value,
                "strategy": snapshot.strategy.value,
                "provider": snapshot.provider,
                "state_change_count": len(trial.memory_state_changes),
                "post_hoc_publication": True,
            },
            version="p3",
        )
        span.end()


def _memory_item_projection(
    item,
    *,
    omit_content: bool,
    classification,
    sensitive_values: Iterable[str],
) -> dict:
    return {
        **({} if omit_content else {"memory_id": item.memory_id}),
        "kind": item.kind.value,
        "content": _text_projection(
            item.content,
            omit_content=omit_content,
            classification=classification,
            sensitive_values=sensitive_values,
        ),
    }


def _text_projection(
    value: str,
    *,
    omit_content: bool,
    classification,
    sensitive_values: Iterable[str],
) -> dict:
    projected = project_remote_content(
        value,
        classification=classification,
        no_content=False,
        sensitive_values=sensitive_values,
    )
    if not isinstance(projected, str):
        return {
            "sha256": projected.get("sha256"),
            "length": projected.get("serialized_length"),
            "size_bytes": projected.get("serialized_size_bytes"),
            "content_omitted": True,
        }
    encoded = projected.encode("utf-8")
    result = {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "length": len(projected),
        "size_bytes": len(encoded),
        "content_omitted": omit_content,
    }
    if not omit_content:
        result["value"] = projected
    return result


def _has_memory_facts(request: LangfuseTrialRequest) -> bool:
    trial = request.trial
    return any(
        (
            request.case.execution.memory_strategy is not None,
            bool(trial.memory_query_results),
            bool(trial.memory_snapshots),
            bool(trial.memory_state_changes),
            bool(trial.memory_errors),
        )
    )


def _publish_judge_generation(
    root: Any,
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    result = request.trial.judge_result
    if result is None:
        return
    expectation = request.case.expected.judges[0]
    usage = {
        key: value
        for key, value in {
            "input": result.prompt_tokens,
            "output": result.completion_tokens,
            "total": result.total_tokens,
        }.items()
        if value is not None
    }
    generation = root.start_observation(
        name=JUDGE_NAME,
        as_type="generation",
        input=project_remote_content(
            {
                "rubric": expectation.rubric,
                "criteria": [
                    {
                        "name": criterion.name,
                        "description": criterion.description,
                        "weight": criterion.weight,
                    }
                    for criterion in expectation.criteria
                ],
                "candidate_content_reused_from_trace": True,
                "hidden_prompt_uploaded": False,
            },
            classification=request.data_classification,
            no_content=request.no_content,
            sensitive_values=sensitive_values,
        ),
        output=project_remote_content(
            {
                "overall_score": result.overall_score,
                "passed": result.passed,
                "criteria": [
                    item.model_dump(mode="json", exclude={"schema_version"})
                    for item in result.criteria
                ],
                "summary": result.summary,
            },
            classification=request.data_classification,
            no_content=request.no_content,
            sensitive_values=sensitive_values,
        ),
        metadata={
            "judge_id": result.judge_id,
            "judge_provider": result.judge_provider,
            "judge_prompt_version": result.prompt_version,
            "runtime_duration_ms": result.duration_ms,
            "retry_count": result.retry_count,
            "raw_response_uploaded": False,
            "private_reasoning_uploaded": False,
            "post_hoc_publication": True,
        },
        model=result.judge_model,
        usage_details=usage or None,
        version=result.prompt_version,
    )
    generation.end()


def _case_input(request: LangfuseTrialRequest) -> dict:
    case_input = request.case.input
    if case_input.message is not None:
        value = {"message": case_input.message}
        if case_input.session_id is not None:
            value["session_id"] = case_input.session_id
        return value
    return {
        "turns": [
            {
                **{"role": turn.role.value, "message": turn.message},
                **(
                    {}
                    if turn.session_id is None
                    else {"session_id": turn.session_id}
                ),
            }
            for turn in case_input.turns
        ]
    }


__all__ = (
    "ABLATION_COMPARISON_NAME",
    "CHECKPOINT_NAME",
    "COMPRESSION_NAME",
    "DISTORTION_NAME",
    "FACT_RETENTION_NAME",
    "JUDGE_NAME",
    "MEMORY_EVALUATOR_NAME",
    "MEMORY_QUERY_NAME",
    "MEMORY_SEED_NAME",
    "MEMORY_SNAPSHOT_NAME",
    "MODEL_NAME",
    "TOOL_NAME",
    "TRACE_NAME",
    "TURN_NAME",
    "VALIDATOR_NAME",
    "publish_replay_observations",
)
