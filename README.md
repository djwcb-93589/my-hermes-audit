# my-hermes-audit

## P6.4 Representative Agent Benchmark

The representative benchmark is the compact, synthetic entry point at
[`examples/representative_benchmark_v1.yaml`](examples/representative_benchmark_v1.yaml).
It contains exactly nine Cases copied from the established Toolchain, Memory
Retrieval, and Background Review Suites: two Toolchain, four Memory Retrieval,
and three Background Review Cases. Their original inputs, fixtures, expected
facts, Scenarios, evidence windows, and required Evaluators remain unchanged;
the new Case metadata records each source Suite and Case ID.

It reuses the existing task-success, tool-trajectory, Memory evidence/
Recall@K/MRR, Background Review decision, runtime, cache, and P6.3 cost
projections. There is no composite score, required Judge, benchmark-specific
Langfuse path, Case include mechanism, repeated-Trial mode, Baseline, or CI
threshold. The default is one synthetic Trial, 180 seconds, no Sandbox
preservation, and no DeepSeek pricing, so cache observations remain available
while cost status is legitimately `not_evaluated`. Local pricing can be added
in a copied Suite through the existing `defaults.deepseek_pricing` contract.

See [`docs/p6-representative-benchmark.md`](docs/p6-representative-benchmark.md)
for Case provenance, metric denominators, exclusions, and the documented local
run command. P6.4 does not alter the source Suites, Result Schema, Worker
Protocol v13, or evaluator formulas.

## P7 Repeat Runs, Baselines, and Regression Comparison

P7 reuses the existing serial Suite runner.  The optional `--trials` override
accepts an explicit integer from 1 through 100 and leaves the P6.4 YAML default
at `trials: 1`.  Each Trial continues to receive its own Sandbox, MyHermes
Session, workspace, Memory state, Artifact root, and database; no state is
shared between repeats.  The override is part of the run Suite fingerprint,
while a companion semantic Suite fingerprint allows comparisons whose declared
repeat counts differ.

```bash
myhermes-audit run examples/representative_benchmark_v1.yaml \
  --subject-repo ../my-hermes --subject-config ./local-config.yaml \
  --trials 5 --output reports/representative-repeat.json

myhermes-audit baseline create reports/representative-repeat.json \
  --output baselines/representative-v1.json

myhermes-audit baseline compare baselines/representative-v1.json \
  reports/representative-current.json \
  --policy configs/regression-policy.yaml \
  --output reports/representative-regression.json
```

`AuditBaseline` is an immutable, content-fingerprinted projection of a
validated result and may contain failed Trials.  `AuditRegressionReport`
compares correctness, efficiency, cache, cost, failure, and Case-level action
distribution facts without a weighted score. Pricing mismatches make only
pricing-sensitive metrics `not_comparable`; other metrics remain comparable.
P7 task success rates use only explicit boolean `task_passed` samples, with
sample/passed counts and rates retained for both the Suite and every Case;
unknown (`None`) values are excluded.  Baseline and Regression contracts are
versioned independently (`baseline-v5`, `regression-v8`) and retain total
Trial counts separately from declared repeats per Case.  Identity conflicts
are represented as `ambiguous` and cannot be compared; missing
identities are represented as `missing` and also cannot satisfy the core
Baseline identity contract.
Metric comparisons carry independent comparability fact codes and an explicit
pricing-applicability fact; serialized reason codes and decisions are checked
outputs. Invalid comparison inputs are rejected before a Regression Report is
created rather than represented by a self-asserted status.
Each complete Regression Report also stores a content-safe, fingerprinted
`RegressionPolicySnapshot`. Metric policy fields and pricing applicability are
re-derived from that snapshot during reload; standalone Metric/Case projections
are marked report-only and are not a regression trust boundary.
Pricing-sensitive metrics treat any missing pricing fingerprint as
`not_comparable`; this remains local when other core metrics are comparable.
Metrics whose effective policy requires pricing are `local`; all other Metrics
are `core`. Reports persist and verify separate comparable core/local counts.
Local decisions remain visible for diagnostics, but local comparability cannot
establish an overall core regression conclusion.
Metric direction, policy thresholds, Case precedence, and report gate status are
derived by shared pure decision helpers and revalidated when JSON is loaded.
Comparison is read-only and does not invoke an Agent, Judge, Langfuse, or
network service.  See [`docs/p7-baseline-regression.md`](docs/p7-baseline-regression.md)
for denominators, compatibility rules, policy thresholds, and safe-data
boundaries.

