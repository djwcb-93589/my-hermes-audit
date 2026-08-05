"""Replay local Audit facts as children of an Experiment Runner task trace."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

from myhermes_audit.contracts import MemorySnapshotPhase, MetricSource, MetricStatus
from myhermes_audit.contracts.regression import AuditRegressionReport
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
BACKGROUND_REVIEW_NAME = "myhermes.audit.background_review"
BACKGROUND_REVIEW_FOREGROUND_EVIDENCE_NAME = "myhermes.audit.background_review.foreground_evidence"
BACKGROUND_REVIEW_PREPARED_EVIDENCE_NAME = "myhermes.audit.background_review.prepared_evidence"
BACKGROUND_REVIEW_TOOL_NAME = "myhermes.audit.background_review.tool"
BACKGROUND_REVIEW_SNAPSHOT_NAME = "myhermes.audit.background_review.snapshot"
BACKGROUND_REVIEW_CHANGE_NAME = "myhermes.audit.background_review.observed_change"
BACKGROUND_REVIEW_LIFECYCLE_NAME = "myhermes.audit.background_review.lifecycle"
BACKGROUND_REVIEW_EVALUATOR_NAME = "myhermes.audit.background_review.evaluator"
SCENARIO_NAME = "myhermes.audit.scenario"
PROCESS_STEP_NAME = "myhermes.audit.process.step"


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
    p5_enabled = _has_background_review_facts(request)
    metadata = {
        "suite_id": request.suite_id,
        "suite_sha256": request.suite_sha256,
        "case_id": request.case.case_id,
        "case_sha256": request.dataset_item.case_sha256,
        "trial_id": trial.trial_id,
        "audit_run_id": request.experiment.audit_run_id,
        "trial_run_id": trial.run_id,
        "trial_number": trial.trial_number,
        "scenario_fingerprint": trial.scenario_fingerprint,
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
        "representative_task_success_rate": (
            None if trial.task_passed is None else float(trial.task_passed)
        ),
        "representative_tool_correctness_rate": _trial_metric_rate(
            trial,
            source=MetricSource.RUNTIME,
            metric_type="tool_trajectory",
        ),
        "representative_memory_evidence_hit_rate": _trial_metric_rate(
            trial,
            source=MetricSource.RETRIEVAL,
            metric_type="required_evidence",
        ),
        "representative_background_review_decision_accuracy": _trial_metric_rate(
            trial,
            source=MetricSource.BACKGROUND_REVIEW,
            metric_type="decision_correctness",
        ),
        "runtime_prompt_tokens": None if runtime is None else runtime.prompt_tokens,
        "runtime_completion_tokens": (
            None if runtime is None else runtime.completion_tokens
        ),
        "runtime_model_call_count": (
            None if runtime is None else runtime.model_call_count
        ),
        "deepseek_cache_status": (
            "not_evaluated"
            if runtime is None
            else runtime.deepseek_cache_status.value
        ),
        "deepseek_cache_hit_tokens": (
            None if runtime is None else runtime.prompt_cache_hit_tokens
        ),
        "deepseek_cache_miss_tokens": (
            None if runtime is None else runtime.prompt_cache_miss_tokens
        ),
        "deepseek_cache_evaluated_prompt_tokens": (
            None
            if runtime is None
            else runtime.deepseek_cache_evaluated_prompt_tokens
        ),
        "deepseek_cache_hit_rate": (
            None if runtime is None else runtime.deepseek_cache_hit_rate
        ),
        "deepseek_cache_evaluated_model_call_count": (
            None
            if runtime is None
            else runtime.deepseek_cache_evaluated_model_call_count
        ),
        "deepseek_cache_model_call_coverage": (
            None
            if runtime is None or runtime.model_call_count == 0
            else runtime.deepseek_cache_evaluated_model_call_count
            / runtime.model_call_count
        ),
        "deepseek_cache_trial_coverage": (
            None
            if runtime is None
            else float(runtime.deepseek_cache_evaluated_model_call_count > 0)
        ),
        "deepseek_cache_invalid_model_call_count": (
            None
            if runtime is None
            else runtime.deepseek_cache_invalid_model_call_count
        ),
        **_cost_metadata(trial),
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
        **(
            {
                "background_review_plan_count": len(
                    request.case.fixture.background_review_plans
                ),
                "background_review_result_count": len(
                    trial.background_review_results
                ),
                "background_review_error_count": len(
                    trial.background_review_errors
                ),
                "review_gate_passed": trial.review_gate_passed,
            }
            if p5_enabled
            else {}
        ),
        **(
            {
                "scenario_count": len(trial.scenario_results),
                "toolchain_scenario_count": sum(
                    item.kind.value == "toolchain" for item in trial.scenario_results
                ),
                "process_scenario_count": sum(
                    item.kind.value == "process_background" for item in trial.scenario_results
                ),
                "toolchain_gate_passed": trial.toolchain_gate_passed,
                "process_gate_passed": trial.process_gate_passed,
            }
            if trial.scenario_results
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
    version = (
        "p6.1"
        if trial.scenario_results
        else (
            "p5"
            if p5_enabled
            else ("p4" if p4_enabled else ("p3" if memory_enabled else "p2"))
        )
    )
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
            _publish_background_reviews(root, request)
            _publish_scenarios(root, request)
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


def _publish_scenarios(root: Any, request: LangfuseTrialRequest) -> None:
    """Replay only local scenario facts; never restart a process or a Validator."""
    trial = request.trial
    scenario_plans = {item.scenario_id: item for item in request.case.scenarios}
    for scenario in trial.scenario_results:
        scenario_plan = scenario_plans.get(scenario.scenario_id)
        step_plans = {
            item.step_id: item
            for item in getattr(scenario_plan, "steps", ())
        }
        checkpoint_plans = {
            item.checkpoint_id: item
            for item in getattr(scenario_plan, "checkpoints", ())
        }
        common = {
            "scenario_id": scenario.scenario_id,
            "scenario_kind": scenario.kind.value,
            "status": scenario.status.value,
            "duration_ms": scenario.duration_ms,
            "scenario_observation_span_ms": getattr(
                scenario, "scenario_observation_span_ms", None
            ),
            "scenario_observation_span_status": getattr(
                getattr(scenario, "scenario_observation_span_status", None),
                "value",
                getattr(scenario, "scenario_observation_span_status", None),
            ),
            "scenario_observation_timing_source": getattr(
                getattr(scenario, "scenario_observation_timing_source", None),
                "value",
                getattr(scenario, "scenario_observation_timing_source", None),
            ),
            "scenario_hook_span_status": getattr(
                getattr(scenario, "scenario_hook_span_status", None),
                "value",
                getattr(scenario, "scenario_hook_span_status", None),
            ),
            "scenario_pre_to_post_hook_span_ms": getattr(
                scenario, "scenario_pre_to_post_hook_span_ms", None
            ),
            "tool_duration_sum_ms": getattr(scenario, "tool_duration_sum_ms", None),
            "error_count": len(scenario.errors),
            "command_matched": getattr(scenario, "command_matched", None),
            "process_identity_matched": getattr(scenario, "process_identity_matched", None),
            "input_matched": getattr(scenario, "input_matched", None),
            "file_fixture_read_observed": getattr(scenario, "file_fixture_read_observed", False),
            "cursor_unit": getattr(scenario, "cursor_unit", "character"),
            "timeout_seconds": getattr(scenario, "scenario_timeout_seconds", None),
            "scenario_timeout_seconds": getattr(
                scenario, "scenario_timeout_seconds", None
            ),
            "hard_timeout_source": getattr(
                getattr(scenario, "hard_timeout_source", None),
                "value",
                getattr(scenario, "hard_timeout_source", None),
            ),
            "hard_timeout_seconds": getattr(scenario, "hard_timeout_seconds", None),
            "hard_timeout_triggered": getattr(scenario, "hard_timeout_triggered", None),
            "trial_watchdog_timed_out": getattr(scenario, "trial_watchdog_timed_out", None),
            "scenario_watchdog_timed_out": getattr(scenario, "scenario_watchdog_timed_out", None),
            "scenario_observation_span_exceeded": getattr(
                scenario, "scenario_observation_span_exceeded", None
            ),
            "wait_remaining_budget_status": getattr(
                getattr(scenario, "wait_remaining_budget_status", None),
                "value",
                getattr(scenario, "wait_remaining_budget_status", None),
            ),
            "process_start_pre_hook_available": getattr(
                scenario, "process_start_pre_hook_available", None
            ),
            "wait_pre_hook_available": getattr(
                scenario, "wait_pre_hook_available", None
            ),
            "elapsed_before_wait_ms": getattr(scenario, "elapsed_before_wait_ms", None),
            "scenario_remaining_before_wait_seconds": getattr(
                scenario, "scenario_remaining_before_wait_seconds", None
            ),
            "wait_timeout_budget_matched": getattr(
                scenario, "wait_timeout_budget_matched", None
            ),
            "wait_budget_timing_source": getattr(
                getattr(scenario, "wait_budget_timing_source", None),
                "value",
                getattr(scenario, "wait_budget_timing_source", None),
            ),
            "hard_watchdog_fallback_allowed": getattr(
                scenario, "hard_watchdog_fallback_allowed", None
            ),
            "hard_watchdog_fallback_used": getattr(
                scenario, "hard_watchdog_fallback_used", None
            ),
            "event_alignment_passed": not any(
                getattr(scenario, field_name, ())
                for field_name in (
                    "unexpected_events",
                    "missing_expected_events",
                    "event_order_violations",
                    "foreign_process_events",
                    "unconsumed_events",
                )
            ),
            "unexpected_event_count": len(
                getattr(scenario, "unexpected_events", ())
            ),
            "missing_expected_event_count": len(
                getattr(scenario, "missing_expected_events", ())
            ),
            "event_order_violation_count": len(
                getattr(scenario, "event_order_violations", ())
            ),
            "foreign_process_event_count": len(
                getattr(scenario, "foreign_process_events", ())
            ),
            "unconsumed_event_count": len(
                getattr(scenario, "unconsumed_events", ())
            ),
            "scenario_observation_started_at": (
                scenario.scenario_observation_started_at.isoformat()
                if getattr(scenario, "scenario_observation_started_at", None) is not None
                else None
            ),
            "scenario_observation_completed_at": (
                scenario.scenario_observation_completed_at.isoformat()
                if getattr(scenario, "scenario_observation_completed_at", None) is not None
                else None
            ),
            "agent_close_required": getattr(scenario, "agent_close_required", False),
            "agent_close_observed": getattr(scenario, "agent_close_observed", False),
            "worker_cleanup_completed": getattr(
                getattr(scenario, "worker_cleanup_result", None),
                "complete",
                None,
            ),
            "content_omitted": True,
        }
        observation = root.start_observation(
            name=SCENARIO_NAME,
            as_type="span",
            input={"content_omitted": True},
            output={"content_omitted": True, "status": scenario.status.value},
            metadata=common,
            version="p6.1",
        )
        try:
            steps = getattr(scenario, "steps", ())
            for step in steps:
                step_plan = step_plans.get(step.step_id)
                expected_step_status = next(
                    (
                        getattr(step_plan, field_name, None)
                        for field_name in (
                            "expected_initial_status",
                            "expected_status",
                            "expected_terminal_status",
                        )
                        if getattr(step_plan, field_name, None) is not None
                    ),
                    None,
                )
                actual_step_status = step.actual_status
                step_metadata = {
                    "scenario_id": scenario.scenario_id,
                    "step_id": step.step_id,
                    "action": step.action.value,
                    "status": step.status.value,
                    "duration_ms": step.duration_ms,
                    "actual_action": step.actual_action,
                    "expected_process_status": (
                        None
                        if expected_step_status is None
                        else expected_step_status.value
                    ),
                    "actual_status": (
                        None if actual_step_status is None else actual_step_status.value
                    ),
                    "process_status_matched": (
                        None
                        if expected_step_status is None or actual_step_status is None
                        else expected_step_status is actual_step_status
                    ),
                    "timeout_seconds": step.timeout_seconds,
                    "timing_status": getattr(
                        getattr(step, "timing_status", None),
                        "value",
                        getattr(step, "timing_status", None),
                    ),
                    "timing_source": getattr(
                        getattr(step, "timing_source", None),
                        "value",
                        getattr(step, "timing_source", None),
                    ),
                    "timed_out": step.timed_out,
                    "event_pre_hook_offset_ms": getattr(
                        step, "event_pre_hook_offset_ms", None
                    ),
                    "event_post_hook_offset_ms": getattr(
                        step, "event_post_hook_offset_ms", None
                    ),
                    "event_pre_hook_source": getattr(
                        getattr(step, "event_pre_hook_source", None),
                        "value",
                        getattr(step, "event_pre_hook_source", None),
                    ),
                    "event_post_hook_source": getattr(
                        getattr(step, "event_post_hook_source", None),
                        "value",
                        getattr(step, "event_post_hook_source", None),
                    ),
                    "elapsed_before_wait_ms": getattr(
                        step, "elapsed_before_wait_ms", None
                    ),
                    "scenario_remaining_before_wait_seconds": getattr(
                        step, "scenario_remaining_before_wait_seconds", None
                    ),
                    "wait_remaining_budget_status": getattr(
                        getattr(step, "wait_remaining_budget_status", None),
                        "value",
                        getattr(step, "wait_remaining_budget_status", None),
                    ),
                    "wait_timeout_budget_matched": getattr(
                        step, "wait_timeout_budget_matched", None
                    ),
                    "wait_budget_timing_source": getattr(
                        getattr(step, "wait_budget_timing_source", None),
                        "value",
                        getattr(step, "wait_budget_timing_source", None),
                    ),
                    "hard_watchdog_fallback_allowed": getattr(
                        step, "hard_watchdog_fallback_allowed", None
                    ),
                    "hard_watchdog_fallback_used": getattr(
                        step, "hard_watchdog_fallback_used", None
                    ),
                    "expected_process_id_safe": step.expected_process_id_safe,
                    "actual_process_id_safe": step.actual_process_id_safe,
                    "process_identity_matched": step.process_identity_matched,
                    "observation_ref_count": len(step.observation_refs),
                    "content_omitted": True,
                }
                child = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="span",
                    input={"content_omitted": True},
                    output={"content_omitted": True, "status": step.status.value},
                    metadata=step_metadata,
                    version="p6.1",
                )
                child.end()
            for checkpoint in getattr(scenario, "checkpoints", ()):
                checkpoint_plan = checkpoint_plans.get(checkpoint.checkpoint_id)
                expected_process_status = getattr(
                    checkpoint_plan,
                    "expected_process_status",
                    None,
                )
                actual_process_status = checkpoint.observed_process_status
                is_output_checkpoint = checkpoint.kind.value == "output"
                checkpoint_span = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="event",
                    input={"content_omitted": True},
                    output={"content_omitted": True},
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "action": "checkpoint",
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "required": checkpoint.required,
                        "passed": checkpoint.passed,
                        "kind": checkpoint.kind.value,
                        "expected_process_status": (
                            None
                            if expected_process_status is None
                            else expected_process_status.value
                        ),
                        "actual_process_status": (
                            None
                            if actual_process_status is None
                            else actual_process_status.value
                        ),
                        "process_status_matched": (
                            None
                            if expected_process_status is None
                            or actual_process_status is None
                            else expected_process_status is actual_process_status
                        ),
                        "target_step_id": checkpoint.target_step_id,
                        "target_artifact_id": getattr(checkpoint, "target_artifact_id", None),
                        "observed_step_status": (
                            None if checkpoint.observed_step_status is None
                            else checkpoint.observed_step_status.value
                        ),
                        "observed_process_status": (
                            None if checkpoint.observed_process_status is None
                            else checkpoint.observed_process_status.value
                        ),
                        "agent_close_observed": checkpoint.agent_close_observed,
                        "worker_cleanup_completed": checkpoint.worker_cleanup_completed,
                        "artifact_exists": getattr(checkpoint, "artifact_exists", None),
                        "content_sha256": getattr(checkpoint, "content_sha256", None),
                        "content_char_length": getattr(checkpoint, "content_char_length", None),
                        "content_utf8_bytes": getattr(checkpoint, "content_utf8_bytes", None),
                        "required_markers_found": getattr(checkpoint, "required_markers_found", []),
                        "missing_required_markers": getattr(checkpoint, "missing_required_markers", []),
                        "forbidden_markers_found": getattr(checkpoint, "forbidden_markers_found", []),
                        "required_marker_count": getattr(checkpoint, "required_marker_count", 0),
                        "missing_required_marker_count": getattr(checkpoint, "missing_required_marker_count", 0),
                        "forbidden_marker_count": getattr(checkpoint, "forbidden_marker_count", 0),
                        "output_checkpoint_passed": (
                            checkpoint.passed if is_output_checkpoint else None
                        ),
                        "truncated": getattr(checkpoint, "truncated", None),
                        "content_omitted": True,
                    },
                    version="p6.1",
                )
                checkpoint_span.end()
            for read in getattr(scenario, "incremental_reads", ()):
                read_span = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="event",
                    input={"content_omitted": True},
                    output={"content_omitted": True},
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "action": "read_incremental",
                        "step_id": read.step_id,
                        "read_index": read.read_index,
                        "cursor_unit": read.cursor_unit,
                        "cursor_before": read.cursor_before,
                        "cursor_after": read.cursor_after,
                        "cursor_source_step_id": read.cursor_source_step_id,
                        "cursor_reference_matched": read.cursor_reference_matched,
                        "cursor_chain_matched": read.cursor_chain_matched,
                        "new_output_char_length": read.new_output_char_length,
                        "new_output_utf8_bytes": read.new_output_utf8_bytes,
                        "content_sha256": read.content_sha256,
                        "required_markers_found": read.required_markers_found,
                        "required_markers_missing": read.required_markers_missing,
                        "forbidden_markers_found": read.forbidden_markers_found,
                        "required_marker_count": len(read.required_markers_found),
                        "missing_required_marker_count": len(read.required_markers_missing),
                        "forbidden_marker_count": len(read.forbidden_markers_found),
                        "truncated": read.truncated,
                        "content_omitted": True,
                    },
                    version="p6.1",
                )
                read_span.end()
            for event in getattr(scenario, "input_events", ()):
                input_span = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="event",
                    input={"content_omitted": True},
                    output={"content_omitted": True},
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "action": "send_input",
                        "submitted": event.submitted,
                        "accepted": event.accepted,
                        "expected_input_sha256": event.expected_input_sha256,
                        "actual_input_sha256": event.actual_input_sha256,
                        "expected_input_char_length": event.expected_input_char_length,
                        "actual_input_char_length": event.actual_input_char_length,
                        "expected_input_utf8_bytes": event.expected_input_utf8_bytes,
                        "actual_input_utf8_bytes": event.actual_input_utf8_bytes,
                        "input_matched": event.input_matched,
                        "file_fixture_read_observed": event.file_fixture_read_observed,
                        "file_fixture_read_sha256": event.file_fixture_read_sha256,
                        "file_fixture_read_char_length": event.file_fixture_read_char_length,
                        "file_fixture_read_utf8_bytes": event.file_fixture_read_utf8_bytes,
                        "process_identity_matched": event.process_identity_matched,
                        "bytes_written": event.bytes_written,
                        "content_omitted": True,
                    },
                    version="p6.1",
                )
                input_span.end()
            for tool in getattr(scenario, "tool_calls", ()):
                tool_span = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="event",
                    input={"content_omitted": True},
                    output={"content_omitted": True},
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "action": "tool_trace",
                        "tool_name": tool.tool_name,
                        "call_count": tool.call_count,
                        "successful_count": tool.successful_count,
                        "content_omitted": True,
                    },
                    version="p6.1",
                )
                tool_span.end()
            cleanup = getattr(scenario, "worker_cleanup_result", None)
            if cleanup is not None:
                cleanup_span = observation.start_observation(
                    name=PROCESS_STEP_NAME,
                    as_type="event",
                    input={"content_omitted": True},
                    output={"content_omitted": True},
                    metadata={
                        "scenario_id": scenario.scenario_id,
                        "action": "worker_cleanup",
                        "gate": cleanup.complete,
                        "session_cleanup_completed": cleanup.session_cleanup_completed,
                        "live_process_count_before": cleanup.live_process_count_before,
                        "live_process_count_after": cleanup.live_process_count_after,
                        "attempted_count": len(cleanup.attempted_process_ids),
                        "completed_count": len(cleanup.completed_process_ids),
                        "unresolved_count": len(cleanup.unresolved_process_ids),
                        "content_omitted": True,
                    },
                    version="p6.1",
                )
                cleanup_span.end()
        finally:
            observation.end()


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
                "prompt_cache_hit_tokens": item.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": item.prompt_cache_miss_tokens,
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
                "deepseek_cache_status": (
                    "not_evaluated"
                    if item.prompt_cache_hit_tokens is None
                    else "available"
                ),
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
                BACKGROUND_REVIEW_EVALUATOR_NAME
                if metric.source is MetricSource.BACKGROUND_REVIEW
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
        )
        is_background_review_metric = metric.source is MetricSource.BACKGROUND_REVIEW
        evaluator = root.start_observation(
            name=name,
            as_type="evaluator",
            input={"metric_name": metric.metric_name},
            output=(
                _safe_background_review_metric(metric)
                if is_background_review_metric
                else project_remote_content(
                    {
                        "status": metric.status.value,
                        "value": metric.value,
                        "passed": metric.passed,
                        "reason": metric.reason,
                    },
                    classification=request.data_classification,
                    no_content=request.no_content,
                    sensitive_values=sensitive_values,
                )
            ),
            metadata={
                "metric_source": metric.source.value,
                "evaluator_version": metric.evaluator_version,
                "post_hoc_publication": True,
            },
            version=(
                "p5"
                if is_background_review_metric
                else (
                    "p4"
                    if metric.source is MetricSource.COMPRESSION
                    else ("p3" if metric.source is MetricSource.RETRIEVAL else "p2")
                )
            ),
        )
        evaluator.end()


def _safe_background_review_metric(metric) -> dict:
    """Project a fixed P5 metric allow-list, never arbitrary validator data."""

    value = metric.value if isinstance(metric.value, dict) else {}
    metric_type = metric.metadata.get("metric_type")
    safe_value: dict[str, object] = {}
    if metric_type == "decision_correctness":
        checks = value.get("checks")
        safe_value = {
            "status": _safe_review_status(value.get("status")),
            "actual_action": _safe_review_action(value.get("actual_action")),
            "has_actual_target": _safe_bool(value.get("has_actual_target")),
            "checks": _safe_review_decision_checks(checks),
        }
        if "expected_action" in value:
            safe_value["expected_action"] = _safe_review_action(
                value.get("expected_action")
            )
        if "allowed_actions" in value:
            safe_value["allowed_actions"] = _safe_review_actions(
                value.get("allowed_actions")
            )
        if "action_matched" in value:
            safe_value["action_matched"] = _safe_bool(value.get("action_matched"))
    elif metric_type == "evidence_completeness":
        safe_value = {
            "prepared_evidence_count": value.get("prepared_evidence_count"),
            "missing_required_kinds": _safe_review_evidence_kinds(
                value.get("missing_required_kinds")
            ),
            "present_forbidden_kinds": _safe_review_evidence_kinds(
                value.get("present_forbidden_kinds")
            ),
            "unlinked_prepared_evidence_count": value.get(
                "unlinked_prepared_evidence_count"
            ),
            "outside_foreground_window_count": value.get(
                "outside_foreground_window_count"
            ),
            "prepared_order_valid": value.get("prepared_order_valid"),
        }
    elif metric_type == "update_correctness":
        safe_value = {
            "changed_target_count": value.get("changed_target_count"),
            "missing_required_change_count": _safe_list_count(
                value.get("missing_required_changes")
            ),
            "forbidden_change_count": _safe_list_count(
                value.get("forbidden_changes")
            ),
            "unexpected_change_count": _safe_list_count(
                value.get("unexpected_changes")
            ),
            "expected_target_revision_matched": value.get(
                "expected_target_revision_matched"
            ),
        }
    elif metric_type == "stale_rejection":
        safe_value = {
            "status": value.get("status"),
            "stale_rejected": value.get("stale_rejected"),
            "changed_after_stale": value.get("changed_after_stale"),
        }
    elif metric_type == "side_effect_safety":
        safe_value = {
            "protected_changed_count": _safe_list_count(
                value.get("protected_changed")
            ),
            "unexpected_change_count": _safe_list_count(
                value.get("unexpected_changes")
            ),
            "half_write_detected": value.get("half_write_detected"),
            "status": value.get("status"),
        }
    elif metric_type == "idempotency":
        safe_value = {
            "attempt_count": value.get("attempt_count"),
            "duplicate_rejected": value.get("duplicate_rejected"),
            "first_attempt_executed": value.get("first_attempt_executed"),
            "second_attempt_zero_side_effects": value.get(
                "second_attempt_zero_side_effects"
            ),
        }
    return {
        "status": metric.status.value,
        "passed": metric.passed,
        "metric_type": metric_type,
        "review_id": metric.metadata.get("review_id"),
        "value": safe_value,
        "content_omitted": True,
    }


def project_regression_metadata(report: AuditRegressionReport) -> dict[str, Any]:
    """Return a content-free comparison projection for an existing mapper.

    This helper performs no network publication.  It is intentionally limited
    to IDs, statuses, counts, deltas, and safe failure codes.
    """

    return {
        "baseline_id": report.baseline_id,
        "current_run_id": report.current_run_id,
        "comparability_status": report.status.value,
        "comparability_reasons": list(report.comparability_reasons),
        "baseline_trial_count": report.baseline_trial_count,
        "current_trial_count": report.current_trial_count,
        "baseline_total_trial_count": report.baseline_total_trial_count,
        "current_total_trial_count": report.current_total_trial_count,
        "baseline_declared_trials_per_case": report.baseline_declared_trials_per_case,
        "current_declared_trials_per_case": report.current_declared_trials_per_case,
        "suite_task_success": {
            "baseline_sample_count": report.baseline_suite_task_success_sample_count,
            "baseline_passed_count": report.baseline_suite_task_success_passed_count,
            "baseline_rate": report.baseline_suite_task_success_rate,
            "current_sample_count": report.current_suite_task_success_sample_count,
            "current_passed_count": report.current_suite_task_success_passed_count,
            "current_rate": report.current_suite_task_success_rate,
            "delta": report.suite_task_success_rate_delta,
        },
        "regression_count": report.regression_count,
        "improvement_count": report.improvement_count,
        "unchanged_count": report.unchanged_count,
        "warning_count": report.warning_count,
        "not_comparable_count": report.not_comparable_count,
        "not_evaluated_count": report.not_evaluated_count,
        "overall_regression_gate": report.overall_regression_gate,
        "case_task_success": {
            item.case_id: {
                "baseline_sample_count": item.baseline_task_success_sample_count,
                "baseline_passed_count": item.baseline_task_success_passed_count,
                "baseline_rate": item.baseline_task_success_rate,
                "current_sample_count": item.current_task_success_sample_count,
                "current_passed_count": item.current_task_success_passed_count,
                "current_rate": item.current_task_success_rate,
                "delta": item.task_success_rate_delta,
                "baseline_trial_count": item.baseline_trial_count,
                "current_trial_count": item.current_trial_count,
                "decision": item.decision.value,
            }
            for item in report.case_summaries
        },
        "metric_deltas": [
            {
                "metric_name": item.metric_name,
                "baseline_value": _safe_regression_scalar(item.baseline_value),
                "current_value": _safe_regression_scalar(item.current_value),
                "absolute_delta": _safe_regression_scalar(item.absolute_delta),
                "relative_delta": item.relative_delta,
                "baseline_sample_count": item.baseline_sample_count,
                "current_sample_count": item.current_sample_count,
                "decision": item.decision.value,
                "reason": item.reason,
            }
            for item in report.suite_metrics
        ],
    }


def _safe_regression_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _safe_review_evidence_kinds(value: object) -> list[str]:
    """Keep only the closed public evidence-kind vocabulary in a metric."""

    allowed = {
        "user_message",
        "tool_observation",
        "tool_error",
        "assistant_decision_unverified",
        "assistant_report_unverified",
    }
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item in allowed]


def _safe_review_action(value: object) -> str | None:
    allowed = {"no_op", "create", "update", "replace", "remove", "reject"}
    return value if isinstance(value, str) and value in allowed else None


def _safe_review_actions(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and _safe_review_action(item) is not None
    ]


def _safe_review_status(value: object) -> str | None:
    allowed = {"pending", "running", "completed", "failed", "rejected", "stale"}
    return value if isinstance(value, str) and value in allowed else None


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _trial_metric_rate(trial, *, source: MetricSource, metric_type: str) -> float | None:
    values = [
        metric.passed
        for metric in trial.metrics
        if metric.source is source
        and metric.status is MetricStatus.COMPLETED
        and type(metric.passed) is bool
        and (
            metric.metadata.get("metric_type") == metric_type
            or metric.metadata.get("evaluator_kind") == metric_type
        )
    ]
    return None if not values else float(sum(values) / len(values))


def _safe_review_decision_checks(value: object) -> dict[str, bool]:
    allowed = {
        "terminal_status",
        "expected_action",
        "allowed_actions",
        "must_be_no_op",
        "expected_target",
        "expected_stale",
        "execution_not_failed",
    }
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and key in allowed and isinstance(item, bool)
    }


def _safe_list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


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


def _publish_background_reviews(root: Any, request: LangfuseTrialRequest) -> None:
    """Replay only safe P5 facts; never publish evidence or state bodies."""

    trial = request.trial
    if not _has_background_review_facts(request):
        return
    for result in trial.background_review_results:
        review = root.start_observation(
            name=BACKGROUND_REVIEW_NAME,
            as_type="span",
            input={
                "review_id": result.review_id,
                "kind": result.kind.value,
                "lifecycle": result.lifecycle.value,
            },
            output={
                "status": result.status.value,
                "actual_action": result.actual_action.value,
                "has_actual_target": result.actual_target is not None,
                "attempt_count": result.attempt_count,
                "duplicate_rejected": result.duplicate_rejected,
                "stale_rejected": result.stale_rejected,
                "duration_ms": result.duration_ms,
                "error_types": [item.error_type for item in result.errors],
            },
            metadata={
                "post_hoc_publication": True,
                "content_uploaded": False,
                "foreground_evidence_count": len(result.foreground_evidence),
                "prepared_evidence_count": len(result.subject_review_evidence),
                "tool_observation_count": len(result.tool_observations),
                "observed_change_count": len(result.observed_changes),
            },
            version="p5",
        )
        try:
            _publish_review_evidence(
                review,
                result.review_id,
                result.foreground_evidence,
                name=BACKGROUND_REVIEW_FOREGROUND_EVIDENCE_NAME,
                source="foreground",
            )
            _publish_review_evidence(
                review,
                result.review_id,
                result.subject_review_evidence,
                name=BACKGROUND_REVIEW_PREPARED_EVIDENCE_NAME,
                source="subject_prepared",
            )
            for item in result.tool_observations:
                observation = review.start_observation(
                    name=BACKGROUND_REVIEW_TOOL_NAME,
                    as_type="tool",
                    input={"content_omitted": True},
                    output={
                        "success": item.success,
                        "status": item.status,
                        "error_type": item.error_type,
                    },
                    metadata={
                        "review_id": result.review_id,
                        "tool_name": item.tool_name,
                        "duration_ms": item.duration_ms,
                        "post_hoc_publication": True,
                        "content_uploaded": False,
                    },
                    version="p5",
                )
                observation.end()
            for phase, snapshot in (
                ("before", result.before_snapshot),
                ("after", result.after_snapshot),
            ):
                if snapshot is not None:
                    _publish_review_snapshot(
                        review,
                        review_id=result.review_id,
                        phase=phase,
                        snapshot=snapshot,
                    )
            for change in result.observed_changes:
                observation = review.start_observation(
                    name=BACKGROUND_REVIEW_CHANGE_NAME,
                    as_type="span",
                    input={
                        "target_type": change.target_type,
                        "target_id_sha256": _identifier_sha256(change.target_id),
                    },
                    output={
                        "action": change.action.value,
                        "before_hash": change.before_hash,
                        "after_hash": change.after_hash,
                        "before_governance_revision": change.before_governance_revision,
                        "after_governance_revision": change.after_governance_revision,
                    },
                    metadata={
                        "review_id": result.review_id,
                        "post_hoc_publication": True,
                        "content_uploaded": False,
                    },
                    version="p5",
                )
                observation.end()
            lifecycle = review.start_observation(
                name=BACKGROUND_REVIEW_LIFECYCLE_NAME,
                as_type="span",
                input={
                    "review_id": result.review_id,
                    "lifecycle": result.lifecycle.value,
                },
                output={
                    "attempts": [
                        {
                            "sequence": item.sequence,
                            "claim_valid": item.claim_valid,
                            "loop_executed": item.loop_executed,
                            "model_call_count": item.model_call_count,
                            "tool_call_count": item.tool_call_count,
                            "state_change_count": item.state_change_count,
                            "error_type": item.error_type,
                        }
                        for item in result.attempts
                    ],
                    "duplicate_rejected": result.duplicate_rejected,
                    "stale_rejected": result.stale_rejected,
                },
                metadata={"post_hoc_publication": True, "content_uploaded": False},
                version="p5",
            )
            lifecycle.end()
        finally:
            review.end()


def _publish_review_evidence(
    parent: Any,
    review_id: str,
    items: list,
    *,
    name: str,
    source: str,
) -> None:
    for item in items:
        observation = parent.start_observation(
            name=name,
            as_type="span",
            input={
                "review_id": review_id,
                "kind": item.kind.value,
                "sequence": item.sequence,
            },
            output={
                "content_sha256": item.content_sha256,
                "content_length": item.content_length,
                "source_turn_number": item.source_turn_number,
                "source_tool_call_id": item.source_tool_call_id,
                "source_evidence_id": item.source_evidence_id,
                "content_omitted": True,
            },
            metadata={
                "source": source,
                "post_hoc_publication": True,
                "content_uploaded": False,
            },
            version="p5",
        )
        observation.end()


def _publish_review_snapshot(
    parent: Any,
    *,
    review_id: str,
    phase: str,
    snapshot,
) -> None:
    observation = parent.start_observation(
        name=BACKGROUND_REVIEW_SNAPSHOT_NAME,
        as_type="span",
        input={"review_id": review_id, "phase": phase},
        output={
            "snapshot_id": snapshot.snapshot_id,
            "memory_item_count": (
                None if snapshot.memory is None else len(snapshot.memory.items)
            ),
            "skill_count": len(snapshot.skills),
            "skill_revisions": [
                {
                    "revision": item.revision,
                    "governance_revision": item.governance_revision,
                    "source": item.source.value,
                    "managed_by": item.managed_by.value,
                    "pinned": item.pinned,
                }
                for item in snapshot.skills
            ],
            "content_omitted": True,
        },
        metadata={"post_hoc_publication": True, "content_uploaded": False},
        version="p5",
    )
    observation.end()


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


def _has_background_review_facts(request: LangfuseTrialRequest) -> bool:
    trial = request.trial
    return bool(
        request.case.fixture.background_review_plans
        or trial.background_review_results
        or trial.background_review_errors
    )


def _identifier_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _cost_metadata(trial) -> dict[str, object]:
    """Project only safe cost facts; never include the pricing source note."""

    cost = trial.deepseek_cost
    if cost is None:
        return {
            "deepseek_cost_status": "not_evaluated",
            "deepseek_cost_model": None,
            "deepseek_cost_currency": None,
            "deepseek_cost_pricing_fingerprint": None,
        }

    def amount(value):
        return None if value is None else format(value, "f")

    return {
        "deepseek_cost_status": cost.status.value,
        "deepseek_cost_model": (
            None if cost.pricing_snapshot is None else cost.pricing_snapshot.model
        ),
        "deepseek_cost_currency": cost.currency,
        "deepseek_cost_pricing_fingerprint": cost.pricing_fingerprint,
        "deepseek_cost_classified_cost_usd": amount(cost.classified_cost_usd),
        "deepseek_cost_total_cost_usd": amount(cost.total_cost_usd),
        "deepseek_cost_estimated_without_cache_usd": amount(
            cost.estimated_cost_without_cache_usd
        ),
        "deepseek_cost_cache_savings_usd": amount(cost.cache_savings_usd),
        "deepseek_cost_cache_savings_rate": amount(cost.cache_savings_rate),
    }


__all__ = (
    "ABLATION_COMPARISON_NAME",
    "BACKGROUND_REVIEW_CHANGE_NAME",
    "BACKGROUND_REVIEW_EVALUATOR_NAME",
    "BACKGROUND_REVIEW_FOREGROUND_EVIDENCE_NAME",
    "BACKGROUND_REVIEW_LIFECYCLE_NAME",
    "BACKGROUND_REVIEW_NAME",
    "BACKGROUND_REVIEW_PREPARED_EVIDENCE_NAME",
    "BACKGROUND_REVIEW_SNAPSHOT_NAME",
    "BACKGROUND_REVIEW_TOOL_NAME",
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
    "project_regression_metadata",
)
