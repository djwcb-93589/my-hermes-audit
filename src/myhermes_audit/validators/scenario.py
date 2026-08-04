"""Deterministic validation of P6.1 scenario observations."""

from __future__ import annotations

from myhermes_audit.contracts import (
    E2EScenarioKind,
    MetricError,
    MetricEvidence,
    MetricResult,
    MetricSource,
    MetricStatus,
    ProcessHardTimeoutSource,
    ProcessObservationSpanStatus,
    ProcessTimingSource,
    ProcessScenarioPlan,
    ProcessTimingStatus,
    ScenarioExecutionResult,
    ScenarioPlan,
    ScenarioStatus,
    ToolchainScenarioPlan,
    ProcessReadIncrementalStep,
    WaitRemainingBudgetStatus,
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


def _not_applicable_metric(
    *,
    name: str,
    scenario_kind: E2EScenarioKind,
    metric_type: str,
    reason: str,
    hard_gate: bool,
) -> MetricResult:
    return MetricResult(
        metric_name=name,
        source=MetricSource.RUNTIME,
        status=MetricStatus.NOT_APPLICABLE,
        value=None,
        passed=None,
        reason=reason,
        evidence=[
            MetricEvidence(
                evidence_id=f"{name}.evidence",
                kind="scenario_observation",
                description="optional Process timing was not evaluable",
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
        item is not None
        and item.timing_status
        in {
            ProcessTimingStatus.AVAILABLE,
            ProcessTimingStatus.AVAILABLE_DURATION_ONLY,
        }
        and item.duration_ms is not None
        and item.timed_out is False
        for item in required_step_results
    )
    required_step_timing_missing = any(
        item is None
        or item.timing_status is ProcessTimingStatus.UNAVAILABLE
        for item in required_step_results
    )
    required_step_timing_invalid = any(
        item is not None and item.timing_status is ProcessTimingStatus.INVALID
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
    required_process_status_checkpoints = [
        item
        for item in plan.checkpoints
        if item.required and item.kind.value == "process_status"
    ]
    required_process_output_checkpoints = [
        item
        for item in plan.checkpoints
        if item.required and item.kind.value == "output"
    ]
    process_status_checkpoint_passed = all(
        observed_checkpoints.get(item.checkpoint_id) is not None
        and observed_checkpoints[item.checkpoint_id].kind.value == "process_status"
        and observed_checkpoints[item.checkpoint_id].passed is True
        for item in required_process_status_checkpoints
    )
    process_output_checkpoint_passed = all(
        observed_checkpoints.get(item.checkpoint_id) is not None
        and observed_checkpoints[item.checkpoint_id].kind.value == "output"
        and observed_checkpoints[item.checkpoint_id].passed is True
        for item in required_process_output_checkpoints
    )
    business_status_passed = (
        observed.final_status is not None
        and observed.final_status.value != "unknown"
        and all(
            observed_checkpoints.get(item.checkpoint_id) is not None
            and observed_checkpoints[item.checkpoint_id].passed is True
            for item in required_process_status_checkpoints
        )
    )
    status_passed = observed.status is ScenarioStatus.COMPLETED
    command_identity_passed = observed.command_matched is True
    process_identity_passed = observed.process_identity_matched is True
    required_input_steps = {
        item.step_id
        for item in plan.steps
        if item.required and item.action.value == "send_input"
    }
    input_identity_passed = (
        observed.input_matched is True
        if required_input_steps
        else observed.input_matched is not False
    )
    cursor_passed = all(
        item.cursor_unit == "character"
        and item.cursor_after >= item.cursor_before
        and item.new_output_char_length == item.cursor_after - item.cursor_before
        and (item.cursor_reference_matched is not False)
        and (item.cursor_chain_matched is not False)
        for item in observed.incremental_reads
    )
    status_transition_passed = observed.status_transitions_valid is True
    alignment_diagnostics = (
        list(getattr(observed, "unexpected_events", ()))
        + list(getattr(observed, "missing_expected_events", ()))
        + list(getattr(observed, "event_order_violations", ()))
        + list(getattr(observed, "foreign_process_events", ()))
        + list(getattr(observed, "unconsumed_events", ()))
    )
    event_alignment_passed = not alignment_diagnostics
    unexpected_event_gate_passed = not (
        getattr(observed, "unexpected_events", ())
        or getattr(observed, "foreign_process_events", ())
        or getattr(observed, "unconsumed_events", ())
    )
    event_order_gate_passed = not getattr(observed, "event_order_violations", ())
    scenario_observation_span_available = (
        observed.scenario_observation_span_status
        is ProcessObservationSpanStatus.AVAILABLE
        and observed.scenario_observation_timing_source
        is ProcessTimingSource.PUBLIC_OBSERVATION_PERSISTENCE
        and observed.scenario_observation_started_at is not None
        and observed.scenario_observation_completed_at is not None
        and observed.scenario_observation_span_ms is not None
    )
    scenario_observation_span_exceeded = (
        observed.scenario_observation_span_exceeded is True
    )
    scenario_observation_span_invalid = (
        observed.scenario_observation_span_status
        is ProcessObservationSpanStatus.INVALID
    )
    scenario_observation_span_passed = (
        not scenario_observation_span_invalid
        and not scenario_observation_span_exceeded
    )
    observed_hard_timeout_source = observed.hard_timeout_source
    scenario_hard_timeout_passed = (
        observed_hard_timeout_source
        in {
            ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG,
            ProcessHardTimeoutSource.TRIAL_WATCHDOG,
        }
        and observed.hard_timeout_seconds is not None
        and observed.hard_timeout_triggered is False
        and observed.scenario_watchdog_timed_out is False
        and observed.trial_watchdog_timed_out is False
    )
    wait_results = [
        item
        for step_id, item in observed_steps.items()
        if any(step.step_id == step_id and step.action.value == "wait" for step in plan.steps)
    ]
    if not wait_results:
        wait_remaining_budget_passed = True
        wait_remaining_budget_reason = "no WAIT step was declared"
    else:
        exact_wait_budget = all(
            item.wait_remaining_budget_status is WaitRemainingBudgetStatus.MATCHED
            and item.wait_timeout_budget_matched is True
            and item.hard_watchdog_fallback_used is False
            for item in wait_results
        )
        fallback_wait_budget = (
            all(
                item.wait_remaining_budget_status
                is WaitRemainingBudgetStatus.FALLBACK_USED
                and item.wait_timeout_budget_matched is None
                and item.hard_watchdog_fallback_allowed is True
                and item.hard_watchdog_fallback_used is True
                for item in wait_results
            )
            and observed.hard_watchdog_fallback_allowed
            and observed.hard_watchdog_fallback_used
            and scenario_hard_timeout_passed
            and observed.hard_timeout_source
            is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
        )
        wait_remaining_budget_passed = exact_wait_budget or fallback_wait_budget
        wait_remaining_budget_reason = (
            "Worker PRE-to-PRE remaining budget matched"
            if exact_wait_budget
            else (
                "remaining budget was unavailable; explicitly declared Process "
                "Scenario watchdog fallback was enabled and did not fire"
                if fallback_wait_budget
                else (
                    "remaining budget was mismatched"
                    if any(
                        item.wait_remaining_budget_status
                        is WaitRemainingBudgetStatus.MISMATCHED
                        for item in wait_results
                    )
                    else "remaining budget was unavailable and no explicit watchdog fallback passed"
                )
            )
        )
    trace_passed = all(
        any(
            item.tool_name == requirement.tool_name
            and item.call_count >= requirement.minimum_calls
            and item.successful_count >= requirement.minimum_successful_calls
            for item in observed.tool_calls
        )
        for requirement in plan.trace_requirements
        if requirement.required
    )
    fixture_read_required = any(
        requirement.required and requirement.tool_name == "file"
        for requirement in plan.trace_requirements
    )
    fixture_read_passed = (
        not fixture_read_required or observed.file_fixture_read_observed
    )
    agent_close_required = any(item.action.value == "close" and item.required for item in plan.steps)
    agent_close_passed = not agent_close_required or observed.agent_close_observed
    metric_specs = [
        ("process_event_alignment", "event_alignment", event_alignment_passed, "declared Process steps aligned to public events without omissions or extras"),
        ("process_unexpected_event_gate", "unexpected_event_gate", unexpected_event_gate_passed, "no unexpected, foreign, or trailing Process events were observed"),
        ("process_event_order_gate", "event_order_gate", event_order_gate_passed, "public Process events respected declared step order"),
        ("process_command_identity", "command_identity", command_identity_passed, "declared and observed command identity matched"),
        ("process_identity", "process_identity", process_identity_passed, "all public Process calls referenced the start process"),
        ("process_input_identity", "input_identity", input_identity_passed, "fixture input matched the observed public input"),
        ("process_business_status", "business_status", business_status_passed, "declared Process status and lifecycle outcome were observed"),
        ("process_status_checkpoint", "process_status_checkpoint", process_status_checkpoint_passed, "required public Process status checkpoints matched"),
        ("process_output_checkpoint", "process_output_checkpoint", process_output_checkpoint_passed, "required incremental Process output checkpoints matched"),
        ("process_step_action", "step_action", step_action_passed, "required step actions matched public Tool observations"),
        ("process_cursor_integrity", "cursor_integrity", cursor_passed, "Process log cursor used character units without gaps"),
        ("process_cursor_reference", "cursor_reference_missing", not cursor_reference_missing, "later Process reads referenced the preceding read result"),
        ("process_cursor_chain", "cursor_chain_mismatch", not cursor_chain_mismatch, "Process read cursor references matched the runtime chain"),
        ("process_marker_expectations", "marker_expectations", marker_passed, "required and forbidden output markers were evaluated"),
        ("process_status_transitions", "status_transitions", status_transition_passed, "Process status did not return to active after terminal"),
        ("process_trace", "process_trace", trace_passed, "required public Tool trace calls were observed"),
        ("process_fixture_read", "fixture_read", fixture_read_passed, "input fixture was read by the public file tool before submit"),
        ("process_step_duration", "step_duration_gate", not required_step_timing_missing and not required_step_timing_invalid, "required Process steps supplied public handler duration facts"),
        ("process_step_timing", "step_timing", not required_step_timing_missing and not required_step_timing_invalid, "required Process steps supplied public handler duration facts"),
        ("process_step_timeout", "step_timeout", step_timeout_passed, "required steps supplied real duration facts within timeout"),
        ("process_scenario_observation_span", "scenario_observation_span_gate", scenario_observation_span_passed, "public persistence observation span was within the hard budget or was not available for diagnosis"),
        ("process_scenario_hard_timeout", "scenario_hard_timeout_gate", scenario_hard_timeout_passed, "the applicable Worker watchdog was configured and did not fire"),
        ("process_wait_remaining_budget", "wait_remaining_budget_gate", wait_remaining_budget_passed, wait_remaining_budget_reason),
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
        if metric_type == "step_duration_gate" and required_step_timing_missing:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process handler duration was not present in public Observation facts",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_step_timing_unavailable",
            )
        if metric_type == "step_duration_gate" and required_step_timing_invalid:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process handler duration was invalid",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_step_timing_invalid",
            )
        if metric_type == "step_timing" and required_step_timing_missing:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process step timing was not present in public Observation facts",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_step_timing_unavailable",
            )
        if metric_type == "step_timing" and required_step_timing_invalid:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process step timing was invalid",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_step_timing_invalid",
            )
        if metric_type == "step_timeout" and (
            required_step_timing_missing or required_step_timing_invalid
        ):
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="required Process step timeout could not be evaluated without reliable timing",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=(
                    "process_step_timing_invalid"
                    if required_step_timing_invalid
                    else "process_step_timing_unavailable"
                ),
            )
        if metric_type == "step_timeout" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="a required Process step exceeded its timeout budget",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_step_timeout",
            )
        if (
            metric_type == "scenario_observation_span_gate"
            and not scenario_observation_span_available
            and not scenario_observation_span_invalid
            and not scenario_observation_span_exceeded
        ):
            return _not_applicable_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                metric_type=metric_type,
                reason="public persistence observation span was unavailable; this is diagnostic only",
                hard_gate=False,
            )
        if (
            metric_type == "scenario_observation_span_gate"
            and scenario_observation_span_invalid
        ):
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="public persistence observation timestamps were invalid",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_scenario_observation_span_invalid",
            )
        if metric_type == "scenario_observation_span_gate" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="public persistence observation span exceeded its hard budget",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_scenario_observation_span_exceeded",
            )
        if metric_type == "scenario_hard_timeout_gate" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="the applicable Worker watchdog was unavailable or fired",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=(
                    "process_scenario_watchdog_timeout"
                    if observed_hard_timeout_source
                    is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
                    else "trial_watchdog_timeout"
                ),
            )
        if metric_type == "wait_remaining_budget_gate" and not passed:
            wait_error_type = (
                "process_wait_remaining_budget_mismatch"
                if any(
                    item.wait_remaining_budget_status
                    is WaitRemainingBudgetStatus.MISMATCHED
                    for item in wait_results
                )
                else "process_wait_remaining_budget_unavailable"
            )
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason=wait_remaining_budget_reason,
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type=wait_error_type,
            )
        if metric_type == "fixture_read" and not passed:
            return _error_metric(
                name=f"{metric_prefix}.{name}",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                reason="declared input fixture was not proven by the public file tool",
                hard_gate=plan.required,
                metric_type=metric_type,
                error_type="process_fixture_read_missing",
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
    optional_timing_missing = any(
        item is not None
        and item.timing_status
        in {
            ProcessTimingStatus.UNAVAILABLE,
            ProcessTimingStatus.INVALID,
        }
        for item in (
            observed_steps.get(step.step_id)
            for step in plan.steps
            if not step.required
        )
    )
    if optional_timing_missing:
        metrics.append(
            _not_applicable_metric(
                name=f"{metric_prefix}.optional_step_timing",
                scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
                metric_type="optional_step_timing",
                reason="optional Process step timeout was not evaluable without reliable timing",
                hard_gate=False,
            )
        )
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
