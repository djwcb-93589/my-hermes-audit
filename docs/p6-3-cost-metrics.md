# P6.3 DeepSeek cost metrics

P6.3 computes cost only in the Audit parent process from the canonical
`TrialRuntimeSummary` facts produced by P6.2B. Pricing is never sent to the
MyHermes Worker and is never inferred from a model name, URL, environment, or
network response.

## Explicit pricing

`TrialConfig.deepseek_pricing` is optional and accepts a strict
`DeepSeekPricingConfig`: model, `USD` currency, hit/miss prompt prices,
completion price, pricing version, effective date, and an optional local source
note. Prices are expressed in USD per million tokens, validated as finite
non-negative decimal values, and money is quantized to eight decimal places
using `Decimal`/ROUND_HALF_UP. The pricing fingerprint includes model,
currency, all three prices, pricing version, and effective date. The source
note is not projected to remote metadata.

No pricing means `not_evaluated`; it is not zero cost. No online price lookup,
GLM/Volcengine pricing, provider registry, budget gate, or cache control is
implemented.

The result schema is `1.3`; older reports without cost fields are legacy data
and are never backfilled or interpreted as zero.

## Trial and aggregate status

`DeepSeekCostSummary` has `available`, `partial`, `not_evaluated`, and `invalid`
states. `available` requires ordinary prompt/completion tokens and a complete
paired hit/miss cache observation. `partial` charges only the classified
prompt portion and completion tokens; `prompt_tokens - evaluated_prompt_tokens`
is recorded as unclassified, and full total/no-cache/savings amounts remain
`None`. Missing usage or cache facts are `not_evaluated`; contradictions and
negative/non-finite calculations are `invalid` without failing the Trial.

For `available` Trials, total cost is hit input cost plus miss input cost plus
completion cost. A no-cache estimate prices every prompt token as a miss.
Savings is the estimate minus actual total; the savings rate is omitted when
the denominator is zero. Failed or timed-out Trials with complete costs are
included in `effective_cost_per_success_usd` (complete Trial costs divided by
the number of successful Trials), while partial and unevaluated Trials are
excluded and reported through coverage.

Suite and Case projections reuse the same aggregation component. Coverage is
`available_trial_count / token_bearing_trial_count`; an empty denominator is
shown as not evaluated. A partial aggregate never presents classified cost as
a complete Suite total, and cost status never changes task, tool, Memory,
Review, failure, timeout, or pass gates.

## Public projections

Console output labels amounts as USD and distinguishes partial and not
evaluated states. The local Langfuse mapper may project status, currency,
pricing fingerprint, numeric amounts, savings, and coverage only. It never
projects the full pricing configuration, source note, prompt/output content,
credentials, base URL, raw responses, or user identity. The Worker protocol
remains v13 because cost is derived after Worker execution from the existing
runtime summary.

DeepSeek cache fields remain passive observations. This phase does not
calculate provider fees for other vendors, synchronize prices, or control or
pre-warm server-side caches.
