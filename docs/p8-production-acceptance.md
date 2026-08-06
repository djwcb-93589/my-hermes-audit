# P8 production acceptance checklist

This document is an acceptance contract and does not execute an Audit,
Benchmark, Baseline operation, model, Judge, Langfuse call, or CI job.

## Contract and compatibility

- [ ] Result JSON remains strict `1.7`; Worker protocol remains `v13`.
- [ ] Baseline remains `baseline-v7`; Regression remains `regression-v10`.
- [ ] Markdown uses the independent strict `report-v1` `ReportRenderOptions`
  contract and accepts only validated Result, Baseline, or Regression JSON.
- [ ] Unknown fields and wrong versions are rejected by the corresponding strict
  source contract before rendering.
- [ ] Render output is atomic, `.md`, and refuses implicit overwrite.
- [ ] Rendering does not recalculate facts, invoke an evaluator, or alter a
  stored gate/decision.

## Representative report facts

- [ ] Executive summary projects task, Tool, Memory evidence / Recall@K / MRR,
  Background Review, turns, iterations, duration, Tokens, tools, cache, cost,
  failure, timeout, environment, cancelled, Judge, Langfuse, and final status
  using explicit sample/coverage semantics.
- [ ] Cache shows status, hit/miss tokens, hit rate, model-call coverage, and
  Trial coverage only when stored facts provide them.
- [ ] Cost remains `not evaluated` without valid pricing; it is not rendered as
  a zero-dollar value. Cost comparability remains local to pricing-sensitive
  metrics.
- [ ] Case reports distinguish available stored aggregates from `not evaluated`
  projections; Regression reports distinguish currently failing Cases from
  regressed Cases.
- [ ] Regression Markdown includes identities, Suite semantic and run-config
  fingerprints, policy fingerprint/snapshot, old/current/delta/sample/policy/
  decision/reason facts, and Case-level comparison facts.
- [ ] No composite score or weighted total is introduced.

## Security and remote boundaries

- [ ] Markdown/console/CI artifacts omit prompts, output, reasoning, credentials,
  Base URLs, configuration bodies, Memory text, Review evidence, user identity,
  message destinations, host absolute paths, SQLite, and raw requests/responses.
- [ ] Safe IDs, commits, hashes, relative Artifact paths, enums, numerical facts,
  and structured codes remain available for diagnosis.
- [ ] Optional Judge status never overrides deterministic gates.
- [ ] Langfuse remains a content-safe optional projection and never receives a
  Markdown report, baseline body, config body, prompt, output, Review evidence,
  secret, or user identity.

## Operations and CI

- [ ] A one-run Result can be atomically rendered to Markdown.
- [ ] Repeated Results can be deliberately turned into a read-only Baseline and
  compared to a current Result through the existing strict policy path.
- [ ] Deterministic push/PR CI has no model/network dependency and cannot mutate
  a Baseline.
- [ ] Representative and Regression CI are manual; no P8 Cron/schedule is
  introduced.
- [ ] Missing CI credentials produce redacted structured skip/failure output.
- [ ] CI Secrets are not echoed or saved, and only approved safe artifacts are
  uploaded.
- [ ] Baseline creation or update is an explicit reviewed PR action; comparison
  is read-only.

## Platform and cleanup checks for a later directed acceptance run

- [ ] Validate strict JSON, CLI help/schema, report render, and path refusal on
  Windows and Git Bash without a network connection.
- [ ] Exercise missing credentials and disabled Judge/Langfuse states without
  disclosing values.
- [ ] Confirm pricing-missing and pricing-mismatch cases retain non-cost facts.
- [ ] Confirm normal cleanup and preserved-on-failure behavior remain as defined
  by the existing Sandbox contract; no new P8 cleanup mechanism exists.

P8 intentionally excludes Cron, multi-agent/Delegate development, DOCX,
Dashboard, simulated user, Background task specialties, new model providers,
new evaluator algorithms, and modifications to `my-hermes`.
