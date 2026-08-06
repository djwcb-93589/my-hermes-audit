# P8: representative reports and CI operations

P8 adds a presentation and operating layer over existing strict Audit facts. It
does not add a Subject capability, evaluator, metric formula, score, model
request, or remote publication path.

## Three production chains

1. A single representative Benchmark produces an `AuditRunResult` JSON file.
   The JSON is the fact source; `report render` turns that already validated
   file into concise Markdown.
2. A repeated representative Benchmark produces a strict Baseline, a later
   strict current Result, and a strict Regression report. The Markdown
   regression summary presents that report's existing decisions and policy
   facts; it does not compare values itself.
3. CI is tiered. Push/PR CI is deterministic and has no model or network
   access. A manually started representative run can use supplied CI Secrets.
   A separate manual regression workflow compares a read-only checked-in
   Baseline. P8 intentionally does not add a schedule/Cron trigger.

## Strict Markdown render contract

`report-v1` is independent from `AuditRunResult` `1.7`, Worker protocol `v13`,
Baseline `baseline-v7`, and Regression `regression-v10`. Its only public
options model is `ReportRenderOptions`; unknown option fields are rejected.

The command accepts **only** one strict JSON model, never an arbitrary mapping
or permissively decoded JSON:

```bash
uv run myhermes-audit report render reports/representative-run.json \
  --output reports/representative-run.md

uv run myhermes-audit report render baselines/representative-v1.json \
  --input-type baseline --output reports/representative-baseline.md

uv run myhermes-audit report render reports/representative-regression.json \
  --input-type regression --output reports/representative-regression.md
```

Input type defaults to `auto`; the explicit values are `run`, `baseline`, and
`regression`. Output must end in `.md`, uses the existing same-directory atomic
writer, and is rejected when it already exists unless `--overwrite` is passed.
The output cannot overwrite its source input. Rendering returns exit code `0`
on success. It neither runs a Trial nor invokes Judge, Langfuse, or a model.

Markdown has three layers:

* **Executive Summary**: safe identity, core representative facts, counts,
  final execution or Regression gate, current failures, regressions, and safe
  warning codes.
* **Case Summary / Case Comparison**: stored Case aggregates or comparison
  facts, including trial counts, task success, runtime, cache, cost, Review
  action distributions, failure-category projections, and decision where the
  source contract carries it.
* **Diagnostics**: status/count/code facts, identity and comparability facts,
  policy snapshot, and safe Artifact relative paths and hashes. It does not
  turn raw event streams into a homepage.

The renderer formats stored values only. It does not calculate a rate, mean,
percentile, cost, cache ratio, comparison, gate, or total score. When a source
contract does not project a fact (for example Case-level failure categories in
an `AuditRunResult`), the report says `not evaluated` rather than inventing it.

## Metric and safety boundary

The executive metric section shows existing Task success, Tool correctness,
Memory evidence hit / Recall@K / MRR, Background Review decision accuracy,
turns, iterations, duration, Tokens, tool calls, DeepSeek cache facts and
coverage, DeepSeek cost and coverage, failure/timeout/environment/cancelled
facts, and optional Judge/Langfuse status. Cost is `not evaluated` when no
valid pricing fact exists; that state is never rendered as `$0`. There is no
weighted or composite score.

The render and CI artifacts may contain stable IDs, commit IDs, safe model
identities, numeric facts, enums, error/warning codes, hashes, synthetic fixture
names, and Artifact relative paths. They intentionally exclude API keys, Base
URLs, full configuration bodies, prompts, model output, reasoning, Memory text,
Review evidence, user identifiers, message destinations, host absolute paths,
SQLite files, and raw requests/responses.

Judge is an optional diagnostic: its enabled/disabled state, declared,
completed, error, skipped, and answer-quality facts are presented but it cannot
override deterministic gates. Langfuse is only a status/publication projection;
it cannot become a capability metric or publish a Markdown report. The existing
Langfuse mapper exposes only safe Regression type/status/count/metric/identity
facts when it is otherwise used.

## Formal operator flow

Run one representative Benchmark:

```bash
uv run myhermes-audit run examples/representative_benchmark_v1.yaml \
  --subject-repo ../my-hermes --subject-config ./local-config.yaml \
  --output reports/representative-current.json
```

Run a repeat, create a deliberate Baseline, compare it read-only, then render:

```bash
uv run myhermes-audit run examples/representative_benchmark_v1.yaml \
  --subject-repo ../my-hermes --subject-config ./local-config.yaml \
  --trials 5 --output reports/representative-repeat.json

uv run myhermes-audit baseline create reports/representative-repeat.json \
  --output baselines/representative-v1.json

uv run myhermes-audit baseline compare baselines/representative-v1.json \
  reports/representative-current.json \
  --policy configs/regression-policy.yaml \
  --output reports/representative-regression.json

uv run myhermes-audit report render reports/representative-regression.json \
  --input-type regression --output reports/representative-regression.md
```

These are operator examples, not a claim that every environment or model will
pass. A model credential, subject repository, and safe local Subject
configuration are required only for real `run` commands.

Current CLI exit codes are preserved: `run` returns `1` for Trial or integration
failure; `baseline compare` returns `0` only when its stored Regression gate
passes and `1` for either regression or not-comparable gate failure; a handled
`AuditError` returns `2`; an unexpected error returns `3`; `report render`
returns `0` after successful atomic output. CI must retain the JSON report and
its safe console summary before propagating a nonzero real-run or comparison
status.

## CI tiers and safe artifacts

`audit-deterministic.yml` runs on push and pull request. It has no model
credentials, no Langfuse, no Subject execution, and no Baseline mutation. It
performs strict imports/contract schema generation, Suite validation, CLI help,
static compilation, and tracked-file secret scanning. Its summary is structured
and its artifacts contain only generated schema/help/validation records.

`audit-representative.yml` is manual only. It requires a selected Subject
repository plus model credentials supplied solely through GitHub Secrets. A
missing required secret produces a structured skip and prints no value. Judge
and Langfuse remain disabled unless an operator makes a separately reviewed,
explicit change; P8 does not set fixed pricing.

`audit-regression.yml` is manual only. It executes an explicit repeat/current
Benchmark and compares it against a versioned read-only Baseline path. A
Regression or not-comparable result remains nonzero. It never filters trials,
updates a Baseline, or creates an automatic approval.

Allowed uploaded artifacts are strict Result/Baseline/Regression JSON,
Markdown, a safe Artifact manifest, safe console summary, Suite fingerprint,
and safe commit/workflow identity. Artifact names use the Suite ID, short
subject commit when available, and workflow/run identity. Workflows never
upload Sandboxes, SQLite, prompts, Memory stores, Review evidence, full config,
credentials, raw responses, or user IDs.

Use GitHub Secrets only for `MYHERMES_API_KEY`, `MYHERMES_MODEL`, optional
`MYHERMES_BASE_URL`, and an optional private-repository read token. Workflows
use `bash` with `set -euo pipefail`, never dump the environment, and emit only
redacted/status diagnostics. See [`../examples/ci/README.md`](../examples/ci/README.md)
for the safe placeholders.
