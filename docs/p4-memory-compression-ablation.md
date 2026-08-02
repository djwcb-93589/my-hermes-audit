# P4：长短期记忆与 Compression 消融框架

## 边界与事实来源

P4 在 P3 的 `MyHermesMemoryAdapter`、Memory Fixture、query、snapshot 和 state diff 之上增加消融编排，不创建第二套长期记忆实现。`AuditRunResult` 仍是唯一事实来源；Case-level 比较只回放已经存在的 `TrialResult`。

Audit 不实现摘要器、消息裁剪器、向量检索、BM25、Hybrid 或任何替代 Compression。Compression 只能由 Subject 的公开开关、公开配置或公开运行接口触发。父进程不导入 `hermes.*`，Worker 也不调用私有下划线对象或直接修改 Session 数据库。

## 四种 Memory Mode

| Mode | Subject Session 上下文 | 长期 Memory / USER Prompt | Memory tool | Session 策略 |
| --- | --- | --- | --- | --- |
| `no_memory` | 关闭跨 turn 继承 | 关闭 | 关闭 | 每 turn 独立 Session |
| `short_term_only` | 同一逻辑 Session 内保留 | 关闭 | 关闭 | Suite 声明的 Session 分组 |
| `long_term_only` | 关闭跨 turn 继承 | 开启 | 仅在 Case 已授权时开启 | 每 turn 独立 Session |
| `short_and_long_term` | 同一逻辑 Session 内保留 | 开启 | 仅在 Case 已授权时开启 | Suite 声明的 Session 分组 |

短期记忆始终来自 MyHermes 正常的 Session / conversation 公共接口。Audit 不在父进程缓存历史消息，也不把临时文件称为 short-term Memory。所有 P4 Session ID 都按 Variant 命名空间投影；不同 Trial 的 SQLite、`HERMES_HOME` 和 workspace 又由 Sandbox 物理隔离。

## Variant 与显式配置

`AblationPlan` 只执行 `variants` 中按声明顺序列出的组合，不生成笛卡尔积。每个 `AblationVariant` 明确声明：

- `variant_id`；
- `memory_mode`；
- `compression_mode`（`disabled` 或 `enabled`）；
- 仅允许公开 `compression.*` 路径的 `config_overrides`。

允许的路径是 `threshold`、`protect_first`、`keep_recent_tool_results` 和 `tail_token_budget`。它们只能是非负整数，enabled Variant 必须显式给出可触发的正阈值。disabled Variant 使用框架固定的 `2147483647` 公共阈值；凭据、ToolPolicy、Sandbox 路径和任意未知配置均不能由 Variant 覆盖。

最终有效投影 `EffectiveSubjectConfiguration` 记录 Memory Prompt、User Profile、memory tool、Session 模式、Memory strategy、Compression 控制、阈值、事件上限和公开配置投影。该投影进入 Worker 请求、Trial 结果与配置 fingerprint，但不包含凭据。

`RequiredFactExpectation.applicable_variant_ids` 可显式限制一组事实适用于哪些 Variant；空列表表示适用于 Case 的全部声明 Variant。引用未知 Variant 会在 Suite 加载时失败。

## 当前 MyHermes 能力

当前公开源码表面提供：

- `run_conversation`、Session 创建和 Session 消息读取，可表达短期上下文及隔离；
- `build_system_prompt(include_memory=..., include_user_profile=...)`，可控制长期 Memory / User Profile 注入；
- 公开 Memory 读写、渲染和 tool 注册；
- `ConversationAgentLoop(compression_threshold=...)` 以及公开 `compression.*` 配置，可用阈值控制 Compression；
- ModelCall 的 provider token 字段。

因此当前 Probe 可报告四种 Memory Mode，以及 `disabled` / `enabled` 两种 Compression Mode，控制方式为 `threshold_configuration`。但当前 `ModelCallObservationView` 没有公开的 `compression_applied`、输入消息数和输出消息数投影，所以 `compression_observation` 为 unsupported；精确 context-size 公开 Observation 也不可用。

这意味着：

