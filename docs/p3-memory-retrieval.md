# P3 Memory Retrieval

P3 评测 MyHermes 自己公开的 Memory 能力，不在 Audit 中实现检索算法。生产数据流是：

```text
AuditSuite / strict Memory expectations
→ Sandbox 前 Subject Capability Probe
→ versioned Worker file protocol
→ isolated MyHermesMemoryAdapter
→ public MyHermes Memory API
→ query/snapshot/diff facts
→ Subject-neutral retrieval/state validators
→ TrialResult / AuditRunResult
→ optional Langfuse display projection
```

核心合同、Loader、Validator、聚合、报告、CLI、Dataset planner 和 Langfuse mapper 均不导入 `hermes.*`。只有隔离 Capability Probe、Worker 及其 MyHermes 适配组件能看到 Subject 类型。Validator 消费已经序列化的 Audit Memory 合同，不读取 Memory 文件或 SQLite。

## 策略与当前能力

`RetrievalStrategy` 严格接受 `subject_native`、`disabled`、`dense`、`bm25`、`hybrid`。未知值在合同加载时拒绝。

当前 MyHermes 的真实可运行策略只有：

- `subject_native`：公开 provider 语义为 `prompt_context_injection`；
- `disabled`：同一 Sandbox 中保留 seed 数据，但对 Agent 和查询诊断隐藏。

当前没有公开 ranked query API、score、user filter 或 session filter，所以 `dense`、`bm25`、`hybrid` 会在任何 Trial Sandbox 创建前以 `memory_strategy_unsupported` 失败。错误只列 Case ID、请求/支持策略和缺少的 capability，不包含 Memory 正文。Audit 不静默降级，也不以 metadata 标签伪造算法。

## `subject_native` 的准确语义

Adapter 使用公开 `read_memory_entries` 和 `render_memory_section` 对齐真实 Prompt 暴露。结果取 Subject 原生注入顺序，`top_k` 只截取该顺序的前 K 项；query 文本不参与排序。结果固定记录：

```text
provider=prompt_context_injection
query_used=false
score_semantics=none
score=null
```

Adapter 会逐项确认公开读取结果存在于公开渲染投影中。无法安全映射时返回 capability error，不读取 `MEMORY.md`/`USER.md`，也不复制私有分隔格式。MyHermes 的字符限制由公共写入和渲染 API 自己执行。

## Fixture、kind 与清理

当前映射只有：

| Audit kind | MyHermes target |
| --- | --- |
| `long_term` | `memory` |
| `user_profile` | `user` |

`short_term`、`episodic`、`semantic`、`unknown` 不会被改写为长期记忆。Fixture 通过 `mutate_memory_entries(action="add")` 注入；同一 target 的重复正文会在 preflight/Adapter 映射阶段拒绝。Subject 返回只保留安全 error type。

Adapter 的 `clear()` 只尝试移除它本 Trial 注入且仍存在的条目，并继续使用公共 mutation API。运行期新增或替换内容不由 Adapter 猜测清理；Trial Sandbox 的所有权校验与最终目录清理是隔离兜底。整个过程从不使用调用方默认 `HERMES_HOME`。

## 查询、快照与状态 diff

每个 `MemoryExpectation` 有 Case 内唯一的 `query_id` 和显式 phase：

- `before_conversation`（默认）：会话开始前的可见证据；
- `after_conversation`：会话可能写入后再次观察的证据。

before/after snapshot 分别通过公共读取接口生成。Fixture 完全匹配项保留声明 ID；未识别原生项使用 `target + NFKC/空白规范化正文` 的 SHA-256 稳定 ID，不依赖列表下标或 UUID。Snapshot 保留 `memory` 后 `user` 的原生顺序，不记录绝对路径。

