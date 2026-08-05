"""P7 repeat-run projections, immutable baselines, and regression comparison."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from myhermes_audit.contracts.regression import (
    AuditBaseline,
    AuditRegressionReport,
    BenchmarkCaseSummary,
    BenchmarkSummary,
    CaseRegressionSummary,
    IdentityEvidence,
    IdentityStatus,
)
from myhermes_audit.contracts.result import (
    AuditRunResult,
    CaseAggregate,
    DeepSeekCacheStatus,
    MetricSource,
    MetricStatus,
    TrialResult,
    TrialStatus,
)
from myhermes_audit.contracts.regression import (
    MetricAvailability,
    MetricComparison,
    MetricDecision,
    MetricDirection,
    MetricSnapshot,
    metric_decision_counts,
    derive_metric_decision,
    RegressionPolicy,
    RegressionPolicySnapshot,
    RegressionStatus,
    METRIC_CONTRACT_VERSION,
    BASELINE_SCHEMA_VERSION,
    pricing_applicability_fingerprint,
)
from myhermes_audit.serialization import canonical_sha256
from myhermes_audit.regression_decision import (
    MetricDecisionInput,
    MetricEvaluationInput,
    MetricPolicyFacts,
    PolicySnapshotFacts,
    decide_case_regression,
    decide_metric_comparison,
    decide_report_status,
    derive_comparability_reason_codes,
    derive_metric_evaluation_facts,
    resolve_metric_policy,
)


def _number(value: object) -> float | Decimal | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return None


def _mean(values: Sequence[float | int]) -> float | None:
    return None if not values else float(sum(values) / len(values))


def _nearest_rank(values: Sequence[float | int], quantile: float) -> float | int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _stddev(values: Sequence[float | int]) -> float | None:
    # P7 uses the sample standard deviation consistently. A singleton has no
    # estimate of variation and is intentionally represented as None.
    return None if len(values) < 2 else float(statistics.stdev(values))


def _metric(
    name: str,
    value: object,
    *,
    sample_count: int = 0,
    denominator: int | None = None,
    unit: str = "value",
    direction: MetricDirection = MetricDirection.NEUTRAL,
) -> MetricSnapshot:
    numeric = _number(value)
    if numeric is None:
        return MetricSnapshot(
            metric_name=name,
            value=None,
            sample_count=0,
            denominator=denominator,
            unit=unit,
            direction=direction,
            availability=MetricAvailability.NOT_EVALUATED,
        )
    return MetricSnapshot(
        metric_name=name,
        value=numeric,
        sample_count=max(0, int(sample_count)),
        denominator=(None if denominator is None else max(0, int(denominator))),
        unit=unit,
        direction=direction,
    )


def _metric_numbers(trials: Sequence[TrialResult], getter) -> list[float]:
    numbers: list[float] = []
    for trial in trials:
        value = getter(trial)
        if value is None or isinstance(value, bool):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            numbers.append(numeric)
    return numbers


def _rate_metric(name: str, value: float | None, samples: int, denominator: int) -> MetricSnapshot:
    return _metric(
        name,
        value,
        sample_count=samples,
        denominator=denominator,
        unit="rate",
        direction=MetricDirection.HIGHER_IS_BETTER,
    )


def _failure_categories(trials: Sequence[TrialResult]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for trial in trials:
        if trial.status is TrialStatus.TIMEOUT:
            counts["timeout"] += 1
        elif trial.status is TrialStatus.ENVIRONMENT_ERROR:
            counts["environment_error"] += 1
        elif trial.status is TrialStatus.CANCELLED:
            counts["cancelled"] += 1
        elif trial.task_passed is False:
            counts["task_failure"] += 1
        elif trial.status is TrialStatus.FAILED or (
            trial.status is TrialStatus.COMPLETED and trial.passed is not True
        ):
            counts["trial_failed"] += 1
        if any(
            metric.status is MetricStatus.ERROR
            or (metric.status is MetricStatus.COMPLETED and metric.passed is False)
            for metric in trial.metrics
            if metric.source is MetricSource.RUNTIME
            and metric.metadata.get("required") is True
            and metric.metadata.get("evaluator_kind") == "tool_trajectory"
        ):
            counts["tool_trajectory_failure"] += 1
        if any(
            metric.status is MetricStatus.ERROR
            or (metric.status is MetricStatus.COMPLETED and metric.passed is False)
            for metric in trial.metrics
            if metric.source is MetricSource.RETRIEVAL
        ):
            counts["memory_retrieval_failure"] += 1
        if any(
            metric.status is MetricStatus.ERROR
            or (metric.status is MetricStatus.COMPLETED and metric.passed is False)
            for metric in trial.metrics
            if metric.source is MetricSource.BACKGROUND_REVIEW
            and metric.metadata.get("metric_type") == "decision_correctness"
        ):
            counts["background_review_decision_failure"] += 1
        if trial.runtime is not None and trial.runtime.deepseek_cache_status is DeepSeekCacheStatus.INVALID:
            counts["cache_invalid"] += 1
        if trial.deepseek_cost is not None and trial.deepseek_cost.status.value == "invalid":
            counts["cost_invalid"] += 1
    return dict(sorted(counts.items()))


def _review_actions(trials: Sequence[TrialResult]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for trial in trials:
        for review in trial.background_review_results:
            counts[review.actual_action.value] += 1
    return dict(sorted(counts.items()))


def _review_accuracy(trials: Sequence[TrialResult]) -> float | None:
    values = [
        metric.passed
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.BACKGROUND_REVIEW
        and metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "decision_correctness"
        and type(metric.passed) is bool
    ]
    return None if not values else sum(value is True for value in values) / len(values)


def _review_accuracy_sample_count(trials: Sequence[TrialResult]) -> int:
    return sum(
        1
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.BACKGROUND_REVIEW
        and metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "decision_correctness"
        and type(metric.passed) is bool
    )


def _task_success_facts(trials: Sequence[TrialResult]) -> tuple[int, int, float | None]:
    """Use only explicit bool ``task_passed`` facts; unknowns are not failures."""

    values = [trial.task_passed for trial in trials if type(trial.task_passed) is bool]
    sample_count = len(values)
    passed_count = sum(value is True for value in values)
    rate = None if sample_count == 0 else passed_count / sample_count
    return sample_count, passed_count, rate


def _declared_trials_per_case(trials: Sequence[TrialResult]) -> int:
    """Return the number of distinct repeat numbers represented by a Case."""

    return len({trial.trial_number for trial in trials})


def _declared_trial_mapping(
    cases: Sequence[BenchmarkCaseSummary],
) -> tuple[int | None, dict[str, int]]:
    mapping = {case.case_id: case.declared_trial_count for case in cases}
    values = set(mapping.values())
    return (next(iter(values)) if len(values) == 1 else None), mapping


def _identity_evidence(values: set[str]) -> IdentityEvidence:
    ordered = sorted(values)
    if not ordered:
        return IdentityEvidence(status=IdentityStatus.MISSING)
    if len(ordered) == 1:
        return IdentityEvidence(
            status=IdentityStatus.AVAILABLE,
            value=ordered[0],
            values=ordered,
        )
    return IdentityEvidence(
        status=IdentityStatus.AMBIGUOUS,
        values=ordered,
        fingerprint=canonical_sha256(ordered),
    )


def _build_metrics(trials: Sequence[TrialResult], summary) -> list[MetricSnapshot]:
    trial_count = len(trials)
    task_values = [trial.task_passed for trial in trials if type(trial.task_passed) is bool]
    tool_values = [
        metric.passed
        for trial in trials
        for metric in trial.metrics
        if metric.source is MetricSource.RUNTIME
        and metric.status is MetricStatus.COMPLETED
        and type(metric.passed) is bool
        and metric.metadata.get("required") is True
        and metric.metadata.get("evaluator_kind") == "tool_trajectory"
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
    recalls = [
        float(metric.value)
        for metric in memory_metrics
        if metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "recall_at_k"
        and type(metric.value) in (int, float)
        and not isinstance(metric.value, bool)
        and math.isfinite(float(metric.value))
    ]
    mrrs = [
        float(metric.value)
        for metric in memory_metrics
        if metric.status is MetricStatus.COMPLETED
        and metric.metadata.get("metric_type") == "mrr"
        and type(metric.value) in (int, float)
        and not isinstance(metric.value, bool)
        and math.isfinite(float(metric.value))
    ]
    durations = _metric_numbers(trials, lambda trial: trial.duration_ms)
    iterations = _metric_numbers(trials, lambda trial: None if trial.runtime is None else trial.runtime.iterations)
    turns = [float(len(trial.turns)) for trial in trials]
    prompt = _metric_numbers(trials, lambda trial: None if trial.runtime is None else trial.runtime.prompt_tokens)
    completion = _metric_numbers(trials, lambda trial: None if trial.runtime is None else trial.runtime.completion_tokens)
    total = _metric_numbers(trials, lambda trial: None if trial.runtime is None else trial.runtime.total_tokens)
    successful_total = _metric_numbers(
        [trial for trial in trials if trial.passed is True],
        lambda trial: None if trial.runtime is None else trial.runtime.total_tokens,
    )
    tool_counts = _metric_numbers(trials, lambda trial: None if trial.runtime is None else trial.runtime.tool_call_count)
    successful_tools = _metric_numbers(
        [trial for trial in trials if trial.passed is True],
        lambda trial: None if trial.runtime is None else trial.runtime.tool_call_count,
    )
    failure_count = sum(
        trial.status is TrialStatus.FAILED
        or (trial.status is TrialStatus.COMPLETED and trial.passed is not True)
        for trial in trials
    )
    timeout_count = sum(trial.status is TrialStatus.TIMEOUT for trial in trials)
    environment_count = sum(trial.status is TrialStatus.ENVIRONMENT_ERROR for trial in trials)
    cancelled_count = sum(trial.status is TrialStatus.CANCELLED for trial in trials)

    metrics: list[MetricSnapshot] = [
        _rate_metric("task_success_rate", summary.task_success_rate, len(task_values), len(task_values)),
        _rate_metric("tool_correctness_rate", summary.tool_correctness_rate, len(tool_values), len(tool_values)),
        _rate_metric("memory_required_evidence_hit_rate", summary.memory_required_evidence_hit_rate, len(evidence), len(evidence)),
        _rate_metric("memory_recall_at_k_mean", summary.memory_recall_at_k_mean, len(recalls), len(recalls)),
        _metric("memory_recall_at_k_p50", _nearest_rank(recalls, 0.5), sample_count=len(recalls), denominator=len(recalls), unit="rate", direction=MetricDirection.HIGHER_IS_BETTER),
        _rate_metric("memory_mrr_mean", summary.memory_mrr_mean, len(mrrs), len(mrrs)),
        _metric("memory_mrr_p50", _nearest_rank(mrrs, 0.5), sample_count=len(mrrs), denominator=len(mrrs), unit="rate", direction=MetricDirection.HIGHER_IS_BETTER),
        _rate_metric("background_review_decision_accuracy", summary.background_review_decision_accuracy, summary.background_review_decision_sample_count, summary.background_review_decision_sample_count),
        _rate_metric("failure_rate", (failure_count / trial_count if trial_count else 0.0), trial_count, trial_count),
        _rate_metric("timeout_rate", (timeout_count / trial_count if trial_count else 0.0), trial_count, trial_count),
        _rate_metric("environment_error_rate", (environment_count / trial_count if trial_count else 0.0), trial_count, trial_count),
        _rate_metric("cancelled_rate", (cancelled_count / trial_count if trial_count else 0.0), trial_count, trial_count),
        _metric("conversation_turn_count_mean", _mean(turns), sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("conversation_turn_count_p50", _nearest_rank(turns, 0.5), sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("conversation_turn_count_p95", _nearest_rank(turns, 0.95), sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("conversation_turn_count_min", min(turns) if turns else None, sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("conversation_turn_count_max", max(turns) if turns else None, sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("conversation_turn_count_stddev", _stddev(turns), sample_count=len(turns), denominator=len(turns), unit="turns", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_mean", _mean(iterations), sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_p50", _nearest_rank(iterations, 0.5), sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_p95", _nearest_rank(iterations, 0.95), sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_min", min(iterations) if iterations else None, sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_max", max(iterations) if iterations else None, sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("agent_iterations_stddev", _stddev(iterations), sample_count=len(iterations), denominator=len(iterations), unit="iterations", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_mean_ms", _mean(durations), sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_p50_ms", _nearest_rank(durations, 0.5), sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_p95_ms", _nearest_rank(durations, 0.95), sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_min_ms", min(durations) if durations else None, sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_max_ms", max(durations) if durations else None, sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("duration_stddev_ms", _stddev(durations), sample_count=len(durations), denominator=len(durations), unit="milliseconds", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("prompt_tokens_mean_per_trial", _mean(prompt), sample_count=len(prompt), denominator=len(prompt), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("prompt_tokens_p50", _nearest_rank(prompt, 0.5), sample_count=len(prompt), denominator=len(prompt), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("prompt_tokens_p95", _nearest_rank(prompt, 0.95), sample_count=len(prompt), denominator=len(prompt), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("completion_tokens_mean_per_trial", _mean(completion), sample_count=len(completion), denominator=len(completion), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("completion_tokens_p50", _nearest_rank(completion, 0.5), sample_count=len(completion), denominator=len(completion), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("completion_tokens_p95", _nearest_rank(completion, 0.95), sample_count=len(completion), denominator=len(completion), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("total_tokens_mean_per_trial", _mean(total), sample_count=len(total), denominator=len(total), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("total_tokens_p50", _nearest_rank(total, 0.5), sample_count=len(total), denominator=len(total), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("total_tokens_p95", _nearest_rank(total, 0.95), sample_count=len(total), denominator=len(total), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("total_tokens_mean_per_success", _mean(successful_total), sample_count=len(successful_total), denominator=len(successful_total), unit="tokens", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_mean", _mean(tool_counts), sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_p50", _nearest_rank(tool_counts, 0.5), sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_p95", _nearest_rank(tool_counts, 0.95), sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_min", min(tool_counts) if tool_counts else None, sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_max", max(tool_counts) if tool_counts else None, sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_stddev", _stddev(tool_counts), sample_count=len(tool_counts), denominator=len(tool_counts), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
        _metric("tool_call_count_mean_per_success", _mean(successful_tools), sample_count=len(successful_tools), denominator=len(successful_tools), unit="calls", direction=MetricDirection.LOWER_IS_BETTER),
    ]
    cache = summary.deepseek_cache
    cache_values = {
        "deepseek_cache_hit_rate": None if cache is None else cache.cache_hit_rate,
        "deepseek_cache_model_call_coverage_rate": None if cache is None else cache.model_call_coverage_rate,
        "deepseek_cache_trial_coverage_rate": None if cache is None else cache.trial_coverage_rate,
        "deepseek_cache_hit_tokens": None if cache is None else cache.prompt_cache_hit_tokens,
        "deepseek_cache_miss_tokens": None if cache is None else cache.prompt_cache_miss_tokens,
        "deepseek_cache_evaluated_prompt_tokens": None if cache is None else cache.deepseek_cache_evaluated_prompt_tokens,
    }
    for name in (
        "deepseek_cache_hit_rate",
        "deepseek_cache_model_call_coverage_rate",
        "deepseek_cache_trial_coverage_rate",
    ):
        metrics.append(_rate_metric(name, cache_values[name], cache.evaluated_model_call_count if cache else 0, cache.model_call_count if cache else 0))
    for name in (
        "deepseek_cache_hit_tokens",
        "deepseek_cache_miss_tokens",
        "deepseek_cache_evaluated_prompt_tokens",
    ):
        metrics.append(
            _metric(
                name,
                cache_values[name],
                sample_count=cache.evaluated_model_call_count if cache else 0,
                denominator=cache.model_call_count if cache else 0,
                unit="tokens",
            )
        )
    cost = summary.deepseek_cost
    cost_values = {
        "deepseek_cost_available_total_usd": None if cost is None else cost.available_total_cost_usd,
        "deepseek_cost_total_usd": None if cost is None else cost.total_cost_usd,
        "deepseek_cost_mean_per_evaluated_trial_usd": None if cost is None else cost.mean_cost_per_evaluated_trial_usd,
        "deepseek_cost_mean_per_successful_trial_usd": None if cost is None else cost.mean_cost_per_successful_trial_usd,
        "deepseek_cost_effective_cost_per_success_usd": None if cost is None else cost.effective_cost_per_success_usd,
        "deepseek_cost_cache_savings_usd": None if cost is None else cost.cache_savings_usd,
        "deepseek_cost_coverage_rate": None if cost is None else cost.cost_coverage_rate,
    }
    for name, value in cost_values.items():
        metrics.append(
            _metric(
                name,
                value,
                sample_count=cost.available_trial_count if cost else 0,
                denominator=cost.trial_count if cost else 0,
                unit=("rate" if name.endswith("rate") else "USD"),
                direction=(
                    MetricDirection.LOWER_IS_BETTER
                    if "cost" in name and not name.endswith("savings_usd")
                    else MetricDirection.HIGHER_IS_BETTER
                ),
            )
        )
    return metrics


def _benchmark_summary(result: AuditRunResult, trials: Sequence[TrialResult]) -> BenchmarkSummary:
    from myhermes_audit.reports.aggregate import aggregate_audit

    summary = result.summary if len(trials) == len(result.trials) else aggregate_audit(
        sorted({trial.case_id for trial in trials}), trials
    )
    task_sample, task_passed, task_rate = _task_success_facts(trials)
    return BenchmarkSummary(
        summary=summary,
        task_success_sample_count=task_sample,
        task_success_passed_count=task_passed,
        task_success_rate=task_rate,
        metrics=_build_metrics(trials, summary),
        failure_categories=_failure_categories(trials),
        background_review_actions=_review_actions(trials),
        warnings=sorted(
            {
                "cache_invalid"
                for trial in trials
                if trial.runtime is not None
                and trial.runtime.deepseek_cache_status is DeepSeekCacheStatus.INVALID
            }
        ),
    )


def _benchmark_cases(result: AuditRunResult) -> list[BenchmarkCaseSummary]:
    from myhermes_audit.reports.aggregate import aggregate_audit

    items: list[BenchmarkCaseSummary] = []
    for aggregate in result.cases:
        trials = [trial for trial in result.trials if trial.case_id == aggregate.case_id]
        case_summary = aggregate_audit([aggregate.case_id], trials)
        task_sample, task_passed, task_rate = _task_success_facts(trials)
        items.append(
            BenchmarkCaseSummary(
                case_id=aggregate.case_id,
                summary=aggregate,
                declared_trial_count=_declared_trials_per_case(trials),
                task_success_sample_count=task_sample,
                task_success_passed_count=task_passed,
                task_success_rate=task_rate,
                metrics=_build_metrics(trials, case_summary),
                failure_categories=_failure_categories(trials),
                background_review_actions=_review_actions(trials),
                background_review_decision_accuracy=_review_accuracy(trials),
                background_review_decision_sample_count=max(
                    _review_accuracy_sample_count(trials),
                    sum(_review_actions(trials).values()),
                ),
                deepseek_cache=case_summary.deepseek_cache,
                deepseek_cost=case_summary.deepseek_cost,
                warnings=sorted(
                    {
                        "cache_invalid"
                        for trial in trials
                        if trial.runtime is not None
                        and trial.runtime.deepseek_cache_status is DeepSeekCacheStatus.INVALID
                    }
                ),
            )
        )
    return items


def _identity_values(result: AuditRunResult) -> tuple[dict[str, IdentityEvidence], list[str]]:
    models = {
        value
        for trial in result.trials
        for value in (
            None if trial.runtime is None else trial.runtime.subject_model,
            None if trial.trial_identity is None else trial.trial_identity.model_identifier,
        )
        if value
    }
    configs = {trial.configuration_fingerprint for trial in result.trials if trial.configuration_fingerprint}
    protocols = {
        trial.observations.worker_protocol_version
        for trial in result.trials
        if trial.observations is not None
    }
    identities = {
        "model": _identity_evidence(models),
        "configuration": _identity_evidence(configs),
        "worker_protocol": _identity_evidence(protocols),
        "result_schema": _identity_evidence({result.schema_version}),
        "metric_contract": _identity_evidence({METRIC_CONTRACT_VERSION}),
    }
    warnings = [
        f"{name}_identity_ambiguous"
        for name, evidence in identities.items()
        if evidence.status is IdentityStatus.AMBIGUOUS
    ]
    return identities, warnings


def _identity_projection(evidence: IdentityEvidence) -> str | None:
    return evidence.value if evidence.status is IdentityStatus.AVAILABLE else None


def _identity_comparison(
    baseline: IdentityEvidence,
    current: IdentityEvidence,
    name: str,
) -> str | None:
    """Return a safe structural reason, or None when identities are comparable."""

    if baseline.status is IdentityStatus.AMBIGUOUS or current.status is IdentityStatus.AMBIGUOUS:
        return f"{name}_identity_ambiguous"
    if baseline.status is IdentityStatus.MISSING or current.status is IdentityStatus.MISSING:
        return f"{name}_identity_missing"
    if baseline.value != current.value:
        return f"{name}_identity_mismatch"
    return None


def build_baseline(result: AuditRunResult) -> AuditBaseline:
    if not isinstance(result, AuditRunResult):
        raise ValueError("baseline input must be an AuditRunResult")
    identities, warnings = _identity_values(result)
    incomplete = sorted(
        name for name, evidence in identities.items()
        if evidence.status is not IdentityStatus.AVAILABLE
    )
    if incomplete:
        raise ValueError(
            "baseline core identity is missing or ambiguous: "
            + ", ".join(incomplete)
        )
    suite = _benchmark_summary(result, result.trials)
    cases = _benchmark_cases(result)
    case_ids = [case.case_id for case in cases]
    declared_per_case, declared_mapping = _declared_trial_mapping(cases)
    baseline_fields = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "source_run_id": result.run_id,
        "audit_commit": result.audit_fingerprint.audit_commit,
        "subject_commit": result.subject_fingerprint.git_commit,
        "suite_id": result.suite_id,
        "suite_fingerprint": result.audit_fingerprint.suite_sha256,
        "suite_comparison_fingerprint": (
            result.audit_fingerprint.suite_comparison_sha256
            or result.audit_fingerprint.suite_sha256
        ),
        "result_schema_version": result.schema_version,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "model_identity": identities["model"],
        "configuration_identity": identities["configuration"],
        "worker_protocol_identity": identities["worker_protocol"],
        "result_schema_identity": identities["result_schema"],
        "metric_contract_identity": identities["metric_contract"],
        "worker_protocol_version": _identity_projection(identities["worker_protocol"]),
        "model_identifier": _identity_projection(identities["model"]),
        "configuration_fingerprint": _identity_projection(identities["configuration"]),
        "pricing_fingerprint": result.deepseek_pricing_fingerprint,
        "declared_trial_count": result.summary.trial_count,
        "total_trial_count": result.summary.trial_count,
        "declared_trials_per_case": declared_per_case,
        "declared_trial_counts_by_case": declared_mapping,
        "case_ids": case_ids,
        "suite_summary": suite,
        "case_summaries": cases,
        "warnings": sorted(set(warnings + suite.warnings)),
    }
    fingerprint = canonical_sha256(baseline_fields)
    return AuditBaseline(
        baseline_id=f"baseline-{fingerprint[:16]}",
        baseline_fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc),
        **baseline_fields,
    )


def load_regression_policy(path: Path) -> RegressionPolicy:
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("cannot read regression policy") from exc
    try:
        return RegressionPolicy.model_validate(document)
    except Exception as exc:
        raise ValueError("regression policy is invalid") from exc


def _snapshot_map(items: Iterable[MetricSnapshot]) -> dict[str, MetricSnapshot]:
    return {item.metric_name: item for item in items}


def _compare_metric(
    baseline: MetricSnapshot | None,
    current: MetricSnapshot | None,
    policy_facts: MetricPolicyFacts,
    *,
    pricing_comparable: bool,
    pricing_reason: str | None = None,
    comparability_fact_codes: Sequence[str] = (),
) -> MetricComparison:
    name = (current or baseline).metric_name  # type: ignore[union-attr]
    base_value = None if baseline is None else baseline.value
    current_value = None if current is None else current.value
    base_samples = 0 if baseline is None else baseline.sample_count
    current_samples = 0 if current is None else current.sample_count
    base_denominator = None if baseline is None else baseline.denominator
    current_denominator = None if current is None else current.denominator
    common = dict(
        metric_name=name,
        baseline_value=base_value,
        current_value=current_value,
        baseline_sample_count=base_samples,
        current_sample_count=current_samples,
        baseline_denominator=base_denominator,
        current_denominator=current_denominator,
        policy_mode=policy_facts.mode,
        direction=policy_facts.direction,
        max_absolute_drop=policy_facts.max_absolute_drop,
        max_relative_increase=policy_facts.max_relative_increase,
        max_absolute_increase=policy_facts.max_absolute_increase,
    )
    requires_pricing_match = policy_facts.requires_pricing_match
    pricing_issue = not pricing_comparable and requires_pricing_match
    facts = derive_metric_evaluation_facts(
        MetricEvaluationInput(
            metric_name=name,
            baseline_present=baseline is not None,
            current_present=current is not None,
            baseline_value=base_value,
            current_value=current_value,
            baseline_sample_count=base_samples,
            current_sample_count=current_samples,
            comparability_fact_codes=tuple(
                [*comparability_fact_codes]
                + ([pricing_reason or "pricing_fingerprint_mismatch"] if pricing_issue else [])
            ),
        )
    )
    decision_result = decide_metric_comparison(
        MetricDecisionInput(
            baseline_value=base_value,
            current_value=current_value,
            direction=policy_facts.direction,
            policy_mode=policy_facts.mode,
            max_absolute_drop=policy_facts.max_absolute_drop,
            max_relative_increase=policy_facts.max_relative_increase,
            max_absolute_increase=policy_facts.max_absolute_increase,
            evaluation_status=facts.evaluation_status.value,
            comparability_status=facts.comparability_status.value,
            reason_codes=facts.reason_codes,
        )
    )
    return MetricComparison(
        **common,
        baseline_metric_present=baseline is not None,
        current_metric_present=current is not None,
        comparability_fact_codes=list(facts.comparability_fact_codes),
        requires_pricing_match=requires_pricing_match,
        comparability_facts_verified=False,
        policy_facts_verified=False,
        evaluation_status=facts.evaluation_status,
        comparability_status=facts.comparability_status,
        reason_codes=list(facts.reason_codes),
        absolute_delta=decision_result.absolute_delta,
        relative_delta=decision_result.relative_delta,
        decision=MetricDecision(decision_result.decision.value),
        reason=decision_result.reason,
    )


def _case_decision(
    metrics: Sequence[MetricComparison],
    policy_facts: PolicySnapshotFacts,
) -> MetricDecision:
    result = decide_case_regression(
        [
            derive_metric_decision(
                metric,
                resolve_metric_policy(metric.metric_name, policy_facts),
            ).value
            for metric in metrics
        ]
    )
    return MetricDecision(result.decision.value)


def _case_decision_reason(
    metrics: Sequence[MetricComparison],
    policy_facts: PolicySnapshotFacts,
) -> str | None:
    return decide_case_regression(
        [
            derive_metric_decision(
                metric,
                resolve_metric_policy(metric.metric_name, policy_facts),
            ).value
            for metric in metrics
        ]
    ).reason


def _report_counts(
    metrics: Sequence[MetricComparison],
    cases: Sequence[CaseRegressionSummary],
    policy_facts: PolicySnapshotFacts,
) -> dict[str, int]:
    all_metrics = [*metrics, *(metric for case in cases for metric in case.metrics)]
    return metric_decision_counts(all_metrics, policy_facts)


def compare_baseline(
    baseline: AuditBaseline,
    current: AuditRunResult,
    policy: RegressionPolicy,
) -> AuditRegressionReport:
    if (
        not isinstance(baseline, AuditBaseline)
        or not isinstance(current, AuditRunResult)
        or not isinstance(policy, RegressionPolicy)
    ):
        raise ValueError("compare inputs must be validated baseline, AuditRunResult, and RegressionPolicy")
    policy_snapshot = RegressionPolicySnapshot.from_policy(policy)
    policy_facts = policy_snapshot.to_facts()
    current_suite = _benchmark_summary(current, current.trials)
    current_cases = {item.case_id: item for item in _benchmark_cases(current)}
    baseline_cases = {item.case_id: item for item in baseline.case_summaries}
    current_comparison_fingerprint = current.audit_fingerprint.suite_comparison_sha256
    baseline_comparison_fingerprint = baseline.suite_comparison_fingerprint
    baseline_ids = baseline.case_ids
    current_ids = [item.case_id for item in current.cases]
    current_identities, identity_warnings = _identity_values(current)
    reasons = list(
        derive_comparability_reason_codes(
            baseline_suite_id=baseline.suite_id,
            current_suite_id=current.suite_id,
            baseline_suite_fingerprint=baseline.suite_fingerprint,
            current_suite_fingerprint=current.audit_fingerprint.suite_sha256,
            baseline_suite_comparison_fingerprint=baseline_comparison_fingerprint,
            current_suite_comparison_fingerprint=current_comparison_fingerprint,
            baseline_case_ids=baseline_ids,
            current_case_ids=current_ids,
            identities=tuple(
                (
                    name,
                    old_identity.status.value,
                    old_identity.value,
                    new_identity.status.value,
                    new_identity.value,
                )
                for name, old_identity, new_identity in (
                    ("worker_protocol", baseline.worker_protocol_identity, current_identities["worker_protocol"]),
                    ("model", baseline.model_identity, current_identities["model"]),
                    ("configuration", baseline.configuration_identity, current_identities["configuration"]),
                    ("result_schema", baseline.result_schema_identity, current_identities["result_schema"]),
                    ("metric_contract", baseline.metric_contract_identity, current_identities["metric_contract"]),
                )
            ),
            baseline_pricing_fingerprint=baseline.pricing_fingerprint,
            current_pricing_fingerprint=current.deepseek_pricing_fingerprint,
        )
    )
    pricing_comparable = "pricing_fingerprint_mismatch" not in reasons and "pricing_fingerprint_missing" not in reasons
    pricing_reason = next(
        (
            reason
            for reason in ("pricing_fingerprint_missing", "pricing_fingerprint_mismatch")
            if reason in reasons
        ),
        None,
    )
    core_reasons = [
        reason
        for reason in reasons
        if reason not in {"pricing_fingerprint_mismatch", "pricing_fingerprint_missing"}
    ]
    suite_metrics: list[MetricComparison] = []
    current_map = _snapshot_map(current_suite.metrics)
    baseline_map = _snapshot_map(baseline.suite_summary.metrics)
    for name in sorted(set(current_map) | set(baseline_map)):
        suite_metrics.append(
            _compare_metric(
                baseline_map.get(name),
                current_map.get(name),
                resolve_metric_policy(name, policy_facts),
                pricing_comparable=pricing_comparable,
                pricing_reason=pricing_reason,
                comparability_fact_codes=core_reasons,
            )
        )
    case_reports: list[CaseRegressionSummary] = []
    for case_id in baseline_ids:
        old = baseline_cases.get(case_id)
        new = current_cases.get(case_id)
        if old is None or new is None:
            continue
        old_map = _snapshot_map(old.metrics)
        new_map = _snapshot_map(new.metrics)
        metrics = [
            _compare_metric(
                old_map.get(name),
                new_map.get(name),
                resolve_metric_policy(name, policy_facts),
                pricing_comparable=pricing_comparable,
                pricing_reason=pricing_reason,
                comparability_fact_codes=core_reasons,
            )
            for name in sorted(set(old_map) | set(new_map))
        ]
        case_reports.append(
            CaseRegressionSummary(
                case_id=case_id,
                baseline_trial_count=old.summary.trial_count,
                current_trial_count=new.summary.trial_count,
                baseline_declared_trial_count=old.declared_trial_count,
                current_declared_trial_count=new.declared_trial_count,
                baseline_task_success_sample_count=old.task_success_sample_count,
                baseline_task_success_passed_count=old.task_success_passed_count,
                baseline_task_success_rate=old.task_success_rate,
                current_task_success_sample_count=new.task_success_sample_count,
                current_task_success_passed_count=new.task_success_passed_count,
                current_task_success_rate=new.task_success_rate,
                task_success_rate_delta=(
                    None
                    if old.task_success_rate is None or new.task_success_rate is None
                    else new.task_success_rate - old.task_success_rate
                ),
                pass_rate_delta=(
                    None
                    if old.task_success_rate is None or new.task_success_rate is None
                    else new.task_success_rate - old.task_success_rate
                ),
                baseline_failure_categories=old.failure_categories,
                current_failure_categories=new.failure_categories,
                baseline_background_review_actions=old.background_review_actions,
                current_background_review_actions=new.background_review_actions,
                baseline_review_decision_accuracy=old.background_review_decision_accuracy,
                current_review_decision_accuracy=new.background_review_decision_accuracy,
                baseline_background_review_decision_sample_count=old.background_review_decision_sample_count,
                background_review_decision_sample_count=new.background_review_decision_sample_count,
                comparability_facts_verified=False,
                policy_facts_verified=False,
                metrics=metrics,
                metric_comparison_count=len(metrics),
                decision_reason=_case_decision_reason(metrics, policy_facts),
                decision=_case_decision(metrics, policy_facts),
            )
        )
    counts = _report_counts(suite_metrics, case_reports, policy_facts)
    comparable_metric_count = sum(
        derive_metric_decision(
            item,
            resolve_metric_policy(item.metric_name, policy_facts),
        ).value
        in {
            MetricDecision.IMPROVED.value,
            MetricDecision.UNCHANGED.value,
            MetricDecision.REGRESSED.value,
            MetricDecision.WARNING.value,
        }
        for item in [
            *suite_metrics,
            *(metric for case in case_reports for metric in case.metrics),
        ]
    )
    if comparable_metric_count == 0:
        reasons.append("no_comparable_core_metrics")
    pricing_applicability_fingerprint_value = pricing_applicability_fingerprint(
        policy_snapshot.policy_fingerprint,
        suite_metrics,
        case_reports,
    )
    core_reasons = [
        reason
        for reason in reasons
        if reason not in {"pricing_fingerprint_mismatch", "pricing_fingerprint_missing"}
    ]
    report_decision = decide_report_status(
        regression_count=counts["regression_count"],
        warning_count=counts["warning_count"],
        comparable_metric_count=comparable_metric_count,
        core_reason_count=len(core_reasons),
    )
    status = RegressionStatus(report_decision.status)
    warnings = sorted(set(identity_warnings + ([reason for reason in ("pricing_fingerprint_missing", "pricing_fingerprint_mismatch") if reason in reasons] if not pricing_comparable else [])))
    current_model = _identity_projection(current_identities["model"])
    current_config = _identity_projection(current_identities["configuration"])
    current_protocol = _identity_projection(current_identities["worker_protocol"])
    current_declared_per_case, current_declared_mapping = _declared_trial_mapping(
        list(current_cases.values())
    )
    return AuditRegressionReport(
        baseline_id=baseline.baseline_id,
        current_run_id=current.run_id,
        status=status,
        regression_policy=policy_snapshot,
        regression_policy_fingerprint=policy_snapshot.policy_fingerprint,
        policy_facts_verified=True,
        comparability_facts_verified=True,
        comparability_reasons=sorted(set(reasons)),
        baseline_suite_id=baseline.suite_id,
        suite_id=current.suite_id,
        baseline_suite_fingerprint=baseline.suite_fingerprint,
        current_suite_fingerprint=current.audit_fingerprint.suite_sha256,
        baseline_suite_comparison_fingerprint=baseline_comparison_fingerprint,
        current_suite_comparison_fingerprint=current_comparison_fingerprint,
        baseline_subject_commit=baseline.subject_commit,
        current_subject_commit=current.subject_fingerprint.git_commit,
        baseline_audit_commit=baseline.audit_commit,
        current_audit_commit=current.audit_fingerprint.audit_commit,
        baseline_model_identifier=baseline.model_identifier,
        current_model_identifier=current_model,
        baseline_configuration_fingerprint=baseline.configuration_fingerprint,
        current_configuration_fingerprint=current_config,
        baseline_model_identity=baseline.model_identity,
        current_model_identity=current_identities["model"],
        baseline_configuration_identity=baseline.configuration_identity,
        current_configuration_identity=current_identities["configuration"],
        baseline_worker_protocol_identity=baseline.worker_protocol_identity,
        current_worker_protocol_identity=current_identities["worker_protocol"],
        baseline_result_schema_identity=baseline.result_schema_identity,
        current_result_schema_identity=current_identities["result_schema"],
        baseline_metric_contract_identity=baseline.metric_contract_identity,
        current_metric_contract_identity=current_identities["metric_contract"],
        baseline_pricing_fingerprint=baseline.pricing_fingerprint,
        current_pricing_fingerprint=current.deepseek_pricing_fingerprint,
        pricing_applicability_fingerprint=pricing_applicability_fingerprint_value,
        baseline_trial_count=baseline.declared_trial_count,
        current_trial_count=current.summary.trial_count,
        baseline_total_trial_count=baseline.total_trial_count,
        current_total_trial_count=current.summary.trial_count,
        baseline_declared_trials_per_case=baseline.declared_trials_per_case,
        current_declared_trials_per_case=current_declared_per_case,
        baseline_declared_trial_counts_by_case=baseline.declared_trial_counts_by_case,
        current_declared_trial_counts_by_case=current_declared_mapping,
        baseline_suite_task_success_sample_count=baseline.suite_summary.task_success_sample_count,
        baseline_suite_task_success_passed_count=baseline.suite_summary.task_success_passed_count,
        baseline_suite_task_success_rate=baseline.suite_summary.task_success_rate,
        current_suite_task_success_sample_count=current_suite.task_success_sample_count,
        current_suite_task_success_passed_count=current_suite.task_success_passed_count,
        current_suite_task_success_rate=current_suite.task_success_rate,
        suite_task_success_rate_delta=(
            None
            if baseline.suite_summary.task_success_rate is None or current_suite.task_success_rate is None
            else current_suite.task_success_rate - baseline.suite_summary.task_success_rate
        ),
        baseline_metric_contract_version=baseline.metric_contract_version,
        baseline_result_schema_version=baseline.result_schema_version,
        current_result_schema_version=current.schema_version,
        current_worker_protocol_version=current_protocol,
        suite_metrics=suite_metrics,
        case_summaries=case_reports,
        **counts,
        overall_regression_gate=report_decision.gate,
        warnings=warnings,
    )


__all__ = ("build_baseline", "compare_baseline", "load_regression_policy")