- enabled Variant 可以使用 Subject 公共阈值运行；
- Audit 不能确认某次调用是否真正发生 Compression，也不会根据 token 下降或消息数变化猜测；
- 声明 `must_survive_compression: true` 的 required fact 需要公开 Compression Observation，当前会在 Sandbox 创建前以 `compression_observation_error` 失败；
- 没有该要求的 enabled Variant仍可记录 provider token、duration 和“事件不可观察”的诊断；
- 若基础配置缺少允许的 `compression.*` 路径，配置预检同样在 Sandbox 前失败。

未来 Subject 若公开上述 Observation 字段，Worker v3 会从公开 ModelCall projection 映射事件；字段缺失继续保持 `None`。

## Capability Probe 与 preflight

Probe 只导入公开模块、检查公开符号并用 `inspect.signature()` 绑定真实调用形状，不运行会话、模型、Session、数据库或 Compression。它探测：

```text
short_term_context
session_context_isolation
long_term_memory
user_profile
memory_prompt_toggle
memory_tool
compression_available
compression_toggle
compression_configuration
compression_observation
token_usage_observation
context_size_observation
```

`doctor` 仅输出 short-term、long-term、Compression toggle 和 Compression observation 的 supported/unsupported 摘要。Case preflight 在任何 Sandbox 或 Worker 之前验证 Memory Mode、Compression Mode、所需 Observation、memory tool 和最终公开配置形状；不支持时不静默降级。

## Variant 展开、稳定身份与隔离

执行顺序固定为 Suite Case 顺序、Variant 声明顺序、Trial ordinal。没有 `ablation` 的旧 Case 仍只生成原有 Trial，不产生默认 Variant。

P4 `TrialIdentity` 的 canonical SHA-256 包含：

```text
Suite SHA-256
Case ID
Variant ID
Trial ordinal
Subject commit
Subject execution fingerprint SHA-256（commit、tree、dirty、Python requirement；不含本机路径）
有效配置 SHA-256
实际配置中的模型标识
```

相同输入得到相同 `trial_id`。配置 fingerprint 还显式包含 `variant_id`、有效 toolsets 和公开配置；comparison-basis fingerprint 则排除 Variant 投影，用于确认除消融变量外的 Case 条件一致。

P4 Sandbox 布局为：

```text
<root>/<audit-run>/<case>/<variant>/<trial>-<sandbox-id>/
```

每个目录分别拥有 workspace、`HERMES_HOME`、SQLite、Memory 文件和 Artifact；Manifest 记录 `variant_id`。执行仍完全串行，不包含并发调度。

## Required facts

`RequiredFact` 使用 synthetic `canonical_value`、显式 `accepted_variants` 和 `exact` / `normalized_exact` / `contains` 匹配。事实 scope 可为：

- `subject_context`：在声明 checkpoint 的 Subject 可见上下文中检查；
- `long_term_memory`：在 P3 after-conversation snapshot 中检查；
- `final_answer`：在最终答案中检查。

`must_be_absent` 把不存在视为通过，把出现视为 `present_when_forbidden`。所有 scope 都可声明 survival：`subject_context` 使用对应 checkpoint 的观察，`final_answer` / `long_term_memory` 使用会话末尾前已形成的公开诊断。`must_survive_session_change` 只接受公开观察到的 Session 切换；`must_survive_compression` 只接受公开确认已经发生 Compression。缺少证据产生 `not_evaluable` / error metric，而不是猜测通过或填 0。

所有事实证据都以 SHA-256、字符长度和状态投影；Validator Artifact、TrialResult 和 Langfuse P4 Observation 不复制 canonical 正文。

## Required-fact loss 与 Distortion

只统计 `RequiredFactExpectation.required: true` 的事实。成功状态为 `retained` 或预期的 `absent`：

```text
required_fact_loss_count = required_fact_count - retained_required_fact_count
required_fact_loss_rate = required_fact_loss_count / required_fact_count
```

没有 required fact 时为 `not_applicable`。任一 required fact 不可评估时 loss 为结构化 error，不伪造为 0。

Distortion 只依据 Suite 显式声明的候选值和确定性匹配，支持 `missing`、`contradicted`、`value_changed`、`entity_changed`、`temporal_order_changed`、`unsupported_addition` 与 `not_evaluable`。开放式语义可继续由现有 Judge 产生 `answer_quality`，但 Judge 不能替代 required-fact 门禁。

