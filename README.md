# my-hermes-audit

`my-hermes-audit` 是面向 [MyHermes](../my-hermes) 的独立审计与评测工具。它以严格、可版本化的事实合同描述评测输入、执行结果、指标、基线与回归结论，并在隔离的 Worker 进程中调用 MyHermes 的公共接口。

项目的设计重点是让评测可复现、可审计且不泄露内容或凭据：Suite、Artifact、报告和远程观测均有明确的安全边界；本仓库不修改被测 MyHermes 的业务逻辑。

## 主要能力

- 静态校验 YAML Suite，并输出 JSON Schema。
- 在每个独立 Trial 中构建隔离的配置、工作区、状态目录和 Artifact 根目录。
- 评测文件、终端、进程、记忆检索、记忆压缩、后台 Review 等公共工具和能力。
- 校验任务结果、工具轨迹、证据、场景事件、Review 决策、性能、缓存 Token 和成本事实。
- 生成严格的 `AuditRunResult`，创建不可变 Baseline，并按显式策略输出 Regression Report。
- 从已经校验的 JSON 事实渲染 Markdown；渲染过程不会重新计算指标或调用模型。
- 可选集成 LLM Judge 与 Langfuse；内容投影可使用无正文模式。

## 环境要求与安装

- Python `>= 3.13`
- 可读取的 MyHermes 源码目录
- 用于运行 Trial 的本地 MyHermes 配置文件

安装基础功能：

```bash
python -m pip install -e .
```

按需安装可选能力：

```bash
# 仅 Judge
python -m pip install -e ".[judge]"

# 仅 Langfuse
python -m pip install -e ".[langfuse]"

# 两者均安装
python -m pip install -e ".[eval]"
```

未安装可选依赖时，静态校验、Schema 输出、基础诊断和不使用相应集成的本地运行仍可使用。

## 快速开始

先检查本地安装、MyHermes 公共能力及基础配置。该命令不会启动会话、执行 Trial 或打印凭据：

```bash
myhermes-audit doctor \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml
```

静态校验 Suite：

```bash
myhermes-audit validate examples/core_run_v1.yaml
myhermes-audit schema --output build/audit-suite.schema.json
```

运行代表性 Suite，并将严格事实写入 JSON：

```bash
myhermes-audit run examples/representative_benchmark_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/representative-current.json
```

从严格结果创建 Baseline，并对新的结果执行只读比较：

```bash
myhermes-audit baseline create reports/representative-current.json \
  --output baselines/representative-v1.json

myhermes-audit baseline compare baselines/representative-v1.json \
  reports/representative-current.json \
  --policy configs/regression-policy.yaml \
  --output reports/representative-regression.json
```

将已经校验的事实 JSON 渲染为 Markdown：

```bash
myhermes-audit report render reports/representative-current.json \
  --output reports/representative-current.md
```

`baseline compare` 和 `report render` 不会调用 Agent、模型、Judge 或 Langfuse。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `doctor` | 读取并检查 MyHermes 公共能力、基础配置和可选集成的本地条件。 |
| `validate <suite>` | 加载并严格校验 Suite YAML，不执行 Agent。 |
| `schema` | 输出 `AuditSuite` 的 JSON Schema。 |
| `run <suite>` | 在隔离 Worker 中运行 Suite，生成严格 JSON 结果。 |
| `sync <suite> --dry-run` | 生成 Langfuse Dataset 同步计划，不连接远端。 |
| `baseline create` | 从已校验的运行结果创建不可变 Baseline。 |
| `baseline compare` | 按策略比较 Baseline 与当前结果。 |
| `report render` | 从严格 JSON 事实渲染 Markdown。 |

`run` 支持重复传入 `--case <case-id>` 选择 Case，支持 `--trials 1..100` 覆盖 Suite 的重复次数，并可用 `--preserve-on-failure` 保留失败 Trial 的本地 Sandbox 以便诊断。

## 示例 Suite

