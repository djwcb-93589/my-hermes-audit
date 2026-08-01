"""Deterministic Trial, Case, and run-level aggregation."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from myhermes_audit.contracts import (
    AuditSummary,
    CaseAggregate,
    JudgeRunSummary,
    MetricSource,
    MetricStatus,
    MetricSummary,
    TrialResult,
)


def aggregate_cases(
    case_ids: Sequence[str],
    trials: Sequence[TrialResult],
) -> list[CaseAggregate]:
    aggregates: list[CaseAggregate] = []
    for case_id in case_ids:
        case_trials = [trial for trial in trials if trial.case_id == case_id]
        if not case_trials:
            continue
        passed_count = sum(trial.passed is True for trial in case_trials)
        aggregates.append(
            CaseAggregate(
                case_id=case_id,
                trial_count=len(case_trials),
                passed_count=passed_count,
                pass_rate=float(passed_count / len(case_trials)),
                metric_summaries=_metric_summaries(case_trials),
            )
        )
    return aggregates


def aggregate_audit(
    case_ids: Sequence[str],
    trials: Sequence[TrialResult],
) -> AuditSummary:
    passed_count = sum(trial.passed is True for trial in trials)
    trial_count = len(trials)
    tool_outcomes = [
        metric.passed
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.RUNTIME and metric.passed is not None
        and metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("required") is True
        and metric.metadata.get("evaluator_kind") == "tool_trajectory"
    ]
    durations = sorted(
        trial.duration_ms
        for trial in trials
        if trial.duration_ms is not None
    )
    token_values = [
        trial.runtime.total_tokens
        for trial in trials
        if trial.runtime is not None and trial.runtime.total_tokens is not None
    ]
    return AuditSummary(
        case_count=len(case_ids),
        trial_count=trial_count,
        passed_count=passed_count,
        pass_rate=float(passed_count / trial_count) if trial_count else 0.0,
        task_success_rate=(
            float(
                sum(trial.task_passed is True for trial in trials)
                / len(trials)
            )
            if trials and all(trial.task_passed is not None for trial in trials)
            else None
        ),
        tool_correctness_rate=(
            float(sum(value is True for value in tool_outcomes) / len(tool_outcomes))
            if tool_outcomes
            else None
        ),
        duration_p50_ms=_nearest_rank(durations, 0.50),
        duration_p95_ms=_nearest_rank(durations, 0.95),
        total_tokens=(
            sum(token_values)
            if trials and len(token_values) == len(trials)
            else None
        ),
        metadata={
            "duration_percentile_method": "nearest-rank",
            "duration_sample_count": len(durations),
            "token_sample_count": len(token_values),
        },
    )


def _metric_summaries(trials: Sequence[TrialResult]) -> list[MetricSummary]:
    grouped = defaultdict(list)
    for trial in trials:
        for metric in trial.metrics:
            grouped[metric.metric_name].append(metric)

    summaries: list[MetricSummary] = []
    for metric_name in sorted(grouped):
        metrics = [
            metric
            for metric in grouped[metric_name]
            if metric.status is MetricStatus.COMPLETED
        ]
        passed_count = sum(metric.passed is True for metric in metrics)
        numbers = [
            float(metric.value)
            for metric in metrics
            if type(metric.value) in (int, float)
            and math.isfinite(float(metric.value))
        ]
        if not numbers and all(type(metric.value) is bool for metric in metrics):
            numbers = [1.0 if metric.value else 0.0 for metric in metrics]
        summaries.append(
            MetricSummary(
                metric_name=metric_name,
                sample_count=len(metrics),
                passed_count=passed_count,
                mean=(float(sum(numbers) / len(numbers)) if numbers else None),
                minimum=(float(min(numbers)) if numbers else None),
                maximum=(float(max(numbers)) if numbers else None),
            )
        )
    return summaries


def aggregate_judges(trials: Sequence[TrialResult]) -> JudgeRunSummary:
    metrics = [
        metric
        for trial in trials
        for metric in trial.metrics
        if metric.metric_name == "answer_quality"
        and metric.source is MetricSource.JUDGE
    ]
    completed_values = [
        float(metric.value)
        for metric in metrics
        if metric.status is MetricStatus.COMPLETED
        and type(metric.value) in (int, float)
    ]
    return JudgeRunSummary(
        declared_count=len(metrics),
        completed_count=sum(
            metric.status is MetricStatus.COMPLETED for metric in metrics
        ),
        skipped_count=sum(
            metric.status is MetricStatus.SKIPPED for metric in metrics
        ),
        error_count=sum(metric.status is MetricStatus.ERROR for metric in metrics),
        not_applicable_count=sum(
            metric.status is MetricStatus.NOT_APPLICABLE for metric in metrics
        ),
        mean_answer_quality=(
            float(sum(completed_values) / len(completed_values))
            if completed_values
            else None
        ),
    )


def _nearest_rank(values: Sequence[int], quantile: float) -> int | None:
    """Return sorted[ceil(q*n)-1]; a one-item sample returns that item."""

    if not values:
        return None
    index = max(0, math.ceil(quantile * len(values)) - 1)
    return values[index]


__all__ = ("aggregate_audit", "aggregate_cases", "aggregate_judges")
