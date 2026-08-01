# Langfuse 兼容性合同

## 支持范围

支持的 Python SDK 范围是 `langfuse>=4.7.0,<5`，且只接受最终语义版本。Langfuse 当前 [兼容性矩阵](https://langfuse.com/docs/compatibility) 将 Python SDK v4 列为 GA，并指出 4.7.0 是 v4 数据模型实时 Trace 行为的最低版本。上界固定在下一主版本之前，避免在未知破坏性变更上继续写远端。

版本范围只是第一道门。`LangfuseCapabilityReport` 还使用公开对象的可检查签名确认：client 初始化、Dataset 读取/创建、带稳定 ID 的 Dataset Item 写入、Experiment Runner 与 Item 关联、当前 Trace/Observation 身份、子 Observation、属性传播、带稳定 ID/timestamp 的 Score 写入、flush、shutdown 和认证检查。

## 正式公共 API

适配层只调用文档公开的 `Langfuse` client 方法：

- `get_dataset`、`create_dataset`、`create_dataset_item`；
- [`run_experiment`](https://python.reference.langfuse.com/langfuse#Langfuse.run_experiment)；
- `start_as_current_observation`、公开 span 的 `start_observation`、`get_current_trace_id`、`get_current_observation_id` 与模块级 `propagate_attributes`；
- `create_score`、`flush`、`shutdown`、`auth_check`。

业务代码不访问自动生成客户端资源，不调用私有 transport 或下划线成员，不手写远端 REST 路径，也不按猜测的方法名做版本 fallback。SDK 返回对象会在适配层内立即投影成 Audit 自有合同。

## Experiment 策略

当前采用官方 Experiment Runner，而不是直接构造 Experiment Item。Runner 接收已同步的 Dataset Item 和纯回放 task，并正式建立 Dataset Item、Experiment Item、Trace 与 Dataset Run 的关联。`name` 是用户提供的 Experiment 名；`run_name` 固定为 `experiment_name::audit_run_id`。SDK 公开结果中的 item、Trace ID、Dataset Run ID 与 URL 必须逐项一致，否则视为结构化关联错误。

Dataset-backed Runner 在一个 run 内以 Dataset Item 作为 Experiment Item 身份。为避免多个 Trial 争用同一个远端 Item，P2.1 的显式 Langfuse 发布要求每个 Case 恰好一个 Trial，并在启动 Worker 前拒绝其他配置；无 `--langfuse` 的本地多 Trial 能力不受影响。未来若官方公开一对多 Item 身份，应在独立阶段扩展合同。

没有采用“后置挂接任意已有远端 Trace”的方案：当前公开高层 client 没有供业务代码直接完成这种挂接的方法。回放会新建一个用于展示的远端 Trace，但不会重新执行被测 Trial；真实运行耗时和顺序作为 post-hoc metadata 保存。

## 为什么不会重新执行 MyHermes

`ReplayTrialPayload` 只由不可变的 `LangfuseTrialRequest.trial` 和安全投影构造。Runner callback 只能返回既有 final output、runtime 状态、指标摘要、Artifact 摘要和本地关联 ID，并创建展示用 Observation。它没有 `TrialRunnerPort`、MyHermes Worker、AgentLoop、模型/工具 client、Validator 或 `JudgeService` 引用。编排器也在所有本地 Trial 完成后才进入发布循环。

## 已移除的旧路径

旧的低层 Dataset/Experiment 关联资源调用已从适配层和文档删除；没有保留兼容 fallback。正式 Runner 若在安装版本中缺失或签名不满足当前合同，capability check 会在 client 远端写入前失败，而不是尝试旧方法。

## 不兼容行为

未安装、低于 4.7.0、达到 5.0.0、预发布版本或缺少必要公开能力时，显式 `--langfuse` 抛出 Audit 自有依赖/能力异常并返回非零。`doctor --check-langfuse` 报告安装版本、最低版本、兼容性、Experiment 策略、Score 幂等策略、各能力和缺失项；它只初始化并关闭 client，不做连接或资源创建。普通无 Langfuse 路径仍可运行。

本阶段只做官方资料与公开接口的代码级兼容建设，没有连接真实服务验证 Cloud 或自托管部署差异。自托管用户还应按官方矩阵确认 server 与 SDK 的组合。
