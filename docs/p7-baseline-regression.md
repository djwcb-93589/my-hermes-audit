# P7: repeat runs, Baseline, and regression comparison

P7 is an Audit-side reporting layer. It does not add a capability, change a
Case, or alter the MyHermes worker. It answers whether a result is stable over
repeated Trials and whether a later result differs from a recorded historical
Baseline.

## One run and repeated runs

The existing serial Suite runner remains the only execution path. `run`
accepts `--trials N`, where `N` is an integer in the inclusive range 1--100.
The value is validated before a runner is created and overrides only the loaded
Suite's defaults in memory. The P6.4 YAML remains `trials: 1`.

Every repeat has a fresh Sandbox and fresh MyHermes Session. Workspace,
`HERMES_HOME`, SQLite, Memory, Artifacts, and cleanup are Trial-local. Trial
identity retains Case ID, ordinal, Trial ID, run ID, subject and Audit
fingerprints, model/config/pricing identity where available, Result Schema, and
Worker Protocol. A failed Trial is retained in the result; it is never silently
filtered before aggregation.

The ordinary Suite digest includes the declared repeat count and therefore
identifies the exact run configuration. A companion semantic Suite digest
excludes only `defaults.trials`, so a baseline with one repeat and a current run
with five repeats can still be compared when every Case contract is unchanged.

## Metrics and denominators

P7 projects the existing Trial facts. It does not introduce a new capability,
weighted score, or Case-specific exception.

* Task success uses only Trials where `task_passed` is explicitly present.
  `None` is not an automatic failure.
* Tool correctness uses completed, required Tool Trajectory metrics only.
* Memory evidence, Recall@K, and MRR use the existing Retrieval metrics and
  their own sample counts; Cases without a Retrieval evaluator do not enter
  those denominators.
* Background Review records decision sample/accuracy and actual action
  distributions (`no_op`, `replace`, `remove`, and other public actions) per
  Case. No Case name receives special handling.
* Turns, iterations, duration, tokens, and tool calls use Trial runtime facts.
  Means, nearest-rank P50/P95, minimum, maximum, and sample standard deviation
  are retained where the sample is available. A sample smaller than two has no
  standard-deviation estimate and is represented as `null`.
* Missing tokens or runtime facts remain missing; they are not replaced with
  zero.

Cache aggregation is token-weighted: total valid hit tokens divided by total
evaluated prompt tokens. It is not an average of per-Trial hit rates. Cache
status, hit/miss totals, evaluated prompt tokens, model-call coverage, and
Trial coverage remain separate facts.

Cost facts are emitted only when the existing DeepSeek pricing contract is
valid and consistent. The projection includes available subtotal, total,
means per evaluated/successful Trial, effective cost per success, savings, and
coverage. Without pricing the cost state is `not_evaluated`; a pricing mismatch
does not invalidate non-cost metrics.

Failure categories are structured facts: task failure, Tool Trajectory failure,
Memory Retrieval failure, Background Review decision failure, timeout,
environment error, cancelled, cache invalid, and cost invalid. They are not
inferred by parsing free-form logs and are never collapsed into “model issue”.

## Baseline contract

`AuditBaseline` is `baseline-v5`. It can be created from any strictly valid
`AuditRunResult`, including a result with failed or timed-out Trials. It stores
the source run ID, Audit and Subject commits, Suite ID and both Suite digests,
Result Schema, Worker Protocol, model/config/pricing identities, total Trial
count and per-Case declared repeat counts, ordered Case IDs, suite and Case metric projections, cache/cost
aggregates, failure distributions, Review action distributions, sample counts,
and safe warnings.
The legacy `declared_trial_count` field is retained only as an exact alias of
`total_trial_count`; it is never interpreted as a per-Case repeat count.

Baseline creation is deterministic for the same result facts. The content
fingerprint excludes only the creation timestamp and the derived Baseline ID;
the ID is `baseline-` plus the first 16 hexadecimal characters of that digest.
The model is frozen after load. `baseline create` rejects an existing output
unless `--overwrite` is explicit and reports the old and new IDs when replacing
one. It never updates Git, a remote store, or the source result.

Baseline creation rejects conflicting or missing model, configuration, Worker
Protocol, Result Schema, or metric-contract identities before writing a file.
All five are core identities and must be `available` with exactly one value;
`missing` and `ambiguous` are retained in comparison diagnostics but cannot
become a Baseline.

No Baseline contains API keys, Base URLs, prompts, model responses, reasoning,
Memory text, Review evidence正文, user identity, or local absolute paths.

## Comparability