纯核心 `diff_memory_snapshots` 按稳定 ID 产生 added、removed、modified、unchanged 事实。`MemoryStateExpectation` 可声明 present/absent、added content、forbidden added content、removed、unchanged 和 `allow_other_changes`。内容匹配只有 `exact`、`contains`、`normalized_exact`，不使用 embedding。

## 跨 Session 与 Memory tool

单轮 input 和每个 scripted user turn 均可选声明逻辑 `session_id`。未声明时使用 Trial 默认逻辑 Session；同名映射同一 MyHermes Session，不同名称通过公开 `create_session` 分别创建。它们共享当前 Trial 的隔离 `HERMES_HOME`，但不共享 SQLite 会话历史。Worker 每轮重新构建 P3 Prompt，使一个 Session 的持久 Memory 写入能被后续 Session 看见，并逐个调用公开 session resource cleanup。

`execution.enabled_toolsets: [memory]` 是显式写权限。只从 Prompt 读取 Memory 不需要该 toolset；`disabled` 与 memory tool 同时声明会在 preflight 失败。ToolPolicy/ToolRegistry 仍是唯一工具授权路径，Observation 只投影公开工具状态，不包含参数正文、隐藏 Prompt 或推理。

## Retrieval 与状态指标

每个查询生成独立事实：found/missing required IDs、found forbidden IDs、matched kinds、match count 和 `required_evidence_found`。硬门禁至少要求：

```text
match_count >= minimum_matches
全部 required_kinds 出现
forbidden_memory_ids 均未出现
```

`Recall@K = top K 命中的 required ID 数 / required ID 总数`。没有 required ID 时为 `not_applicable`，不是 0。MRR 是首个 required ID 的 reciprocal rank；有 required ID 但均未出现时为 0，没有 required ID 时同样不适用。两者默认仅诊断；只有 expectation 明确声明阈值时才增加确定性硬门禁。

状态门禁独立验证新增、覆盖、删除、no-op 和额外变化。所有 Memory 指标使用 `MetricSource.RETRIEVAL`；evaluator error 保持 `ERROR`，不会伪装成数值 0。

`TrialResult` 分开记录 `retrieval_gate_passed`、`final_answer_gate_passed` 和 `memory_state_gate_passed`，所以“检索成功/失败”与“最终答案正确/错误”的四种组合都能表达。`task_success` 汇总 required 本地 deterministic、tool 和 retrieval/state 门禁，但一级质量分仍只有 `task_success`、`tool_correctness`、`answer_quality`。

## `disabled` 对照

`disabled` 仍使用公共 mutation API seed 相同 Fixture，并照常生成前后快照与状态 diff，但：

- Prompt 使用 `include_memory=False`、`include_user_profile=False`；
- memory tool 必须未启用；
- 查询返回空 items、`query_used=false`、`score_semantics=none`。

因此对照只改变可见性/写权限，不改变初始数据。

## Langfuse 与内容安全

本地 `AuditRunResult` 是完整事实来源。P3 Trace 可增加 seed、query、snapshot 和 retrieval evaluator Observation，不增加一级 Score。

`--langfuse-no-content` 下，Memory/query 每项只投影哈希、长度、字节数、kind、rank、required hit、duration 和安全 metadata。`sensitive` 分类无论开关如何都不上传 Memory、User Profile、Fixture 或 query 正文。任何模式都不上传隐藏 Prompt、隐藏推理、数据库正文、凭据或绝对 Sandbox 路径。远端失败不改变本地门禁。

## 隔离和明确不做

Orchestrator 先完成 Suite/Subject/strategy/kind/scope preflight，再创建 Trial。每个 Trial 有独立 `HERMES_HOME`、Memory 存储、SQLite、workspace、进程和 Adapter；父进程不缓存 Memory 状态，Case/Trial 之间没有复制。

P3 不实现 Audit 向量库、embedding、BM25、Hybrid、Compression、Background Review、Baseline、并行 Trial、CI 或自定义 Dashboard。检索能力必须来自 Subject 公共 API；否则就明确 unsupported。