## Checkpoint 与上限

`LongConversationCheckpoint` 声明 `after_turn`、适用 fact ID、可选 `expected_answer` 和 required 状态。一个 Variant 只运行一次 scripted conversation；Worker 在相应 turn 产生 context/fact Observation，Validator 随后从同一 Trial 计算 checkpoint 结果。required checkpoint 的答案缺失或 context diagnostic 不可用会明确失败；`compression_applied` 缺少公开证据时保持 `None`，只有 survival 声明才把该证据要求升级为预检/事实门禁。

`maximum_turns`、`maximum_compression_events` 和现有 Trial timeout 都在协议中强制执行。公开事件超过上限会产生 `compression_limit_error`，不会截断后伪装成完整结果。

## Token、duration 与可比性

模型 token 来自 MyHermes 公共 ModelCall provider observations，标记为 `provider_reported`。Worker 对公开 message list 使用 Subject 提供的 estimator 时只标记 `audit_estimated`，绝不包装成 provider token。不可用字段保持 `unavailable`。

每个 Variant记录 input/output/total token、可用的 Compression input/output、Trial duration、retrieval duration 和可用的 Compression duration。Trial 与 P3 retrieval duration 标记为 `audit_measured`；只有 Subject 公开事件直接提供的 Compression duration 才标记为 `subject_reported`，缺失则为 `unavailable`。只有以下条件全部满足才计算 token savings：同一 Case、模型、Subject commit、Suite fingerprint、comparison basis、相同可靠 token 来源，且双方 total token 完整。否则值为 `None` 并在 Case comparison 中记录不可比较原因。

## Case-level 比较与一级指标

`AblationComparisonResult` 按 Variant 聚合现有 Trial，仅产生独立、可解释的差值：task success 是否变化、retrieval success 是否变化、answer-quality 差值、required-fact-loss 差值、distortion 数量差、token 差和 duration 差。它不重新运行 Agent、Validator 或 Judge。

一级质量指标仍只有：

```text
task_success
tool_correctness
answer_quality
```

required fact、required absence、required distortion、required checkpoint、P3 retrieval/state 和最终答案确定性门禁可以共同影响 `task_success`。Token、duration、loss rate、distortion count 和 Compression event count仅为诊断，不形成第四个 Score 或综合总分。

## Langfuse 与 no-content

Langfuse 只在本地 Audit 完成并持久化后回放：Variant metadata、Compression/context diagnostics、checkpoint、fact retention、distortion 和 Case comparison。它不重新运行 Subject、Compression、Validator 或 Judge。

P4 Dataset 同步按 `(case_id, variant_id)` 生成稳定 Item identity，避免不同 Variant 复用一个 Experiment Item；旧 Case 仍是一 Case 一 Item。远端发布要求每个 Case/Variant 恰好一个本地 Trial，本地多 ordinal 消融本身不受此限制。

P4 fact projection无论数据分类都不上传 canonical value。`--langfuse-no-content` 或 `data_classification=sensitive` 时，现有 turn、final answer、Memory、User Profile 和 query正文也按统一策略省略；远端只收到 Variant/mode、fact ID、哈希、长度、状态、计数、token、duration 和安全 metadata。Score mapper仍只发布三个一级 Score。

## 示例与后续验证

[`memory_compression_ablation_v1.yaml`](../examples/memory_compression_ablation_v1.yaml) 包含三个受控 synthetic Case：四种 Memory 边界、Compression 事实保留、长短期联合。Compression survival Case 显式标记当前缺少公开 Observation 的 capability-negative 条件。

独立 T4 应验证合同拒绝、稳定 fingerprint/identity、Variant 顺序与所有隔离目录、四种 Memory 语义、阈值确实传入 Subject、capability-negative preflight、公开 Observation 映射、事实/absence/loss/distortion/checkpoint 门禁、超时和事件上限恢复、token 可比性、旧 P1–P3 回归、Langfuse no-content 与 Score 集合。T4 才执行真实 Trial、模型、Judge 或远端集成验证。