## P6.1 E2E scenarios

The first P6 stage provides strict synthetic Toolchain and Process/Background
scenario contracts. See [`docs/p6-e2e-scenarios.md`](docs/p6-e2e-scenarios.md),
[`examples/e2e_toolchain_v1.yaml`](examples/e2e_toolchain_v1.yaml), and
[`examples/e2e_process_background_v1.yaml`](examples/e2e_process_background_v1.yaml).
Process lifecycle facts come from the Subject's public terminal/process tools;
the capability probe reads the public `process.action` schema (not
`ProcessManager` methods). Audit does not start commands directly or create a
second ProcessManager. `interrupt` is currently unsupported and is covered by
the preflight-only negative Suite; Agent `close` and Worker cleanup remain
separate facts.

`process(action="close")` only closes stdin for a running Process. It is not
Session cleanup, so the default completed and killed Cases do not call it;
Worker cleanup independently proves that no live process or background read
resource remains. The fixture-input Case enables `file` and `terminal`, reads
`fixtures/process-input.txt` through the public `file` tool before starting the
Process, and verifies the submitted input by SHA-256, character length, and
UTF-8 byte length. File observations remain outside the Process event sequence.

Required Process Step timing is explicitly classified as available,
duration-only, unavailable, or invalid. Missing/invalid required timing fails
the Process gate; optional timing is not evaluable. Timeout uses the strict
`duration_ms > timeout_seconds * 1000` rule. PRE is the public control hook
before Tool dispatch; POST is the public hook after Observation-batch
persistence, not exact handler completion. The Scenario exposes a separate
persistence observation span (`scenario_observation_span_*`) and a conservative
PRE-to-POST hook span. WAIT remaining budget uses only Process-start PRE to
WAIT PRE with an explicit fallback contract; `tool_duration_sum_ms` and
persistence timestamps never substitute for it. The observation span is
diagnostic when unavailable, while an exceeded available span remains a
separate fact. Only a required Process Scenario can tighten the Worker
watchdog; Toolchain, optional Process, and P0–P5 cases keep the Trial timeout.

Process status expectations are capability-driven from the public
`ProcessStatus` enum. The current Subject exposes `starting`, `running`,
`exited`, `killed`, `lost`, and `failed_start`; a process blocked on stdin
remains `running`. The default input Case independently gates the `running`
status and the `P6-WAIT` incremental output marker. It never infers
`waiting_for_input` from output or Python `readline()` behavior; that status is
reserved for a future Subject capability and preflight.

Process events are aligned with a bounded forward-only matcher rather than
array position. Extra, missing, out-of-order, foreign-process, and trailing
events are preserved as safe structured diagnostics and fail the strict
Process gate without overwriting facts from later correctly matched events.
The observation span is projected from persisted UTC timestamps between the
first and last matched foreground Process observations; the sum of individual
Tool durations is diagnostic only. A required Process Scenario may use the
Worker Process watchdog; otherwise the existing Trial watchdog remains in
force. Both are separate from the observation span and cleanup timing.
Relative Process event offsets come from the public PRE/POST Tool hooks; host
specific absolute monotonic values are not serialized.
The default Process prompts state exact commands and public
`log`, `poll`, and `kill(process_id, grace_seconds)` actions.

The P6.1 closing contracts use an explicit stdin handshake in the short
Process example, cursor references between incremental reads, at most one
`process_background` Scenario per Case, and bounded Artifact OutputCheckpoint
projections. Toolchain checkpoint results contain only hashes, lengths, marker
IDs, truncation, and pass/fail facts; Artifact text is never sent to Langfuse.

## P5 Background Review 评测

P5 把 MyHermes 的公开 Memory/Skill Background Review 生命周期接入隔离 Trial Worker。仅声明 `fixture.background_review_plans` 的 Case 才创建 trial-local Driver Registry、Review Agent Loop 与受限 Review ToolPolicy；旧 P0–P4 Case 不初始化任何 Review 组件。P5 记录真实 foreground evidence、Subject `prepare_run()` 证据窗口、before/after live snapshot、独立的 `observed_changes`、重复 claim 事实和安全错误投影，并由六个确定性 Review 维度形成 required Review gate。它评测同步、可收口的单 Review 生命周期，不实现异步队列、并行调度或完整后台闭环；后者留给 P6。