`AuditRegressionReport` is `regression-v8` and reports structured reasons when
comparison is not valid. Core correctness comparison requires the same Suite
ID, semantic Suite digest, ordered Case set, Result Schema identity, metric
contract, Worker Protocol, model identity, and configuration identity. Each
identity is explicitly `available`, `missing`, or `ambiguous`; ambiguous
identities are never comparable, even when both sides are ambiguous. Any core
missing identity is also not comparable; `missing == missing` is not treated as
equality.
Audit commit, Subject commit, run ID, Trial IDs, Sandbox IDs, and run time may
differ; Subject commit differences are the normal version-regression use case.

The declared repeat counts may differ. Rates are computed using each side's
actual denominator and both counts are displayed, so raw passed counts are not
treated as a conclusion. A result with an existing failure can still improve,
for example 80% to 88.9%; non-100% is not itself a regression.

Pricing identity is deliberately independent. For a Metric whose effective
policy requires pricing, either missing fingerprint (including both sides
missing) yields `pricing_fingerprint_missing`; two present but different
fingerprints yield `pricing_fingerprint_mismatch`. Equal present fingerprints
yield no pricing reason. `None == None` is never treated as pricing identity.
Only the affected money, cost, savings, or explicitly pricing-sensitive Metric
is marked `not_comparable`; task, Tool, Memory, Review, turn, time, token, and
cache observations remain comparable when their own facts are valid.
Each `MetricComparison` carries the generated `requires_pricing_match` policy
fact. The comparison engine and Report validator use that field; the validator
derives it through one shared policy resolver. The resolver applies the fixed
DeepSeek-cost convention in addition to an explicit `require_pricing_match`
flag; it does not use the metric prefix for mode, direction, or thresholds. An
explicit custom policy can require pricing identity for any metric. A pricing
mismatch remains local to metrics whose effective fact is true.

Metric roles are derived only from the effective Policy snapshot: an effective
`requires_pricing_match=false` policy makes a Metric `core`, while
`requires_pricing_match=true` makes it `local`. This includes both the fixed
DeepSeek cost family and explicitly pricing-sensitive custom Metrics. The
comparison engine and strict validator use the same pure role and count
helpers. Reports persist `comparable_core_metric_count` and
`comparable_local_metric_count`; comparable decisions are `improved`,
`unchanged`, `warning`, or `regressed`.

Metric evaluation is a one-way chain: raw baseline/current values and sample
counts first derive `evaluation_status`; independent `comparability_fact_codes`
and applicable pricing facts then derive `comparability_status` and the exact
finite `reason_codes`. Only after those facts are established are deltas and
Metric decisions calculated. `reason_codes` are verification output, never
input to fact derivation. `MetricComparison` and Case projections mark their
comparability and policy facts as report-only; the complete `AuditRegressionReport`
re-derives them from both sides' identities and rejects fabricated facts.
`not_evaluated` means a required metric/sample fact is absent, while
`not_comparable` means evaluated facts cannot be aligned; the latter never
masks the former.

Report reasons are finalized only after effective Metric policies,
evaluation/comparability facts, and Metric decisions have been re-derived.
`no_comparable_core_metrics` is added only when there is no more-specific core
identity/contract reason and `comparable_core_metric_count == 0` (therefore
every core Metric is `not_evaluated` or `not_comparable`). A pricing-only local
failure never adds that core reason when another core Metric is comparable.
Local Metrics may retain evaluated decisions even when the Report is
`not_comparable` because no core Metric is comparable; those decisions are
diagnostic and cannot make the overall gate pass. The generator and strict
reload validator share the same role, count, pricing-reason, and final-reason
helpers.

The complete report contains an immutable `RegressionPolicySnapshot` with the
policy schema version, default mode, sorted explicit metric entries, and a
fingerprint over those safe fields. The Report validator resolves every
Suite/Case metric through the same pure resolver used by comparison, then
checks mode, direction, thresholds, and effective pricing applicability before
deriving decisions, Case outcomes, counts, and status. The pricing
applicability fingerprint is bound to the policy fingerprint and each
Suite/Case metric identity. The snapshot contains no paths, prompts, model
text, credentials, or environment values.
Because the policy contract has no direction/threshold fields for an implicit
default entry, a non-disabled `default_mode` is rejected; enabled behavior must
be represented by an explicit, fully validated metric policy entry.

## RegressionPolicy

`RegressionPolicy` is `regression-policy-v1` and is loaded from an explicit YAML
file. Each metric has a mode (`disabled`, `warning`, or `failure`), direction,
and threshold. The checked-in [`../configs/regression-policy.yaml`](../configs/regression-policy.yaml)
is conservative:

