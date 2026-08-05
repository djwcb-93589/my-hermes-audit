# P6.4 Representative Agent Benchmark

`examples/representative_benchmark_v1.yaml` is the small, repeatable P6.4
entry point. It is a single synthetic Suite with one Trial by default and
reuses the existing Toolchain, Memory Retrieval, and Background Review
contracts. It does not introduce Case includes, inheritance, a new evaluator,
a weighted cross-metric score, or a second reporting pipeline.

## The nine representative Cases

The benchmark deliberately keeps only nine Cases:

| Family | Cases | Source Suite | Representative coverage |
| --- | --- | --- | --- |
| Toolchain (2) | `toolchain-text-export`, `toolchain-json-export` | `e2e-toolchain-v1` | file reads/writes, text and JSON Artifacts, tool arguments/order, final output versus produced files |
| Memory Retrieval (4) | `memory-explicit-fact`, `memory-temporal-order`, `memory-irrelevant-distractors`, `memory-cross-session` | `memory-retrieval-v1` | explicit evidence, temporal ordering, distractor resistance, cross-session persistence |
| Background Review (3) | `memory-review-insufficient-evidence`, `skill-review-verified-update`, `memory-review-conflicting-evidence` | `background-review-v1` | evidence-insufficient no-op, verified Skill replacement, conflicting-evidence refusal |

Each copied Case keeps its original `case_id`, input, fixtures, expected
facts, Scenarios, required Evaluators, tool-trajectory gates, Memory evidence,
and Background Review evidence. Only Suite-level defaults and description,
plus the following provenance metadata, are new:

```yaml
metadata:
  benchmark_family: toolchain | memory_retrieval | background_review
  source_suite: <original suite id>
  source_case_id: <original case id>
```

The metadata is descriptive only. It is not used to weaken an Evaluator or to
change the source Case semantics.

## Metric coverage and denominators

The benchmark reuses the existing `AuditSummary`, `CaseAggregate`, Trial, and
Console/JSON projections. There is no composite score or cross-metric weight.

| Metric family | Source and denominator |
| --- | --- |
| Task success, iteration, conversation turns, duration, prompt/completion/total Tokens, tool calls, failure and timeout rates | all nine Trial results, with each metric's existing non-null sample rules |
| Tool correctness | only completed required `tool_trajectory` runtime metrics; the denominator is not automatically nine |
| Required Memory evidence hit rate, Recall@K, MRR | only the four selected Memory Cases and only their completed Retrieval/Evidence metrics; Cases without a Retrieval Evaluator are excluded |
| Background Review decision accuracy | only completed `decision_correctness` metrics from the three selected Review Cases |
| DeepSeek cache hit/miss, hit rate, cache coverage | existing Trial runtime observations and their current coverage rules |
| DeepSeek cost, cache savings, evaluated-success cost | existing P6.3 Trial amount aggregation and cost coverage rules |

Missing runtime observations remain missing or `not_evaluated`; they are never
converted to zero. Failures and timeouts are recorded from real Trial facts,
not manufactured Cases added to inflate a denominator.

## DeepSeek pricing and default state

The default Suite intentionally omits `defaults.deepseek_pricing`. Cache
observations are still collected from the public MyHermes Observation contract,
but the resulting cost status is legitimately `not_evaluated`. A local run may
copy this Suite and add an explicit `defaults.deepseek_pricing` block containing
the model, USD prices, pricing version, effective date, and local source note.
The configured model must exactly match the observed `subject_model`; Audit does
not infer prices from a model name, URL, environment variable, or provider.
Pricing facts enter the result and fingerprint through the existing P6.3
contracts and are never sent to the Worker.

## Judge, Langfuse, and excluded scenarios

The representative benchmark does not declare a required LLM Judge. Existing
deterministic, tool-trajectory, Retrieval, Scenario, and Background Review
Evaluators remain the capability gates. Judge is an optional reporting aid and
Langfuse is an optional publication sink; neither is a benchmark pass
condition, and no benchmark-specific remote adapter is added.

P6.1 Process/Background scenarios, Cron, Delegate or multi-Agent work, Compression
ablation, DOCX, Dashboard, simulated-user, capability-negative, stale Review,
intentional failure, and intentional timeout Cases remain in their dedicated
Suites and are not copied here. The benchmark therefore does not change the
official Toolchain, Memory Retrieval, or Background Review Suites.

## Running and versioning

The normal local command is:

```bash
uv run myhermes-audit run \
  ./examples/representative_benchmark_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output ./reports/representative-benchmark-v1.json
```

The command is a documentation example for a local operator; P6.4 itself does
not execute it. The default is exactly one Trial with a 180-second timeout and
no preserved Sandbox. Repeated runs, variance intervals, Baseline IDs,
regression thresholds, and CI belong to P7. The Suite fingerprint includes all
nine Cases and the Suite configuration; existing Result Schema, Worker Protocol
v13, evaluator formulas, cost contracts, and cache contracts are unchanged.
