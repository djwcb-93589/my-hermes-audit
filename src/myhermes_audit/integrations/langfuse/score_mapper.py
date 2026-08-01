"""Map local facts to the three supported Langfuse quality scores."""

from __future__ import annotations

from dataclasses import dataclass

from myhermes_audit.contracts import MetricSource, MetricStatus, TrialResult


SCORE_MODEL_VERSION = "p2-score-v1"


@dataclass(frozen=True, slots=True)
class ScoreProjection:
    name: str
    value: float
    source: str
    evaluator_version: str
    comment: str
    metadata: dict[str, object]


def project_scores(trial: TrialResult) -> list[ScoreProjection]:
    efficiency = {
        "duration_ms": trial.duration_ms,
        "total_tokens": (
            None if trial.runtime is None else trial.runtime.total_tokens
        ),
        "iterations": None if trial.runtime is None else trial.runtime.iterations,
        "tool_call_count": (
            None if trial.runtime is None else trial.runtime.tool_call_count
        ),
        "score_model_version": SCORE_MODEL_VERSION,
    }
    scores: list[ScoreProjection] = []
    if trial.task_passed is not None:
        scores.append(
            ScoreProjection(
                name="task_success",
                value=1.0 if trial.task_passed else 0.0,
                source="local_task_gate",
                evaluator_version=SCORE_MODEL_VERSION,
                comment="Local deterministic/runtime task gate result.",
                metadata=efficiency,
            )
        )
    tool_metrics = [
        metric
        for metric in trial.metrics
        if metric.source is MetricSource.RUNTIME
        and metric.status is MetricStatus.COMPLETED
        and metric.passed is not None
        and metric.metadata.get("required") is True
        and metric.metadata.get("evaluator_kind") == "tool_trajectory"
    ]
    if tool_metrics:
        scores.append(
            ScoreProjection(
                name="tool_correctness",
                value=float(
                    sum(metric.passed is True for metric in tool_metrics)
                    / len(tool_metrics)
                ),
                source="local_tool_trajectory",
                evaluator_version="+".join(
                    sorted({metric.evaluator_version for metric in tool_metrics})
                ),
                comment="Mean of completed local tool-trajectory gates.",
                metadata={**efficiency, "sample_count": len(tool_metrics)},
            )
        )
    answer_quality = next(
        (
            metric
            for metric in trial.metrics
            if metric.metric_name == "answer_quality"
            and metric.source is MetricSource.JUDGE
            and metric.status is MetricStatus.COMPLETED
            and type(metric.value) in (int, float)
        ),
        None,
    )
    if answer_quality is not None:
        scores.append(
            ScoreProjection(
                name="answer_quality",
                value=float(answer_quality.value),
                source="judge",
                evaluator_version=answer_quality.evaluator_version,
                comment="Locally weighted answer-quality Judge result.",
                metadata={
                    **efficiency,
                    "prompt_version": answer_quality.metadata.get("prompt_version"),
                },
            )
        )
    return scores


__all__ = ("SCORE_MODEL_VERSION", "ScoreProjection", "project_scores")
