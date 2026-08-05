# Worker 文件协议

## P5 Worker, P6.1 Worker and P6.2B Worker v13

## P6.2B Worker v13

P6.3 cost is computed by the Audit parent after Worker execution from
`TrialRuntimeSummary`; pricing configuration and monetary projections do not
cross the Worker protocol. Therefore the Worker protocol remains v13.

`myhermes-audit-worker-v13` projects the two exact public DeepSeek cache
fields, `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`, plus
`deepseek_cache_evaluated_prompt_tokens`, through the
Observation Artifact and Worker Result. They are paired, non-negative, require
`prompt_tokens` when available, and hit plus miss must sum to the evaluated
prompt total. `available` additionally requires that evaluated prompt total to
equal the Trial's complete prompt total. Missing fields mean that the cache was
not evaluated; they are never interpreted as zero. A malformed cache pair is
isolated as a structured warning while ordinary prompt/completion/total token
facts remain usable. No provider is inferred and the generic `cached_tokens`
diagnostic is not used as a source.

## P6.1 Worker v11

`myhermes-audit-worker-v11` adds strict `scenarios` plans to the request and
content-free `scenario_results`, `process_errors`, and scenario Artifact refs
to the result. Parent and Worker reject a version mismatch. Existing P0-P5
Suites remain valid because their scenario list is empty and they continue to
use the same Worker lifecycle.

P6.1 Artifacts are written only when declared by a scenario:

- `toolchain-results.json`
- `process-scenario-results.json`
- bounded `process-output-<scenario>.log`
- `process-cleanup.json`

The protocol carries no command text, stdin body, credentials, environment
values, or unbounded stdout. Subject-owned ProcessManager cleanup remains in
the existing Worker lifecycle; Audit does not create a second manager.

v11 also carries explicit Process cursor references, Process timing availability,
fixture-read evidence, and typed Toolchain Artifact
checkpoint projections (hash, character/UTF-8 lengths, marker IDs, truncation,
and pass/fail). Artifact contents never cross the Worker protocol.

v11 additionally carries content-free Process event-alignment diagnostics and
separate timing facts. A Step exposes only the public handler `duration_ms`.
The Scenario exposes a persistence observation span named
`scenario_observation_started_at`, `scenario_observation_completed_at`, and
`scenario_observation_span_ms`, with source
`public_observation_persistence`; this is an interval projection, not exact
Tool boundaries. PRE and POST hook offsets and sources are separate: PRE is a
control boundary before dispatch, while POST is emitted after the Observation
batch is persisted and is not exact handler completion. The Runner computes
the effective watchdog disposition once and carries it in the request as
`process_watchdog_enabled`, `hard_timeout_source`, `hard_timeout_seconds`, and
(when enabled) the declared Process Scenario ID. The Worker and Scenario
projection consume those fields; they do not infer watchdog scope from
`plan.required`. The effective Worker watchdog is carried as either
`hard_timeout_source=worker_process_scenario_watchdog` or
`trial_watchdog`, independent of the observation span. WAIT remaining-budget
facts use only start-PRE to WAIT-PRE and carry the explicit
`allow_hard_watchdog_fallback` contract; an unavailable value is never marked
as an exact match. Parents strictly reject v10 envelopes instead of silently
defaulting these fields. Alignment records contain only safe indexes, public
labels, hashed identities, status, reason, and Step IDs. Cleanup timing remains
a Worker lifecycle fact and is not folded into the foreground Scenario
timeout. Only relative offsets and safe spans cross the protocol; host-specific
absolute monotonic nanoseconds do not.
Missing timestamps make the observation span `UNAVAILABLE` rather than
successful or invalid. A complete timestamp set with an invalid order is
`INVALID` and contributes a Scenario error; an unavailable span is diagnostic
only and is not added to Scenario or Process error collections.

Current default: `myhermes-audit-worker-v13`; v12 is the declared legacy
version and is not silently accepted. P6.1 timing fields use the
explicit observation-span, hook-span, hard-watchdog, and remaining-budget
semantics above; v10 is legacy and is not accepted by the current parent.

默认协议升级为 `myhermes-audit-worker-v13`。P5 request/result、P6.1 scenario Artifact 必须使用 v13，父子进程会严格拒绝 v12 或其他不兼容版本及不完整的 Artifact 引用。v13 额外携带 DeepSeek 公共缓存 hit/miss token、已评估 prompt Token 和局部非法诊断；缺失表示不可评估，不补零，partial 不再与 Trial 全部 prompt Token 混淆。P6.1 的 Process 结果显式区分事件对齐诊断、persistence observation span、PRE/POST Hook 来源与相对偏移、Worker watchdog、fixture file-read 证据与 Agent close/Worker cleanup；缺少可靠边界不会被解释为精确成功。当前 Worker 仍可执行没有 P5/P6.1 Plan 的 P0–P5 Suite；它们不初始化 Review 或 Process 场景运行时，也不会携带对应结果。遗留 v12 envelope 不会被静默降级。

P5 request 增加严格的 `background_review_plans` 与 Skill Fixture 投影；result 增加 `background_review_results`、`background_review_errors`。`review_gate_passed` 属于父进程 Validator/Orchestrator 的最终 Trial 事实，Worker 不得自行伪造它。每个有 P5 Plan 的 Trial 固定写入并交叉校验：

- `background-review-results.json`：状态、action、attempt、outcome、observed changes 和安全错误；
- `background-review-evidence.json`：foreground 与 Subject prepared evidence 的 ID、kind、hash、长度、顺序和来源关联；
- `background-review-snapshots.json`：before/after Memory 或 Skill 状态投影。

