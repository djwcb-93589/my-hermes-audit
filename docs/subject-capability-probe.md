# Subject Capability Probe

## P6.1 Process projection

The optional public checks include `process_toolset`, `process_start`,
`process_read_incremental`, `process_send_input`, `process_wait`,
`process_interrupt`, `process_kill`, `process_status`,
`process_session_cleanup`, and the derived `background_process_supported`.
Checks inspect declarations, public `ProcessManager` method signatures, and
the public process handler only. They never instantiate a manager, create a
Session, start a command, read a user's process, or call a model/network.

In the current Subject, the `process` declaration is a companion of the
`terminal` Toolset. `interrupt` is reported unavailable when the public
manager does not expose it; a scenario that explicitly requires it fails
before Sandbox creation with Case ID, scenario ID and safe capability names.

## P5 Background Review capability projection

Capability protocol `4.0` adds optional P5 public-surface checks without turning a Probe into a Review run. It binds only public module exports and safe call signatures for:

- Background Review coordinator/runtime, `ReviewClaim`, Driver Registry, Driver lifecycle, `ReviewAgentLoop`, executor shutdown and ReviewRunSpec evidence window;
- `ToolPolicy`/`ToolRegistry` policy resolution, including the public Background Review execution environment;
- Memory and Skill Review drivers/stores, public Memory read/write surface, public Skill inventory/governance surface, supported review kinds and safe outcome observation;
- public foreground message-range access needed to relate the actual triggering turn to the prepared Review window;
- public claim validation, completion and failure call shapes.

The Probe does not create a database or Session, claim a Review, call `record_progress`, create a Tool Registry, run a model, mutate Memory/Skill state, read evidence content, inspect claim tokens, or call a private `hermes.persistence.background_review` object. Its public API fingerprint contains only module/symbol names, availability and safe signatures.

`stale_review_detection` is deliberately independent. It is true only when the public Subject surface can demonstrate that claim validity is bound to the target governance revision. The current Subject does not expose that binding, so the Probe reports it unavailable and any `stale_before_execute` P5 Case fails before Sandbox creation. Audit must not infer stale from an unchanged snapshot or manufacture it by editing a database.

## P4 capability projection

Capability protocol `3.0` 在既有 P3 检查上增加 short-term context、Session isolation、long-term Memory、User Profile、Memory Prompt/tool、Compression threshold control/configuration、emergency disable、observation、token usage 和 context size。Probe 仅导入公开模块、读取公开符号并用 `inspect.signature()` 绑定调用形状；它不运行会话、创建 Session/数据库、执行 Compression、读取真实 Memory 或联网。

当前 MyHermes 的公开 threshold configuration 支持 `compression_mode: threshold_disabled|threshold_enabled`，四种 Memory Mode 也具备所需公开表面；但 `emergency_compression_disable`、`compression_observation` 和精确 `context_size_observation` 当前 unsupported。需要 Compression survival、事件数量或完整紧急禁用的 Case 会在 Sandbox 前失败。`doctor` 分别显示 threshold control/configuration、emergency disable 和 observation，不显示配置值、Memory 或会话正文。详见 [P4 文档](p4-memory-compression-ablation.md)。

The Subject Capability Probe is a strict JSON file-protocol subprocess started
with the same `sys.executable` as Audit. It is a compatibility check, not a
Trial and not a smoke run of MyHermes.

The parent creates a temporary `HERMES_HOME`, workspace, unused database path,
and capability-restricted config. Credential-shaped config references receive
probe-only placeholder values because the probe never performs a model call.
The Subject repository and the Audit `src` directory are the only application
import roots. Langfuse and Judge environment variables are never forwarded.

The subprocess checks these public surfaces:

- package import and Subject origin;
- `run_conversation`, `ToolRegistry`, `register_all`, and `ToolPolicy`;
- `build_system_prompt`;
- public Observation repository and view contracts;
- database initialization, session creation, and resource cleanup entrypoints;
- exported config projections used by the Worker;
- public file and terminal lightweight declaration surfaces;
- public Memory read/write, User Profile read/write, prompt render/toggles,
  Memory tool declaration/handler/registration, ranked query, scores and
  user/session/filter call shapes;
- derived supported Memory kinds, retrieval strategies and provider semantics.

Tool checks inspect public lightweight declarations and public handler/register
signatures. The probe never calls `register_all`, constructs a runtime registry,
executes a handler, invokes Memory read/render/write, starts a process,
initializes SQLite, creates a session, reads Memory content, or triggers
Background Review.

`SubjectCapabilityReport` records the protocol version, Subject commit,
individual checks, missing capabilities, bounded warnings, and a public API
fingerprint. Capability protocol `4.0` distinguishes baseline-required checks
from optional P3/P5 capabilities, so a Subject without Memory or Background
Review support can still run an old non-Memory/non-Review Suite. The fingerprint uses only module names, public object
names, availability, safe call signatures, the stable Memory
kind/strategy/provider projection, and the probe protocol version; it never
hashes MyHermes source text or function `repr`. If a Case requests an optional
capability that is absent, runner preflight fails before any Trial Sandbox and
reports only safe names.

For the current public MyHermes surface, the report derives kinds
`long_term`/`user_profile`, strategies `subject_native`/`disabled`, and provider
`prompt_context_injection`. `ranked_query`, `query_scores`, `user_filtering`,
`session_filtering` and `query_filters` remain unavailable, so Dense/BM25/Hybrid
cannot pass P3 preflight. `doctor` prints these names and statuses but no Memory
body.

Audit no longer imports `hermes.config._config`. The generated config validated
by the parent process is authoritative for disabled plugins. The Worker may
only perform a second check through MyHermes' exported `BROWSER_CONFIG` and
`BACKGROUND_REVIEW_CONFIG` projections. MyHermes currently has no equivalent
public plugins projection, so Audit does not work around that gap by reading a
private object or copying the Subject config model.

`myhermes-audit doctor` exposes the same local check. Its optional Langfuse and
Judge checks initialize and immediately close the configured client to validate
dependency version, required fields, URL and timeout without making a connection.
They do not write remote data or send a model request, and output includes
names/statuses but not credential values or the complete environment.
