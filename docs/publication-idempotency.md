# Langfuse 发布幂等合同

## Score identity

每个可发布分数先生成 Audit 自有 `ScorePublicationIdentity`：

- `score_id = SHA-256(trace_id + separator + score_name + separator + evaluator_version + separator + trial_id)`；
- `trace_id`、`score_name`、`evaluator_version`、`trial_id`、`case_id`；
- `stable_timestamp`；
- `value_hash`。

不使用随机 UUID。Langfuse 官方 [Score 数据模型](https://langfuse.com/docs/evaluation/scores/data-model) 说明自定义 Score ID 可作为幂等键更新同一 Score；适配层通过公开 `create_score` 传入稳定 `score_id`。

## 稳定时间戳与值摘要

时间戳优先取 `TrialResult.finished_at`；若本地合同没有完成时间，则在首次创建 identity 时生成一次并立即持久化。重试从清单读取同一值，绝不生成新的当前时间。

`value_hash` 是 name、value、source、evaluator version、comment 与安全 metadata 的 canonical JSON SHA-256。相同 Score ID 出现不同 identity 字段会报 `ScoreIdentityError`；identity 相同但 `value_hash` 不同会报 `ScorePublicationConflictError`，并要求提升 evaluator version。不会捕获“已存在”异常后假装成功，也不会扫描全部远端 Score 做模糊去重。

## Publication Manifest

每个 Audit run 在报告旁创建一个唯一的 `*.langfuse-manifest.json`，核心字段包括：

- `audit_run_id`、`experiment_name`、`dataset_name`；
- `created_at`、`updated_at`；
- `trial_publications` 与 `score_publications`；
- `remote_ids` 与 `stable_timestamps`；
- `status` 与脱敏 `last_error`。

Trial 记录包含本地 Trial/Case/Dataset Item/Trace 身份、尝试次数、远端 Trace/Observation/Dataset Run 身份和错误。Score 记录包含完整 `ScorePublicationIdentity`、尝试次数、remote ID、最后尝试时间、确认时间和错误。每次状态转换都通过同目录临时文件和原子替换写入，文件不包含凭据或完整内容。

## 状态与重试

单项状态为 pending、publishing、confirmed、uncertain 或 failed：

- confirmed：同一进程再次发布时跳过；
- pending/failed：可以使用原 identity 重试；
- uncertain：只能使用完全相同的 identity 重试，或在后续恢复阶段通过正式查询确认；
- identity/value 冲突：保持错误，不写远端。

网络调用开始前失败记为 failed；调用已经开始后发生超时、连接中断或 flush 异常时，无法证明远端未写入，因此记为 uncertain。它不会被算作 confirmed，也不会生成新 Score ID 或 timestamp。整体状态按 confirmed 与未解决项区分 published、partially published 和 failed。

## 确认边界

公开 `create_score` 正常返回后立即调用 `flush()`；两步均正常返回才把本地记录标为 confirmed。官方 [`flush`](https://python.reference.langfuse.com/langfuse#Langfuse.flush) 文档说明它会强制交付待处理事件，但查询侧仍可能存在摄取延迟，所以 P2.1 不立即反查并把不可见误报为失败。confirmed 是 SDK 交付边界，不是 UI 即时可见承诺。

## 后续恢复扩展点

P2.1 提供 `PublicationManifestStore.read()` 和完整稳定身份，但不新增 resume CLI。后续独立阶段可以读取旧清单，先处理 publishing/uncertain 状态，再按原 identity 重放；若官方提供适合的精确查询，可先确认远端。恢复逻辑不得重新执行 MyHermes、Validator 或 Judge，也不得用新时间戳掩盖未决写入。
