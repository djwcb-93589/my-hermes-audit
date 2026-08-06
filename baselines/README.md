# Baseline management

`baselines/` stores reviewed, versioned `AuditBaseline` JSON only. A Baseline
is created from one strictly valid, non-empty `AuditRunResult`; failed or timed
out Trials are retained as historical facts and are never filtered out.

Create a candidate explicitly:

```bash
uv run myhermes-audit baseline create reports/representative-repeat.json \
  --output baselines/representative-v1.json
```

The command refuses to overwrite an existing output unless `--overwrite` is
explicit. A Baseline update is a deliberate reviewed PR that includes the
source Result, subject/Audit commit rationale, Suite semantic fingerprint,
model and run-configuration identity, repeat count, and policy implications.
CI comparison is read-only and must never update, replace, filter, or silently
approve a Baseline.

Maintain separate Baselines when Suite semantics, model identity, or common run
configuration differs. A Subject commit difference is expected for regression
work and does not by itself require a new Baseline. Pricing identity affects
only pricing-sensitive cost comparability; it does not make correctness,
Tool, Memory, Review, runtime, Token, or cache facts disappear.

Do not store API keys, Base URLs, prompts, model responses, reasoning, Memory
content, Review evidence, user identifiers, absolute paths, raw configuration,
or SQLite/Sandbox artifacts here. This repository intentionally commits no
formal production Baseline in P8. Any local JSON already present is not an
implicit approved baseline.
