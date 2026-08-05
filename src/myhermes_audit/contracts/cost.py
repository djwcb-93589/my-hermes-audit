"""Explicit DeepSeek pricing and cost contracts.

Pricing is an Audit-side input.  The contracts deliberately model money as
``Decimal`` values so that aggregation never accumulates binary floating point
rounding errors.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    NonEmptyText,
    NonNegativeInt,
    Sha256Digest,
)
from myhermes_audit.serialization import canonical_sha256


MILLION = Decimal("1000000")
MONEY_QUANTUM = Decimal("0.00000001")
RATE_QUANTUM = Decimal("0.00000001")


def _decimal_value(value: object, *, quantum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid decimal amounts")
    if isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, (int, str)):
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("value must be a finite decimal") from exc
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("value must be a finite decimal")
        # Convert through the textual representation, never through the
        # binary float's exact expansion.
        decimal = Decimal(str(value))
    else:
        raise ValueError("value must be a decimal, integer, or decimal string")
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("value must be finite and non-negative")
    if quantum is not None:
        try:
            decimal = decimal.quantize(quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ValueError("decimal value has unsupported precision") from exc
    return decimal


def _price_value(value: object) -> Decimal:
    return _decimal_value(value)


class DeepSeekPricingConfig(ContractModel):
    """A complete, explicit DeepSeek price table supplied by the Audit user."""

    model: NonEmptyText
    currency: Literal["USD"] = "USD"
    prompt_cache_hit_usd_per_million_tokens: Decimal
    prompt_cache_miss_usd_per_million_tokens: Decimal
    completion_usd_per_million_tokens: Decimal
    pricing_version: NonEmptyText
    effective_date: date
    source_note: NonEmptyText | None = None

    @field_validator(
        "prompt_cache_hit_usd_per_million_tokens",
        "prompt_cache_miss_usd_per_million_tokens",
        "completion_usd_per_million_tokens",
        mode="before",
    )
    @classmethod
    def validate_price(cls, value: object) -> Decimal:
        return _price_value(value)

    def pricing_fingerprint(self) -> str:
        """Return a stable identity without exposing source notes or secrets."""

        return canonical_sha256(
            {
                "model": self.model,
                "currency": self.currency,
                "prompt_cache_hit_usd_per_million_tokens": _canonical_decimal_text(
                    self.prompt_cache_hit_usd_per_million_tokens
                ),
                "prompt_cache_miss_usd_per_million_tokens": _canonical_decimal_text(
                    self.prompt_cache_miss_usd_per_million_tokens
                ),
                "completion_usd_per_million_tokens": _canonical_decimal_text(
                    self.completion_usd_per_million_tokens
                ),
                "pricing_version": self.pricing_version,
                "effective_date": self.effective_date.isoformat(),
            }
        )


class DeepSeekCostStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    NOT_EVALUATED = "not_evaluated"
    INVALID = "invalid"


_MONEY_FIELDS = (
    "cache_hit_input_cost_usd",
    "cache_miss_input_cost_usd",
    "completion_cost_usd",
    "classified_cost_usd",
    "total_cost_usd",
    "estimated_cost_without_cache_usd",
    "cache_savings_usd",
)


class DeepSeekCostSummary(ContractModel):
    """One Trial's cost facts, including explicit partial-evaluation semantics."""

    status: DeepSeekCostStatus
    pricing_fingerprint: Sha256Digest | None = None
    currency: Literal["USD"] | None = None
    prompt_tokens: NonNegativeInt | None = None
    prompt_cache_hit_tokens: NonNegativeInt | None = None
    prompt_cache_miss_tokens: NonNegativeInt | None = None
    evaluated_prompt_tokens: NonNegativeInt | None = None
    unclassified_prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    cache_hit_input_cost_usd: Decimal | None = None
    cache_miss_input_cost_usd: Decimal | None = None
    completion_cost_usd: Decimal | None = None
    classified_cost_usd: Decimal | None = None
    total_cost_usd: Decimal | None = None
    estimated_cost_without_cache_usd: Decimal | None = None
    cache_savings_usd: Decimal | None = None
    cache_savings_rate: Decimal | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @field_validator(*_MONEY_FIELDS, mode="before")
    @classmethod
    def validate_money(cls, value: object) -> Decimal | None:
        return None if value is None else _decimal_value(value, quantum=MONEY_QUANTUM)

    @field_validator("cache_savings_rate", mode="before")
    @classmethod
    def validate_rate(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        rate = _decimal_value(value, quantum=RATE_QUANTUM)
        if rate > 1:
            raise ValueError("cache_savings_rate must be between 0 and 1")
        return rate

    @model_validator(mode="after")
    def validate_summary(self) -> "DeepSeekCostSummary":
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("cache hit and miss tokens must be paired")
        if self.prompt_cache_hit_tokens is not None:
            if self.evaluated_prompt_tokens is None:
                raise ValueError("cache tokens require evaluated prompt tokens")
            if (
                self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
                != self.evaluated_prompt_tokens
            ):
                raise ValueError("cache tokens must sum to evaluated prompt tokens")
        if self.prompt_tokens is not None and self.evaluated_prompt_tokens is not None:
            if self.evaluated_prompt_tokens > self.prompt_tokens:
                raise ValueError("evaluated prompt tokens exceed prompt tokens")
            expected_unclassified = self.prompt_tokens - self.evaluated_prompt_tokens
            if self.unclassified_prompt_tokens is not None and (
                self.unclassified_prompt_tokens != expected_unclassified
            ):
                raise ValueError("unclassified prompt tokens are inconsistent")
        if self.status is DeepSeekCostStatus.AVAILABLE:
            if (
                self.pricing_fingerprint is None
                or self.currency is None
                or self.prompt_tokens is None
                or self.evaluated_prompt_tokens != self.prompt_tokens
                or self.unclassified_prompt_tokens != 0
                or self.prompt_cache_hit_tokens is None
                or self.completion_tokens is None
                or any(getattr(self, name) is None for name in _MONEY_FIELDS)
            ):
                raise ValueError("available cost requires complete token and money facts")
            expected_total = _quantized_sum(
                self.cache_hit_input_cost_usd,
                self.cache_miss_input_cost_usd,
                self.completion_cost_usd,
            )
            if self.total_cost_usd != expected_total or self.classified_cost_usd != expected_total:
                raise ValueError("total cost must equal its components")
            if self.cache_savings_usd is None or self.estimated_cost_without_cache_usd is None:
                raise ValueError("available cost requires no-cache estimate and savings")
            if self.cache_savings_usd != _quantize_money(
                self.estimated_cost_without_cache_usd - self.total_cost_usd
            ):
                raise ValueError("cache savings must equal no-cache estimate minus total")
        elif self.status is DeepSeekCostStatus.PARTIAL:
            if (
                self.pricing_fingerprint is None
                or self.currency is None
                or self.prompt_tokens is None
                or self.evaluated_prompt_tokens is None
                or self.unclassified_prompt_tokens is None
                or self.completion_tokens is None
                or self.prompt_cache_hit_tokens is None
                or self.prompt_cache_miss_tokens is None
                or self.classified_cost_usd is None
                or self.cache_hit_input_cost_usd is None
                or self.cache_miss_input_cost_usd is None
                or self.completion_cost_usd is None
                or self.total_cost_usd is not None
                or self.estimated_cost_without_cache_usd is not None
                or self.cache_savings_usd is not None
                or self.cache_savings_rate is not None
            ):
                raise ValueError("partial cost must expose classified cost only")
        else:
            if any(getattr(self, name) is not None for name in _MONEY_FIELDS) or self.cache_savings_rate is not None:
                raise ValueError("unevaluated or invalid cost cannot expose money")
        return self


class DeepSeekCostAggregate(ContractModel):
    """Case/Suite cost aggregation with explicit coverage and status."""

    status: DeepSeekCostStatus
    currency: Literal["USD"] | None = None
    pricing_fingerprint: Sha256Digest | None = None
    trial_count: NonNegativeInt
    token_bearing_trial_count: NonNegativeInt = 0
    available_trial_count: NonNegativeInt = 0
    partial_trial_count: NonNegativeInt = 0
    not_evaluated_trial_count: NonNegativeInt = 0
    invalid_trial_count: NonNegativeInt = 0
    total_cost_usd: Decimal | None = None
    classified_cost_usd: Decimal | None = None
    estimated_cost_without_cache_usd: Decimal | None = None
    cache_savings_usd: Decimal | None = None
    cache_savings_rate: Decimal | None = None
    mean_cost_per_evaluated_trial_usd: Decimal | None = None
    mean_cost_per_successful_trial_usd: Decimal | None = None
    effective_cost_per_success_usd: Decimal | None = None
    cost_coverage_rate: Decimal | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @field_validator(
        "total_cost_usd",
        "classified_cost_usd",
        "estimated_cost_without_cache_usd",
        "cache_savings_usd",
        "mean_cost_per_evaluated_trial_usd",
        "mean_cost_per_successful_trial_usd",
        "effective_cost_per_success_usd",
        mode="before",
    )
    @classmethod
    def validate_money(cls, value: object) -> Decimal | None:
        return None if value is None else _decimal_value(value, quantum=MONEY_QUANTUM)

    @field_validator("cache_savings_rate", "cost_coverage_rate", mode="before")
    @classmethod
    def validate_rate(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        rate = _decimal_value(value, quantum=RATE_QUANTUM)
        if rate > 1:
            raise ValueError("rate must be between 0 and 1")
        return rate

    @model_validator(mode="after")
    def validate_aggregate(self) -> "DeepSeekCostAggregate":
        counts = (
            self.available_trial_count,
            self.partial_trial_count,
            self.not_evaluated_trial_count,
            self.invalid_trial_count,
        )
        if sum(counts) != self.trial_count:
            raise ValueError("cost status counts must equal trial_count")
        if self.token_bearing_trial_count > self.trial_count:
            raise ValueError("token-bearing trials cannot exceed trial_count")
        if self.cost_coverage_rate is not None:
            if self.token_bearing_trial_count == 0:
                raise ValueError("coverage requires token-bearing trials")
            expected = Decimal(self.available_trial_count) / Decimal(
                self.token_bearing_trial_count
            )
            if self.cost_coverage_rate != _quantize_rate(expected):
                raise ValueError("cost coverage must equal available/token-bearing trials")
        monetary_fields = (
            "total_cost_usd",
            "classified_cost_usd",
            "estimated_cost_without_cache_usd",
            "cache_savings_usd",
            "cache_savings_rate",
            "mean_cost_per_evaluated_trial_usd",
            "mean_cost_per_successful_trial_usd",
            "effective_cost_per_success_usd",
        )
        if self.status is DeepSeekCostStatus.INVALID:
            if any(
                getattr(self, name) is not None
                for name in monetary_fields
            ):
                raise ValueError("invalid aggregate cannot expose monetary totals")
        if self.status is DeepSeekCostStatus.NOT_EVALUATED and any(
            getattr(self, name) is not None
            for name in monetary_fields
        ):
            raise ValueError("not-evaluated aggregate cannot expose totals")
        if self.status is DeepSeekCostStatus.PARTIAL:
            if any(
                getattr(self, name) is not None
                for name in (
                    "total_cost_usd",
                    "estimated_cost_without_cache_usd",
                    "cache_savings_usd",
                    "cache_savings_rate",
                )
            ):
                raise ValueError("partial aggregate cannot expose complete totals")
            if self.classified_cost_usd is None:
                raise ValueError("partial aggregate requires classified cost")
        if self.status is DeepSeekCostStatus.AVAILABLE:
            if any(
                getattr(self, name) is None
                for name in (
                    "total_cost_usd",
                    "classified_cost_usd",
                    "estimated_cost_without_cache_usd",
                    "cache_savings_usd",
                )
            ):
                raise ValueError("available aggregate requires complete totals")
            if self.total_cost_usd != self.classified_cost_usd:
                raise ValueError("available aggregate total must equal classified cost")
            if self.cache_savings_usd != _quantize_money(
                self.estimated_cost_without_cache_usd - self.total_cost_usd
            ):
                raise ValueError("aggregate savings must equal estimate minus total")
            if self.estimated_cost_without_cache_usd == 0:
                if self.cache_savings_rate is not None:
                    raise ValueError("zero estimate cannot have a savings rate")
            elif self.cache_savings_rate != _quantize_rate(
                self.cache_savings_usd / self.estimated_cost_without_cache_usd
            ):
                raise ValueError("aggregate savings rate must match savings")
        return self


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _quantized_sum(*values: Decimal | None) -> Decimal:
    return _quantize_money(sum((value or Decimal("0")) for value in values))


def _canonical_decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = (
    "DeepSeekCostAggregate",
    "DeepSeekCostStatus",
    "DeepSeekCostSummary",
    "DeepSeekPricingConfig",
    "MILLION",
    "MONEY_QUANTUM",
    "RATE_QUANTUM",
)
