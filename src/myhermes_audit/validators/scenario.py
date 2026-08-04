"""Deterministic validation of P6.1 scenario observations."""

from __future__ import annotations

from myhermes_audit.contracts import (
    E2EScenarioKind,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
    ProcessScenarioPlan,
    ScenarioExecutionResult,
    ScenarioPlan,
    ScenarioStatus,
    ToolchainScenarioPlan,
    ProcessReadIncrementalStep,
)
from myhermes_audit.validators.base import ValidationContext


_EVALUATOR_VERSION = "p6.1"


def _error_metric(
    *,
    name: str,
    scenario_kind: E2EScenarioKind,
    reason: str,
    hard_gate: bool,
    metric_type: str = "scenario_error",
    error_type: str = "scenario_result_unavailable",
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.RUNTIME,
        status=MetricStatus.ERROR,
        value=None,
        passed=None,
        reason=reason,
        evidence=[
            MetricEvidence(
                evidence_id=f"{name}.evidence",
                kind="scenario_error",
                description="scenario execution result unavailable",
            )
        ],
        evaluator_version=_EVALUATOR_VERSION,
        error=MetricError(
            error_type=error_type,
            message=reason,
            details={"scenario_kind": scenario_kind.value},
        ),
        metadata={
            "scenario_kind": scenario_kind.value,
            "metric_type": metric_type,
            "hard_gate": hard_gate,
        },
    )


def _boolean_metric(
    *,
    name: str,
    scenario_kind: E2EScenarioKind,
    metric_type: str,
    passed: bool,
    reason: str,
    hard_gate: bool,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.RUNTIME,
        status=MetricStatus.COMPLETED,
        value=passed,
        passed=passed,
        reason=reason,
        evidence=[
            MetricEvidence(
                evidence_id=f"{name}.evidence",
                kind="scenario_observation",
                description="content-free scenario observation projection",
            )
        ],
        evaluator_version=_EVALUATOR_VERSION,
        metadata={
            "scenario_kind": scenario_kind.value,
            "metric_type": metric_type,
            "hard_gate": hard_gate,
        },
    )


def _result_for(
    context: ValidationContext,
    scenario_id: str,
) -> ScenarioExecutionResult | None:
    return next(
        (item for item in context.scenario_results if item.scenario_id == scenario_id),
        None,
    )


def evaluate_scenario_plan(
    plan: ScenarioPlan,
    context: ValidationContext,
    *,
    metric_prefix: str,
) -> list[MetricResult]:
    observed = _result_for(context, plan.scenario_id)
    if observed is None:
        metric_type = (
            "process_gate"
            if plan.kind is E2EScenarioKind.PROCESS_BACKGROUND
            else "toolchain_gate"
        )
        return [
            _error_metric(
                name=f"{metric_prefix}.status",
                scenario_kind=E2EScenarioKind(plan.kind.value),
                reason="Worker did not produce the declared scenario result",
                hard_gate=plan.required,
                metric_type=metric_type,
            )
        ]
    if plan.kind is E2EScenarioKind.TOOLCHAIN:
        return _evaluate_toolchain(
            plan,
            observed,
            metric_prefix=metric_prefix,
        )
    return _evaluate_process(
        plan,
        observed,
        metric_prefix=metric_prefix,
    )


