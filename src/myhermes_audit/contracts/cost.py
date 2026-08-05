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

from pydantic import ConfigDict, Field, field_validator, model_validator

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

        return _pricing_fingerprint(
            model=self.model,
            currency=self.currency,
            prompt_cache_hit_usd_per_million_tokens=(
                self.prompt_cache_hit_usd_per_million_tokens
            ),
            prompt_cache_miss_usd_per_million_tokens=(
                self.prompt_cache_miss_usd_per_million_tokens
            ),
            completion_usd_per_million_tokens=self.completion_usd_per_million_tokens,
            pricing_version=self.pricing_version,
            effective_date=self.effective_date,
        )

    @model_validator(mode="after")
    def validate_price_order(self) -> "DeepSeekPricingConfig":
        if (
            self.prompt_cache_hit_usd_per_million_tokens
            > self.prompt_cache_miss_usd_per_million_tokens
        ):
            raise ValueError("cache hit price cannot exceed cache miss price")
        return self

    def snapshot(self) -> "DeepSeekPricingSnapshot":
        return DeepSeekPricingSnapshot(
            model=self.model,
            currency=self.currency,
            prompt_cache_hit_usd_per_million_tokens=(
                self.prompt_cache_hit_usd_per_million_tokens
            ),
            prompt_cache_miss_usd_per_million_tokens=(
                self.prompt_cache_miss_usd_per_million_tokens
            ),
            completion_usd_per_million_tokens=self.completion_usd_per_million_tokens,
            pricing_version=self.pricing_version,
            effective_date=self.effective_date,
            pricing_fingerprint=self.pricing_fingerprint(),
        )


