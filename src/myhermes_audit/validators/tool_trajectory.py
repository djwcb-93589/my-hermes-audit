"""Tool trajectory checks over safe Observation projections."""

from __future__ import annotations

from myhermes_audit.contracts import MetricResult, MetricSource
from myhermes_audit.contracts.suite import ToolTrajectoryExpectation
from myhermes_audit.errors import UnsupportedCaseError, ValidatorError
from myhermes_audit.validators.base import (
    ValidationContext,
    evidence,
    metric,
)


class ToolTrajectoryValidator:
    def validate(
        self,
        expectation: ToolTrajectoryExpectation,
        context: ValidationContext,
        *,
        metric_name: str,
    ) -> MetricResult:
        if expectation.calls:
            raise UnsupportedCaseError(
                "exact ordered tool call arguments are not enforced"
            )
        if context.tool_calls is None:
            raise ValidatorError("tool Observation data is unavailable")
        if not context.tool_trace_complete:
            raise ValidatorError("tool Observation data was truncated")

        calls = context.tool_calls
        names = [item.tool_name for item in calls]
        failures: list[str] = []
        for required in expectation.required_tools:
            if required not in names:
                failures.append(f"required tool was not called: {required}")
        for forbidden in expectation.forbidden_tools:
            if forbidden in names:
                failures.append(f"forbidden tool was called: {forbidden}")
        if (
            expectation.minimum_tool_calls is not None
            and len(calls) < expectation.minimum_tool_calls
        ):
            failures.append("tool call count is below minimum_tool_calls")
        if (
            expectation.maximum_tool_calls is not None
            and len(calls) > expectation.maximum_tool_calls
        ):
            failures.append("tool call count exceeds maximum_tool_calls")
        for tool_name in expectation.required_successful_tools:
            if not any(
                item.tool_name == tool_name and item.success for item in calls
            ):
                failures.append(f"tool never succeeded: {tool_name}")

        first_seen = list(dict.fromkeys(names))
        passed = not failures
        return metric(
            name=metric_name,
            source=MetricSource.RUNTIME,
            passed=passed,
            reason=(
                "tool trajectory constraints satisfied"
                if passed
                else "; ".join(failures)
            ),
            evidence_items=[
                evidence(
                    kind="tool_trajectory",
                    description=f"tool_call_count={len(calls)}",
                    metadata={
                        "tool_call_count": len(calls),
                        "tool_names": first_seen,
                    },
                )
            ],
        )


__all__ = ("ToolTrajectoryValidator",)
