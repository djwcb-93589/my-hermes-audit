# P2 边界

P2 在现有 P1 隔离 Runner 之后增加两个父进程适配层：LLM Judge 与 Langfuse。执行顺序是 Suite 静态合同 → Subject Capability Probe → 隔离 Worker → 确定性 Validator → Judge → 本地 `AuditRunResult` → Langfuse 后发布。MyHermes 仍只由 Worker 子进程导入。

## 依赖方向

核心合同、Dataset planner、Validator、Judge service 与报告层不导入 `openai` 或 `langfuse`。`ports/judge.py` 和 `ports/langfuse.py` 只暴露 Audit 自己的 Protocol、数据类和合同；第三方 SDK 的延迟导入局限于 `integrations/judge/` 与 `integrations/langfuse/`。

普通本地 P1 运行不需要 P2 extras。`--judge` 才创建 Judge adapter；`--langfuse` 才创建一个 Langfuse adapter。两者都只存在于父进程，其环境变量不在 Worker 继承白名单中。

## Trial 事实与门禁

`TrialResult.task_passed` 保留 Worker 完成且 required deterministic gate 成功这一原始任务事实。required tool trajectory 和 required Judge 继续参与 `TrialResult.passed`，但不会覆盖 `task_passed`。因此报告能区分任务失败、工具门禁失败和 Judge 失败。

非 completed metric 不伪造零分：

- Worker 没有可评价输出时，`answer_quality` 为 `not_applicable`；
- 可选 Judge 未启用时为 `skipped`；
- Judge 自身失败时为 `error` 并携带结构化错误；
- 只有 `completed` 才有 `value` 和正常 `passed` 语义。

required Judge 的 `error` 或低于阈值会令 Trial 不通过；非 required Judge 的错误和低分不覆盖本地任务结果。

## Langfuse 故障边界

显式 `--langfuse` 在首个 Trial 前检查配置和连接，并完成 Dataset ensure。初始化、认证或同步失败直接终止，不能静默降级。Trial 开始后的 Trace、Dataset Run Item、Score、flush 或 shutdown 失败会转换为脱敏 `LangfusePublishError`，后续 Trial 仍进行本地执行。最终 JSON 保留全部本地事实，CLI 因 integration error 返回非零。

每次 CLI run 只创建一个 adapter。所有 Trial 后统一 `finish_experiment()`、`flush()`、`shutdown()`；Worker 不初始化 SDK，也不接收 Langfuse 凭据。

## 明确不在 P2

P2 不实现模拟用户、Memory Retrieval、Dense/BM25/Hybrid、Compression 消融、Background Review 评测、Baseline Compare、并行执行、CI 或 Langfuse 自定义前端 Dashboard。P2 也不向 MyHermes 增加任何 Audit 专用路径。

## 时间语义

P2 是执行后的观测发布。公共 MyHermes 投影提供真实 duration，但未为所有 Model/Tool Observation 提供可靠的历史开始时间，因此 SDK span 使用发布时生命周期，并把真实 duration 和可用的 Turn 时间放进 metadata；`post_hoc_publication` 与 `runtime_timestamps_not_replayed` 明确标记这一点。实现不伪造历史瀑布图。

公共投影还没有统一的跨 Model/Tool 全序号，因此 P2 只保留各类型内顺序、run/parent_run 关联并显式记录 `runtime_cross_type_order_available=false`。`sync --dry-run` 不连接远端，所以无法区分远端 add/update/unchanged；三项计数显示 unknown。低层 Dataset Run Item API 没有返回可靠 Experiment URL，P2 只保存 remote run ID，不推测链接。Experiment name 由调用方负责唯一命名；重复名称会遵循 Langfuse Dataset Run 的复用语义。同步不会追溯删除旧 Case 版本，因此后来收紧分类不会抹除此前已发布的历史 Item；需要远端数据治理时应在独立阶段处理。
