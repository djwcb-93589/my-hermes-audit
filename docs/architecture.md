# P0 架构与依赖方向

## 核心规则

`myhermes_audit` 是被测运行时之外的独立包。它可以描述输入、环境与结果，但不能导入 `hermes.*`，不能触发模型，也不能解释 MyHermes 内部数据库 schema。

```text
myhermes_audit core
  contracts / datasets / sandbox / fingerprint / ports / CLI
                         ↑
              runners / integrations          （P0 尚未实现）
                         ↓
              my-hermes public runtime         （被测项目）
```

中间适配层是唯一允许同时认识两侧的层：向上实现 Audit ports 并产出 Audit 合同，向下调用 MyHermes 的公共运行入口。核心层不知道 MyHermes 类名、模型 SDK、SQLite 表或 Background Review driver。

## 模块职责

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `contracts` | 版本化 Suite、Result、Memory、Review、Fingerprint 模型 | 执行、评分、持久化 |
| `datasets` | 安全 YAML 读取、严格校验、source 路径解析 | 读取或复制 Fixture 内容 |
| `sandbox` | Trial 目录隔离、环境覆盖值、受控 Fixture I/O、所有权清理 | 修改全局环境、初始化 MyHermes DB |
| `fingerprint` | canonical hash、只读 Git 状态、Audit 平台指纹 | checkout、commit、修改 index |
| `ports` | 未来 Memory/Review adapter 的异步形状 | Provider 或 Runner 实现 |
| `cli` | `validate` 与 `schema` 静态操作 | `run`、`report`、`compare` |

## 与 MyHermes 当前语义的对齐

- MyHermes 要求 Python `>=3.13`，Audit 使用相同下限。
- `AgentLoopResult` 可在没有自然语言 final output 时由文件、工具、数据库或 Review 状态表达结果，因此 `TrialResult.final_output` 可空。
- MyHermes 把 `HERMES_HOME` 作为 SOUL、配置、Memory、USER、Skills、scripts 等状态根，并把相对 `db_path` 解析到该根；Sandbox 分别提供 `HERMES_HOME` 与 `DB_PATH`。
- Review 证据保持五类来源：用户消息、工具观察、工具错误、未验证 Assistant 决策、未验证 Assistant 报告。
- Skill 快照保留 source、managed_by、pinned、revision 与 governance_revision，用于表达治理保护和 stale rejection，而不导入 MyHermes Skill 类型。

## 扩展边界

任意 JSON 只允许出现在窄作用域：Trial metadata、Execution config overrides、单个 evaluator config、Memory filters/metadata 和少数结果 metadata。Suite、Case、Fixture、Expected、Review 与 Result 的领域结构不能整体退化为无约束字典。