默认 synthetic Suite 是 [`examples/background_review_v1.yaml`](examples/background_review_v1.yaml)，包含六个非 stale 场景。`expected_action` 保持严格单一动作匹配，`allowed_actions` 用于 no-op/reject 等多个同样安全的动作；两者互斥，且不会改写 Subject 的真实 action。user-managed/pinned Case 以零修改和无半写入为重点，显式 reject 是否出现取决于模型是否调用治理工具。duplicate Case 的第二次零副作用检查不依赖第一次一定更新；verified-update 则仍严格要求 replace，清晰的前台失败→可复用 fallback→成功证据下 no-op 是 Subject 能力失败。当前 Subject 不能通过公开 API 证明治理 revision 绑定的 stale claim，因此 stale 只在 [`examples/background_review_capability_negative_v1.yaml`](examples/background_review_capability_negative_v1.yaml) 中声明，并预期在 Sandbox 前 capability preflight 拒绝。完整边界见 [`docs/p5-background-review.md`](docs/p5-background-review.md)。

## P4 长短期记忆与 Compression 消融

当前生产边界支持 Suite 显式声明 `no_memory`、`short_term_only`、`long_term_only`、`short_and_long_term`，并分别组合 `compression_mode: threshold_disabled|threshold_enabled`。Runner 按 Case、Variant 声明顺序和 Trial ordinal 串行展开；每个 Variant 独占 Session 命名空间、Sandbox、SQLite、`HERMES_HOME`、Memory 文件和 Artifact 目录。没有 `ablation` 的 P0–P3 Suite 不产生隐含 Variant。

Compression 只通过 MyHermes 公开 `compression.*` 配置和 `ConversationAgentLoop` 阈值控制，Audit 不实现摘要或消息裁剪。`threshold_disabled` 仅关闭配置阈值触发，context-overflow 紧急 Compression 仍可能发生；当前公开 ModelCall Observation 不能确认事件。需要 survival/事件数量或完整紧急禁用的 Case 位于独立 capability-negative Suite，并会在 Sandbox 前明确失败。有效模型按 Case `MODEL`、父环境 `MODEL`、最终配置 `model`、`subject-default` 的顺序统一进入 Worker、Runtime、Trial identity 与比较。完整规则见 [`docs/p4-memory-compression-ablation.md`](docs/p4-memory-compression-ablation.md)。

静态校验 synthetic P4 示例（不会启动 Trial）：

```bash
myhermes-audit validate examples/memory_compression_ablation_v1.yaml
myhermes-audit validate examples/memory_compression_capability_negative_v1.yaml
```

`my-hermes-audit` 是独立于 MyHermes 运行时的本地评测工具。报告总是从调用方指定的 `--subject-repo` 读取实际 Git fingerprint；文档不再把某个历史 MyHermes commit 当作运行基线。核心合同、数据集、Validator 与报告层不导入 `hermes.*`，MyHermes 也不感知自己正在被评测。

P6.2B 的代表性效率、质量和 DeepSeek 缓存指标定义见
[`docs/p6-2b-metrics.md`](docs/p6-2b-metrics.md)。缓存字段只被动读取
MyHermes 公共 Observation；本阶段不计算成本，也不把缓存状态作为任务门禁。

P6.3 的显式 DeepSeek USD 定价、Trial/Suite 成本、缓存节省和覆盖率合同见
[`docs/p6-3-cost-metrics.md`](docs/p6-3-cost-metrics.md)。定价只在 Audit
配置中声明，由父进程基于 `TrialRuntimeSummary` 计算；缺失定价或事实表示
`not_evaluated`，不会影响原有任务门禁，也不会进入 Worker。

项目要求 Python 3.13 或更高版本，与 MyHermes 的 `requires-python` 保持一致。基础运行依赖仅有 Pydantic v2 与 PyYAML；Langfuse Python SDK `>=4.14.2,<5` 与 OpenAI Python SDK 均为延迟导入的可选依赖。

## 已实现

