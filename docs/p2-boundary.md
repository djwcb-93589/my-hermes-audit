# P2 边界

P2 在现有 P1 隔离 Runner 之后增加两个父进程适配层：LLM Judge 与 Langfuse。执行顺序是 Suite 静态合同 → Subject Capability Probe → 隔离 Worker → 确定性 Validator → Judge → 本地 `AuditRunResult` → Langfuse 后发布。MyHermes 仍只由 Worker 子进程导入。

## 依赖方向

核心合同、Dataset planner、Validator、Judge service 与报告层不导入 `openai` 或第三方 Langfuse SDK。`ports/judge.py` 和 `ports/langfuse.py` 只暴露 Audit 自有 Protocol、数据类和合同；SDK 延迟导入局限于 `integrations/` 适配层。

普通本地运行不需要 P2 extras。`--judge` 才创建 Judge adapter；`--langfuse` 才执行 Langfuse capability check 并创建 adapter。两者都只存在于父进程，其凭据不在 Worker 继承白名单中。没有 `--langfuse` 时不创建发布清单，也不改变 P1/P2 本地执行语义。

## Trial 事实与门禁

`TrialResult.task_passed` 保留 Worker 完成且 required deterministic gate 成功这一原始任务事实。required tool trajectory 和 required Judge 继续参与 `TrialResult.passed`，但不会覆盖 `task_passed`。非 completed metric 不伪造零分：未启用、不可评价或 evaluator 错误均保留各自状态，只有 completed 结果才具有正常数值语义。

## Langfuse 后发布边界

显式 `--langfuse` 在首个 Trial 前检查配置、SDK 最低版本、必要公开能力、连接、Dataset 与 Experiment 策略；不兼容会在远端写入前明确失败。`begin_experiment()` 建立本地 pending 清单，不依赖第一条 Trial 隐式初始化。

所有本地 Trial、Validator 和可选 Judge 完成后，适配层才调用官方 Experiment Runner。Runner task 只能把现有 `TrialResult` 映射为安全的 `ReplayTrialPayload` 和 Observation，不持有 MyHermes runner 或 Judge service，因此不能二次执行 Agent、模型、工具、Validator 或 Judge。

Trace、Experiment 关联、Score、flush 或 shutdown 出错会转换成脱敏 Audit 异常。每项清单区分 pending、publishing、confirmed、uncertain 和 failed；整体区分 pending、publishing、published、partially published 和 failed。最终 JSON 保留全部本地事实，显式集成错误使 CLI 返回非零，部分结果不会显示成完整成功。

## 生命周期

一个 adapter 实例只允许一个活动 Experiment。`finish_experiment()` 后拒绝继续发布；它只接受官方 Runner 返回且相互一致的 Dataset Run ID 与 URL，不猜测或拼接身份。每个 Score 写入后执行公开 `flush()`，run 结束再执行统一 `flush()` 和 `shutdown()`。

## 时间与顺序语义

P2 是执行后的观测发布。公共 MyHermes 投影未为全部 Model/Tool Observation 提供可靠历史开始时间，因此 span 使用发布时生命周期，并把真实 duration 和可用 Turn 时间放入 metadata；`post_hoc_publication` 与 `runtime_timestamps_not_replayed` 显式标记这一点。公共投影也没有跨 Model/Tool 的统一全序，P2 只保留各类型内顺序和 run/parent-run 关联。

Score 使用 Trial 完成时间或首次持久化时间，不在重试时生成新时间。官方 flush 正常返回只代表 SDK 已完成交付步骤，查询侧可见性仍可能延迟。

## 明确不在 P2.1

P2.1 只修复 Langfuse 兼容性、Experiment 关联和 Score 幂等。它不新增模拟用户、Memory Retrieval、Dense/BM25/Hybrid、Compression 消融、Background Review 评测、Baseline Compare、并行执行、CI、新 Judge 指标或自定义前端 Dashboard，也不向 MyHermes 增加 Audit 专用路径。P2.1 仅定义后续恢复所需的清单结构，不提供完整 resume CLI。