def _evaluate_toolchain(
    plan: ToolchainScenarioPlan,
    observed: ScenarioExecutionResult,
    *,
    metric_prefix: str,
) -> list[MetricResult]:
    if observed.kind is not E2EScenarioKind.TOOLCHAIN:
        return [
            _error_metric(
                name=f"{metric_prefix}.status",
                scenario_kind=E2EScenarioKind.TOOLCHAIN,
                reason="Worker scenario kind does not match the declared plan",
                hard_gate=plan.required,
                metric_type="toolchain_gate",
            )
        ]
    checkpoints = {item.checkpoint_id: item for item in observed.checkpoints}
    checkpoint_passed = all(
        checkpoints.get(item.checkpoint_id) is not None
        and checkpoints[item.checkpoint_id].passed is True
        for item in plan.checkpoints
        if item.required
    )
    observed_tools = {item.tool_name: item for item in observed.tool_calls}
    trace_passed = all(
        observed_tools.get(item.tool_name) is not None
        and observed_tools[item.tool_name].call_count >= item.minimum_calls
        and observed_tools[item.tool_name].successful_count
        >= item.minimum_successful_calls
        for item in plan.trace_requirements
        if item.required
    )
    artifact_passed = all(
        item.exists
        for item in [*observed.input_artifacts, *observed.output_artifacts]
    )
    checkpoint_metrics: list[MetricResult] = []
    checkpoint_error_types = {
        "artifact_missing": "artifact_missing",
        "toolchain_artifact_target_error": "toolchain_artifact_target_error",
        "toolchain_artifact_read_error": "toolchain_artifact_read_error",
        "toolchain_required_marker_missing": "required_marker_missing",
        "toolchain_forbidden_marker_present": "forbidden_marker_present",
        "toolchain_minimum_length_error": "minimum_length_not_met",
    }
    for checkpoint in plan.checkpoints:
        if not checkpoint.required:
            continue
        result = checkpoints.get(checkpoint.checkpoint_id)
        if result is None:
            checkpoint_metrics.append(_error_metric(
                name=f"{metric_prefix}.checkpoint.{checkpoint.checkpoint_id}",
                scenario_kind=E2EScenarioKind.TOOLCHAIN,
                reason="required Toolchain checkpoint result is missing",
                hard_gate=plan.required,
                metric_type="toolchain_artifact_target_error",
                error_type="toolchain_artifact_target_error",
            ))
            continue
        if result.passed is not True:
            error_type = (
                None if result.error is None
                else checkpoint_error_types.get(
                    result.error.error_type,
                    result.error.error_type,
                )
            )
            if error_type is not None:
                checkpoint_metrics.append(_error_metric(
                    name=f"{metric_prefix}.checkpoint.{checkpoint.checkpoint_id}",
                    scenario_kind=E2EScenarioKind.TOOLCHAIN,
                    reason=result.error.message if result.error is not None else "required Toolchain checkpoint failed",
                    hard_gate=plan.required,
                    metric_type=error_type,
                    error_type=error_type,
                ))
            else:
                checkpoint_metrics.append(_boolean_metric(
                    name=f"{metric_prefix}.checkpoint.{checkpoint.checkpoint_id}",
                    scenario_kind=E2EScenarioKind.TOOLCHAIN,
                    metric_type="toolchain_checkpoint",
                    passed=False,
                    reason="required Toolchain checkpoint failed",
                    hard_gate=plan.required,
                ))
        else:
            checkpoint_metrics.append(_boolean_metric(
                name=f"{metric_prefix}.checkpoint.{checkpoint.checkpoint_id}",
                scenario_kind=E2EScenarioKind.TOOLCHAIN,
                metric_type="toolchain_checkpoint",
                passed=True,
                reason="required Toolchain checkpoint passed",
                hard_gate=plan.required,
            ))
    return [
        _boolean_metric(
            name=f"{metric_prefix}.status",
            scenario_kind=E2EScenarioKind.TOOLCHAIN,
            metric_type="toolchain_gate",
            passed=observed.status is ScenarioStatus.COMPLETED,
            reason="Toolchain scenario completed" if observed.status is ScenarioStatus.COMPLETED else "Toolchain scenario did not complete",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.trace",
            scenario_kind=E2EScenarioKind.TOOLCHAIN,
            metric_type="toolchain_trace",
            passed=trace_passed,
            reason="declared Toolchain trace requirements were observed" if trace_passed else "declared Toolchain trace requirements were not observed",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.artifacts",
            scenario_kind=E2EScenarioKind.TOOLCHAIN,
            metric_type="toolchain_artifacts",
            passed=artifact_passed,
            reason="declared Toolchain Artifacts exist" if artifact_passed else "a declared Toolchain input or output Artifact is missing",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.checkpoints",
            scenario_kind=E2EScenarioKind.TOOLCHAIN,
            metric_type="toolchain_checkpoints",
            passed=checkpoint_passed,
            reason="required Toolchain checkpoints passed" if checkpoint_passed else "a required Toolchain checkpoint failed",
            hard_gate=plan.required,
        ),
        *checkpoint_metrics,
    ]


