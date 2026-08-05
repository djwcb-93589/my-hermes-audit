"""Deterministic Trial, Case, and run-level aggregation."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from myhermes_audit.contracts import (
    AuditSummary,
    CaseAggregate,
    DeepSeekCacheStatus,
    DeepSeekCacheSummary,
    JudgeRunSummary,
    MetricSource,
    MetricStatus,
    MetricSummary,
    TrialResult,
    TrialStatus,
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
        stats = _aggregate_statistics(case_trials)
        cache = _deepseek_cache_summary(case_trials)
        aggregates.append(
            CaseAggregate(
                case_id=case_id,
                trial_count=len(case_trials),
                passed_count=passed_count,
                pass_rate=float(passed_count / len(case_trials)),
                metric_summaries=_metric_summaries(case_trials),
                agent_iterations_mean=stats["agent_iterations_mean"],
                duration_mean_ms=stats["duration_mean_ms"],
                total_tokens_mean_per_trial=stats["total_tokens_mean_per_trial"],
                tool_call_count_mean=stats["tool_call_count_mean"],
                deepseek_cache_status=cache.status,
                deepseek_cache_hit_rate=cache.cache_hit_rate,
                deepseek_cache_hit_tokens=cache.prompt_cache_hit_tokens,
                deepseek_cache_miss_tokens=cache.prompt_cache_miss_tokens,
                deepseek_cache_evaluated_prompt_tokens=(
                    cache.deepseek_cache_evaluated_prompt_tokens
                ),
                deepseek_cache_model_call_coverage_rate=(
                    cache.model_call_coverage_rate
                ),
                deepseek_cache_trial_coverage_rate=cache.trial_coverage_rate,
                deepseek_cache=cache,
            )
        )
    return aggregates


def aggregate_audit(
    case_ids: Sequence[str],
    trials: Sequence[TrialResult],
) -> AuditSummary:
    stats = _aggregate_statistics(trials)
    passed_count = sum(trial.passed is True for trial in trials)
    trial_count = len(trials)
    cache = _deepseek_cache_summary(trials)
    return AuditSummary(
        case_count=len(case_ids),
        trial_count=trial_count,
        passed_count=passed_count,
        pass_rate=float(passed_count / trial_count) if trial_count else 0.0,
        task_success_sample_count=stats["task_success_sample_count"],
        task_success_rate=stats["task_success_rate"],
        tool_correctness_sample_count=stats["tool_correctness_sample_count"],
        tool_correctness_rate=stats["tool_correctness_rate"],
        memory_required_evidence_sample_count=stats["memory_required_evidence_sample_count"],
        memory_required_evidence_hit_rate=stats["memory_required_evidence_hit_rate"],
        memory_recall_at_k_sample_count=stats["memory_recall_at_k_sample_count"],
        memory_recall_at_k_mean=stats["memory_recall_at_k_mean"],
        memory_mrr_sample_count=stats["memory_mrr_sample_count"],
        memory_mrr_mean=stats["memory_mrr_mean"],
        background_review_decision_sample_count=stats[
            "background_review_decision_sample_count"
        ],
        background_review_decision_accuracy=stats[
            "background_review_decision_accuracy"
        ],
        conversation_turn_count_total=stats["conversation_turn_count_total"],
        conversation_turn_count_mean=stats["conversation_turn_count_mean"],
        agent_iterations_total=stats["agent_iterations_total"],
        agent_iterations_mean=stats["agent_iterations_mean"],
        agent_iterations_p50=stats["agent_iterations_p50"],
        agent_iterations_p95=stats["agent_iterations_p95"],
        duration_mean_ms=stats["duration_mean_ms"],
        duration_sample_count=stats["duration_sample_count"],
        duration_p50_ms=stats["duration_p50_ms"],
        duration_p95_ms=stats["duration_p95_ms"],
        prompt_tokens_total=stats["prompt_tokens_total"],
        prompt_tokens_mean_per_trial=stats["prompt_tokens_mean_per_trial"],
        prompt_tokens_sample_count=stats["prompt_tokens_sample_count"],
        completion_tokens_total=stats["completion_tokens_total"],
        completion_tokens_mean_per_trial=stats["completion_tokens_mean_per_trial"],
        completion_tokens_sample_count=stats["completion_tokens_sample_count"],
        total_tokens=stats["total_tokens_total"],
        total_tokens_mean_per_trial=stats["total_tokens_mean_per_trial"],
        total_tokens_mean_per_success=stats["total_tokens_mean_per_success"],
        total_tokens_sample_count=stats["total_tokens_sample_count"],
        total_tokens_success_sample_count=stats["total_tokens_success_sample_count"],
        tool_call_count_total=stats["tool_call_count_total"],
        tool_call_count_mean=stats["tool_call_count_mean"],
        tool_call_count_p50=stats["tool_call_count_p50"],
        tool_call_count_p95=stats["tool_call_count_p95"],
        tool_call_count_mean_per_success=stats["tool_call_count_mean_per_success"],
        tool_call_sample_count=stats["tool_call_sample_count"],
        tool_call_success_sample_count=stats["tool_call_success_sample_count"],
        failure_count=stats["failure_count"],
        failure_rate=stats["failure_rate"],
        timeout_count=stats["timeout_count"],
        timeout_rate=stats["timeout_rate"],
        environment_error_count=stats["environment_error_count"],
        cancelled_count=stats["cancelled_count"],
        deepseek_cache=cache,
        metadata=stats["metadata"],
    )


def _aggregate_statistics(trials: Sequence[TrialResult]) -> dict[str, object]:
    trial_count = len(trials)
    task_values = [trial.task_passed for trial in trials if trial.task_passed is not None]
    tool_outcomes = [
        metric.passed
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.RUNTIME
        and metric.status is MetricStatus.COMPLETED
        and metric.passed is not None
        and metric.metadata.get("required") is True
        and metric.metadata.get("evaluator_kind") == "tool_trajectory"
    ]
    durations = [trial.duration_ms for trial in trials if trial.duration_ms is not None]
    iterations = [
        trial.runtime.iterations
        for trial in trials
        if trial.runtime is not None
    ]
    tool_counts = [
        trial.runtime.tool_call_count
        for trial in trials
        if trial.runtime is not None
    ]
    turn_counts = [len(trial.turns) for trial in trials]
    prompt_values = [
        trial.runtime.prompt_tokens
        for trial in trials
        if trial.runtime is not None and trial.runtime.prompt_tokens is not None
    ]
    completion_values = [
        trial.runtime.completion_tokens
        for trial in trials
        if trial.runtime is not None and trial.runtime.completion_tokens is not None
    ]
    total_values = [
        trial.runtime.total_tokens
        for trial in trials
        if trial.runtime is not None and trial.runtime.total_tokens is not None
    ]
    success_total_values = [
        trial.runtime.total_tokens
        for trial in trials
        if trial.passed is True
        and trial.runtime is not None
        and trial.runtime.prompt_tokens is not None
        and trial.runtime.completion_tokens is not None
        and trial.runtime.total_tokens is not None
    ]
    memory_metrics = [
        metric
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.RETRIEVAL
    ]
    evidence = [
        metric
        for metric in memory_metrics
        if metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "required_evidence"
        and type(metric.passed) is bool
    ]
    recalls = _completed_metric_numbers(memory_metrics, "recall_at_k")
    mrrs = _completed_metric_numbers(memory_metrics, "mrr")
    review_metrics = [
        metric
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.BACKGROUND_REVIEW
        and metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "decision_correctness"
        and type(metric.passed) is bool
    ]
    failure_count = sum(
        trial.status is TrialStatus.FAILED
        or (trial.status is TrialStatus.COMPLETED and trial.passed is not True)
        for trial in trials
    )
    timeout_count = sum(trial.status is TrialStatus.TIMEOUT for trial in trials)
    environment_error_count = sum(
        trial.status is TrialStatus.ENVIRONMENT_ERROR for trial in trials
    )
    cancelled_count = sum(trial.status is TrialStatus.CANCELLED for trial in trials)
    tool_success_values = [
        trial.runtime.tool_call_count
        for trial in trials
        if trial.passed is True and trial.runtime is not None
    ]
    metadata = {
        "duration_percentile_method": "nearest-rank",
        "duration_sample_count": len(durations),
        "token_sample_count": len(total_values),
        "memory_metric_status_counts": _metric_status_counts(memory_metrics),
        "background_review_metric_status_counts": _metric_status_counts(
            [
                metric
                for trial in trials
                for metric in trial.metrics
                if metric.source is MetricSource.BACKGROUND_REVIEW
            ]
        ),
    }
    return {
        "task_success_sample_count": len(task_values),
        "task_success_rate": _rate(sum(value is True for value in task_values), task_values),
        "tool_correctness_sample_count": len(tool_outcomes),
        "tool_correctness_rate": _rate(sum(value is True for value in tool_outcomes), tool_outcomes),
        "memory_required_evidence_sample_count": len(evidence),
        "memory_required_evidence_hit_rate": _rate(
            sum(metric.passed is True for metric in evidence), evidence
        ),
        "memory_recall_at_k_sample_count": len(recalls),
        "memory_recall_at_k_mean": _mean(recalls),
        "memory_mrr_sample_count": len(mrrs),
        "memory_mrr_mean": _mean(mrrs),
        "background_review_decision_sample_count": len(review_metrics),
        "background_review_decision_accuracy": _rate(
            sum(metric.passed is True for metric in review_metrics), review_metrics
        ),
        "conversation_turn_count_total": sum(turn_counts),
        "conversation_turn_count_mean": _mean(turn_counts),
        "agent_iterations_total": sum(iterations),
        "agent_iterations_mean": _mean(iterations),
        "agent_iterations_p50": _nearest_rank(sorted(iterations), 0.50),
        "agent_iterations_p95": _nearest_rank(sorted(iterations), 0.95),
        "duration_mean_ms": _mean(durations),
        "duration_sample_count": len(durations),
        "duration_p50_ms": _nearest_rank(sorted(durations), 0.50),
        "duration_p95_ms": _nearest_rank(sorted(durations), 0.95),
        "prompt_tokens_total": sum(prompt_values) if prompt_values else None,
        "prompt_tokens_mean_per_trial": _mean(prompt_values),
        "prompt_tokens_sample_count": len(prompt_values),
        "completion_tokens_total": (
            sum(completion_values) if completion_values else None
        ),
        "completion_tokens_mean_per_trial": _mean(completion_values),
        "completion_tokens_sample_count": len(completion_values),
        "total_tokens_total": sum(total_values) if total_values else None,
        "total_tokens_mean_per_trial": _mean(total_values),
        "total_tokens_mean_per_success": _mean(success_total_values),
        "total_tokens_sample_count": len(total_values),
        "total_tokens_success_sample_count": len(success_total_values),
        "tool_call_count_total": sum(tool_counts),
        "tool_call_count_mean": _mean(tool_counts),
        "tool_call_count_p50": _nearest_rank(sorted(tool_counts), 0.50),
        "tool_call_count_p95": _nearest_rank(sorted(tool_counts), 0.95),
        "tool_call_count_mean_per_success": _mean(tool_success_values),
        "tool_call_sample_count": len(tool_counts),
        "tool_call_success_sample_count": len(tool_success_values),
        "failure_count": failure_count,
        "failure_rate": _rate(failure_count, trials) or 0.0,
        "timeout_count": timeout_count,
        "timeout_rate": _rate(timeout_count, trials) or 0.0,
        "environment_error_count": environment_error_count,
        "cancelled_count": cancelled_count,
        "metadata": metadata,
    }


def _deepseek_cache_summary(trials: Sequence[TrialResult]) -> DeepSeekCacheSummary:
    runtimes = [trial.runtime for trial in trials if trial.runtime is not None]
    model_call_count = sum(runtime.model_call_count for runtime in runtimes)
    model_call_trial_count = sum(runtime.model_call_count > 0 for runtime in runtimes)
    evaluated_count = sum(
        runtime.deepseek_cache_evaluated_model_call_count for runtime in runtimes
    )
    invalid_trial_count = sum(
        runtime.deepseek_cache_status is DeepSeekCacheStatus.INVALID
        for runtime in runtimes
    )
    evaluated_trial_count = sum(
        runtime.deepseek_cache_evaluated_model_call_count > 0
        and runtime.deepseek_cache_status is not DeepSeekCacheStatus.INVALID
        for runtime in runtimes
    )
    invalid = invalid_trial_count > 0
    if invalid:
        status = DeepSeekCacheStatus.INVALID
        hit = miss = evaluated_prompt = rate = None
    else:
        hit_values = [
            runtime.prompt_cache_hit_tokens
            for runtime in runtimes
            if runtime.deepseek_cache_status
            in (DeepSeekCacheStatus.AVAILABLE, DeepSeekCacheStatus.PARTIAL)
            and runtime.prompt_cache_hit_tokens is not None
        ]
        miss_values = [
            runtime.prompt_cache_miss_tokens
            for runtime in runtimes
            if runtime.deepseek_cache_status
            in (DeepSeekCacheStatus.AVAILABLE, DeepSeekCacheStatus.PARTIAL)
            and runtime.prompt_cache_miss_tokens is not None
        ]
        evaluated_prompt_values = [
            runtime.deepseek_cache_evaluated_prompt_tokens
            for runtime in runtimes
            if runtime.deepseek_cache_status
            in (DeepSeekCacheStatus.AVAILABLE, DeepSeekCacheStatus.PARTIAL)
            and runtime.deepseek_cache_evaluated_prompt_tokens is not None
        ]
        hit = sum(hit_values) if hit_values else None
        miss = sum(miss_values) if miss_values else None
        evaluated_prompt = (
            sum(evaluated_prompt_values) if evaluated_prompt_values else None
        )
        if hit is None or miss is None or evaluated_prompt is None:
            rate = None
        else:
            if hit + miss != evaluated_prompt:
                raise ValueError("Trial cache totals disagree during aggregation")
            rate = None if evaluated_prompt == 0 else hit / evaluated_prompt
        if not evaluated_count:
            status = DeepSeekCacheStatus.NOT_EVALUATED
        elif all(
            runtime.model_call_count > 0
            and runtime.deepseek_cache_status is DeepSeekCacheStatus.AVAILABLE
            for runtime in runtimes
        ) and runtimes and len(runtimes) == len(trials):
            status = DeepSeekCacheStatus.AVAILABLE
        else:
            status = DeepSeekCacheStatus.PARTIAL
    model_coverage = (
        None if model_call_count == 0 else evaluated_count / model_call_count
    )
    trial_coverage = (
        None
        if model_call_trial_count == 0
        else evaluated_trial_count / model_call_trial_count
    )
    return DeepSeekCacheSummary(
        status=status,
        model_call_count=model_call_count,
        evaluated_model_call_count=evaluated_count,
        trial_count=len(trials),
        evaluated_trial_count=evaluated_trial_count,
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
        deepseek_cache_evaluated_prompt_tokens=evaluated_prompt,
        cache_hit_rate=rate,
        model_call_coverage_rate=model_coverage,
        trial_coverage_rate=trial_coverage,
        invalid_trial_count=invalid_trial_count,
        model_call_trial_count=model_call_trial_count,
    )


def _completed_metric_numbers(metrics, metric_type: str) -> list[float]:
    return [
        float(metric.value)
        for metric in metrics
        if metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == metric_type
        and type(metric.value) in (int, float)
        and not isinstance(metric.value, bool)
        and math.isfinite(float(metric.value))
        and 0 <= float(metric.value) <= 1
    ]


def _metric_status_counts(metrics) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for metric in metrics:
        counts[metric.status.value] += 1
    return dict(sorted(counts.items()))


def _rate(passed: int, values) -> float | None:
    return None if not values else float(passed / len(values))


def _mean(values) -> float | None:
    return None if not values else float(sum(values) / len(values))


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
            and not isinstance(metric.value, bool)
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
        and not isinstance(metric.value, bool)
    ]
    return JudgeRunSummary(
        declared_count=len(metrics),
        completed_count=sum(metric.status is MetricStatus.COMPLETED for metric in metrics),
        skipped_count=sum(metric.status is MetricStatus.SKIPPED for metric in metrics),
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