这三份 Artifact 不包含 claim token、Review Prompt、隐藏推理、Memory/Skill 正文、完整工具正文、凭据或绝对路径。所有写入仍使用同目录临时文件和原子替换。

P4 request 保留 `variant_id`、`effective_subject_configuration`、适用的 required-fact expectations、checkpoint 和 `artifacts/ablation.json`。结果及 Ablation Artifact 逐项一致地保存公开 `compression_events`、`context_diagnostics` 和 content-free `fact_context_observations`。超时或父/子进程失败时，父进程只恢复身份匹配的部分 Artifact，并补齐合法空 Artifact，不把缺失观察伪造成事件。所有投影受 `maximum_turns`、`maximum_compression_events` 和协议文件大小限制。

当前 Subject 没有公开 Compression event 字段，因此事件列表为空、`compression_applied` 为 `None`；Worker 不依据 token 或消息变化推断。若未来公开 ModelCall projection 提供声明字段，当前 v4 只从该公开 projection 映射事件。详见 [P4 文档](p4-memory-compression-ablation.md)。

Worker 不使用 stdout 传结构化结果。每个 Trial 的 `artifacts/` 固定包含：

- `worker-request.json`
- `worker-result.json`
- `transcript.json`
- `observations.json`
- `validator-results.json`
- `worker.stdout.log`
- `worker.stderr.log`
- P3 Case 才有的 `memory.json`
- P4 Variant 才有的 `ablation.json`
- P5 Review Plan 才有的 `background-review-results.json`、`background-review-evidence.json`、`background-review-snapshots.json`

请求和结果都使用严格 Pydantic 合同、明确的协议版本、未知字段拒绝、非负计数与有限数值。P3 因 turns 增加逻辑 `session_id`，并增加 strategy、Memory Fixture、稳定 query plan 与 Memory Artifact，协议从 v1 显式升级为 `myhermes-audit-worker-v2`；P4 升级为 v3；P5 因 Review Plan、执行结果与三份安全 Artifact 升级为 v4；P6.1 因 typed scenario 计划、结果与安全 Artifact 首次升级为 v5，随后因 Process 结果合同、字符 cursor、身份和 cleanup 事实变化升级为 v6；因 cursor reference 与 Artifact checkpoint 事实升级为 v7；因事件对齐诊断、persistence observation span、Worker watchdog、公开 Hook monotonic offsets 与 close/cleanup 投影升级为 v10；因 PRE/POST 来源拆分、显式 Wait fallback 与 watchdog 作用域升级为 v11；因 DeepSeek 缓存公共 Observation 合同升级为 v12；本轮因 evaluated prompt Token 分层和四状态双向约束升级为当前默认 v13，v12 仅作为声明的 legacy 版本。请求不携带环境快照或凭据。

结果只保存安全运行投影：状态、逐 turn 输出、run ID、有限的计数/token/duration、Artifact 相对路径、稳定错误类别与安全摘要。它不序列化 MyHermes 对象、完整 Prompt、模型隐藏推理、完整工具参数或完整工具结果。

P3 结果另外保存严格 `memory_query_results`、before/after `memory_snapshots`、`memory_state_changes` 和 `memory_errors`；`memory.json` 必须与 WorkerResult 的这些字段逐项一致。查询 provider/strategy/phase、连续 rank、非负 duration 和稳定 ID 在合同层校验。非 Memory Case 的 request/result 不允许夹带 Memory Artifact 或事实，因此旧 P1/P2 执行不会加载 Adapter。

P5 的 `BackgroundReviewExecutionResult` 把 `ReviewOutcome.changes` 与 live `observed_changes` 分开保存。后者即使在 failed、rejected、stale 或 no-op 时也可以记录意外变化，以便 deterministic safety gate 明确报告半写入。重复 claim 的第二个 attempt 必须证明没有新的 Review loop、模型、工具或状态变化；重复 collect 只返回同一缓存事实。

同一 Trial 的 turn 可映射多个公开 MyHermes Session；WorkerResult 只保存 Suite 声明的逻辑 Session ID，不泄露 Subject 随机 Session ID。Worker 每轮按策略重建 Prompt，并在关闭连接前 best-effort 清理所有公开 Session resource。

JSON 先写入同目录随机临时文件，再用 `os.replace` 原子发布。Worker 已获得可信请求时，会尽力在成功和运行失败时都写 envelope；若进程在可信边界建立前崩溃或没有结果，父进程生成 `environment_error` 兜底结果。stdout/stderr 独立、有大小上限、保留头尾并标记截断。

协议中的 completed 必须没有 error；failed 必须有一致的 `error_type` 和 error。timeout 由父进程映射为稳定的 Trial timeout，不伪装成 completed。

Memory query/snapshot 等后置评测错误保留为 `MemoryOperationError`，由 required retrieval evaluator 产生 `ERROR` 门禁；已经完成的对话事实不会被伪造为空成功。seed/capability 阻断会使用稳定 Memory error type 终止 Worker。clear 是 best effort：失败记录 `memory_clear_error`，但不覆盖更早的主要失败，最终仍由 Sandbox 所有权清理隔离状态。

Worker 在 clear 前先原子写入一份 Memory Artifact checkpoint，随后执行 best-effort clear，并以实际 `clear_attempted` / `clear_succeeded` 状态原子更新同一 Artifact。这样即使清理或后续 envelope 构建失败，父进程仍可恢复已经形成的安全查询、快照、diff 与错误事实。
