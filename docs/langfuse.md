# Langfuse 集成

## P4 本地事实回放

P4 Trace 使用 `p4` version，并回放 Variant metadata、Compression/context diagnostics、checkpoint、required-fact retention、distortion 和 Case comparison Observation。发布请求携带本地 `AblationComparisonResult`；mapper 不重新运行 Agent、Compression、Validator 或 Judge。

事实 projection 永远省略 canonical/actual 正文，仅保留 SHA-256 与长度。`--langfuse-no-content` 或 sensitive 分类还会省略 turn、final answer、Memory、User Profile 和 query正文。Score mapper没有新增名称，仍只有 `task_success`、`tool_correctness`、`answer_quality`。详见 [P4 文档](p4-memory-compression-ablation.md)。

当前支持 Langfuse Python SDK `>=4.14.2,<5`。项目下限要求同时具备适配器使用的 v4 Score 创建与精确查询公开表面；运行时还会逐项检查本项目实际使用的公开方法及参数，版本号本身不是充分条件。

## 配置与预检

安装：

```bash
python -m pip install -e ".[langfuse]"
```

环境变量：

- `LANGFUSE_PUBLIC_KEY`，必填；
- `LANGFUSE_SECRET_KEY`，必填；
- `LANGFUSE_BASE_URL`，可选且优先；
- `LANGFUSE_HOST`，只作为既有环境变量名兼容；
- `LANGFUSE_TIMEOUT`，可选的 1～600 秒整数。

Host 必须是无认证信息、query 或 fragment 的 HTTP(S) URL。Suite、报告和 Artifact 不保存密钥。没有 `--langfuse` 时不会导入第三方 SDK、创建 client 或写发布清单。

显式发布在首个 Trial 前完成依赖、精确版本、公开能力、凭据、轻量认证和 Dataset 检查。`doctor --check-langfuse` 只检查版本、公开方法签名和 client 配置，默认不联网，也不创建任何远端资源。能力明细见 [兼容性合同](langfuse-compatibility.md)。

## Dataset

一个 Suite 对应一个 Dataset。非 P4 Case 内容版本仍对应一个 Dataset Item，Item ID 保持 `dataset_name + suite_id + case_id + case_sha256` 的稳定 UUID5；P4 在其中加入显式 `variant_id`，使每个 Case/Variant 独占 Item。公开 `create_dataset_item(..., id=...)` 提供同 ID 写入语义；Case hash 与发布投影 hash 都不变时不写，投影变化时更新同一版本，Case 内容变化时创建新的稳定版本。历史 Item 和历史 Experiment 保留，没有 prune 或删除命令。

Item input、expected output 和 metadata 均先经过数据分类投影。Fixture、Memory、Skill 与数据库正文不上传。`sync --dry-run` 只生成本地计划，不导入 SDK、不连接远端；因此远端新增、更新、不变计数保持 unknown。

## Experiment 与 Trace

