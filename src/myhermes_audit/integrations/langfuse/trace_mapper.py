"""Replay local Audit facts as children of an Experiment Runner task trace."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from myhermes_audit.contracts import MetricSource
from myhermes_audit.integrations.langfuse.redaction import project_remote_content
from myhermes_audit.ports.langfuse import LangfuseTrialRequest


TRACE_NAME = "myhermes.audit.trial"
TURN_NAME = "myhermes.audit.turn"
MODEL_NAME = "myhermes.agent.model"
TOOL_NAME = "myhermes.agent.tool"
VALIDATOR_NAME = "myhermes.audit.validator"
JUDGE_NAME = "myhermes.audit.judge"


def publish_replay_observations(
    client: Any,
    propagate_attributes: Callable[..., Any],
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    trial = request.trial
    runtime = trial.runtime
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
            runtime.subject_model
            if runtime is not None and runtime.subject_model is not None
            else "unavailable"
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
    with propagate_attributes(
        session_id=session_id[:200],
        metadata={
            "audit_suite_id": request.suite_id,
            "audit_case_id": request.case.case_id,
            "audit_trial_id": trial.trial_id,
        },
        tags=["myhermes-audit", "p2", request.case.mode.value],
        trace_name=TRACE_NAME,
    ):
        with client.start_as_current_observation(
            name=TRACE_NAME,
            as_type="span",
            input=root_input,
            output=root_output,
            metadata=metadata,
            version="p2",
        ) as root:
            _publish_turns(root, request, sensitive_values=sensitive_values)
            _publish_evaluators(root, request, sensitive_values=sensitive_values)


def _publish_turns(
    root: Any,
    request: LangfuseTrialRequest,
    *,
    sensitive_values: Iterable[str],
) -> None:
    observations = request.trial.observations
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
        name = JUDGE_NAME if metric.source is MetricSource.JUDGE else VALIDATOR_NAME
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
            version="p2",
        )
        evaluator.end()


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
        return {"message": case_input.message}
    return {
        "turns": [
            {"role": turn.role.value, "message": turn.message}
            for turn in case_input.turns
        ]
    }


__all__ = (
    "JUDGE_NAME",
    "MODEL_NAME",
    "TOOL_NAME",
    "TRACE_NAME",
    "TURN_NAME",
    "VALIDATOR_NAME",
    "publish_replay_observations",
)