def _evaluate_process(
    plan: ProcessScenarioPlan,
    observed: ScenarioExecutionResult,
    *,
    metric_prefix: str,
) -> list[MetricResult]:
    if observed.kind is not E2EScenarioKind.PROCESS_BACKGROUND:
        return [
            _error_metric(
                name=f"{metric_prefix}.status",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="Worker scenario kind does not match the declared plan",
                hard_gate=plan.required,
                metric_type="process_gate",
            )
        ]
    required_steps = {item.step_id for item in plan.steps if item.required}
    observed_steps = {item.step_id: item for item in observed.steps}
    steps_passed = all(
        observed_steps.get(step_id) is not None
        and observed_steps[step_id].status is ScenarioStatus.COMPLETED
        and observed_steps[step_id].error is None
        for step_id in required_steps
    )
    required_step_results = [
        observed_steps.get(step.step_id)
        for step in plan.steps
        if step.required
    ]
    step_action_passed = all(
        item is not None and item.action_matched is True
        for item in required_step_results
    )
    step_timeout_passed = all(
        item is not None and item.duration_ms is not None and not item.timed_out
        for item in required_step_results
    )
    required_step_timing_missing = any(
        item is None or item.duration_ms is None
        for item in required_step_results
    )
    required_read_steps = [
        item
        for item in plan.steps
        if isinstance(item, ProcessReadIncrementalStep) and item.required
    ]
    observed_reads = {item.step_id: item for item in observed.incremental_reads}
    cursor_reference_missing = any(
        item.cursor_source_step_id is not None
        and (
            observed_reads.get(item.step_id) is None
            or observed_reads[item.step_id].cursor_source_step_id
            != item.cursor_source_step_id
            or observed_reads[item.step_id].cursor_reference_matched is not True
        )
        for item in required_read_steps
    )
    cursor_chain_mismatch = any(
        item.cursor_source_step_id is not None
        and (
            observed_reads.get(item.step_id) is None
            or observed_reads[item.step_id].cursor_chain_matched is not True
        )
        for item in required_read_steps
    )
    marker_passed = all(
        item is not None and item.status is ScenarioStatus.COMPLETED
        for item in required_step_results
        if item is not None and item.action.value == "read_incremental"
    )
    cleanup_required = plan.cleanup is not None and plan.cleanup.required
    cleanup_passed = (
        not cleanup_required
        or (
            observed.worker_cleanup_result is not None
            and observed.worker_cleanup_result.complete
            and (
                not plan.cleanup.expect_no_live_processes
                or observed.worker_cleanup_result.live_process_count_after == 0
            )
        )
    )
    observed_checkpoints = {
        item.checkpoint_id: item for item in observed.checkpoints
    }
    checkpoints_passed = all(
        observed_checkpoints.get(item.checkpoint_id) is not None
        and observed_checkpoints[item.checkpoint_id].passed is True
        for item in plan.checkpoints
        if item.required
    )
    status_passed = observed.status is ScenarioStatus.COMPLETED
    command_identity_passed = observed.command_matched is True
    process_identity_passed = observed.process_identity_matched is True
    input_identity_passed = observed.input_matched is not False
    cursor_passed = all(
        item.cursor_unit == "character"
        and item.cursor_after >= item.cursor_before
        and item.new_output_char_length == item.cursor_after - item.cursor_before
        and (item.cursor_reference_matched is not False)
        and (item.cursor_chain_matched is not False)
        for item in observed.incremental_reads
    )
    status_transition_passed = observed.status_transitions_valid is True
    scenario_timeout_passed = (
        observed.duration_ms is not None and not observed.scenario_timed_out
    )
    agent_close_required = any(item.action.value == "close" and item.required for item in plan.steps)
    agent_close_passed = not agent_close_required or observed.agent_close_observed
    metric_specs = [
        ("process_command_identity", "command_identity", command_identity_passed, "declared and observed command identity matched"),
        ("process_identity", "process_identity", process_identity_passed, "all public Process calls referenced the start process"),
        ("process_input_identity", "input_identity", input_identity_passed, "fixture input matched the observed public input"),
        ("process_step_action", "step_action", step_action_passed, "required step actions matched public Tool observations"),
        ("process_cursor_integrity", "cursor_integrity", cursor_passed, "Process log cursor used character units without gaps"),
        ("process_cursor_reference", "cursor_reference_missing", not cursor_reference_missing, "later Process reads referenced the preceding read result"),
        ("process_cursor_chain", "cursor_chain_mismatch", not cursor_chain_mismatch, "Process read cursor references matched the runtime chain"),
        ("process_marker_expectations", "marker_expectations", marker_passed, "required and forbidden output markers were evaluated"),
        ("process_status_transitions", "status_transitions", status_transition_passed, "Process status did not return to active after terminal"),
        ("process_step_timeout", "step_timeout", step_timeout_passed, "required steps supplied real duration facts within timeout"),
        ("process_scenario_timeout", "scenario_timeout", scenario_timeout_passed, "scenario stayed within its hard deadline"),
        ("process_agent_close", "agent_close", agent_close_passed, "Agent close expectation was observed independently"),
        ("process_worker_cleanup", "worker_cleanup", cleanup_passed, "Worker cleanup report satisfied its lifecycle expectation"),
    ]
    process_gate = (
        status_passed
        and steps_passed
        and checkpoints_passed
        and all(item[2] for item in metric_specs)
    )

    def _process_metric(
        name: str,
        metric_type: str,
        passed: bool,
        reason: str,
    ) -> MetricResult:
        if metric_type == "step_timeout" and required_step_timing_missing:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process step timing was not present in public Observation facts",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=metric_type,
            )
        if metric_type == "scenario_timeout" and observed.duration_ms is None:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="Process scenario duration was not present in public Observation facts",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=metric_type,
            )
        if metric_type == "cursor_reference_missing" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="a required Process read cursor reference was missing or mismatched",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=metric_type,
            )
        if metric_type == "cursor_chain_mismatch" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="a required Process read cursor chain did not match",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=metric_type,
            )
        return _boolean_metric(
            name=f"{metric_prefix}.{name}",
            scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
            metric_type=metric_type,
            passed=passed,
            reason=reason if passed else f"{reason} was not proven",
            hard_gate=plan.required,
        )

    metrics = [
        _process_metric(name, metric_type, passed, reason)
        for name, metric_type, passed, reason in metric_specs
    ]
    metrics.insert(0, _boolean_metric(
        name=f"{metric_prefix}.status",
        scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
        metric_type="process_gate",
        passed=process_gate,
        reason="all required Process gates passed" if process_gate else "one or more required Process gates failed",
        hard_gate=plan.required,
    ))
    metrics.append(_boolean_metric(
        name=f"{metric_prefix}.steps",
        scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
        metric_type="process_steps",
        passed=steps_passed,
        reason="required Process steps passed" if steps_passed else "a required Process step failed",
        hard_gate=plan.required,
    ))
    metrics.append(_boolean_metric(
        name=f"{metric_prefix}.checkpoints",
        scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
        metric_type="process_checkpoints",
        passed=checkpoints_passed,
        reason="required Process checkpoints passed" if checkpoints_passed else "a required Process checkpoint failed",
        hard_gate=plan.required,
    ))
    return metrics


__all__ = ("evaluate_scenario_plan",)
