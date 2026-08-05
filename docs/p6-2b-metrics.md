# P6.2B 统一 Agent 指标

P6.2B 只汇总已经由 Worker 和 MyHermes 公共 Observation 暴露的事实，不
解析最终回答、日志正文或私有数据库。`conversation_turn_count` 是
`TrialResult.turns` 的轮次数；`agent_iterations` 是
`TrialRuntimeSummary.iterations`，二者不混用。耗时使用 Trial 的真实
`duration_ms`，P50/P95 继续采用 nearest-rank；缺失样本不会按零填充。

任务成功率的分母是有明确 `task_passed` 的 Trial，工具正确率只使用已完成
的 required Tool Trajectory Metric。Memory 的 required evidence、Recall@K
和 MRR 复用既有 Retrieval evaluator；Background Review 首页指标只使用
已完成的 `decision_correctness` Metric。Judge 仍是可选诊断，不替代确定性
事实。

failure 统计 `FAILED` 或已完成但未通过的 Trial；timeout、environment
error 和 cancelled 独立统计，不重复计入 failure。所有运行状态率以全部
Trial 为分母；空 Suite 不产生 NaN。

DeepSeek 命中率是 token 加权的
`hit / (hit + miss)`，不是 Trial 或模型调用命中率的简单平均。状态含义为：

- `available`：每个模型调用均有合法 hit/miss；
- `partial`：至少一个合法观察，但仍有缺失调用或未评估 Trial；
- `not_evaluated`：没有合法缓存观察；
- `invalid`：存在非法成对、类型、非负或加总关系。

字段缺失不是 0%；`invalid` 不输出命中总数或命中率。覆盖率同时给出模型
调用和 Trial 两个分母。当前阶段不计算成本、缓存节省金额、Baseline、
GLM/火山引擎缓存或通用供应商适配。Langfuse 只接收这些结构化数值、状态
和覆盖信息；`--langfuse-no-content` 仍不上传 Prompt、模型输出、Memory、
Review 正文、凭据或原始响应。
