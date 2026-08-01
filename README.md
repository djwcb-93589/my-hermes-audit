# my-hermes-audit

`my-hermes-audit` 是独立于 MyHermes 运行时的本地评测工具。报告总是从调用方指定的 `--subject-repo` 读取实际 Git fingerprint；文档不再把某个历史 MyHermes commit 当作运行基线。核心合同、数据集、Validator 与报告层不导入 `hermes.*`，MyHermes 也不感知自己正在被评测。

项目要求 Python 3.13 或更高版本，与 MyHermes 的 `requires-python` 保持一致。运行依赖仅有 Pydantic v2 与 PyYAML；CLI 使用标准库 `argparse`。

## 已实现

- 严格、版本化且拒绝未知字段的 Suite、Case、Trial、Result、Memory、Background Review 与 Fingerprint 合同；
- `yaml.safe_load` 驱动的 Suite 加载、字段校验、重复 ID 检查和 Fixture source 只读路径解析；
- 基于规范化合同、canonical JSON、UTF-8 与 SHA-256 的稳定 Suite 指纹；
- 每 Trial 独立的 `HERMES_HOME`、workspace、SQLite 路径、artifact、fixture 与日志目录；
- 带所有权标记的 Sandbox 默认清理，以及受根目录约束的 Fixture 复制和文本写入方法；
- 只读 Git subject fingerprint 与结构化失败；
- Memory 和 Background Review 的异步 `Protocol` 扩展端口；
- 保留 `validate` 与 `schema` 静态 CLI，并新增隔离执行的 `run`；
- 每 Trial 独立子进程运行真实 MyHermes `run_conversation`，并隔离 `HERMES_HOME`、SQLite、workspace、配置导入副作用和进程组；
- `single_turn` 与固定用户消息的 `scripted_multi_turn`；
- 显式关闭或启用 `file` / `terminal` toolset；
- 文件、最终文本、JSON 文件和公共 Tool Observation 的确定性 Validator；
- 串行多 Trial、单 Trial 超时、结构化 Artifact、稳定 JSON 报告和终端摘要。

## P1 明确未实现

P1 不实现 Langfuse、OpenTelemetry、LLM Judge、模拟用户、Memory Retrieval、Compression、Background Review 评测、Baseline Compare、并行调度或 CI。已有合同中的这些枚举仍只是未来边界；`run` 会在启动 Worker 前拒绝它们。

## 安装

```bash
python -m pip install -e .
```

## CLI

Check the installed Audit package, the base config, the Subject Git identity,
and the public MyHermes compatibility boundary without creating a conversation
or database:

```bash
myhermes-audit doctor \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml
```

`--check-langfuse` and `--check-judge` additionally check dependency and
environment-variable presence. Doctor does not create a Dataset, Trace, Score,
or model request, and never prints credential values. Before any Trial is
created, the same separate read-only Subject Capability Probe checks public API
compatibility. See
[`docs/subject-capability-probe.md`](docs/subject-capability-probe.md).

校验一个 Suite：

```bash
myhermes-audit validate examples/core_contract_v1.yaml
python -m myhermes_audit validate examples/memory_contract_v1.yaml
```

输出 `AuditSuite` JSON Schema：

```bash
myhermes-audit schema
myhermes-audit schema --output build/audit-suite.schema.json
```

失败信息包含 YAML 文件、Case ID、字段路径与可读原因。默认不输出完整 traceback；需要诊断 CLI 自身问题时可在子命令前显式添加 `--debug`。

运行 P1 Suite：

```bash
myhermes-audit run examples/core_run_v1.yaml \
  --subject-repo ../my-hermes \
  --subject-config ./local-config.yaml \
  --output reports/core-run.json
```

可重复使用 `--case <case-id>` 选择 Case；`--preserve-on-failure` 只在终端打印被保留的本地 Sandbox 路径。模型凭据只能从启动 Audit 的环境继承，不能写入 Suite、生成配置、Artifact 或报告。未指定 `--output` 时写入当前目录的 `reports/`。

## 示例 Suite

- [`examples/core_contract_v1.yaml`](examples/core_contract_v1.yaml)：单轮输入、文件 Fixture、文件与工具轨迹预期，以及 deterministic/llm_judge 声明；
- [`examples/memory_contract_v1.yaml`](examples/memory_contract_v1.yaml)：固定多轮输入、Memory Fixture、MemoryQuery 与 retrieval 声明；
- [`examples/background_review_contract_v1.yaml`](examples/background_review_contract_v1.yaml)：Memory no-op、Skill update、stale rejection，以及五类 Review 证据；
- [`examples/core_run_v1.yaml`](examples/core_run_v1.yaml)：P1 可运行的六个合成 Case；本仓库开发阶段不执行该示例。

示例只包含合成数据、相对路径和配置声明。加载示例不会运行 Agent、检索、Judge 或 Review。

## 低耦合集成原则

`myhermes_audit` 核心暴露合同、loader、Sandbox、fingerprint、Validator 与报告。`runners/myhermes.py` 负责父进程适配，`integrations/myhermes/worker.py` 是导入 MyHermes 的子进程边界；依赖方向不能反转到核心层。

父进程通过 `subprocess` 的 `env` 参数传递专属环境，不修改自身 `os.environ`。只有 Worker 在隔离校验完成后导入 MyHermes，并通过公开初始化、会话、工具策略、Observation 读取和关闭接口完成生命周期。

## 开发与验证分离

当前阶段只构建代码与合同。本仓库不创建测试目录或测试文件，也不在本阶段运行 pytest、unittest、集成或烟雾验证。后续独立验证阶段应从公开合同和端口验证行为，不应给 MyHermes 增加评测专用分支。

更多边界见 [`docs/architecture.md`](docs/architecture.md)、[`docs/p1-runner.md`](docs/p1-runner.md)、[`docs/worker-protocol.md`](docs/worker-protocol.md)、[`docs/validators.md`](docs/validators.md)、[`docs/security.md`](docs/security.md) 与 [`docs/p1-boundary.md`](docs/p1-boundary.md)。
