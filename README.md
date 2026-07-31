# my-hermes-audit

`my-hermes-audit` 是独立于 MyHermes 运行时的评测合同与静态基础设施项目。P0 对齐的被测基线是 `my-hermes` commit `2cb27d72e90365d44cdc2d08953acf882c4ae626`，但核心包不会导入 `hermes.*`，也不会让 MyHermes 感知自己是否正在被评测。

项目要求 Python 3.13 或更高版本，与 MyHermes 的 `requires-python` 保持一致。运行依赖仅有 Pydantic v2 与 PyYAML；CLI 使用标准库 `argparse`。

## P0 已实现

- 严格、版本化且拒绝未知字段的 Suite、Case、Trial、Result、Memory、Background Review 与 Fingerprint 合同；
- `yaml.safe_load` 驱动的 Suite 加载、字段校验、重复 ID 检查和 Fixture source 只读路径解析；
- 基于规范化合同、canonical JSON、UTF-8 与 SHA-256 的稳定 Suite 指纹；
- 每 Trial 独立的 `HERMES_HOME`、workspace、SQLite 路径、artifact、fixture 与日志目录；
- 带所有权标记的 Sandbox 默认清理，以及受根目录约束的 Fixture 复制和文本写入方法；
- 只读 Git subject fingerprint 与结构化失败；
- Memory 和 Background Review 的异步 `Protocol` 扩展端口；
- 只包含 `validate` 与 `schema` 的静态 CLI。

## P0 明确未实现

P0 不执行 AgentLoop 或 Background Review，不调用模型，不实现 LLM Judge、Memory 检索、evaluator、指标聚合、Baseline、Langfuse、OpenTelemetry、报告、比较或 CI。`llm_judge`、`retrieval` 等名字在 P0 中只是声明式合同，不代表对应能力已经接入。

## 安装

```bash
python -m pip install -e .
```

## 静态 CLI

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

## 示例 Suite

- [`examples/core_contract_v1.yaml`](examples/core_contract_v1.yaml)：单轮输入、文件 Fixture、文件与工具轨迹预期，以及 deterministic/llm_judge 声明；
- [`examples/memory_contract_v1.yaml`](examples/memory_contract_v1.yaml)：固定多轮输入、Memory Fixture、MemoryQuery 与 retrieval 声明；
- [`examples/background_review_contract_v1.yaml`](examples/background_review_contract_v1.yaml)：Memory no-op、Skill update、stale rejection，以及五类 Review 证据。

示例只包含合成数据、相对路径和配置声明。加载示例不会运行 Agent、检索、Judge 或 Review。

## 低耦合集成原则

`myhermes_audit` 核心只暴露合同、loader、Sandbox、fingerprint 与 ports。未来 `runners/myhermes` 或 `integrations/myhermes` 可以同时依赖 Audit ports 与 MyHermes 运行时，把 `AgentLoopResult`、Memory、Skills、SQLite 或 Background Review 适配成 Audit 合同；依赖方向不能反转到核心层。

MyHermes 的环境由 runner 显式获得 `AuditSandbox.environment_overrides()`，而不是由 Audit 修改 `os.environ`。P0 只生成隔离的 SQLite 文件路径，不导入或初始化 MyHermes schema。

## 开发与验证分离

P0 是代码构建阶段。本仓库不创建测试目录或测试文件，也不在本阶段运行 pytest、unittest 或集成测试。后续独立验证阶段应从公开合同和端口测试行为，不应给 MyHermes 增加评测专用分支。

更多边界见 [`docs/architecture.md`](docs/architecture.md)、[`docs/schema-v1.md`](docs/schema-v1.md)、[`docs/sandbox.md`](docs/sandbox.md) 与 [`docs/p0-boundary.md`](docs/p0-boundary.md)。
