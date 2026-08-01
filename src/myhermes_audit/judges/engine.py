"""Judge execution policy layered after deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from myhermes_audit.contracts import (
    AuditCase,
    JudgeRequest,
    JudgeResult,
    JUDGE_PROMPT_VERSION,
    MetricError,
    MetricResult,
    MetricSource,
    MetricStatus,
    TrialStatus,
    TurnResult,
)
from myhermes_audit.contracts.suite import CaseMode, EvaluatorKind, EvaluatorSpec
from myhermes_audit.errors import AuditError, JudgeConfigError, UnsupportedCaseError
from myhermes_audit.ports.judge import JudgePort
from myhermes_audit.validators import resolve_judge_expectation
from myhermes_audit.validators.base import ToolTraceEntry


JUDGE_EVALUATOR_VERSION = f"{JUDGE_PROMPT_VERSION}/judge-engine-v1"


@dataclass(frozen=True, slots=True)
class JudgeEvaluation:
    metric: MetricResult
    result: JudgeResult | None
    required: bool


class JudgeService:
    def __init__(self, port: JudgePort | None) -> None:
        self.port = port

    def preflight(self, cases: Sequence[AuditCase]) -> None:
        for case in cases:
            evaluators = _judge_evaluators(case)
            if len(evaluators) > 1:
                raise UnsupportedCaseError(
                    "P2 supports one answer_quality evaluator per case",
                    case_id=case.case_id,
                )
            for evaluator in evaluators:
                resolve_judge_expectation(case, evaluator)
                if evaluator.required and self.port is None:
                    raise JudgeConfigError(
                        "required llm_judge evaluator needs --judge and valid Judge configuration",
                        case_id=case.case_id,
                        evaluator_id=evaluator.evaluator_id,
                    )

    def evaluate(
        self,
        case: AuditCase,
        *,
        trial_status: TrialStatus,
        final_output: str | None,
        deterministic_metrics: Sequence[MetricResult],
        tool_calls: Sequence[ToolTraceEntry] | None,
        turns: Sequence[TurnResult],
    ) -> JudgeEvaluation | None:
        evaluators = _judge_evaluators(case)
        if not evaluators:
            return None
        evaluator = evaluators[0]
        _, expectation = resolve_judge_expectation(case, evaluator)
        if trial_status is not TrialStatus.COMPLETED or final_output is None:
            return JudgeEvaluation(
                metric=_inactive_metric(
                    MetricStatus.NOT_APPLICABLE,
                    "worker did not produce an evaluable final output",
                    evaluator,
                ),
                result=None,
                required=evaluator.required,
            )
        if self.port is None:
            return JudgeEvaluation(
                metric=_inactive_metric(
                    MetricStatus.SKIPPED,
                    "optional Judge was not enabled for this run",
                    evaluator,
                ),
                result=None,
                required=evaluator.required,
            )
        request = JudgeRequest(
            judge_id=evaluator.evaluator_id,
            task_input=_task_input(case),
            case_mode=case.mode.value,
            final_output=final_output,
            rubric=expectation.rubric,
            criteria=expectation.criteria,
            deterministic_summary=_deterministic_summary(deterministic_metrics),
            tool_summary=_tool_summary(tool_calls),
            conversation_summary=_conversation_summary(case.mode, turns),
            minimum_score=expectation.minimum_score,
            maximum_score=expectation.maximum_score,
            metadata={
                "case_id": case.case_id,
                "evaluator_id": evaluator.evaluator_id,
            },
        )
        try:
            result = self.port.evaluate(request)
        except AuditError as exc:
            return JudgeEvaluation(
                metric=MetricResult(
                    metric_name="answer_quality",
                    source=MetricSource.JUDGE,
                    status=MetricStatus.ERROR,
                    value=None,
                    passed=None,
                    reason="Judge evaluation failed",
                    evaluator_version=JUDGE_EVALUATOR_VERSION,
                    error=MetricError(
                        error_type=exc.code,
                        message=exc.message,
                        retryable=exc.details.get("retryable") is True,
                        details={
                            key: value
                            for key, value in exc.details.items()
                            if key in {"status_code", "attempts"}
                        },
                    ),
                    metadata=_evaluator_metadata(evaluator),
                ),
                result=None,
                required=evaluator.required,
            )
        except Exception as exc:
            return JudgeEvaluation(
                metric=MetricResult(
                    metric_name="answer_quality",
                    source=MetricSource.JUDGE,
                    status=MetricStatus.ERROR,
                    value=None,
                    passed=None,
                    reason="Judge evaluation failed",
                    evaluator_version=JUDGE_EVALUATOR_VERSION,
                    error=MetricError(
                        error_type="judge_invocation_error",
                        message=f"unexpected Judge adapter error: {type(exc).__name__}",
                        retryable=False,
                    ),
                    metadata=_evaluator_metadata(evaluator),
                ),
                result=None,
                required=evaluator.required,
            )
        return JudgeEvaluation(
            metric=MetricResult(
                metric_name="answer_quality",
                source=MetricSource.JUDGE,
                status=MetricStatus.COMPLETED,
                value=result.overall_score,
                passed=result.passed,
                reason=result.summary,
                evaluator_version=_completed_evaluator_version(result),
                metadata={
                    **_evaluator_metadata(evaluator),
                    "judge_id": result.judge_id,
                    "judge_model": result.judge_model,
                    "judge_provider": result.judge_provider,
                    "prompt_version": result.prompt_version,
                    "criteria": [
                        item.model_dump(mode="json", exclude={"schema_version"})
                        for item in result.criteria
                    ],
                    "duration_ms": result.duration_ms,
                    "retry_count": result.retry_count,
                },
            ),
            result=result,
            required=evaluator.required,
        )


def _judge_evaluators(case: AuditCase) -> list[EvaluatorSpec]:
    return [
        evaluator
        for evaluator in case.evaluators
        if evaluator.kind is EvaluatorKind.LLM_JUDGE
    ]


def _inactive_metric(
    status: MetricStatus,
    reason: str,
    evaluator: EvaluatorSpec,
) -> MetricResult:
    return MetricResult(
        metric_name="answer_quality",
        source=MetricSource.JUDGE,
        status=status,
        value=None,
        passed=None,
        reason=reason,
        evaluator_version=JUDGE_EVALUATOR_VERSION,
        metadata=_evaluator_metadata(evaluator),
    )


def _completed_evaluator_version(result: JudgeResult) -> str:
    adapter_version = result.metadata.get("adapter_version")
    if not isinstance(adapter_version, str) or not adapter_version.strip():
        adapter_version = "adapter-unknown"
    return f"{result.prompt_version}/{adapter_version}"


def _evaluator_metadata(evaluator: EvaluatorSpec) -> dict[str, object]:
    return {
        "evaluator_id": evaluator.evaluator_id,
        "evaluator_kind": evaluator.kind.value,
        "required": evaluator.required,
    }


def _task_input(case: AuditCase) -> str:
    if case.input.message is not None:
        return case.input.message
    return "\n".join(
        f"turn {index}: {turn.message}"
        for index, turn in enumerate(case.input.turns, start=1)
    )


def _deterministic_summary(metrics: Sequence[MetricResult]) -> str:
    if not metrics:
        return "No deterministic metrics were declared."
    return "\n".join(
        (
            f"{metric.metric_name}: status={metric.status.value}; "
            f"passed={metric.passed}; reason={metric.reason or '<none>'}"
        )
        for metric in metrics
    )


def _tool_summary(tool_calls: Sequence[ToolTraceEntry] | None) -> str:
    if tool_calls is None:
        return "Tool Observation evidence is unavailable."
    if not tool_calls:
        return "No tool calls were observed."
    return "\n".join(
        (
            f"{index}. tool={item.tool_name}; status={item.status}; "
            f"success={item.success}; error={item.error_type or '<none>'}; "
            f"duration_ms={item.duration_ms}"
        )
        for index, item in enumerate(tool_calls, start=1)
    )


def _conversation_summary(
    mode: CaseMode,
    turns: Sequence[TurnResult],
) -> str | None:
    if mode is not CaseMode.SCRIPTED_MULTI_TURN:
        return None
    if not turns:
        return "No completed conversation turns were available."
    return "\n".join(
        (
            f"turn {turn.turn_number}: user={turn.user_message!r}; "
            f"assistant={turn.final_output!r}; status={turn.runtime_status}"
        )
        for turn in turns
    )


__all__ = ("JUDGE_EVALUATOR_VERSION", "JudgeEvaluation", "JudgeService")
