# Audit Schema v1

## P4 合同扩展

Schema v1 新增严格 `MemoryMode`、`CompressionMode`（仅 `threshold_disabled` / `threshold_enabled`）、`AblationVariant`、`AblationPlan`、`EffectiveSubjectConfiguration`、`TrialIdentity`、required fact/checkpoint、Compression/context/token/duration diagnostics（含明确来源）和 `AblationComparisonResult`。比较结果分别记录 structural、token、answer-quality 与 duration comparability；所有对象继续拒绝未知字段，Variant override 仅允许白名单 `compression.*` 数字路径。

`AuditCase.ablation` 缺失时保持 P0–P3 形状。存在时，Variant/组合/checkpoint/fact ID 必须唯一，reference Variant 和所有 `applicable_variant_ids` 必须可解析，turn/checkpoint 与 Compression event 上下限必须一致。Suite canonical fingerprint自然包含完整计划；P4 Trial identity另含 Variant、Subject commit、配置 fingerprint 与按 Worker 优先级解析的模型标识。`TrialResult` 的 P4 身份字段必须全有或全无，`AuditRunResult.ablation_comparisons` 必须与本地 P4 Trial逐项对应。详见 [P4 文档](p4-memory-compression-ablation.md)。

输入 Suite 合同的 `schema_version` 仍为字符串 `"1.0"`；P6.2B 的顶层 `AuditRunResult` 使用严格的结果版本 `"1.1"`，以标记代表性效率指标和 DeepSeek 缓存汇总字段。每个公共 Pydantic 合同都继承该字段，顶层 `AuditSuite` 与 `AuditRunResult` 要求调用方显式提供各自版本。所有合同拒绝未知字段，并对可变默认值使用工厂。枚举值和 ID 使用稳定英文标识；时间必须带时区，进入合同后规范化为 UTC。

## AuditSuite 与 AuditCase

`AuditSuite` 包含 `suite_id`、名称、描述、标签、`TrialConfig defaults` 与至少一个 Case。Suite 内 `case_id` 唯一。

`AuditCase` 包含：

- `mode`：`single_turn`、`scripted_multi_turn` 或 `simulated_user`；
- `input`：分别且互斥地使用 `message`、固定 `turns` 或结构化 `simulated_user`；
- `execution`：P0 只允许 `runner: conversation`，另有安全相对 `workdir` 和窄作用域 overrides；
- `fixture`：文件、Memory、Skill、数据库引用与 Review request；
- `expected`：文件、文本、JSON、工具轨迹、Memory、Background Review 与 Judge 声明；
- `evaluators`：Case 内唯一的 `evaluator_id` 列表。

`environment_overrides` 的变量名必须有效，且不能声明 `HERMES_HOME`、`DB_PATH`、`HERMES_WORKSPACE` 或 `MYHERMES_AUDIT_ARTIFACTS_DIR`；这些隔离值由 Sandbox 与未来 runner 最终控制。

P3 为单轮 input 和 scripted user turn 增加可选逻辑 `session_id`；未声明时仍使用 Trial 默认 Session。`execution.memory_strategy` 仅在 Memory Case 显式声明，严格枚举为 `subject_native`、`disabled`、`dense`、`bm25`、`hybrid`。未声明 Memory 的旧 Case 保持 `null`，不会加载 Memory Adapter。

`EvaluatorSpec.required: true` 表示硬门禁；`false` 表示软评分，后者可声明 `weight`。`config` 只属于单个 evaluator，不能向 Case 顶层扩散。

## Fixture 路径

`FixtureFile.path` 必须使用 `/`，并位于 `workspace/` 或 `hermes_home/` 下。`source` 与 `content` 必须二选一。`source` 是相对 Suite YAML 目录的安全路径；loader 会解析并验证它没有逃逸，但不会检查文件存在性、读取内容或执行复制。

数据库 Fixture 在 P0 只是 `DatabaseFixtureReference`。它不是 MyHermes schema dump，也不会被 loader 打开。

## ExpectedSpec

预期按领域拆分，不使用单一 `expected_behavior` 文本：

- `files`：存在性、SHA-256 和包含文本；
- `texts`：final output、artifact、file 或 tool output 的文本匹配；
- `json_values`：exact/subset 声明；
- `tool_trajectories`：顺序、工具名与参数；
- `memories`：稳定 query ID、before/after phase、MemoryQuery、required/forbidden ID、runtime-generated ID、kind、最少命中数和可选 Recall/MRR 阈值；
- `memory_states`：present/absent、added/forbidden content、removed、unchanged 与额外变化策略；
- `background_reviews`：action、目标、变更保护、证据来源和 stale rejection；
- `judges`：未来 Judge 的 rubric、criteria 与分数范围。

这些对象只声明预期。P0 不执行任何验证器。

## Result 合同

`TrialStatus` 包括 `pending`、`running`、`completed`、`failed`、`cancelled`、`timeout` 与 `environment_error`。`TrialResult` 记录 case/run/trial 身份、时间、Metric、Artifact 与结构化错误；`final_output` 始终可空。

`MetricResult` 区分 deterministic、runtime、judge、retrieval、compression 与 background_review 来源。证据使用 `MetricEvidence`，artifact 使用相对路径、SHA-256 和大小。

`CaseAggregate`、`MetricSummary`、`AuditSummary` 与 `AuditRunResult` 固定未来聚合输出形状，但 P0 不提供聚合算法。

## Memory 合同

`MemoryKind` 支持 short-term、long-term、user-profile、episodic、semantic 与 unknown。`MemoryQuery.top_k` 为正整数。`MemoryQueryResult` 明确 query ID、phase、strategy 和 provider；items 必须从 rank 1 开始、按 rank 升序且不重复。`prompt_context_injection` 强制 `query_used=false`、`score_semantics=none` 且 score 为空；disabled 强制 items 为空。

`MemoryStateSnapshot` 为兼容旧 Background Review 声明允许省略 P3 语义字段；一旦进入 P3 `TrialResult` / Worker Artifact，则必须带 before/after phase、strategy、provider 和稳定 item ID。`MemoryStateChange` 严格表达 added/removed/modified/unchanged；`MemoryOperationError` 只接受 P3 稳定错误枚举。`TrialResult` 分开保存 query results、snapshots、state changes、Memory errors 和 retrieval/final-answer/state 三个门禁。

合同不假设向量数据库，也不把 Dense/BM25/Hybrid 当成运行实现。

## Background Review 合同

`ReviewEvidenceKind` 与 MyHermes 当前证据语义对齐：

- `user_message`；
- `tool_observation`；
- `tool_error`；
- `assistant_decision_unverified`；
- `assistant_report_unverified`。

`ReviewStateSnapshot` 可表达 Memory、USER、Skill 列表、Skill source/managed_by/pinned、revision 与 governance_revision。`ReviewOutcome` 表达 no-op、create、update、replace、remove 或 reject，而不依赖 MyHermes 内部类。

## 稳定序列化与扩展 JSON

合同 hash 使用包含显式空值的 JSON 兼容 model dump、Unicode 原文、键排序、紧凑分隔符、UTF-8 和 SHA-256，因此不依赖 YAML 键顺序。

扩展 JSON 仅允许在 metadata、filters、单 evaluator config、Execution config overrides 等明确字段中出现。JSON 值仍限制为标准 JSON 标量、数组和对象，不接受 Python 任意对象或非有限浮点数。
