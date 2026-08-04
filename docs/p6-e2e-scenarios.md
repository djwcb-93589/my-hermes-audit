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
requirements. `ArtifactOutputCheckpoint` names one declared output
`target_artifact_id` and performs a bounded local UTF-8 projection. Required and
forbidden markers use exact substring matching; `minimum_content_char_length`
is measured in Unicode characters. The Worker uses the existing conversation
Worker and public tool observations; Audit never reads a fixture to replace
Agent work or writes an Artifact on the Agent's behalf.

Process plans use typed `start`, `read_incremental`, `send_input`, `wait`,
`kill`, `close`, and `assert_status` steps. `interrupt` remains a
capability-gated action and is documented only by the negative Suite. Worker
cleanup is a separate scenario-level expectation; it is never represented as
an Agent Tool step. The current Subject exposes process management through the
`terminal` Toolset (`process` is its companion declaration), so no synthetic
`process` Toolset is added.

Process output is represented by bounded local log Artifacts. Structured facts
retain only typed checkpoints, command/input hashes and lengths, character-unit
cursors, UTF-8 byte diagnostics, marker matches, truncation, timing and safe
identities. `ProcessOutputCheckpoint` targets only a Process read step. The
first `read_incremental` declares `cursor_before: 0`; every
later read declares `cursor_source_step_id` for the immediately preceding read.
The Worker uses the observed `cursor_after` from that reference rather than a
timing-derived constant. `read_incremental` validates
`cursor_after - cursor_before == len(output)`; UTF-8 byte length is diagnostic
only. Statuses are mapped from public Subject results and unknown values remain
`unknown`. Agent `close` and Worker lifecycle cleanup are recorded separately.

Scenario and step timeouts are hard gates backed by Worker watchdogs and real
public Observation durations. Missing required timing remains unevaluable (it
is never represented as a fabricated `duration_ms=0`) and therefore fails the
required Process timeout gate. Checkpoints are discriminated by `kind` and
target explicit step IDs; checkpoint ID text is never parsed as a hidden DSL.

An Audit Case currently permits at most one `process_background` Scenario. The
contract rejects a second Process lifecycle before Sandbox creation, and the
Worker projection has a defensive structured failure instead of reusing event
index zero. Toolchain Artifact checkpoints read only their declared target,
reject symlinks and traversal, cap the local read at 256 KiB, and persist only
hashes, lengths, marker IDs, truncation and pass/fail facts. Artifact text is
never placed in Worker results or Langfuse.

The required scenario evaluator contributes hard gates to `task_success` and
`task_passed`; it does not create a fourth first-level score. With no required
Process scenario, `process_gate_passed` is `null`.

The default synthetic declarations are
`examples/e2e_toolchain_v1.yaml` and
`examples/e2e_process_background_v1.yaml`. The capability-negative declaration
is `examples/e2e_process_capability_negative_v1.yaml`; it must stop at
preflight for `interrupt`. They use bounded Python commands, no network, no
fixed ports, and no OS-specific shell. The short Process case uses a flushed
stdin handshake (`P6-BEGIN`/`P6-中文`, `continue`, then `P6-END`) rather than a
timing sleep; its second read references the first read result.

P6.2 Cron/Delegate, P6.3 DOCX/Dashboard, and P6.4 full Background Review
closure are intentionally outside this stage.
