# Deterministic Validator

## P6.1 scenario evaluator

The `scenario` evaluator consumes only Worker-produced Toolchain and Process
observations. It emits independent Process metrics for command identity,
process identity, input identity, action, character cursor integrity, marker
expectations, status transitions, step/scenario timeout, Agent close, Worker
cleanup, and typed checkpoints. Required scenario metrics are hard gates;
the aggregate is exposed as `toolchain_gate_passed` or
`process_gate_passed` and flows into the existing task gate. No fourth
first-level score is introduced, and missing observations are errors rather
than fabricated success.

Toolchain checkpoint diagnostics remain separate: `artifact_missing`,
`toolchain_artifact_read_error`, `required_marker_missing`,
`forbidden_marker_present`, and `minimum_length_not_met`. Process diagnostics
distinguish `cursor_reference_missing` from `cursor_chain_mismatch`; both are
hard-gate facts when declared by a required scenario.

## P5 Background Review

`background_review` evaluator 只消费 Worker 已持久化的 `BackgroundReviewExecutionResult`、安全 evidence projection、live snapshot 和 observed state diff；它不导入 MyHermes、不重新运行 Review、模型或工具，也不根据 Assistant 文本猜测状态变化。

每个 `BackgroundReviewExpectation` 生成六个独立的结构化 Metric：

1. `decision_correctness`：terminal status、严格 expected action 或 allowed action set、no-op、目标和声明的 stale 语义；
2. `evidence_completeness`：required/forbidden kind 是否实际进入 `prepare_run()` 的 Subject prepared window，以及顺序和前台来源关联；
3. `update_correctness`：live `observed_changes` 是否满足 `must_change`、`must_not_change`、目标与 revision；
4. `stale_rejection`：仅在声明 stale 时，公开事实是否表明 stale、零写入和 reject；
5. `side_effect_safety`：protected/non-target 状态、failed/rejected/stale/no-op 的意外半写入；
6. `idempotency`：duplicate lifecycle 的第二次 attempt 没有 loop、模型、工具或状态副作用。

缺少执行结果或结构化 execution error 时，required 维度为 `ERROR`/`value=null`，不伪造成零分。所有 required Review evaluator 通过才令 `review_gate_passed=true`；任一失败或 error 令它为 `false`，并使 `task_success`/`task_passed` 为 false。没有 required Review evaluator 时 gate 为 `null`。Review 指标仅用于诊断与硬门禁；一级 Score 仍只有 `task_success`、`tool_correctness`、`answer_quality`。

### 动作合同

`expected_action` 要求 `actual_action` 精确相等；`allowed_actions` 要求 `actual_action` 属于声明的非空、去重动作集合。一个 expectation 必须且只能声明其中之一，集合按规范化顺序进入 Suite fingerprint。Metric 明确记录实际动作、适用的严格动作或允许集合，以及 `action_matched`；不会把 `no_op` 重写成 `reject`，也不会反向改写。

动作通过不替代其余五个维度。默认 protected Skill Case 接受 `no_op` 或 `reject`，但仍以零修改、protected/non-target snapshot 和无半写入为硬约束。显式 `reject` 只有在 Review 模型实际尝试受治理工具调用时才会产生。duplicate Case 的第一次执行不要求一定更新：它可 no-op 或仅替换声明的目标；无论第一次动作是什么，第二次都必须保留 `claim_valid=false`、`loop_executed=false`、零模型/工具/状态变化、`duplicate_rejected=true` 和 `attempt_count=2`。相反，verified-update Case 仍严格要求 `replace`；在完整失败→fallback→成功证据下返回 no-op 属于 Subject 能力失败。

## P4 required facts

`compression` evaluator 消费声明性 `RequiredFactExpectation`，按 Variant 的 `applicable_variant_ids` 选择事实，并从 Subject context Observation、P3 after snapshot 或 final answer 中做 exact/normalized/contains 匹配。它同时产生结构化 checkpoint、fact retention、required-fact loss 和 distortion；证据只含 fact ID、SHA-256、长度和状态。

required fact 不可观察时 Metric 为 error/value null，不填 0。required expectation、required checkpoint 和显式 distortion hard gate 会进入 `task_success`；optional 诊断错误不会自动失败任务。Judge 仍可评开放式答案，但不能替代事实门禁。完整定义见 [P4 文档](p4-memory-compression-ablation.md)。

P1 只做可重复的客观检查，不把文本规则包装成语义 Judge。

- `FileValidator`：存在/不存在、UTF-8 exact/contains/not-contains、SHA-256、大小上下限。
- `TextValidator`：最终输出的 exact/contains/not-contains/受限 regex，可配置大小写。
- `JsonFileValidator`：严格 JSON、根类型、required/forbidden key、简单 dotted path/list index、严格类型值比较。
- `ToolTrajectoryValidator`：基于公共 Observation 投影检查 required/forbidden tool、调用数上下限、某工具至少成功一次。
- P3 Memory evaluator：消费 Worker 已序列化的 query/snapshot/diff，计算 required/forbidden evidence、kind、Recall@K、MRR 与严格状态变化；不导入 MyHermes 或实现检索。

每条 expectation 生成独立 `MetricResult`。`deterministic` evaluator 用 `config.expectation_group` 选择 `all`、`files`、`texts` 或 `json_values`；`tool_trajectory` evaluator 使用空 config 并覆盖全部工具轨迹预期；`retrieval` evaluator 使用空 config 并覆盖全部 Memory query/state 预期。未绑定 expectation、未知 config、空选择或未实现 evaluator 会在任何 Worker 启动前拒绝。

Memory required evidence 是布尔硬门禁；Recall@K/MRR 无 required ID 时为 `not_applicable`，默认只是诊断。Memory 操作失败产生带稳定 error type 的 evaluator `ERROR`，不产生伪造 0。状态内容只支持 exact、contains、normalized_exact，`allow_other_changes=false` 会把未声明新增/删除/修改列为失败。

regex 限制模式与输入长度，只接受不含分组和无界重复的保守子集，并拒绝 lookaround 与反向引用。JSON 不执行 JSONPath、Python 或用户表达式。工具轨迹不解析 Assistant 文本，也不强制精确参数或完整调用顺序；Observation 缺失或截断会形成 evaluator error。

Case 指标按 metric name 汇总。运行级 duration P50/P95 使用 nearest-rank：将毫秒升序排列并取 `ceil(q*n)-1`；单样本返回该样本，无样本显示 `not evaluated`。未知 token 或工具正确率也显示 `not evaluated`，不填零分。