class DeepSeekPricingSnapshot(ContractModel):
    """Immutable, source-note-free pricing facts carried by a result."""

    model_config = ConfigDict(frozen=True)

    model: NonEmptyText
    currency: Literal["USD"]
    prompt_cache_hit_usd_per_million_tokens: Decimal
    prompt_cache_miss_usd_per_million_tokens: Decimal
    completion_usd_per_million_tokens: Decimal
    pricing_version: NonEmptyText
    effective_date: date
    pricing_fingerprint: Sha256Digest

    @field_validator(
        "prompt_cache_hit_usd_per_million_tokens",
        "prompt_cache_miss_usd_per_million_tokens",
        "completion_usd_per_million_tokens",
        mode="before",
    )
    @classmethod
    def validate_price(cls, value: object) -> Decimal:
        return _price_value(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "DeepSeekPricingSnapshot":
        if (
            self.prompt_cache_hit_usd_per_million_tokens
            > self.prompt_cache_miss_usd_per_million_tokens
        ):
            raise ValueError("cache hit price cannot exceed cache miss price")
        if self.pricing_fingerprint != _pricing_fingerprint(
            model=self.model,
            currency=self.currency,
            prompt_cache_hit_usd_per_million_tokens=(
                self.prompt_cache_hit_usd_per_million_tokens
            ),
            prompt_cache_miss_usd_per_million_tokens=(
                self.prompt_cache_miss_usd_per_million_tokens
            ),
            completion_usd_per_million_tokens=self.completion_usd_per_million_tokens,
            pricing_version=self.pricing_version,
            effective_date=self.effective_date,
        ):
            raise ValueError("pricing snapshot fingerprint does not match fields")
        return self


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
    subject_model: NonEmptyText | None = None
    pricing_snapshot: DeepSeekPricingSnapshot | None = None
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
        if self.pricing_snapshot is None:
            if (self.pricing_fingerprint is None) != (self.currency is None):
                raise ValueError("pricing identity fields must be paired")
        elif self.status is DeepSeekCostStatus.NOT_EVALUATED:
            raise ValueError("not-evaluated cost cannot carry a pricing snapshot")
        if self.pricing_snapshot is not None:
            if self.pricing_fingerprint != self.pricing_snapshot.pricing_fingerprint:
                raise ValueError("cost pricing fingerprint must match snapshot")
            if self.currency != self.pricing_snapshot.currency:
                raise ValueError("cost currency must match pricing snapshot")
        if self.status in (DeepSeekCostStatus.AVAILABLE, DeepSeekCostStatus.PARTIAL):
            if self.pricing_snapshot is None:
                raise ValueError("evaluated cost requires a pricing snapshot")
            if self.subject_model != self.pricing_snapshot.model:
                raise ValueError("evaluated cost model must match pricing snapshot")
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("cache hit and miss tokens must be paired")
        if self.prompt_cache_hit_tokens is not None:
            if self.prompt_tokens is None or self.evaluated_prompt_tokens is None:
                raise ValueError("cache tokens require prompt and evaluated totals")
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
            if self.cache_savings_usd < 0:
                raise ValueError("cache savings cannot be negative")
            if self.estimated_cost_without_cache_usd == 0:
                if self.cache_savings_rate is not None:
                    raise ValueError("zero estimate cannot have a savings rate")
            elif self.cache_savings_rate != _quantize_rate(
                self.cache_savings_usd / self.estimated_cost_without_cache_usd
            ):
                raise ValueError("cache savings rate must match savings")
            expected_hit = charge_tokens(
                self.prompt_cache_hit_tokens,
                self.pricing_snapshot.prompt_cache_hit_usd_per_million_tokens,
            )
            expected_miss = charge_tokens(
                self.prompt_cache_miss_tokens,
                self.pricing_snapshot.prompt_cache_miss_usd_per_million_tokens,
            )
            expected_completion = charge_tokens(
                self.completion_tokens,
                self.pricing_snapshot.completion_usd_per_million_tokens,
            )
            if (
                self.cache_hit_input_cost_usd != expected_hit
                or self.cache_miss_input_cost_usd != expected_miss
                or self.completion_cost_usd != expected_completion
            ):
                raise ValueError("cost components do not match tokens and pricing")
            expected_estimated = _quantized_sum(
                charge_tokens(
                    self.prompt_tokens,
                    self.pricing_snapshot.prompt_cache_miss_usd_per_million_tokens,
                ),
                expected_completion,
            )
            if self.estimated_cost_without_cache_usd != expected_estimated:
                raise ValueError("no-cache estimate does not match tokens and pricing")
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
            if self.evaluated_prompt_tokens >= self.prompt_tokens:
                raise ValueError("partial cost must leave unclassified prompt tokens")
            expected_hit = charge_tokens(
                self.prompt_cache_hit_tokens,
                self.pricing_snapshot.prompt_cache_hit_usd_per_million_tokens,
            )
            expected_miss = charge_tokens(
                self.prompt_cache_miss_tokens,
                self.pricing_snapshot.prompt_cache_miss_usd_per_million_tokens,
            )
            expected_completion = charge_tokens(
                self.completion_tokens,
                self.pricing_snapshot.completion_usd_per_million_tokens,
            )
            if (
                self.cache_hit_input_cost_usd != expected_hit
                or self.cache_miss_input_cost_usd != expected_miss
                or self.completion_cost_usd != expected_completion
                or self.classified_cost_usd
                != _quantized_sum(expected_hit, expected_miss, expected_completion)
            ):
                raise ValueError("partial cost components do not match tokens and pricing")
        else:
            if any(getattr(self, name) is not None for name in _MONEY_FIELDS) or self.cache_savings_rate is not None:
                raise ValueError("unevaluated or invalid cost cannot expose money")
        return self


class DeepSeekCostAggregate(ContractModel):
    """Case/Suite cost aggregation with explicit coverage and status."""

    status: DeepSeekCostStatus
    currency: Literal["USD"] | None = None
    pricing_fingerprint: Sha256Digest | None = None
    pricing_snapshot: DeepSeekPricingSnapshot | None = None
    trial_count: NonNegativeInt
    token_bearing_trial_count: NonNegativeInt = 0
    successful_trial_count: NonNegativeInt = 0
    cost_evaluated_success_count: NonNegativeInt = 0
    available_trial_count: NonNegativeInt = 0
    partial_trial_count: NonNegativeInt = 0
    not_evaluated_trial_count: NonNegativeInt = 0
    invalid_trial_count: NonNegativeInt = 0
    prompt_tokens: NonNegativeInt | None = None
    prompt_cache_hit_tokens: NonNegativeInt | None = None
    prompt_cache_miss_tokens: NonNegativeInt | None = None
    evaluated_prompt_tokens: NonNegativeInt | None = None
    unclassified_prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    cache_hit_input_cost_usd: Decimal | None = None
    cache_miss_input_cost_usd: Decimal | None = None
    completion_cost_usd: Decimal | None = None
    total_cost_usd: Decimal | None = None
    classified_cost_usd: Decimal | None = None
    estimated_cost_without_cache_usd: Decimal | None = None
    cache_savings_usd: Decimal | None = None
    cache_savings_rate: Decimal | None = None
    mean_cost_per_evaluated_trial_usd: Decimal | None = None
    mean_cost_per_successful_trial_usd: Decimal | None = None
    cost_evaluated_success_total_usd: Decimal | None = None
    effective_cost_per_success_usd: Decimal | None = None
    available_total_cost_usd: Decimal | None = None
    available_estimated_cost_without_cache_usd: Decimal | None = None
    available_cache_savings_usd: Decimal | None = None
    cost_coverage_rate: Decimal | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @field_validator(
        "total_cost_usd",
        "cache_hit_input_cost_usd",
        "cache_miss_input_cost_usd",
        "completion_cost_usd",
        "classified_cost_usd",
        "estimated_cost_without_cache_usd",
        "cache_savings_usd",
        "mean_cost_per_evaluated_trial_usd",
        "mean_cost_per_successful_trial_usd",
        "cost_evaluated_success_total_usd",
        "effective_cost_per_success_usd",
        "available_total_cost_usd",
        "available_estimated_cost_without_cache_usd",
        "available_cache_savings_usd",
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
        if self.successful_trial_count > self.trial_count:
            raise ValueError("successful trials cannot exceed trial_count")
        if self.cost_evaluated_success_count > self.successful_trial_count:
            raise ValueError("evaluated successes cannot exceed successful trials")
        if self.cost_evaluated_success_count > self.available_trial_count:
            raise ValueError("evaluated successes cannot exceed available trials")
        if self.pricing_snapshot is not None:
            if self.pricing_fingerprint != self.pricing_snapshot.pricing_fingerprint:
                raise ValueError("aggregate pricing fingerprint must match snapshot")
            if self.currency != self.pricing_snapshot.currency:
                raise ValueError("aggregate currency must match pricing snapshot")
        elif (self.pricing_fingerprint is None) != (self.currency is None):
            raise ValueError("aggregate pricing identity fields must be paired")
        elif self.status in (
            DeepSeekCostStatus.AVAILABLE,
            DeepSeekCostStatus.PARTIAL,
        ):
            raise ValueError("evaluated aggregate requires a pricing snapshot")
        # A configured-but-unobserved aggregate may retain only the safe
        # currency/fingerprint identity; it must not claim an evaluable
        # pricing snapshot.
        if (
            self.status is DeepSeekCostStatus.NOT_EVALUATED
            and self.pricing_snapshot is not None
        ):
            raise ValueError("not-evaluated aggregate cannot carry a pricing snapshot")
        if (self.prompt_cache_hit_tokens is None) != (
            self.prompt_cache_miss_tokens is None
        ):
            raise ValueError("aggregate cache tokens must be paired")
        if self.prompt_cache_hit_tokens is not None:
            if self.prompt_tokens is None or self.evaluated_prompt_tokens is None:
                raise ValueError(
                    "aggregate cache tokens require prompt and evaluated totals"
                )
            if (
                self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
                != self.evaluated_prompt_tokens
            ):
                raise ValueError("aggregate cache tokens must sum to evaluated prompt tokens")
        if self.prompt_tokens is not None and self.evaluated_prompt_tokens is not None:
            if self.evaluated_prompt_tokens > self.prompt_tokens:
                raise ValueError("aggregate evaluated prompt exceeds prompt tokens")
            if self.unclassified_prompt_tokens != (
                self.prompt_tokens - self.evaluated_prompt_tokens
            ):
                raise ValueError("aggregate unclassified prompt tokens are inconsistent")
        if self.available_trial_count > self.token_bearing_trial_count:
            raise ValueError("available trials cannot exceed token-bearing trials")
        if self.token_bearing_trial_count == 0:
            if self.cost_coverage_rate is not None:
                raise ValueError("zero token-bearing trials require no coverage rate")
        else:
            if self.cost_coverage_rate is None:
                raise ValueError("token-bearing trials require a coverage rate")
            expected = Decimal(self.available_trial_count) / Decimal(
                self.token_bearing_trial_count
            )
            if self.cost_coverage_rate != _quantize_rate(expected):
                raise ValueError("cost coverage must equal available/token-bearing trials")
        monetary_fields = (
            "total_cost_usd",
            "cache_hit_input_cost_usd",
            "cache_miss_input_cost_usd",
            "completion_cost_usd",
            "classified_cost_usd",
            "estimated_cost_without_cache_usd",
            "cache_savings_usd",
            "cache_savings_rate",
            "mean_cost_per_evaluated_trial_usd",
            "mean_cost_per_successful_trial_usd",
            "cost_evaluated_success_total_usd",
            "effective_cost_per_success_usd",
            "available_total_cost_usd",
            "available_estimated_cost_without_cache_usd",
            "available_cache_savings_usd",
        )
        if self.status is DeepSeekCostStatus.INVALID:
            if any(getattr(self, name) is not None for name in monetary_fields):
                raise ValueError("invalid aggregate cannot expose monetary totals")
            if self.invalid_trial_count == 0 and (
                "inconsistent_pricing_identity" not in self.warnings
            ):
                raise ValueError("invalid aggregate requires invalid Trials")
        elif self.invalid_trial_count != 0:
            raise ValueError("non-invalid aggregate cannot contain invalid Trials")
        if self.status is DeepSeekCostStatus.NOT_EVALUATED:
            if any(getattr(self, name) is not None for name in monetary_fields):
                raise ValueError("not-evaluated aggregate cannot expose totals")
            if (
                self.available_trial_count
                or self.partial_trial_count
                or self.invalid_trial_count
            ):
                raise ValueError("not-evaluated aggregate cannot contain evaluated Trials")
        if self.status in (
            DeepSeekCostStatus.NOT_EVALUATED,
            DeepSeekCostStatus.INVALID,
        ) and self.cost_evaluated_success_count:
            raise ValueError(
                "unevaluated or invalid aggregate cannot contain evaluated successes"
            )
        if self.status is DeepSeekCostStatus.PARTIAL:
            if self.invalid_trial_count != 0:
                raise ValueError("partial aggregate cannot contain invalid Trials")
            if self.available_trial_count + self.partial_trial_count == 0:
                raise ValueError("partial aggregate requires evaluated Trials")
            if self.partial_trial_count == 0 and self.not_evaluated_trial_count == 0:
                raise ValueError("partial aggregate requires incomplete coverage")
            if any(
                getattr(self, name) is not None
                for name in (
                    "total_cost_usd",
                    "estimated_cost_without_cache_usd",
                    "cache_savings_usd",
                    "cache_savings_rate",
                    "effective_cost_per_success_usd",
                )
            ):
                raise ValueError("partial aggregate cannot expose complete totals")
            if self.pricing_snapshot is None or any(
                getattr(self, name) is None
                for name in (
                    "prompt_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                    "evaluated_prompt_tokens",
                    "unclassified_prompt_tokens",
                    "completion_tokens",
                    "classified_cost_usd",
                    "cache_hit_input_cost_usd",
                    "cache_miss_input_cost_usd",
                    "completion_cost_usd",
                )
            ):
                raise ValueError("partial aggregate requires classified cost facts")
            expected_classified = _quantized_sum(
                self.cache_hit_input_cost_usd,
                self.cache_miss_input_cost_usd,
                self.completion_cost_usd,
            )
            if self.classified_cost_usd != expected_classified:
                raise ValueError("partial classified cost must equal components")
            if self.available_trial_count == 0:
                if any(
                    getattr(self, name) is not None
                    for name in (
                        "available_total_cost_usd",
                        "available_estimated_cost_without_cache_usd",
                        "available_cache_savings_usd",
                        "mean_cost_per_evaluated_trial_usd",
                    )
                ):
                    raise ValueError("partial aggregate has no available subtotal")
            else:
                if any(
                    getattr(self, name) is None
                    for name in (
                        "available_total_cost_usd",
                        "available_estimated_cost_without_cache_usd",
                        "available_cache_savings_usd",
                        "mean_cost_per_evaluated_trial_usd",
                    )
                ):
                    raise ValueError("partial aggregate requires available subtotals")
                if self.available_cache_savings_usd != _quantize_money(
                    self.available_estimated_cost_without_cache_usd
                    - self.available_total_cost_usd
                ):
                    raise ValueError("available savings must equal estimate minus total")
                if self.available_cache_savings_usd < 0:
                    raise ValueError("available savings cannot be negative")
                if self.mean_cost_per_evaluated_trial_usd != _quantize_money(
                    self.available_total_cost_usd
                    / Decimal(self.available_trial_count)
                ):
                    raise ValueError("evaluated cost mean does not match subtotal")
                if self.classified_cost_usd < self.available_total_cost_usd:
                    raise ValueError("classified cost cannot be below available subtotal")
            if self.cost_evaluated_success_count == 0 and (
                self.mean_cost_per_successful_trial_usd is not None
                or self.cost_evaluated_success_total_usd is not None
            ):
                raise ValueError("successful cost fields require evaluated successes")
        if self.status is DeepSeekCostStatus.AVAILABLE:
            if (
                self.available_trial_count == 0
                or self.partial_trial_count != 0
                or self.not_evaluated_trial_count != 0
                or self.invalid_trial_count != 0
                or self.token_bearing_trial_count != self.available_trial_count
                or self.cost_coverage_rate != Decimal("1.00000000")
            ):
                raise ValueError("available aggregate requires complete Trial coverage")
            if self.cost_evaluated_success_count != self.successful_trial_count:
                raise ValueError("available successes must all be cost-evaluated")
            required = (
                "prompt_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "evaluated_prompt_tokens",
                "unclassified_prompt_tokens",
                "completion_tokens",
                "cache_hit_input_cost_usd",
                "cache_miss_input_cost_usd",
                "completion_cost_usd",
                "total_cost_usd",
                "classified_cost_usd",
                "estimated_cost_without_cache_usd",
                "cache_savings_usd",
                "available_total_cost_usd",
                "available_estimated_cost_without_cache_usd",
                "available_cache_savings_usd",
            )
            if any(getattr(self, name) is None for name in required):
                raise ValueError("available aggregate requires complete cost facts")
            if self.evaluated_prompt_tokens != self.prompt_tokens:
                raise ValueError("available aggregate must classify all prompt tokens")
            if self.unclassified_prompt_tokens != 0:
                raise ValueError("available aggregate cannot contain unclassified tokens")
            expected_classified = _quantized_sum(
                self.cache_hit_input_cost_usd,
                self.cache_miss_input_cost_usd,
                self.completion_cost_usd,
            )
            if self.classified_cost_usd != expected_classified:
                raise ValueError("available classified cost must equal components")
            if self.total_cost_usd != self.available_total_cost_usd:
                raise ValueError("available total must equal available subtotal")
            if self.classified_cost_usd != self.available_total_cost_usd:
                raise ValueError("available classified cost must equal subtotal")
            if self.estimated_cost_without_cache_usd != (
                self.available_estimated_cost_without_cache_usd
            ):
                raise ValueError("available estimate must equal available subtotal")
            if self.cache_savings_usd != self.available_cache_savings_usd:
                raise ValueError("available savings must equal available subtotal")
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
            if self.mean_cost_per_evaluated_trial_usd != _quantize_money(
                self.available_total_cost_usd / Decimal(self.available_trial_count)
            ):
                raise ValueError("evaluated cost mean does not match subtotal")
            if self.successful_trial_count == 0:
                if self.effective_cost_per_success_usd is not None:
                    raise ValueError("effective cost requires successful Trials")
            elif self.effective_cost_per_success_usd != _quantize_money(
                self.total_cost_usd / Decimal(self.successful_trial_count)
            ):
                raise ValueError("effective cost does not match total and successes")
        if self.cost_evaluated_success_count == 0:
            if (
                self.mean_cost_per_successful_trial_usd is not None
                or self.cost_evaluated_success_total_usd is not None
            ):
                raise ValueError("successful cost fields require evaluated successes")
        elif (
            self.cost_evaluated_success_total_usd is None
            or self.mean_cost_per_successful_trial_usd is None
            or self.mean_cost_per_successful_trial_usd
            != _quantize_money(
                self.cost_evaluated_success_total_usd
                / Decimal(self.cost_evaluated_success_count)
            )
        ):
            raise ValueError("successful cost mean does not match its facts")
        return self


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _quantized_sum(*values: Decimal | None) -> Decimal:
    return _quantize_money(sum((value or Decimal("0")) for value in values))


def quantize_money(value: Decimal) -> Decimal:
    return _quantize_money(value)


def quantize_rate(value: Decimal) -> Decimal:
    return _quantize_rate(value)


def charge_tokens(tokens: int, price: Decimal) -> Decimal:
    return quantize_money(Decimal(tokens) * price / MILLION)


def _pricing_fingerprint(
    *,
    model: str,
    currency: str,
    prompt_cache_hit_usd_per_million_tokens: Decimal,
    prompt_cache_miss_usd_per_million_tokens: Decimal,
    completion_usd_per_million_tokens: Decimal,
    pricing_version: str,
    effective_date: date,
) -> str:
    return canonical_sha256(
        {
            "model": model,
            "currency": currency,
            "prompt_cache_hit_usd_per_million_tokens": _canonical_decimal_text(
                prompt_cache_hit_usd_per_million_tokens
            ),
            "prompt_cache_miss_usd_per_million_tokens": _canonical_decimal_text(
                prompt_cache_miss_usd_per_million_tokens
            ),
            "completion_usd_per_million_tokens": _canonical_decimal_text(
                completion_usd_per_million_tokens
            ),
            "pricing_version": pricing_version,
            "effective_date": effective_date.isoformat(),
        }
    )


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
    "DeepSeekPricingSnapshot",
    "MILLION",
    "MONEY_QUANTUM",
    "RATE_QUANTUM",
    "charge_tokens",
    "quantize_money",
    "quantize_rate",
)
