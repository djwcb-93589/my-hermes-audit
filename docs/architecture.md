# 架构与依赖方向

## 核心规则

`myhermes_audit` 是被测运行时之外的独立包。其核心层可以描述输入、环境与结果，但不能导入 `hermes.*`、触发模型或解释 MyHermes 内部数据库 schema；只有隔离适配层能够启动真实运行。

```text
contracts / datasets / sandbox / validators / reports / fingerprint
                              ↑
                        runner ports
                              ↑
          integrations.myhermes Worker / adapters
                              ↓
                    my-hermes public runtime
```

中间适配层是唯一允许同时认识两侧的层：向上实现 Audit ports 并产出 Audit 合同，向下调用 MyHermes 的公共运行入口。核心层不知道 MyHermes 类名、模型 SDK、SQLite 表或 Background Review driver。

## 模块职责

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `contracts` | 版本化 Suite、Result、Memory、Review、Fingerprint 模型 | 执行、评分、持久化 |
| `datasets` | 安全 YAML 读取、严格校验、source 路径解析 | 读取或复制 Fixture 内容 |
| `sandbox` | Trial 目录隔离、环境覆盖值、受控 Fixture I/O、所有权清理 | 修改全局环境、初始化 MyHermes DB |
| `fingerprint` | canonical hash、只读 Git 状态、Audit 平台指纹 | checkout、commit、修改 index |
| `ports` / `runners/base` | Subject-neutral 扩展与 Trial runner 形状 | MyHermes 导入 |
| `validators` | 文件、文本、JSON、工具轨迹、Memory 检索与状态客观校验 | 模型语义评分、Subject 检索实现 |
| `reports` | Trial/Case 聚合、nearest-rank 百分位、JSON/终端输出 | 基线比较 |
| `integrations/myhermes` | Worker 协议、配置、公开 Observation、Memory Adapter、生命周期 | 核心合同反向依赖、私有 Memory 文件/SQLite 解析 |
| `cli` | `validate`、`schema` 与 `run` | 并行调度、CI |

父进程模块不导入 `hermes.*`。Worker 在确认 cwd、环境、配置和 Artifact 路径都属于当前 Trial 后才首次导入 MyHermes。每个 Trial 进程退出后，全局配置、模型客户端与注册表状态随进程一起消失，不需要 reload 或清理 `sys.modules`。

P3 的 `MemoryEvaluationPort` 依赖方向保持不变：父进程只传严格 Fixture/query 计划并接收合同事实；Worker 延迟加载 `MyHermesMemoryAdapter`，后者只调用公开 `read_memory_entries`、`mutate_memory_entries` 和 `render_memory_section`。纯核心 diff/Validator 不认识 MyHermes 路径、分隔符、类或数据库。Audit 不建立 Subject 的 Dense、BM25 或 Hybrid 索引。

## 与 MyHermes 当前语义的对齐

- MyHermes 要求 Python `>=3.13`，Audit 使用相同下限。
- `AgentLoopResult` 可在没有自然语言 final output 时由文件、工具、数据库或 Review 状态表达结果，因此 `TrialResult.final_output` 可空。
- MyHermes 把 `HERMES_HOME` 作为 SOUL、配置、Memory、USER、Skills、scripts 等状态根，并把相对 `db_path` 解析到该根；Sandbox 分别提供 `HERMES_HOME` 与 `DB_PATH`。
- 当前公开 Memory 是按原生顺序和字符上限注入 Prompt 的 `prompt_context_injection`，不是 ranked semantic retrieval。P3 只把它命名为 `subject_native`。
- Review 证据保持五类来源：用户消息、工具观察、工具错误、未验证 Assistant 决策、未验证 Assistant 报告。
- Skill 快照保留 source、managed_by、pinned、revision 与 governance_revision，用于表达治理保护和 stale rejection，而不导入 MyHermes Skill 类型。

## 扩展边界

任意 JSON 只允许出现在窄作用域：Trial metadata、Execution config overrides、单个 evaluator config、Memory filters/metadata 和少数结果 metadata。Suite、Case、Fixture、Expected、Review 与 Result 的领域结构不能整体退化为无约束字典。

只有 `integrations/myhermes/*` 与 `runners/myhermes.py` 可以认识 MyHermes 边界。Orchestrator 只消费 `TrialRunnerPort`，因此 Validator、聚合和报告不依赖 MyHermes 的 Python 类型或 SQLite 私有表。
