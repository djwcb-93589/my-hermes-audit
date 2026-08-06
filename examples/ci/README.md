# CI safe configuration placeholders

This directory contains only non-secret examples. It is not a runnable
production Subject configuration and must not be replaced with a copied local
configuration.

Representative CI receives credentials only through GitHub Secrets:

- `MYHERMES_API_KEY` — required model credential.
- `MYHERMES_MODEL` — required safe model identifier.
- `MYHERMES_BASE_URL` — optional endpoint, never printed or uploaded.
- `MYHERMES_READ_TOKEN` — optional private Subject repository read token.

During the one real Benchmark step only, the workflows map those names to the
public MyHermes runtime names: `MYHERMES_API_KEY` to `OPENAI_API_KEY`,
`MYHERMES_MODEL` to `MODEL`, and optional `MYHERMES_BASE_URL` to
`OPENAI_BASE_URL`. The example fragment therefore uses `${MODEL}` and
`${OPENAI_API_KEY}`. A reviewed Subject configuration retains its non-secret
default `base_url`; when the optional override is absent, MyHermes uses that
normal default rather than an empty placeholder.

The credential check and real Benchmark are the only steps that receive model
Secrets. Checkout, lockfile export, dependency installation, no-model
`import hermes` preflight, strict JSON validation, report rendering, manifest
creation, summaries, and Artifact upload do not receive them. Both checkouts
disable persisted Git credentials; the private Subject read token is not left
in Git configuration for Subject code.

Manual input paths and Trial counts are validated before shell use. The
Subject config must be a regular, non-symlink file beneath `my-hermes/`; the
Baseline must be a regular, non-symlink, Git-tracked Audit file that passes
strict `AuditBaseline` loading; and Trials are decimal integers from 1 through
100. Never commit a key, endpoint credentials, prompt, Memory state, Langfuse
secret, Judge secret, or full local configuration.

The workflows place safe outputs in the hidden `.p8-ci/` directory. Their
Artifact upload steps explicitly enable hidden files, use only a fixed safe
file allowlist, and treat a missing allowlisted file as a CI error. Strict JSON
is the only fact source; Markdown, the safe Manifest, and the console summary
are derived or auxiliary outputs. A failed or skipped run uploads only its
separate safe Manifest and console summary, never an entire `.p8-ci/` directory.
`regression-policy.example.yaml` is a safe policy template; it controls only
existing Regression decisions and does not contain pricing or model credentials.
