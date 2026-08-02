# Subject Capability Probe

## P4 capability projection

Capability protocol `3.0` 在既有 P3 检查上增加 short-term context、Session isolation、long-term Memory、User Profile、Memory Prompt/tool、Compression runtime/toggle/configuration/observation、token usage 和 context size。Probe 仅导入公开模块、读取公开符号并用 `inspect.signature()` 绑定调用形状；它不运行会话、创建 Session/数据库、执行 Compression、读取真实 Memory 或联网。

当前 MyHermes 的公开 threshold configuration 支持 `compression_mode: disabled|enabled`，四种 Memory Mode 也具备所需公开表面；但 `compression_observation` 和精确 `context_size_observation` 当前 unsupported。需要公开 Compression survival 证据的 Variant 会在 Sandbox 前失败。`doctor` 只显示四行 supported/unsupported 摘要，不显示配置值、Memory 或会话正文。详见 [P4 文档](p4-memory-compression-ablation.md)。

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
fingerprint. Capability protocol `3.0` distinguishes baseline-required checks
from optional P3 capabilities, so a Subject without Memory support can still run
an old non-Memory Suite. The fingerprint uses only module names, public object
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
