# P4：长短期记忆与 Compression 消融框架

## 边界与事实来源

P4 在 P3 `MyHermesMemoryAdapter` 之上增加显式 Variant 编排，不创建第二套长期记忆、检索或 Compression 实现。`AuditRunResult` 是唯一事实来源；比较层只回放其中已有的 `TrialResult`。Audit 不摘要、裁剪消息、解析日志猜测 Compression，也不调用 MyHermes 私有接口。

没有 `ablation` 的 P0–P3 Case 仍只产生原有 Trial，不会生成隐含 Variant，也不进入 P4 比较。

## Memory Mode 与隔离

| Mode | Subject Session 上下文 | 长期 Memory / USER Prompt | Session 策略 |
| --- | --- | --- | --- |
| `no_memory` | 不跨 turn 继承 | 关闭 | 每 turn 独立 |
| `short_term_only` | 同一逻辑 Session 保留 | 关闭 | Suite Session 分组 |
| `long_term_only` | 不跨 turn 继承 | 开启 | 每 turn 独立 |
| `short_and_long_term` | 同一逻辑 Session 保留 | 开启 | Suite Session 分组 |

短期上下文始终来自 MyHermes 公共 Session/conversation 行为。每个 Variant 使用独立 Session 命名空间、Sandbox、SQLite、`HERMES_HOME`、workspace、Memory 文件和 Artifact。

## Compression Mode 的准确语义

P4 只接受两个严格值，旧值不会被静默映射：

```text
threshold_disabled
threshold_enabled
```

`threshold_disabled` 把公开 `compression.threshold` 投影为框架固定的大阈值，只关闭配置阈值触发；context-overflow 紧急 Compression 仍可能发生。`threshold_enabled` 使用 Suite 显式给出的可触发正阈值；overflow 紧急 Compression 同样可能发生。两者都不能证明某次 Trial 已发生 Compression。

Variant override 仅允许公开的 `compression.threshold`、`protect_first`、`keep_recent_tool_results`、`tail_token_budget` 数字路径。未知模式、旧模式、未知路径和不可触发阈值均拒绝。

每个 P4 Trial 的 `EffectiveSubjectConfiguration` 结构化记录：

```text
requested_compression_mode
effective_compression_semantics
compression_threshold_control
compression_threshold
emergency_overflow_compression_disable_supported
emergency_compression_possible
compression_events_observable
minimum_compression_events
maximum_compression_events
```

当前正常配置的 `emergency_compression_possible` 为 `true`。任何终端、JSON 或 Langfuse 投影都不得把 `threshold_disabled` 描述成“完全关闭 Compression”。

## 有效模型标识

Audit 和 Worker 共用一个解析器，优先级固定为：

```text
Case execution.environment_overrides.MODEL
→ 父进程 MODEL
→ 已合并并生成的最终 Subject 配置 model
→ subject-default
```

空字符串视为未配置。解析器只做 NFC、首尾空白和控制字符等安全规范化，不改变有意义的大小写或 provider 前缀。疑似凭据或完整 HTTP(S) endpoint 只进入不可逆 SHA-256 身份投影，不写入报告正文。

相同结果进入 Worker 环境、`TrialRuntimeSummary.subject_model`、`EffectiveSubjectConfiguration.model_identifier`、`TrialIdentity.model_identifier`、配置 fingerprint 和比较层。环境来源才写入 Worker `MODEL`；配置来源会移除空的环境覆盖，让 MyHermes 使用同一份最终配置。不同有效模型的 Variant 结构不可比。

## Capability Probe 与 preflight

Probe 只检查公开模块、公开符号和 `inspect.signature()` 调用形状，不运行会话、模型、数据库或 Compression。Compression 能力分为：

```text
compression_threshold_control
compression_threshold_configuration
emergency_compression_disable
compression_observation
```

当前公共表面可配置并传入阈值，但不提供紧急 overflow Compression 的完整禁用能力，也不提供 Compression event Observation。因此典型投影为：

```text
compression_threshold_control = true
compression_threshold_configuration = true
emergency_overflow_compression_disable_supported = false
compression_observation_supported = false
```

阈值可配置不代表 Compression 完全可关闭，也不代表事件可观察。下列声明必须要求 `compression_observation`：

- `must_survive_compression: true`；
- `minimum_compression_events > 0`；
- Compression event/message/token/duration 的精确诊断。

