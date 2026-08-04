# P6.1 E2E scenarios

P6.1 adds two strict, versioned scenario kinds: `toolchain` and
`process_background`. A case declares a typed plan under `scenarios`; the
complete plan is part of the Suite fingerprint. Unknown fields and unknown
scenario kinds are rejected by the normal Suite loader.

The data flow is:

```text
Suite plan -> capability preflight -> isolated Worker -> public
MyHermes conversation -> safe observations -> local Artifacts -> Validator
-> TrialResult -> optional local Langfuse projection
```

Toolchain plans name required/forbidden tools, input/output Artifacts and trace
requirements. The Worker uses the existing conversation Worker and public tool
observations; Audit never reads a fixture to replace Agent work or writes an
Artifact on the Agent's behalf.

Process plans use typed `start`, `read_incremental`, `send_input`, `wait`,
`interrupt`, `kill`, `assert_status`, and `cleanup_session` steps. The current
Subject exposes process management through the `terminal` Toolset (`process` is
its companion declaration), so no synthetic `process` Toolset is added.

Process output is represented by bounded local log Artifacts. Structured facts
retain only checkpoints, hashes, lengths, offsets, marker matches, truncation
and safe identities. `read_incremental` offsets are monotonic; repeated reads
with no new bytes report a zero-length delta. Statuses are mapped from public
Subject results and unknown values remain `unknown`.

The required scenario evaluator contributes hard gates to `task_success` and
`task_passed`; it does not create a fourth first-level score. With no required
Process scenario, `process_gate_passed` is `null`.

The default synthetic declarations are
`examples/e2e_toolchain_v1.yaml` and
`examples/e2e_process_background_v1.yaml`. They use bounded Python commands,
no network, no fixed ports, and no OS-specific shell.

P6.2 Cron/Delegate, P6.3 DOCX/Dashboard, and P6.4 full Background Review
closure are intentionally outside this stage.
