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
static compilation, tracked-file secret scanning, and a small in-memory
contract smoke. The smoke constructs existing strict Result, Baseline, and
Regression objects without a Trial; validates their JSON reload; renders each
with the production renderer; verifies Markdown overwrite refusal; and rejects
a wrong Result schema. The upload allowlist contains only its strict synthetic
facts, corresponding Markdown, safe Manifest, and safe console summary; local
schema/help/validation records are not uploaded.

`audit-representative.yml` is manual only. It runs `uv sync --locked` for Audit,
exports the reviewed Subject's locked runtime requirements, installs those
requirements and the editable Subject into the exact Audit `.venv` interpreter
that starts the Worker, and then performs a no-model `import hermes` preflight.
It never creates or relies on `my-hermes/.venv`. An export, install, or import
failure is a redacted structured environment failure; it never starts the
Benchmark or publishes a partial Result. A missing required credential produces
a structured skip and prints no value. Judge and Langfuse remain disabled unless
an operator makes a separately reviewed, explicit change; P8 does not set fixed
pricing.

`audit-regression.yml` uses the same shared Worker environment preparation,
then executes an explicit repeat/current Benchmark and compares it against a
versioned read-only Baseline path. A run exit of `1` with a strict current
Result still runs Compare: the strict Regression Report gate, not the original
run exit, determines the final status. The safe summary records the original
current-run exit code and failed-Case count separately from Regression status.
Run exits `2`/`3`, or a missing/invalid strict current Result, never run Compare
and end nonzero with a redacted structured failure instead of a pseudo
Regression. A Regression or not-comparable report remains nonzero. The workflow
never filters trials, updates a Baseline, or creates an automatic approval.

Representative Artifacts contain a strict `representative-result.json`, its
rendered Markdown, a safe Artifact manifest, and a safe console summary.
Regression Artifacts contain a strict `current-result.json`, strict
`representative-regression.json`, its rendered Markdown, a safe manifest, and a
safe console summary. JSON remains the only fact source; Markdown is only a
rendering of strict JSON. Invalid or partial JSON is not uploaded. Workflows
never upload raw logs, Sandboxes, SQLite, Subject configuration bodies, prompts,
model output, Memory stores, Review evidence, credentials, raw responses, or
user IDs.

All safe CI artifacts live under the hidden `.p8-ci/` directory. Every
`actions/upload-artifact` step therefore sets `include-hidden-files: true` and
`if-no-files-found: error`; an empty Artifact is a CI configuration failure, not
a warning. Normal uploads use fixed allowlists only:

- Deterministic: `contract-smoke-result.json`, `contract-smoke-baseline.json`,
  `contract-smoke-regression.json`, their three corresponding Markdown files,
  `safe-artifact-manifest.json`, and `console-summary.txt`.
- Representative: `representative-result.json`,
  `representative-report.md`, `safe-artifact-manifest.json`, and
  `console-summary.txt`.
- Regression: `current-result.json`, `representative-regression.json`,
  `representative-regression.md`, `safe-artifact-manifest.json`, and
  `console-summary.txt`.

Before a normal upload, the strict JSON facts are revalidated, Markdown must
already exist, and the safe Manifest records the exact upload allowlist. A
failed or skipped workflow uses a separate status Artifact containing only the
safe Manifest and console summary; it never broadens a normal allowlist to
include temporary files. Artifact upload occurs before the final task or
Regression gate, so a task failure can still publish its valid, safe evidence,
while an Artifact upload failure fails the Job.

Use GitHub Secrets only for `MYHERMES_API_KEY`, `MYHERMES_MODEL`, optional
`MYHERMES_BASE_URL`, and an optional private-repository read token. The real
Benchmark step maps them to MyHermes' public runtime names
`OPENAI_API_KEY`, `MODEL`, and optional `OPENAI_BASE_URL`; no provider is
inferred and the reviewed Subject keeps its normal non-secret endpoint default
when the optional override is absent. Model Secrets are scoped only to the
credential check and real Benchmark step, never to checkout, dependency
preparation, preflight, rendering, manifest creation, or Artifact upload.
Both checkouts use `persist-credentials: false`, including the private Subject
checkout. Dispatch paths and Trial counts are first placed in step-level
environment variables, then strictly validated: Trials are decimal `1..100`,
Subject configs are regular non-symlink files beneath `my-hermes/`, and
Baselines are regular non-symlink Git-tracked Audit files that pass strict
`AuditBaseline` loading. Third-party Actions are pinned to reviewed full commit
SHAs. Workflows use `bash` with `set -euo pipefail`, never dump the environment,
and emit only redacted/status diagnostics. See
[`../examples/ci/README.md`](../examples/ci/README.md) for the safe placeholders.