`require_emergency_compression_disable: true` 则要求独立的 `emergency_compression_disable`。缺少能力时，统一 preflight 在 Sandbox 创建前失败；结构化错误包含 Case ID、请求能力、缺少能力和安全的当前支持摘要，不包含对话、事实、Memory、凭据或本地绝对路径。配置了 `threshold_enabled` 本身不会伪造事件发生。

## 稳定身份

P4 `TrialIdentity` 的 canonical SHA-256 包含 Suite SHA-256、Case/Variant/ordinal、Subject commit、无本机路径的 Subject execution fingerprint、有效配置 SHA-256 和实际有效模型标识。配置 fingerprint 包含新 Compression Mode、有效 Subject 配置、toolsets 和公开 overrides；Suite fingerprint 自然包含完整 AblationPlan。

## Required Fact、Distortion 与 Checkpoint

Required Fact 支持 `subject_context`、`long_term_memory`、`final_answer` scope，以及 exact/normalized-exact/contains 匹配、required absence、Compression/Session survival 和显式 Distortion 候选。事实证据在本地结果和 Langfuse P4 Observation 中使用 SHA-256、长度和状态投影，不复制 canonical 正文。

`must_survive_compression` 只接受公开确认的 Compression 证据，且与请求的阈值模式无关：紧急 Compression 也属于 Compression。缺少 Observation 时 preflight 失败；运行期证据不足时为 not-evaluable/error，不猜测通过。required-fact-loss、Distortion、Checkpoint 和 `task_success` 门禁保持原有语义。

## Token、duration 与分维度可比性

`AblationComparisonResult` 不再使用一个总 `comparability`。它分别记录严格的 `status` 和稳定原因码：

```text
structural_comparability
token_comparability
answer_quality_comparability
duration_comparability
```

结构可比要求同一 Case、Suite/Audit 语义、Subject commit/fingerprint、实际模型、comparison basis，以及除消融变量外一致的执行与数据语义。结构不兼容时，task、retrieval、fact loss、distortion、Token、duration 和 answer quality delta 全部为 `None`。

Token 可比还要求双方 total token 可用、来源相同且非 unavailable、统计 scope 相同、Subject model-call count 可用且相同。`provider_reported` 与 `audit_estimated` 不会混用。Token 不可比时 `token_delta`、`token_savings`、`token_savings_rate` 为 `None`，但不影响结构上仍有效的 task、retrieval、fact、distortion 和 duration delta。

Answer quality 仅在结构可比、全部 Trial Judge completed、Prompt 版本唯一且相同、Judge model identifier 唯一且相同时产生 delta。Duration 只要求结构可比且双方 Trial duration 可用；Token 缺失不会阻止 duration delta。框架不生成综合总分。

## Langfuse 与 no-content

Langfuse 只回放已经持久化的本地结果。P4 metadata 使用 `requested_compression_mode`、`effective_compression_semantics`、`emergency_overflow_compression_may_still_occur` 和 `compression_events_observable`。事件不可观察时，远端 event count 为 `null`，不会把空列表解释成“确认发生 0 次”。Case comparison 投影保留四个独立可比性维度。

`--langfuse-no-content`、敏感数据投影、三个一级 Score、发布清单和失败隔离语义保持不变。

## 示例与 T4

[`memory_compression_ablation_v1.yaml`](../examples/memory_compression_ablation_v1.yaml) 是默认可运行 Suite，只包含当前公共能力支持的四种 Memory Mode、两种阈值控制模式、Required Fact、Distortion、Token/duration 诊断和 Variant comparison，不声明 Compression 确实发生。

[`memory_compression_capability_negative_v1.yaml`](../examples/memory_compression_capability_negative_v1.yaml) 单独声明至少一次可观察 Compression、压缩后事实 survival，以及完整禁用紧急 Compression；当前预期是在 Sandbox 前明确失败。

精简 T4 必须验证：新旧模式严格拒绝、MODEL 四级优先级与空串、Worker/identity/runtime 一致性、不同模型结构不可比、overflow 语义、两类 negative preflight、默认 Suite 不受负例阻断、四种 Memory、Variant/Sandbox 隔离、Required Fact/loss/Distortion/Checkpoint、四维可比性、Token 来源/scope/model-call count、Langfuse no-content 与 P0–P3 无 ablation 回归。真实 Trial、模型、Judge、Langfuse 和 Dataset 同步只在 T4 执行。
