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
)
from myhermes_audit.validators.base import ValidationContext


_EVALUATOR_VERSION = "p6.1"


def _error_metric(
    *,
    name: str,
    scenario_kind: E2EScenarioKind,
    reason: str,
    hard_gate: bool,
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
            error_type="scenario_result_unavailable",
            message=reason,
            details={"scenario_kind": scenario_kind.value},
        ),
        metadata={
            "scenario_kind": scenario_kind.value,
            "metric_type": "scenario_error",
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
        return [
            _error_metric(
                name=f"{metric_prefix}.status",
                scenario_kind=E2EScenarioKind(plan.kind.value),
                reason="Worker did not produce the declared scenario result",
                hard_gate=plan.required,
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
    cleanup_required = any(
        item.action.value == "cleanup_session" and item.required
        for item in plan.steps
    )
    cleanup_passed = (
        not cleanup_required
        or (
            observed.cleanup_result is not None
            and observed.cleanup_result.complete
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
    return [
        _boolean_metric(
            name=f"{metric_prefix}.status",
            scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
            metric_type="process_gate",
            passed=status_passed,
            reason="Process scenario completed" if status_passed else "Process scenario did not complete",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.steps",
            scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
            metric_type="process_steps",
            passed=steps_passed,
            reason="required Process steps passed" if steps_passed else "a required Process step failed",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.cleanup",
            scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
            metric_type="process_cleanup",
            passed=cleanup_passed,
            reason="Process cleanup completed" if cleanup_passed else "Process cleanup is incomplete",
            hard_gate=plan.required,
        ),
        _boolean_metric(
            name=f"{metric_prefix}.checkpoints",
            scenario_kind=E2EScenarioKind.PROCESS_BACKGROUND,
            metric_type="process_checkpoints",
            passed=checkpoints_passed,
            reason=(
                "required Process checkpoints passed"
                if checkpoints_passed
                else "a required Process checkpoint failed"
            ),
            hard_gate=plan.required,
        ),
    ]


__all__ = ("evaluate_scenario_plan",)
