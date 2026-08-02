# P1 能力边界

P1 支持真实 MyHermes 单轮和固定 user-only 多轮会话、显式无工具/file/terminal toolset、文件 Fixture、每 Trial 独立子进程与 SQLite、公共 run/model/tool Observation 安全投影、确定性文件/文本/JSON/工具轨迹校验、串行多 Trial、超时、JSON 报告和 CLI 摘要。

P1 阶段当时不支持 simulated_user、LLM Judge、Retrieval、Compression、Background Review evaluator、Memory/Skill/数据库 Fixture、Gateway、Cron、Delegate、Browser、Langfuse、OpenTelemetry、Baseline Compare、并行调度或 CI。后续 P2–P4 只在各自文档声明的边界内扩展；没有 `ablation` 的旧 P1 Suite 仍保持原执行路径。

当前 MyHermes 公共 Monitoring 投影能关联 run、model 与 tool 调用，并提供安全状态、计数、token 与工具成功信息；它不公开模型错误的完整分类，因此 Audit 不查询 SQLite 私有表补齐该字段。P1 也不验证精确工具参数或精确调用顺序。父进程 fallback 无法关闭尚未成功构造的 MyHermes 对象，但独立 Worker 进程退出会限定这类资源的生命周期。

Trial cwd、路径校验和 ToolPolicy 是评测隔离，不是操作系统级容器；`terminal` 仍具有启动 Audit 的账户权限。Audit 在启动前检查配置结构、密钥和覆盖边界，MyHermes 完整语义校验仍发生在隔离 Worker 导入时。默认清理会删除 Trial 内的实体 Artifact，报告保留其相对引用、大小和摘要；需要现场文件时应启用保留策略。

本阶段仅进行代码审阅、合同设计和 Git 差异检查，不运行示例、Agent、模型或测试入口。