- 严格、版本化且拒绝未知字段的 Suite、Case、Trial、Result、Memory、Background Review 与 Fingerprint 合同；
- `yaml.safe_load` 驱动的 Suite 加载、字段校验、重复 ID 检查和 Fixture source 只读路径解析；
- 基于规范化合同、canonical JSON、UTF-8 与 SHA-256 的稳定 Suite 指纹；
- 每 Trial 独立的 `HERMES_HOME`、workspace、SQLite 路径、artifact、fixture 与日志目录；
- 带所有权标记的 Sandbox 默认清理，以及受根目录约束的 Fixture 复制和文本写入方法；
- 只读 Git subject fingerprint 与结构化失败；
- Memory 与 P5 Background Review 的版本化 Worker/Port 合同；
- 保留 `validate` 与 `schema` 静态 CLI，并新增隔离执行的 `run`；
- 每 Trial 独立子进程运行真实 MyHermes `run_conversation`，并隔离 `HERMES_HOME`、SQLite、workspace、配置导入副作用和进程组；
- `single_turn` 与固定用户消息的 `scripted_multi_turn`；
- 显式关闭或启用 `file` / `terminal` / `memory` toolset；
- 文件、最终文本、JSON 文件和公共 Tool Observation 的确定性 Validator；
- 串行多 Trial、单 Trial 超时、结构化 Artifact、稳定 JSON 报告和终端摘要。
- 独立、只读的 Subject Capability Probe，以及不调用模型的 `doctor` 诊断；
- `MetricStatus`、结构化 Judge 结果、任务结果与 Judge 门禁分离；
- OpenAI-compatible LLM Judge、版本化 `answer-quality-v1` Prompt、本地 criterion 加权与唯一一级指标 `answer_quality`；
- Langfuse Dataset 幂等版本化同步、正式 Experiment Runner 纯回放关联、Trial Trace、Turn/Model/Tool/Validator/Judge Observation，以及带本地发布清单的 Score 幂等发布；
- `task_success`、`tool_correctness`、`answer_quality` 三个一级质量分数，以及独立效率元数据；
- `synthetic`、`internal`、`sensitive` 数据分类和 `--langfuse-no-content` 内容关闭开关；
- 本地报告中的 Judge coverage/error、Experiment identity、发布计数和脱敏 integration error。
- P3 `subject_native` Memory Prompt 暴露与 `disabled` 对照、公共 API seed/read/render/clear Adapter、before/after snapshot、稳定状态 diff 和跨逻辑 Session；
- required Memory evidence、Recall@K、MRR、Memory 状态门禁，以及与最终答案门禁分离的结构化 Trial 事实；
- P3 Memory seed/query/snapshot/retrieval Langfuse Observation；no-content 或 sensitive 时只投影逐项哈希、长度、kind、rank 与安全 metadata。
- P5 trial-local Background Review Adapter：公开 Driver/Registry/ReviewAgentLoop、真实 foreground/prepared evidence、Memory/Skill live snapshot、observed state diff、重复 claim 零副作用事实，以及 Worker cleanup 前的同步收口；
- P5 `background_review` evaluator：decision、evidence、update、stale、side-effect 与 idempotency 六维硬门禁；required Review 失败会使 `task_success` 失败，但不新增第四个一级 Score；
- P5 Langfuse 本地结果回放：Review 仅投影 ID、kind、hash、长度、状态、action、revision hash、duration 与安全 metadata，永不上传 Review evidence、Prompt、Memory、Skill 或工具正文。

## P5 实现边界与仍未实现

P3 已实现当前 MyHermes 公开能力对应的 `subject_native`（准确语义为 `prompt_context_injection`）和 `disabled`。当前 Subject 没有公开 ranked retrieval API，因此 Dense、BM25、Hybrid 只有严格枚举与 capability-negative 边界，`run` 会在创建 Sandbox 前明确拒绝，绝不由 Audit 自己实现或静默降级。

仍未实现 LLM 模拟用户、真实 Background Review 异步队列/并行调度、完整任务→Review→后续回归闭环、Baseline Compare、CI 或 Langfuse 自定义前端 Dashboard。P5 不读取 MyHermes 私有 Review 持久化层，也不把 Audit 自己的决策逻辑替代 Subject Review。

## 安装

```bash
python -m pip install -e .
```

安装完整 P2 可选依赖：

```bash
python -m pip install -e ".[eval]"
```

也可仅安装 `.[judge]` 或 `.[langfuse]`。未安装这些 extras 时，普通本地 P1 `run`、`validate`、`schema` 与默认 `doctor` 仍不导入第三方 SDK。

## CLI

Check the installed Audit package, the base config, the Subject Git identity,
and the public MyHermes compatibility boundary without creating a conversation
or database:

