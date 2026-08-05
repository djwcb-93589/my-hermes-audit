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

`AuditBaseline` is `baseline-v2`. It can be created from any strictly valid
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

Baseline creation rejects conflicting model, configuration, Worker Protocol,
Result Schema, or metric-contract identities before writing a file. A missing
model, configuration, or Worker Protocol identity is retained as `missing`;
these three are optional only when the source run did not expose them. Result
Schema and metric-contract identities are required and always explicit. Missing
is not rewritten as a conflict.

No Baseline contains API keys, Base URLs, prompts, model responses, reasoning,
Memory text, Review evidence正文, user identity, or local absolute paths.

## Comparability

`AuditRegressionReport` is `regression-v2` and reports structured reasons when
comparison is not valid. Core correctness comparison requires the same Suite
ID, semantic Suite digest, ordered Case set, Result Schema identity, metric
contract, Worker Protocol, model identity, and configuration identity. Each
identity is explicitly `available`, `missing`, or `ambiguous`; ambiguous
identities are never comparable, even when both sides are ambiguous. Optional
missing identities are comparable only when both sides explicitly report
`missing`.
Audit commit, Subject commit, run ID, Trial IDs, Sandbox IDs, and run time may
differ; Subject commit differences are the normal version-regression use case.

The declared repeat counts may differ. Rates are computed using each side's
actual denominator and both counts are displayed, so raw passed counts are not
treated as a conclusion. A result with an existing failure can still improve,
for example 80% to 88.9%; non-100% is not itself a regression.

Pricing identity is deliberately independent. If pricing fingerprints differ,
task, Tool, Memory, Review, turn, time, token, and cache observations remain
comparable. Money, cost, savings, and effective-cost metrics are marked
`not_comparable` with a structured reason.

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
`not_comparable`, or `not_evaluated`. Every decision carries baseline/current
values, absolute and (where defined) relative deltas, and both sample counts.
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
safe deltas, Case task-success facts, sample counts, and reason codes. P7 does not call
it and does not publish a Baseline or Regression report to Langfuse. Prompt,
output, Memory/Review正文, credentials, Base URLs, local paths, and identities
outside the safe contract are excluded.

## Versioning and scope

Baseline and Regression contracts have independent versions. P7 adds the
semantic Suite comparison digest to `AuditFingerprint`, so the Audit Result
Schema is `1.6`; Worker Protocol remains v13. Existing Trial and evaluator
semantics are unchanged. P6.4 Cases and YAML are untouched.

P7 deliberately does not implement CI gates, parallel Trial scheduling,
automatic retries or Baseline updates, cloud Baseline storage, Dashboard/HTML,
Cron, Process-specific development, Delegate/multi-Agent orchestration, GLM,
or any new pricing/model adapter.
