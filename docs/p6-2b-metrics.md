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

DeepSeek 命中率是已评估 prompt Token 加权的
`hit / deepseek_cache_evaluated_prompt_tokens`，等价于
`hit / (hit + miss)`，不是 Trial 或模型调用命中率的简单平均。完整
`prompt_tokens` 仍表示 Trial 全部模型调用；partial 状态只统计合法缓存
调用的 evaluated prompt Token。状态含义为：

- `available`：每个模型调用均有合法 hit/miss；
- `partial`：至少一个合法观察，但仍有缺失调用或未评估 Trial；
- `not_evaluated`：没有合法缓存观察；
- `invalid`：存在非法成对、类型、非负或加总关系。

字段缺失不是 0%；`invalid` 不输出命中总数、evaluated prompt Token 或命中率。
模型调用覆盖率的分母是全部模型调用；Trial 覆盖率的分母是至少有一次模型
调用的 Trial 数，未产生模型调用的 Trial 不会伪装成已评估。当前阶段不计算成本、缓存节省金额、Baseline、
GLM/火山引擎缓存或通用供应商适配。Langfuse 只接收这些结构化数值、状态
和覆盖信息；`--langfuse-no-content` 仍不上传 Prompt、模型输出、Memory、
Review 正文、凭据或原始响应。

Case 与 Suite 的 `DeepSeekCacheSummary` 使用同一份 Trial 聚合语义；Case
保留既有状态和命中率投影，并机械投影 hit、miss、evaluated prompt Token
及两类覆盖率，同时通过嵌套汇总保留完整计数事实。