| 文件 | 覆盖范围 |
| --- | --- |
| [`core_run_v1.yaml`](examples/core_run_v1.yaml) | 基础对话、Fixture、文件与工具轨迹。 |
| [`core_judge_v1.yaml`](examples/core_judge_v1.yaml) | 基础评测与可选回答质量 Judge。 |
| [`memory_retrieval_v1.yaml`](examples/memory_retrieval_v1.yaml) | 记忆检索、证据、Recall@K 与 MRR。 |
| [`memory_compression_ablation_v1.yaml`](examples/memory_compression_ablation_v1.yaml) | 长短期记忆、压缩和消融比较。 |
| [`background_review_v1.yaml`](examples/background_review_v1.yaml) | Memory/Skill 后台 Review、证据和治理决策。 |
| [`e2e_toolchain_v1.yaml`](examples/e2e_toolchain_v1.yaml) | 文件与终端工具链的端到端轨迹。 |
| [`e2e_process_background_v1.yaml`](examples/e2e_process_background_v1.yaml) | 后台进程、输入、等待和日志场景。 |
| [`representative_benchmark_v1.yaml`](examples/representative_benchmark_v1.yaml) | 面向日常验收的代表性综合 Benchmark。 |

`*_capability_negative_*` Suite 用于明确验证不具备公共能力时的拒绝行为，不应用作常规正向运行输入。

## 事实、安全与隔离边界

- `AuditRunResult` 是运行事实的唯一来源；Baseline、Regression Report、Markdown 和控制台摘要均基于已校验的事实生成。
- 每个 Trial 使用独立 Sandbox、工作区、MyHermes 状态目录和数据库；被测仓库以只读方式使用。
- Suite、生成配置、Artifact、报告和远程投影均不写入模型凭据。凭据仅从启动进程环境继承。
- 默认 Artifact 与错误信息经过内容与路径安全处理；不要将 Sandbox、原始日志、Prompt、模型输出、记忆或 Review 正文作为 CI Artifact 上传。
- 使用 `--langfuse-no-content` 时，远程投影仅包含哈希、长度、状态、指标和安全元数据，不包含输入或输出正文。

详情请参阅 [安全说明](docs/security.md)、[数据分类](docs/data-classification.md)、[Sandbox 说明](docs/sandbox.md) 和 [Worker 协议](docs/worker-protocol.md)。

## 可选集成

### LLM Judge

通过 `run --judge` 启用环境配置的 OpenAI 兼容 Judge。Judge 结果是独立的结构化事实，不会替代任务成功、工具轨迹或其他确定性校验。相关说明见 [LLM Judge](docs/llm-judge.md) 与 [评分模型](docs/score-model.md)。

### Langfuse

`sync` 用于 Dataset 同步，`run --langfuse` 用于发布本地已完成 Trial 的观测和分数。使用前可先运行 `sync --dry-run`，并阅读 [Langfuse 使用说明](docs/langfuse.md) 与 [兼容性说明](docs/langfuse-compatibility.md)。

DeepSeek 缓存 Token 仅被动记录服务端返回的 hit/miss 数据；字段缺失表示不可评估，不代表零命中。本项目不控制缓存、不计算服务端缓存策略，也不将其他供应商字段冒充为 DeepSeek 缓存事实。

## CI 与报告

GitHub Actions 只上传经过安全筛选的明确文件白名单。`.p8-ci/` 是隐藏目录，因此上传步骤显式启用隐藏文件上传，并将缺失 Artifact 视为错误。严格 JSON 是唯一事实源；Markdown、manifest 和控制台摘要为派生产物。

CI 流程、Artifact 白名单和正式验收操作请见：

- [报告与 CI](docs/p8-reporting-ci.md)
- [生产验收](docs/p8-production-acceptance.md)
- [CI 示例说明](examples/ci/README.md)

## 合同版本

当前公开事实合同保持独立版本化：

| 合同 | 当前版本 |
| --- | --- |
| 运行结果 | `1.7` |
| Worker 协议 | `v13` |
| Baseline | `baseline-v7` |
| Regression | `regression-v10` |
| Markdown 报告 | `report-v1` |

版本化合同拒绝不兼容的旧载荷；不要手动编辑已发布的 JSON 来伪造运行、基线或回归事实。

## 深入文档

- [架构](docs/architecture.md)
- [验证器](docs/validators.md)
- [公共能力探测](docs/subject-capability-probe.md)
- [记忆检索](docs/p3-memory-retrieval.md)
- [记忆与压缩消融](docs/p4-memory-compression-ablation.md)
- [后台 Review](docs/p5-background-review.md)
- [端到端场景](docs/p6-e2e-scenarios.md)
- [代表性 Benchmark](docs/p6-representative-benchmark.md)
- [Baseline 与回归比较](docs/p7-baseline-regression.md)

## 贡献约定

提交修改时请保持合同、运行逻辑和报告投影一致；新增能力应先明确事实来源、隔离边界、错误语义和版本兼容性。不要通过放宽验证器、伪造证据或编辑派生报告来绕过评测结论。
