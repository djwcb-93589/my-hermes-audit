"""Small, subject-neutral metric projections shared by Worker and reports."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from myhermes_audit.contracts.result import DeepSeekCacheStatus


@dataclass(frozen=True, slots=True)
class CacheAggregation:
    model_call_count: int
    evaluated_model_call_count: int
    invalid_model_call_count: int
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    deepseek_cache_evaluated_prompt_tokens: int | None
    deepseek_cache_hit_rate: float | None
    deepseek_cache_status: DeepSeekCacheStatus


def aggregate_model_cache(
    observations: Iterable[object],
    *,
    invalid_model_call_count: int = 0,
) -> CacheAggregation:
    """Aggregate only the two public DeepSeek cache fields.

    The input is deliberately duck-typed so the same function can consume the
    Worker record and the parent-side observation summary without importing a
    runtime-specific implementation.  Invalid source rows are counted but
    never contribute token totals or a rate.
    """

    items = list(observations)
    model_call_count = len(items)
    invalid_count = max(0, min(invalid_model_call_count, model_call_count))
    evaluated = 0
    hit_total = 0
    miss_total = 0
    evaluated_prompt_total = 0
    for item in items:
        hit = getattr(item, "prompt_cache_hit_tokens", None)
        miss = getattr(item, "prompt_cache_miss_tokens", None)
        prompt = getattr(item, "prompt_tokens", None)
        if hit is None and miss is None:
            continue
        if (
            type(hit) is not int
            or type(miss) is not int
            or hit < 0
            or miss < 0
            or type(prompt) is not int
            or prompt < 0
            or hit + miss != prompt
        ):
            invalid_count += 1
            continue
        evaluated += 1
        evaluated_prompt_total += prompt
        hit_total += hit
        miss_total += miss
    invalid_count = min(invalid_count, model_call_count)
    if invalid_count:
        return CacheAggregation(
            model_call_count=model_call_count,
            evaluated_model_call_count=evaluated,
            invalid_model_call_count=invalid_count,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            deepseek_cache_evaluated_prompt_tokens=None,
            deepseek_cache_hit_rate=None,
            deepseek_cache_status=DeepSeekCacheStatus.INVALID,
        )
    if evaluated == 0:
        status = DeepSeekCacheStatus.NOT_EVALUATED
        hit_value = miss_value = rate = None
        evaluated_prompt_value = None
    else:
        status = (
            DeepSeekCacheStatus.AVAILABLE
            if evaluated == model_call_count
            else DeepSeekCacheStatus.PARTIAL
        )
        hit_value = hit_total
        miss_value = miss_total
        evaluated_prompt_value = evaluated_prompt_total
        rate = (
            None
            if evaluated_prompt_total == 0
            else hit_total / evaluated_prompt_total
        )
    return CacheAggregation(
        model_call_count=model_call_count,
        evaluated_model_call_count=evaluated,
        invalid_model_call_count=0,
        prompt_cache_hit_tokens=hit_value,
        prompt_cache_miss_tokens=miss_value,
        deepseek_cache_evaluated_prompt_tokens=evaluated_prompt_value,
        deepseek_cache_hit_rate=rate,
        deepseek_cache_status=status,
    )


__all__ = ("CacheAggregation", "aggregate_model_cache")
