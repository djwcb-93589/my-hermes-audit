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
    PolicyMode,
    RegressionMetricPolicy,
    RegressionPolicy,
    RegressionStatus,
    METRIC_CONTRACT_VERSION,
)
from myhermes_audit.serialization import canonical_sha256


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
    return BenchmarkSummary(
        summary=summary,
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
        items.append(
            BenchmarkCaseSummary(
                case_id=aggregate.case_id,
                summary=aggregate,
                metrics=_build_metrics(trials, case_summary),
                failure_categories=_failure_categories(trials),
                background_review_actions=_review_actions(trials),
                background_review_decision_accuracy=_review_accuracy(trials),
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


def _identity_values(result: AuditRunResult) -> tuple[str | None, str | None, str | None, list[str]]:
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
    warnings: list[str] = []
    model = next(iter(models)) if len(models) == 1 else None
    config = next(iter(configs)) if len(configs) == 1 else None
    protocol = next(iter(protocols)) if len(protocols) == 1 else None
    if len(models) > 1:
        warnings.append("multiple_model_identifiers")
    if len(configs) > 1:
        warnings.append("multiple_configuration_fingerprints")
    if len(protocols) > 1:
        warnings.append("multiple_worker_protocol_versions")
    return model, config, protocol, warnings


def build_baseline(result: AuditRunResult) -> AuditBaseline:
    if not isinstance(result, AuditRunResult):
        raise ValueError("baseline input must be an AuditRunResult")
    model, config, protocol, warnings = _identity_values(result)
    suite = _benchmark_summary(result, result.trials)
    cases = _benchmark_cases(result)
    case_ids = [case.case_id for case in cases]
    baseline_fields = {
        "schema_version": "baseline-v1",
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
        "worker_protocol_version": protocol,
        "model_identifier": model,
        "configuration_fingerprint": config,
        "pricing_fingerprint": result.deepseek_pricing_fingerprint,
        "declared_trial_count": result.summary.trial_count,
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


def _numeric_float(value: object) -> float | None:
    number = _number(value)
    return None if number is None else float(number)


def _compare_metric(
    baseline: MetricSnapshot | None,
    current: MetricSnapshot | None,
    policy: RegressionMetricPolicy,
    *,
    core_comparable: bool,
    pricing_comparable: bool,
    reason: str | None = None,
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
        policy_mode=policy.mode,
    )
    if not core_comparable:
        return MetricComparison(
            **common,
            decision=MetricDecision.NOT_COMPARABLE,
            reason=reason or "core_contract_not_comparable",
        )
    if policy.require_pricing_match and not pricing_comparable:
        return MetricComparison(
            **common,
            decision=MetricDecision.NOT_COMPARABLE,
            reason="pricing_fingerprint_mismatch",
        )
    if name.startswith("deepseek_cost_") and not pricing_comparable:
        return MetricComparison(
            **common,
            decision=MetricDecision.NOT_COMPARABLE,
            reason="pricing_fingerprint_mismatch",
        )
    base = _numeric_float(base_value)
    current_number = _numeric_float(current_value)
    if (
        baseline is None
        or current is None
        or baseline.availability is MetricAvailability.NOT_EVALUATED
        or current.availability is MetricAvailability.NOT_EVALUATED
        or base is None
        or current_number is None
    ):
        return MetricComparison(
            **common,
            decision=MetricDecision.NOT_COMPARABLE,
            reason="metric_not_evaluated",
        )
    delta = current_number - base
    relative = None if base == 0 else delta / abs(base)
    decision = MetricDecision.UNCHANGED
    adverse = False
    if policy.mode is PolicyMode.DISABLED:
        decision = MetricDecision.DISABLED
    elif policy.direction is MetricDirection.HIGHER_IS_BETTER:
        if delta > 0:
            decision = MetricDecision.IMPROVED
        elif delta < 0:
            adverse = (
                policy.max_absolute_drop is not None
                and -delta > policy.max_absolute_drop
            )
            decision = MetricDecision.REGRESSED if adverse and policy.mode is PolicyMode.FAILURE else (
                MetricDecision.WARNING if adverse else MetricDecision.UNCHANGED
            )
    elif policy.direction is MetricDirection.LOWER_IS_BETTER:
        if delta < 0:
            decision = MetricDecision.IMPROVED
        elif delta > 0:
            adverse = (
                (
                    policy.max_absolute_increase is not None
                    and delta > policy.max_absolute_increase
                )
                or (
                    policy.max_relative_increase is not None
                    and current_number > 0
                    and (
                        relative is None
                        or relative > policy.max_relative_increase
                    )
                )
            )
            decision = MetricDecision.REGRESSED if adverse and policy.mode is PolicyMode.FAILURE else (
                MetricDecision.WARNING if adverse else MetricDecision.UNCHANGED
            )
    return MetricComparison(
        **common,
        absolute_delta=delta,
        relative_delta=relative,
        decision=decision,
    )


def _case_decision(metrics: Sequence[MetricComparison]) -> MetricDecision:
    decisions = {metric.decision for metric in metrics}
    if MetricDecision.REGRESSED in decisions:
        return MetricDecision.REGRESSED
    if MetricDecision.WARNING in decisions:
        return MetricDecision.WARNING
    if MetricDecision.IMPROVED in decisions:
        return MetricDecision.IMPROVED
    if decisions and decisions <= {MetricDecision.NOT_COMPARABLE}:
        return MetricDecision.NOT_COMPARABLE
    if decisions and decisions <= {MetricDecision.DISABLED}:
        return MetricDecision.DISABLED
    return MetricDecision.UNCHANGED


def _report_counts(metrics: Sequence[MetricComparison], cases: Sequence[CaseRegressionSummary]) -> dict[str, int]:
    all_metrics = [*metrics, *(metric for case in cases for metric in case.metrics)]
    return {
        "regression_count": sum(item.decision is MetricDecision.REGRESSED for item in all_metrics),
        "improvement_count": sum(item.decision is MetricDecision.IMPROVED for item in all_metrics),
        "unchanged_count": sum(item.decision is MetricDecision.UNCHANGED for item in all_metrics),
        "warning_count": sum(item.decision is MetricDecision.WARNING for item in all_metrics),
        "not_comparable_count": sum(item.decision is MetricDecision.NOT_COMPARABLE for item in all_metrics),
    }


def compare_baseline(
    baseline: AuditBaseline,
    current: AuditRunResult,
    policy: RegressionPolicy,
) -> AuditRegressionReport:
    if not isinstance(baseline, AuditBaseline) or not isinstance(current, AuditRunResult):
        raise ValueError("compare inputs must be a validated baseline and AuditRunResult")
    current_suite = _benchmark_summary(current, current.trials)
    current_cases = {item.case_id: item for item in _benchmark_cases(current)}
    baseline_cases = {item.case_id: item for item in baseline.case_summaries}
    reasons: list[str] = []
    current_comparison_fingerprint = current.audit_fingerprint.suite_comparison_sha256
    baseline_comparison_fingerprint = baseline.suite_comparison_fingerprint
    if baseline.suite_id != current.suite_id:
        reasons.append("suite_id_mismatch")
    if baseline_comparison_fingerprint is None or current_comparison_fingerprint is None:
        if baseline.suite_fingerprint != current.audit_fingerprint.suite_sha256:
            reasons.append("suite_fingerprint_mismatch")
    elif baseline_comparison_fingerprint != current_comparison_fingerprint:
        reasons.append("suite_fingerprint_mismatch")
    baseline_ids = baseline.case_ids
    current_ids = [item.case_id for item in current.cases]
    if baseline_ids != current_ids:
        reasons.append("case_set_or_order_mismatch")
    if baseline.result_schema_version.split(".")[0] != current.schema_version.split(".")[0]:
        reasons.append("result_schema_incompatible")
    if baseline.metric_contract_version != METRIC_CONTRACT_VERSION:
        reasons.append("metric_contract_incompatible")
    _, _, current_protocol, identity_warnings = _identity_values(current)
    if baseline.worker_protocol_version != current_protocol:
        reasons.append("worker_protocol_mismatch")
    current_model, current_config, _, _ = _identity_values(current)
    if baseline.model_identifier != current_model:
        reasons.append("model_identifier_mismatch")
    if baseline.configuration_fingerprint != current_config:
        reasons.append("configuration_fingerprint_mismatch")
    pricing_comparable = baseline.pricing_fingerprint == current.deepseek_pricing_fingerprint
    if not pricing_comparable:
        reasons.append("pricing_fingerprint_mismatch")
    core_reasons = [reason for reason in reasons if reason != "pricing_fingerprint_mismatch"]
    core_comparable = not core_reasons
    policy_metrics = policy.metrics
    default_policy = RegressionMetricPolicy(mode=policy.default_mode)
    suite_metrics: list[MetricComparison] = []
    current_map = _snapshot_map(current_suite.metrics)
    baseline_map = _snapshot_map(baseline.suite_summary.metrics)
    for name in sorted(set(current_map) | set(baseline_map)):
        suite_metrics.append(
            _compare_metric(
                baseline_map.get(name),
                current_map.get(name),
                policy_metrics.get(name, default_policy),
                core_comparable=core_comparable,
                pricing_comparable=pricing_comparable,
                reason=(core_reasons[0] if core_reasons else None),
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
                policy_metrics.get(name, default_policy),
                core_comparable=core_comparable,
                pricing_comparable=pricing_comparable,
                reason=(core_reasons[0] if core_reasons else None),
            )
            for name in sorted(set(old_map) | set(new_map))
        ]
        case_reports.append(
            CaseRegressionSummary(
                case_id=case_id,
                baseline_trial_count=old.summary.trial_count,
                current_trial_count=new.summary.trial_count,
                baseline_pass_rate=old.summary.pass_rate,
                current_pass_rate=new.summary.pass_rate,
                pass_rate_delta=new.summary.pass_rate - old.summary.pass_rate,
                baseline_failure_categories=old.failure_categories,
                current_failure_categories=new.failure_categories,
                baseline_background_review_actions=old.background_review_actions,
                current_background_review_actions=new.background_review_actions,
                baseline_review_decision_accuracy=old.background_review_decision_accuracy,
                current_review_decision_accuracy=new.background_review_decision_accuracy,
                metrics=metrics,
                decision=_case_decision(metrics),
            )
        )
    counts = _report_counts(suite_metrics, case_reports)
    if not core_comparable:
        status = RegressionStatus.NOT_COMPARABLE
    elif counts["regression_count"]:
        status = RegressionStatus.REGRESSED
    elif counts["warning_count"]:
        status = RegressionStatus.PASSED_WITH_WARNINGS
    else:
        status = RegressionStatus.PASSED
    warnings = sorted(set(identity_warnings + (["pricing_fingerprint_mismatch"] if not pricing_comparable else [])))
    return AuditRegressionReport(
        baseline_id=baseline.baseline_id,
        current_run_id=current.run_id,
        status=status,
        comparability_reasons=sorted(set(reasons)),
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
        baseline_pricing_fingerprint=baseline.pricing_fingerprint,
        current_pricing_fingerprint=current.deepseek_pricing_fingerprint,
        baseline_trial_count=baseline.declared_trial_count,
        current_trial_count=current.summary.trial_count,
        baseline_metric_contract_version=baseline.metric_contract_version,
        current_result_schema_version=current.schema_version,
        current_worker_protocol_version=current_protocol,
        suite_metrics=suite_metrics,
        case_summaries=case_reports,
        **counts,
        overall_regression_gate=status in {
            RegressionStatus.PASSED,
            RegressionStatus.PASSED_WITH_WARNINGS,
        },
        warnings=warnings,
    )


__all__ = ("build_baseline", "compare_baseline", "load_regression_policy")
