# Worker 文件协议

Worker 不使用 stdout 传结构化结果。每个 Trial 的 `artifacts/` 固定包含：

- `worker-request.json`
- `worker-result.json`
- `transcript.json`
- `observations.json`
- `validator-results.json`
- `worker.stdout.log`
- `worker.stderr.log`

请求和结果都使用严格 Pydantic 合同、明确的协议版本、未知字段拒绝、非负计数与有限数值。请求描述 Trial/Case、固定 turns、隔离路径、显式 toolset、超时和全部目标 Artifact；它不携带环境快照或凭据。

结果只保存安全运行投影：状态、逐 turn 输出、run ID、有限的计数/token/duration、Artifact 相对路径、稳定错误类别与安全摘要。它不序列化 MyHermes 对象、完整 Prompt、模型隐藏推理、完整工具参数或完整工具结果。

JSON 先写入同目录随机临时文件，再用 `os.replace` 原子发布。Worker 已获得可信请求时，会尽力在成功和运行失败时都写 envelope；若进程在可信边界建立前崩溃或没有结果，父进程生成 `environment_error` 兜底结果。stdout/stderr 独立、有大小上限、保留头尾并标记截断。

协议中的 completed 必须没有 error；failed 必须有一致的 `error_type` 和 error。timeout 由父进程映射为稳定的 Trial timeout，不伪装成 completed。
