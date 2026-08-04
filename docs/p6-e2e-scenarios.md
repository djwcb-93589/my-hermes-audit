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
`kill`, `close`, and `assert_status` steps. `process(action="close")` closes
stdin for a still-running process; it does not release a completed Process
record or perform Session cleanup. The default completion and kill Cases
therefore do not call `close`. Worker cleanup is a separate scenario-level
expectation and remains distinct from `agent_close_observed`; it verifies no
live process, released Session resources, and no background read residue. The
current Subject exposes process management through the `terminal` Toolset
(`process` is its companion declaration), so no synthetic `process` Toolset is
added.

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
`unknown`. The current MyHermes public `ProcessStatus` values are projected by
the capability probe (`starting`, `running`, `exited`, `killed`, `lost`, and
`failed_start`). A process blocked on stdin remains publicly `running`; the
input Case therefore requires a `running` status checkpoint and independently
requires the incremental `P6-WAIT` output marker. Audit never changes that
status based on a marker or infers `waiting_for_input` from `readline()`.
`waiting_for_input` remains a future, capability-gated status and is not used
by the default Suite. Agent `close` and Worker lifecycle cleanup are recorded
separately.

Scenario and step timeouts are hard gates backed by Worker watchdogs and real
public Observation durations. A Process timing result is explicitly
`available`, `available_duration_only`, `unavailable`, or `invalid`; missing or
invalid timing on a required Step fails with a timing diagnostic instead of
defaulting `timed_out` to false. Optional missing timing is not evaluable for
the timeout dimension. The timeout comparison is strict: `duration_ms >
timeout_seconds * 1000` means timed out. A `wait` also proves the real Tool Call
timeout is within the Step budget, the Step budget is within
`maximum_wait_seconds`, and that maximum is within the remaining Scenario
budget. Checkpoints are discriminated by `kind` and target explicit step IDs;
checkpoint ID text is never parsed as a hidden DSL.

An input fixture is only accepted as Process input when the public `file` Tool
Call successfully reads the declared `fixtures/...` path before the Process
submit. The submitted bytes are independently compared with the materialized
fixture by SHA-256, character length, and UTF-8 byte length. File events remain
in the global Tool trace and never consume Process event-sequence positions.

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

### Process event alignment and timing sources

Process observations are aligned with a bounded, forward-only matcher. The
matcher searches from its current cursor for the first event that satisfies
each typed Step; it never rewinds, reuses an event, or matches across process
identities. File, Memory, Skill, Review, Assistant text, and marker text are
not Process events. Events skipped while searching are retained as structured
diagnostics, and trailing events are retained as unconsumed diagnostics.

The result keeps separate `unexpected_events`, `missing_expected_events`,
`event_order_violations`, `foreign_process_events`, and `unconsumed_events`
lists. Their safe fields contain only an event index, public tool/action,
hashed process and Tool-call identities, observation status, reason, and an
optional Step ID. The corresponding stable error types are
`process_unexpected_event`, `process_missing_expected_event`,
`process_event_order_violation`, `process_foreign_process_event`, and
`process_unconsumed_event`. Required extra, missing, foreign, or out-of-order
events fail the Process gate, while facts from correctly matched later events
remain available for diagnosis.

Each Step uses the public Tool Observation `duration_ms`, which measures only
the handler and is never treated as model-thinking or inter-call time. The
Scenario separately projects an observation interval from persisted
`created_at` values as `scenario_observation_started_at`,
`scenario_observation_completed_at`, and `scenario_observation_span_ms`, with
`scenario_timing_source=public_observation_persistence`. These timestamps are
persistence metadata and do not establish exact per-Tool boundaries. The
Worker also registers the public PRE/POST Tool hooks and records aggregate
monotonic boundaries for the declared `terminal`/`process` calls. WAIT
remaining-budget facts use that Worker-monotonic source when every boundary is
available; they otherwise remain explicitly unavailable rather than inferred
from a duration sum.
Only relative per-event offsets and the aggregate monotonic duration are
serialized; host-specific absolute monotonic nanoseconds never leave the
Worker.

The hard deadline is the effective Worker case watchdog
(`hard_timeout_source=worker_case_watchdog`), which is conservative because it
starts at Case execution and may include pre-Process setup. It is independent
of the persistence observation span and exposes separate watchdog booleans.
`tool_duration_sum_ms` is retained only as a diagnostic. Worker lifecycle
cleanup timing remains outside this foreground observation span.

The official Process prompts provide the exact Case B/C commands and the
public `log`, `poll`, and `kill(process_id, grace_seconds)` actions. Case A
explicitly uses one `submit` (never `write`) and performs its second `log`
before `wait`; these prompts do not loosen command, identity, cursor, or timing
gates.

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
