# P1 Validator

P1 只做可重复的客观检查，不把文本规则包装成语义 Judge。

- `FileValidator`：存在/不存在、UTF-8 exact/contains/not-contains、SHA-256、大小上下限。
- `TextValidator`：最终输出的 exact/contains/not-contains/受限 regex，可配置大小写。
- `JsonFileValidator`：严格 JSON、根类型、required/forbidden key、简单 dotted path/list index、严格类型值比较。
- `ToolTrajectoryValidator`：基于公共 Observation 投影检查 required/forbidden tool、调用数上下限、某工具至少成功一次。

每条 expectation 生成一个独立 `MetricResult`。`deterministic` evaluator 用 `config.expectation_group` 选择 `all`、`files`、`texts` 或 `json_values`；`tool_trajectory` evaluator 使用空 config 并覆盖当前 Case 的全部工具轨迹预期。未绑定的 expectation、未知 config、空选择或 P1 外 evaluator 会在任何 Worker 启动前被拒绝。

regex 限制模式与输入长度，只接受不含分组和无界重复的保守子集，并拒绝 lookaround 与反向引用。JSON 不执行 JSONPath、Python 或用户表达式。工具轨迹不解析 Assistant 文本，也不强制精确参数或完整调用顺序；Observation 缺失或截断会形成 evaluator error。

Case 指标按 metric name 汇总。运行级 duration P50/P95 使用 nearest-rank：将毫秒升序排列并取 `ceil(q*n)-1`；单样本返回该样本，无样本显示 `not evaluated`。未知 token 或工具正确率也显示 `not evaluated`，不填零分。