```bash
myhermes-audit doctor \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml
```

`--check-langfuse` and `--check-judge` additionally initialize and close the
configured optional client to validate the exact SDK minimum, required public
capabilities, URL, timeout, and required environment fields. Doctor does not connect, create a Dataset,
Trace or Score, or send a model request, and never prints credential values. Before any Trial is
created, the same separate read-only Subject Capability Probe checks public API
compatibility. See
[`docs/subject-capability-probe.md`](docs/subject-capability-probe.md).

校验一个 Suite：

```bash
myhermes-audit validate examples/core_contract_v1.yaml
python -m myhermes_audit validate examples/memory_contract_v1.yaml
python -m myhermes_audit validate examples/memory_retrieval_v1.yaml
```

输出 `AuditSuite` JSON Schema：

```bash
myhermes-audit schema
myhermes-audit schema --output build/audit-suite.schema.json
```

失败信息包含 YAML 文件、Case ID、字段路径与可读原因。默认不输出完整 traceback；需要诊断 CLI 自身问题时可在子命令前显式添加 `--debug`。

运行本地 P1 Suite（无远端依赖）：

```bash
myhermes-audit run examples/core_run_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/core-run.json
```

运行 P3 synthetic Memory Suite 使用相同 `run` 命令，不新增 CLI：

```bash
myhermes-audit run examples/memory_retrieval_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/memory-retrieval.json
```

每个 P3 Trial 仍使用独立 Sandbox；示例只声明 `subject_native`/`disabled`，不会把 Dense/BM25/Hybrid 描述成当前可运行能力。

P5 synthetic Background Review Suite 也使用同一 `run` 命令；它会在真实执行时使用当前 Subject 模型和公开 Review API：

```bash
myhermes-audit run examples/background_review_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/background-review.json
```

该默认 Suite 不含 stale。`background_review_capability_negative_v1.yaml` 只用于确认缺少公开 stale claim validation 时会在创建 Sandbox 前失败；示例文档不把它描述为已运行的 stale Review。

可重复使用 `--case <case-id>` 选择 Case；`--preserve-on-failure` 只在终端打印被保留的本地 Sandbox 路径。模型凭据只能从启动 Audit 的环境继承，不能写入 Suite、生成配置、Artifact 或报告。未指定 `--output` 时写入当前目录的 `reports/`。

规划 Dataset 同步且不导入或连接 Langfuse：

```bash
myhermes-audit sync examples/core_judge_v1.yaml \
  --dataset-name myhermes-core-judge \
  --dry-run
```

真实同步去掉 `--dry-run`。启用 P2 Judge 与 Langfuse 发布：

```bash
myhermes-audit run examples/core_judge_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/core-judge.json \
  --judge \
  --langfuse \
  --dataset-name myhermes-core-judge \
  --experiment-name myhermes-core-judge-local
```

Judge 只读取 `AUDIT_JUDGE_MODEL`、`AUDIT_JUDGE_API_KEY`、可选 `AUDIT_JUDGE_BASE_URL` 与 `AUDIT_JUDGE_TIMEOUT_SECONDS`。Langfuse 读取 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、优先 `LANGFUSE_BASE_URL`（兼容 `LANGFUSE_HOST`）及可选 `LANGFUSE_TIMEOUT`。两组凭据不会进入 MyHermes Worker。`--langfuse-no-content` 强制远端只接收哈希、长度、状态、指标和安全 metadata。

显式 `--langfuse` 会在第一个 Trial 前完成版本/能力、连接、Dataset 与 Experiment 策略检查。全部本地 Trial 完成后，正式 Experiment Runner 的 task 只回放既有 `TrialResult`，不会启动 Worker、Validator 或 Judge。发布清单与报告并列写入；发布、Score、flush 或 shutdown 失败不会丢弃本地 Trial 事实，网络结果不确定时不会显示为成功，CLI 最终返回非零。

## 示例 Suite

