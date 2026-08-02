# Deterministic Validator

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