* correctness, required evidence, Review accuracy, failure, timeout, environment
  error, and cancelled rates can fail the gate;
* Recall@K and MRR are warnings by default;
* iterations, duration, token counts, tool calls, and cache hit rate are
  warnings;
* cost and savings are warnings and require matching pricing identity.

Rate policies use an explicit maximum absolute drop. Efficiency policies use a
maximum relative increase. Failure policies use a maximum absolute increase.
Cost policies use a maximum relative increase only when pricing matches. The
comparison engine reads these policy fields; thresholds are not hard-coded in
the comparison decision path.

Metric decisions are `improved`, `unchanged`, `regressed`, `warning`,
`not_comparable`, or `not_evaluated`. Invalid comparison inputs are rejected
before a Report is created; `invalid_input` is not a self-asserted Report
status. The pure decision helper is the only
place that applies direction, policy mode, and thresholds; both comparison and
strict contract reload validation call it. Independent `evaluation_status`,
`comparability_status`, finite `reason_codes`, and the explicit
`requires_pricing_match` fact are derived from raw values, sample counts,
identity, policy, and contract facts before a saved decision is checked;
the saved decision is never used to infer those facts. Every decision carries
baseline/current values, absolute and (where defined) relative deltas, and both
sample counts. A disabled policy may still record `improved` or `unchanged`,
with the reason code `policy_disabled`; it never emits warning or regression.
There is no composite or weighted score.

## Case-level stability

The report lists baseline/current Trial counts and declared repeats for every
Case. Task success facts are separate: explicit-bool sample count, passed count,
rate, and rate delta. `task_passed=None` is excluded from every task-success
denominator; the generic `CaseAggregate.pass_rate` is not used for P7 task
success. The report also includes structured failure-category distributions,
mean runtime
facts, cache/cost states, and the metric decision. Background Review Cases also
show actual action distributions and decision accuracy. This makes one
intermittent failure (for example 1/5) distinguishable from a repeatable
failure (for example 5/5), without naming a special Case rule.

Case decisions use one shared precedence: hard regression, warning, improvement,
unchanged, all-not-comparable, then all-not-evaluated. Mixed unavailable
states receive a stable reason code. A report is `not_comparable` when no core
Metric is comparable, even if no identity mismatch exists; `passed` requires
at least one comparable core Metric. Local pricing-sensitive decisions remain
visible as diagnostics and cannot establish the Report gate.

## CLI, console, and JSON

Create a Baseline:

```bash
myhermes-audit baseline create \
  reports/representative-repeat.json \
  --output baselines/representative-v1.json
```

Compare it without starting an Agent:

```bash
myhermes-audit baseline compare \
  baselines/representative-v1.json \
  reports/representative-current.json \
  --policy configs/regression-policy.yaml \
  --output reports/representative-regression.json
```

Both inputs are loaded strictly. Output is atomic, rejects symbolic links, and
rejects an existing file unless `--overwrite` is provided. The compare command
does not call a model, Judge, Langfuse, network service, or pricing API. Its
console output includes baseline/current values, deltas, sample counts, Case
task-success facts, decisions, and comparability reasons rather than only
PASS/FAIL.

Exit status is zero for `passed` and `passed_with_warnings`; hard regression,
`not_comparable`, and invalid input are non-zero. Warning behavior is determined
by the policy mode.

## Langfuse boundary

The existing local trace mapper exposes a pure, content-free
`project_regression_metadata()` projection containing IDs, status, counts,
safe deltas, Case task-success facts, sample counts, reason codes, and the
content-free policy snapshot/fingerprint. P7 does not call it and does not
publish a Baseline or Regression report to Langfuse. Prompt,
output, Memory/Review正文, credentials, Base URLs, local paths, and identities
outside the safe contract are excluded.

## Versioning and scope

Baseline and Regression contracts are `baseline-v5` and `regression-v8`.
The v8 Report requires both comparable core/local count fields and an explicit
`schema_version`; v7 Reports or payloads missing these fields are rejected.
P7 adds the
semantic Suite comparison digest to `AuditFingerprint`, so the Audit Result
Schema is `1.6`; Worker Protocol remains v13. Existing Trial and evaluator
semantics are unchanged. P6.4 Cases and YAML are untouched.

P7 deliberately does not implement CI gates, parallel Trial scheduling,
automatic retries or Baseline updates, cloud Baseline storage, Dashboard/HTML,
Cron, Process-specific development, Delegate/multi-Agent orchestration, GLM,
or any new pricing/model adapter.