- [`examples/core_contract_v1.yaml`](examples/core_contract_v1.yaml)：单轮输入、文件 Fixture、文件与工具轨迹预期，以及 deterministic/llm_judge 声明；
- [`examples/memory_contract_v1.yaml`](examples/memory_contract_v1.yaml)：provider-neutral、capability-negative 的 Memory 合同示例，不代表当前 `hybrid` 可运行；
- [`examples/memory_retrieval_v1.yaml`](examples/memory_retrieval_v1.yaml)：十个 P3 synthetic Case，覆盖事实、偏好、时间、覆盖、冲突、干扰、跨 Session、no-write、隔离与 disabled 对照；
- [`examples/memory_compression_ablation_v1.yaml`](examples/memory_compression_ablation_v1.yaml)：默认可运行的两个 P4 synthetic Case，覆盖四种 Memory Mode、两种阈值控制模式、Required Fact、Distortion、Token/duration 和 Variant comparison，但不声称 Compression 已发生；
- [`examples/memory_compression_capability_negative_v1.yaml`](examples/memory_compression_capability_negative_v1.yaml)：独立 P4 capability-negative Case，要求可观察 Compression、压缩后事实 survival 或完整关闭紧急 Compression，当前预期在 Sandbox 前失败；
- [`examples/background_review_contract_v1.yaml`](examples/background_review_contract_v1.yaml)：Memory no-op、Skill update、stale rejection，以及五类 Review 证据；
- [`examples/background_review_v1.yaml`](examples/background_review_v1.yaml)：默认可运行的六个 P5 synthetic Case，覆盖 Memory no-op、Skill verified update、冲突证据、duplicate idempotency、user-managed/pinned 保护与无半写入；不含 stale；
- [`examples/background_review_capability_negative_v1.yaml`](examples/background_review_capability_negative_v1.yaml)：仅含 stale-governance 负例；当前预期由 capability preflight 在 Sandbox 前拒绝；
- [`examples/core_run_v1.yaml`](examples/core_run_v1.yaml)：P1 可运行的六个合成 Case；本仓库开发阶段不执行该示例。
- [`examples/core_judge_v1.yaml`](examples/core_judge_v1.yaml)：六个合成 P2 Case，将原有确定性门禁与单个 `answer_quality` Judge 组合；代码建设阶段不执行该示例。

示例只包含合成数据、相对路径和配置声明。加载示例不会运行 Agent、检索、Judge 或 Review。

## 低耦合集成原则

`myhermes_audit` 核心暴露合同、loader、Sandbox、fingerprint、Validator 与报告。`runners/myhermes.py` 负责父进程适配，`integrations/myhermes/worker.py` 与仅由它延迟加载的 `memory_adapter.py`、`background_review_adapter.py` 是导入 MyHermes 的子进程边界；依赖方向不能反转到核心层。

父进程通过 `subprocess` 的 `env` 参数传递专属环境，不修改自身 `os.environ`。只有 Worker 在隔离校验完成后导入 MyHermes，并通过公开初始化、会话、工具策略、Observation 读取和关闭接口完成生命周期。

Judge 与 Langfuse 仅在 Audit 父进程的 `integrations/` 适配层初始化；核心合同、Dataset planner、Validator、Judge service 与报告不导入 SDK。`AuditRunResult` 是事实来源，Langfuse 只承担 Dataset 管理、Experiment/Trace 展示和 Score 分析。

## 开发与验证分离

当前 P5 阶段只构建生产代码、合同、示例与文档。本仓库不创建或修改测试文件，也不在本阶段运行 pytest、unittest、真实 Trial、模型、Judge 或远端集成。后续独立 T5 应从公开合同和端口验证真实行为，不应给 MyHermes 增加评测专用分支。

更多边界见 [`docs/architecture.md`](docs/architecture.md)、[`docs/p5-background-review.md`](docs/p5-background-review.md)、[`docs/p4-memory-compression-ablation.md`](docs/p4-memory-compression-ablation.md)、[`docs/p3-memory-retrieval.md`](docs/p3-memory-retrieval.md)、[`docs/p1-runner.md`](docs/p1-runner.md)、[`docs/worker-protocol.md`](docs/worker-protocol.md)、[`docs/validators.md`](docs/validators.md)、[`docs/security.md`](docs/security.md)、[`docs/p1-boundary.md`](docs/p1-boundary.md)、[`docs/p2-boundary.md`](docs/p2-boundary.md)、[`docs/langfuse.md`](docs/langfuse.md)、[`docs/langfuse-compatibility.md`](docs/langfuse-compatibility.md)、[`docs/publication-idempotency.md`](docs/publication-idempotency.md)、[`docs/llm-judge.md`](docs/llm-judge.md)、[`docs/score-model.md`](docs/score-model.md) 与 [`docs/data-classification.md`](docs/data-classification.md)。