采用官方公开 [`Langfuse.run_experiment`](https://python.reference.langfuse.com/langfuse#Langfuse.run_experiment)，输入为已经同步的 Langfuse Dataset Item。每个 Audit run 使用精确、稳定的 `run_name = experiment_name::audit_run_id`。公开 Runner 自动创建 Trace 并将 Dataset Item、Experiment Item 和 Trace 关联；适配层立即把 `ExperimentResult` 映射成 Audit 自有 receipt，只接受 SDK 实际返回的 Dataset Run ID 和 URL。

Runner 的 task 是纯回放函数：它只读取已经完成且已脱敏的 `TrialResult`，返回 `ReplayTrialPayload`，并在 Runner 当前 Trace 下补充本地观测投影。载荷包含 Audit/Trial/Case/Dataset Item 身份、既有 final output、安全指标摘要、本地 Trace ID、runtime 状态和 Artifact 摘要。它没有 runner、worker、agent、model、tool、validator 或 judge 依赖，不能修改本地结果。

全部本地 Trial 完成后才开始远端发布。调用顺序为：

1. `begin_experiment()` 校验远端 Dataset Item，并原子写入本地 pending 清单；
2. `publish_trial()` 对单个既有结果执行纯回放，验证 Runner 返回的 Dataset Item、Trace 和 Dataset Run 一致；
3. `publish_scores()` 只把本地已计算的三个一级指标写到该 Trace；
4. `finish_experiment()` 验证所有已确认 receipt 指向同一个真实 Dataset Run，并返回 SDK 提供的身份和 URL；
5. `flush()` 与 `shutdown()` 完成公开生命周期。

重复的单项 Runner 调用使用同一精确 run name；若 SDK 返回不同 Dataset Run ID，发布立即转为关联错误，不会把多个远端 run 拼成一个本地成功结果。适配层不直接访问自动生成客户端资源，不手写旧端点，也没有旧协议 fallback。

Dataset-backed 发布仍要求每个 Case/Variant 恰好一个 Trial；普通本地多 Trial 运行保持不变。旧 Case 对应一个 Dataset Item，P4 Case 则按显式 Variant 建立稳定、互不复用的 Dataset Item identity，使每个 Variant 获得独立 Experiment Item/Trace。若 `defaults.trials` 大于 1，远端发布在本地结果持久化后明确拒绝，不影响本地消融结果。

## Observation 投影

`TrialResult` 映射为 Runner task Trace 的子树：Trial span、scripted turn、公共 Model/Tool Observation、Validator evaluator，以及实际完成的 Judge generation。P3 Memory Case 还增加 seed、query、before/after snapshot 和 retrieval evaluator Observation；它们只是本地事实回放，不执行第二次查询或评测。未执行的 Judge 不伪造 generation。真实 runtime duration 和可用时间只进入 metadata；发布发生时创建的 span 不伪装成历史瀑布图。

Trace metadata 保存 Audit/Suite/Case/Trial 身份、subject/audit commit、模型标识（安全可得时）、数据分类、runtime 状态和效率摘要。P4 另保存经过白名单约束的公开有效配置投影；它不保存基础 config、凭据、绝对路径、隐藏 Prompt、隐藏推理或工具正文。

Memory Observation 在允许内容的 `synthetic`/`internal` 模式下仍先经过统一脱敏与长度限制。`--langfuse-no-content` 时，每个 query/Memory item 只保留 SHA-256、长度/字节数、kind、rank、required hit、duration 与安全 metadata；不上传 Memory、User Profile、Fixture 或 query 正文。`sensitive` 分类无条件使用相同省略规则，即使没有传 no-content。Snapshot 不包含路径，跨 Session 只发布 Suite 的逻辑 ID，不发布 Subject Session ID。

Dataset 规划对 P3 只增加逻辑 Session 与严格 Memory query/state expectation 投影，继续只发布 Fixture 指纹摘要并固定记录 `memory_fixture_uploaded=false`；它不导入 Subject，也不执行查询。相同的 no-content/sensitive 规则在远端写入前生效。

## Score 与发布清单

只发布 `task_success`、`tool_correctness`、`answer_quality`。P3 required evidence、Memory state、Recall@K 和 MRR 只作为 Observation/本地 Metric，不增加 Score。Score ID 由 `trace_id + score_name + evaluator_version + trial_id` 的 SHA-256 派生；同一身份固定使用首次持久化的 timestamp 和 `value_hash`。Langfuse 官方 [Score 数据模型](https://langfuse.com/docs/evaluation/scores/data-model) 明确允许自定义 Score ID 作为幂等键更新同一个 Score。

公开 `create_score(..., score_id=..., timestamp=...)` 后立即 `flush()`。只有调用与 flush 都正常返回才记为 confirmed；超时或连接中断后无法判断远端结果时记为 uncertain。相同身份但不同 `value_hash` 会报冲突，不能静默覆盖。完整规则见 [发布幂等合同](publication-idempotency.md)。

## 事实来源与可见性

`AuditRunResult` 始终是任务门禁、Judge 结果、效率数据和集成错误的唯一事实来源。Langfuse 只承担 Dataset 管理、Experiment/Trace 展示和 Score 分析，远端不会重新计算本地指标。

官方 `flush()` 保证把待处理事件交给 API，但查询侧可能存在摄取延迟；P2.1 不做立即远端反查，也不把短暂不可查询等同于失败。这里的 confirmed 表示公开 SDK 写入调用和 flush 已正常返回，不表示 UI 已立即可见。本阶段没有连接真实 Langfuse 服务进行远端行为验证。
