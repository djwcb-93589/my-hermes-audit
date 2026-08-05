"""Audit-side DeepSeek cost calculation and shared aggregation."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from myhermes_audit.contracts import (
    DeepSeekCostAggregate,
    DeepSeekCostStatus,
    DeepSeekCostSummary,
    DeepSeekPricingConfig,
    DeepSeekPricingSnapshot,
    DeepSeekCacheStatus,
    TrialResult,
)
from myhermes_audit.contracts.cost import charge_tokens, quantize_money, quantize_rate


def compute_deepseek_cost(
    pricing: DeepSeekPricingConfig | None,
    runtime: Any | None,
) -> DeepSeekCostSummary:
    """Return a local invalid summary instead of disrupting a Trial."""

    try:
        return _compute_deepseek_cost(pricing, runtime)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.INVALID,
            pricing_snapshot=(None if pricing is None else pricing.snapshot()),
            pricing_fingerprint=(
                None if pricing is None else pricing.pricing_fingerprint()
            ),
            currency=(None if pricing is None else pricing.currency),
            warnings=["cost_calculation_invalid"],
        )


def _compute_deepseek_cost(
    pricing: DeepSeekPricingConfig | None,
    runtime: Any | None,
) -> DeepSeekCostSummary:
    """Compute one Trial cost from the canonical runtime summary only."""

    if runtime is None:
        return _not_evaluated(pricing, warnings=("no_runtime_summary",))
    prompt_tokens = runtime.prompt_tokens
    completion_tokens = runtime.completion_tokens
    evaluated_prompt_tokens = runtime.deepseek_cache_evaluated_prompt_tokens
    hit_tokens = runtime.prompt_cache_hit_tokens
    miss_tokens = runtime.prompt_cache_miss_tokens
    cache_status = runtime.deepseek_cache_status
    common = {
        "subject_model": runtime.subject_model,
        "prompt_tokens": prompt_tokens,
        "prompt_cache_hit_tokens": hit_tokens,
        "prompt_cache_miss_tokens": miss_tokens,
        "evaluated_prompt_tokens": evaluated_prompt_tokens,
        "unclassified_prompt_tokens": _unclassified(
            prompt_tokens, evaluated_prompt_tokens
        ),
        "completion_tokens": completion_tokens,
    }
    if pricing is None:
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.NOT_EVALUATED,
            **common,
            warnings=["pricing_not_configured"],
        )
    snapshot = pricing.snapshot()
    identity = {
        "pricing_snapshot": snapshot,
        "pricing_fingerprint": snapshot.pricing_fingerprint,
        "currency": snapshot.currency,
    }
    if runtime.subject_model is None:
        return _not_evaluated(
            pricing,
            facts=common,
            warnings=("subject_model_unavailable",),
        )
    if runtime.subject_model != pricing.model:
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.INVALID,
            **identity,
            **common,
            warnings=["deepseek_pricing_model_mismatch"],
        )
    if prompt_tokens is None or completion_tokens is None:
        return _not_evaluated(
            pricing,
            facts=common,
            warnings=("ordinary_token_usage_unavailable",),
        )
    if cache_status is DeepSeekCacheStatus.INVALID:
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.INVALID,
            **identity,
            **common,
            warnings=["invalid_deepseek_cache_usage"],
        )
    if cache_status is DeepSeekCacheStatus.NOT_EVALUATED:
        return _not_evaluated(
            pricing,
            facts=common,
            warnings=("deepseek_cache_usage_unavailable",),
        )
    if (
        evaluated_prompt_tokens is None
        or hit_tokens is None
        or miss_tokens is None
        or hit_tokens + miss_tokens != evaluated_prompt_tokens
        or evaluated_prompt_tokens > prompt_tokens
    ):
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.INVALID,
            **identity,
            **common,
            warnings=["inconsistent_deepseek_cache_token_totals"],
        )
    if cache_status is DeepSeekCacheStatus.AVAILABLE:
        if evaluated_prompt_tokens != prompt_tokens:
            return DeepSeekCostSummary(
                status=DeepSeekCostStatus.INVALID,
                **identity,
                **common,
                warnings=["available_cache_does_not_cover_prompt_tokens"],
            )
        return _available_cost(
            snapshot=identity["pricing_snapshot"],
            **common,
        )
    if cache_status is DeepSeekCacheStatus.PARTIAL:
        if evaluated_prompt_tokens == prompt_tokens:
            return DeepSeekCostSummary(
                status=DeepSeekCostStatus.INVALID,
                **identity,
                **common,
                warnings=["partial_cache_covers_all_prompt_tokens"],
            )
        return _partial_cost(
            snapshot=identity["pricing_snapshot"],
            **common,
        )
    return DeepSeekCostSummary(
        status=DeepSeekCostStatus.INVALID,
        **identity,
        **common,
        warnings=["unknown_deepseek_cache_status"],
    )


def apply_deepseek_costs(
    pricing: DeepSeekPricingConfig | None,
    trials: Sequence[TrialResult],
) -> list[TrialResult]:
    """Attach parent-computed cost summaries without changing Worker facts."""

    return [
        trial.model_copy(
            update={
                "deepseek_cost": compute_deepseek_cost(
                    pricing,
                    None if trial.runtime is None else trial.runtime,
                )
            }
        )
        for trial in trials
    ]


def aggregate_deepseek_costs(
    trials: Sequence[TrialResult],
) -> DeepSeekCostAggregate:
    """Aggregate Trial summaries for both Case and Suite projections."""

    costs = [trial.deepseek_cost for trial in trials]
    statuses = [
        DeepSeekCostStatus.NOT_EVALUATED if cost is None else cost.status
        for cost in costs
    ]
    available_count = statuses.count(DeepSeekCostStatus.AVAILABLE)
    partial_count = statuses.count(DeepSeekCostStatus.PARTIAL)
    not_evaluated_count = statuses.count(DeepSeekCostStatus.NOT_EVALUATED)
    invalid_count = statuses.count(DeepSeekCostStatus.INVALID)
    token_bearing_count = sum(
        trial.runtime is not None
        and trial.runtime.prompt_tokens is not None
        and trial.runtime.completion_tokens is not None
        for trial in trials
    )
    fingerprint_values = [
        None if cost is None else cost.pricing_fingerprint for cost in costs
    ]
    currency_values = [None if cost is None else cost.currency for cost in costs]
    snapshot_values = [
        None if cost is None else cost.pricing_snapshot for cost in costs
    ]
    fingerprints = {value for value in fingerprint_values if value is not None}
    currencies = {value for value in currency_values if value is not None}
    snapshots = {
        value.pricing_fingerprint
        for value in snapshot_values
        if value is not None
    }
    snapshot_models = {
        value.model for value in snapshot_values if value is not None
    }
    warnings: list[str] = []
    inconsistent_identity = (
        len(fingerprints) > 1
        or len(currencies) > 1
        or len(snapshots) > 1
        or len(snapshot_models) > 1
    )
    if inconsistent_identity:
        warnings.append("inconsistent_pricing_identity")
    pricing_fingerprint = next(iter(fingerprints), None)
    currency = next(iter(currencies), None)
    pricing_snapshot = next(
        (value for value in snapshot_values if value is not None), None
    )
    if inconsistent_identity:
        pricing_fingerprint = None
        currency = None
        pricing_snapshot = None
    coverage = (
        None
        if token_bearing_count == 0
        else quantize_rate(Decimal(available_count) / Decimal(token_bearing_count))
    )
    base = dict(
        status=DeepSeekCostStatus.NOT_EVALUATED,
        currency=currency,
        pricing_fingerprint=pricing_fingerprint,
        pricing_snapshot=pricing_snapshot,
        trial_count=len(trials),
        token_bearing_trial_count=token_bearing_count,
        successful_trial_count=sum(trial.passed is True for trial in trials),
        available_trial_count=available_count,
        partial_trial_count=partial_count,
        not_evaluated_trial_count=not_evaluated_count,
        invalid_trial_count=invalid_count,
        cost_coverage_rate=coverage,
        warnings=warnings,
    )
    if invalid_count or inconsistent_identity:
        base["status"] = DeepSeekCostStatus.INVALID
        if invalid_count:
            base["warnings"].append("invalid_trial_cost")
        return DeepSeekCostAggregate(**base)
    if not available_count and not partial_count:
        base["status"] = DeepSeekCostStatus.NOT_EVALUATED
        return DeepSeekCostAggregate(**base)

    complete_costs = [
        cost
        for cost in costs
        if cost is not None
        and cost.status is DeepSeekCostStatus.AVAILABLE
        and cost.total_cost_usd is not None
    ]
    classified_costs = [
        cost.classified_cost_usd
        for cost in costs
        if cost is not None
        and cost.status in (DeepSeekCostStatus.AVAILABLE, DeepSeekCostStatus.PARTIAL)
        and cost.classified_cost_usd is not None
    ]
    successful_complete = [
        cost.total_cost_usd
        for trial, cost in zip(trials, costs)
        if trial.passed is True
        and cost is not None
        and cost.status is DeepSeekCostStatus.AVAILABLE
        and cost.total_cost_usd is not None
    ]
    successful_count = sum(trial.passed is True for trial in trials)
    evaluated_costs = [
        cost
        for cost in costs
        if cost is not None
        and cost.status in (DeepSeekCostStatus.AVAILABLE, DeepSeekCostStatus.PARTIAL)
    ]
    cost_evaluated_success_count = sum(
        trial.passed is True
        and cost is not None
        and cost.status is DeepSeekCostStatus.AVAILABLE
        for trial, cost in zip(trials, costs)
    )
    base["cost_evaluated_success_count"] = cost_evaluated_success_count
    base["cost_evaluated_success_total_usd"] = _sum_money(successful_complete)
    base.update(
        prompt_tokens=_sum_int(evaluated_costs, "prompt_tokens"),
        prompt_cache_hit_tokens=_sum_int(
            evaluated_costs, "prompt_cache_hit_tokens"
        ),
        prompt_cache_miss_tokens=_sum_int(
            evaluated_costs, "prompt_cache_miss_tokens"
        ),
        evaluated_prompt_tokens=_sum_int(
            evaluated_costs, "evaluated_prompt_tokens"
        ),
        unclassified_prompt_tokens=_sum_int(
            evaluated_costs, "unclassified_prompt_tokens"
        ),
        completion_tokens=_sum_int(evaluated_costs, "completion_tokens"),
        cache_hit_input_cost_usd=_sum_money(
            cost.cache_hit_input_cost_usd for cost in evaluated_costs
        ),
        cache_miss_input_cost_usd=_sum_money(
            cost.cache_miss_input_cost_usd for cost in evaluated_costs
        ),
        completion_cost_usd=_sum_money(
            cost.completion_cost_usd for cost in evaluated_costs
        ),
    )
    complete_total = _sum_money(cost.total_cost_usd for cost in complete_costs)
    classified_total = _sum_money(classified_costs)
    mean_evaluated = _mean_money(cost.total_cost_usd for cost in complete_costs)
    mean_successful = _mean_money(successful_complete)
    successful_complete_total = _sum_money(successful_complete)
    base["cost_evaluated_success_count"] = cost_evaluated_success_count
    base["cost_evaluated_success_total_usd"] = successful_complete_total
    base["available_total_cost_usd"] = complete_total
    effective = (
        None
        if successful_count == 0 or complete_total is None
        else quantize_money(complete_total / Decimal(successful_count))
    )
    status = (
        DeepSeekCostStatus.AVAILABLE
        if not partial_count and not not_evaluated_count and available_count == token_bearing_count
        else DeepSeekCostStatus.PARTIAL
    )
    base["status"] = status
    estimated = _sum_money(
        cost.estimated_cost_without_cache_usd
        for cost in complete_costs
        if cost.estimated_cost_without_cache_usd is not None
    )
    savings = _sum_money(
        cost.cache_savings_usd
        for cost in complete_costs
        if cost.cache_savings_usd is not None
    )
    if status is DeepSeekCostStatus.AVAILABLE:
        total = complete_total
        rate = (
            None
            if estimated in (None, Decimal("0")) or savings is None
            else quantize_rate(savings / estimated)
        )
    else:
        total = estimated = savings = rate = None
        effective = None
    return DeepSeekCostAggregate(
        **base,
        total_cost_usd=total,
        classified_cost_usd=classified_total,
        estimated_cost_without_cache_usd=estimated,
        cache_savings_usd=savings,
        cache_savings_rate=rate,
        mean_cost_per_evaluated_trial_usd=mean_evaluated,
        mean_cost_per_successful_trial_usd=mean_successful,
        effective_cost_per_success_usd=effective,
    )


def _available_cost(
    *,
    snapshot: DeepSeekPricingSnapshot,
    **facts: Any,
) -> DeepSeekCostSummary:
    hit_cost = charge_tokens(
        facts["prompt_cache_hit_tokens"],
        snapshot.prompt_cache_hit_usd_per_million_tokens,
    )
    miss_cost = charge_tokens(
        facts["prompt_cache_miss_tokens"],
        snapshot.prompt_cache_miss_usd_per_million_tokens,
    )
    completion_cost = charge_tokens(
        facts["completion_tokens"], snapshot.completion_usd_per_million_tokens
    )
    total = _quantized_sum(hit_cost, miss_cost, completion_cost)
    estimated = _quantized_sum(
        charge_tokens(
            facts["prompt_tokens"],
            snapshot.prompt_cache_miss_usd_per_million_tokens,
        ),
        completion_cost,
    )
    savings = quantize_money(estimated - total)
    if savings < 0:
        return DeepSeekCostSummary(
            status=DeepSeekCostStatus.INVALID,
            pricing_snapshot=snapshot,
            pricing_fingerprint=snapshot.pricing_fingerprint,
            currency=snapshot.currency,
            **facts,
            warnings=["cache_savings_would_be_negative"],
        )
    rate = None if estimated == 0 else quantize_rate(savings / estimated)
    return DeepSeekCostSummary(
        status=DeepSeekCostStatus.AVAILABLE,
        pricing_snapshot=snapshot,
        pricing_fingerprint=snapshot.pricing_fingerprint,
        currency=snapshot.currency,
        **facts,
        cache_hit_input_cost_usd=hit_cost,
        cache_miss_input_cost_usd=miss_cost,
        completion_cost_usd=completion_cost,
        classified_cost_usd=total,
        total_cost_usd=total,
        estimated_cost_without_cache_usd=estimated,
        cache_savings_usd=savings,
        cache_savings_rate=rate,
    )


def _partial_cost(
    *,
    snapshot: DeepSeekPricingSnapshot,
    **facts: Any,
) -> DeepSeekCostSummary:
    hit_cost = charge_tokens(
        facts["prompt_cache_hit_tokens"],
        snapshot.prompt_cache_hit_usd_per_million_tokens,
    )
    miss_cost = charge_tokens(
        facts["prompt_cache_miss_tokens"],
        snapshot.prompt_cache_miss_usd_per_million_tokens,
    )
    completion_cost = charge_tokens(
        facts["completion_tokens"], snapshot.completion_usd_per_million_tokens
    )
    return DeepSeekCostSummary(
        status=DeepSeekCostStatus.PARTIAL,
        pricing_snapshot=snapshot,
        pricing_fingerprint=snapshot.pricing_fingerprint,
        currency=snapshot.currency,
        **facts,
        classified_cost_usd=_quantized_sum(hit_cost, miss_cost, completion_cost),
        cache_hit_input_cost_usd=hit_cost,
        cache_miss_input_cost_usd=miss_cost,
        completion_cost_usd=completion_cost,
    )


def _not_evaluated(
    pricing: DeepSeekPricingConfig | None,
    *,
    facts: dict[str, Any] | None = None,
    warnings: tuple[str, ...],
) -> DeepSeekCostSummary:
    return DeepSeekCostSummary(
        status=DeepSeekCostStatus.NOT_EVALUATED,
        **({} if facts is None else facts),
        pricing_fingerprint=(
            None if pricing is None else pricing.pricing_fingerprint()
        ),
        currency=(None if pricing is None else pricing.currency),
        warnings=list(warnings),
    )


def _unclassified(prompt: int | None, evaluated: int | None) -> int | None:
    if prompt is None or evaluated is None or evaluated > prompt:
        return None
    return prompt - evaluated


def _quantized_sum(*values: Decimal | None) -> Decimal:
    return quantize_money(sum((value or Decimal("0")) for value in values))


def _sum_money(values) -> Decimal | None:
    values = [value for value in values if value is not None]
    return None if not values else _quantized_sum(*values)


def _sum_int(costs, field_name: str) -> int | None:
    if not costs:
        return None
    values = [getattr(cost, field_name) for cost in costs]
    return None if any(value is None for value in values) else sum(values)


def _mean_money(values) -> Decimal | None:
    values = [value for value in values if value is not None]
    return None if not values else quantize_money(sum(values) / Decimal(len(values)))


__all__ = (
    "aggregate_deepseek_costs",
    "apply_deepseek_costs",
    "compute_deepseek_cost",
)
