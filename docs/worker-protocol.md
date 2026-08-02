# Worker 文件协议

Worker 不使用 stdout 传结构化结果。每个 Trial 的 `artifacts/` 固定包含：

- `worker-request.json`
- `worker-result.json`
- `transcript.json`
- `observations.json`
- `validator-results.json`
- `worker.stdout.log`
- `worker.stderr.log`
- P3 Case 才有的 `memory.json`

请求和结果都使用严格 Pydantic 合同、明确的协议版本、未知字段拒绝、非负计数与有限数值。P3 因 turns 增加逻辑 `session_id`，并增加 strategy、Memory Fixture、稳定 query plan 与 Memory Artifact，协议从 `myhermes-audit-worker-v1` 显式升级为 `myhermes-audit-worker-v2`，没有静默复用旧 envelope。请求不携带环境快照或凭据。

结果只保存安全运行投影：状态、逐 turn 输出、run ID、有限的计数/token/duration、Artifact 相对路径、稳定错误类别与安全摘要。它不序列化 MyHermes 对象、完整 Prompt、模型隐藏推理、完整工具参数或完整工具结果。

P3 结果另外保存严格 `memory_query_results`、before/after `memory_snapshots`、`memory_state_changes` 和 `memory_errors`；`memory.json` 必须与 WorkerResult 的这些字段逐项一致。查询 provider/strategy/phase、连续 rank、非负 duration 和稳定 ID 在合同层校验。非 Memory Case 的 request/result 不允许夹带 Memory Artifact 或事实，因此旧 P1/P2 执行不会加载 Adapter。

同一 Trial 的 turn 可映射多个公开 MyHermes Session；WorkerResult 只保存 Suite 声明的逻辑 Session ID，不泄露 Subject 随机 Session ID。Worker 每轮按策略重建 Prompt，并在关闭连接前 best-effort 清理所有公开 Session resource。

JSON 先写入同目录随机临时文件，再用 `os.replace` 原子发布。Worker 已获得可信请求时，会尽力在成功和运行失败时都写 envelope；若进程在可信边界建立前崩溃或没有结果，父进程生成 `environment_error` 兜底结果。stdout/stderr 独立、有大小上限、保留头尾并标记截断。

协议中的 completed 必须没有 error；failed 必须有一致的 `error_type` 和 error。timeout 由父进程映射为稳定的 Trial timeout，不伪装成 completed。

Memory query/snapshot 等后置评测错误保留为 `MemoryOperationError`，由 required retrieval evaluator 产生 `ERROR` 门禁；已经完成的对话事实不会被伪造为空成功。seed/capability 阻断会使用稳定 Memory error type 终止 Worker。clear 是 best effort：失败记录 `memory_clear_error`，但不覆盖更早的主要失败，最终仍由 Sandbox 所有权清理隔离状态。

Worker 在 clear 前先原子写入一份 Memory Artifact checkpoint，随后执行 best-effort clear，并以实际 `clear_attempted` / `clear_succeeded` 状态原子更新同一 Artifact。这样即使清理或后续 envelope 构建失败，父进程仍可恢复已经形成的安全查询、快照、diff 与错误事实。
