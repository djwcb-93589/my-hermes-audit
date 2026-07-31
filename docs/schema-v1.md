# Audit Schema v1

当前 `schema_version` 为字符串 `"1.0"`。每个公共 Pydantic 合同都继承该字段，顶层 `AuditSuite` 与 `AuditRunResult` 要求调用方显式提供该版本。所有合同拒绝未知字段，并对可变默认值使用工厂。枚举值和 ID 使用稳定英文标识；时间必须带时区，进入合同后规范化为 UTC。

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
- `memories`：MemoryQuery、所需 ID/kind 与最少命中数；
- `background_reviews`：action、目标、变更保护、证据来源和 stale rejection；
- `judges`：未来 Judge 的 rubric、criteria 与分数范围。

这些对象只声明预期。P0 不执行任何验证器。

## Result 合同

`TrialStatus` 包括 `pending`、`running`、`completed`、`failed`、`cancelled`、`timeout` 与 `environment_error`。`TrialResult` 记录 case/run/trial 身份、时间、Metric、Artifact 与结构化错误；`final_output` 始终可空。

`MetricResult` 区分 deterministic、runtime、judge、retrieval、compression 与 background_review 来源。证据使用 `MetricEvidence`，artifact 使用相对路径、SHA-256 和大小。

`CaseAggregate`、`MetricSummary`、`AuditSummary` 与 `AuditRunResult` 固定未来聚合输出形状，但 P0 不提供聚合算法。

## Memory 合同

`MemoryKind` 支持 short-term、long-term、user-profile、episodic、semantic 与 unknown。`MemoryQuery.top_k` 为正整数。`MemoryQueryResult.items` 必须从 rank 1 开始、按 rank 升序且不重复；score 不限定范围或方向，Provider 必须自行说明语义。

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
