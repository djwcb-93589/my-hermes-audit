# P1 Runner

P1 为每个 Trial 启动一个新的 Python 子进程。原因不是并行性能，而是 MyHermes 配置在导入时读取 `HERMES_HOME`、`DB_PATH`、`config.yaml` 与环境，并构造运行期对象；进程边界能够让这些一次性副作用天然隔离。

父进程依次完成 Suite/Case/Fixture/evaluator/config preflight、创建 Sandbox、物化文件 Fixture、生成无明文凭据配置，并通过 `sys.executable -P -m myhermes_audit.integrations.myhermes.worker` 启动 Worker。`-P` 阻止 workspace 被隐式放到模块搜索路径最前方。父进程只用 `subprocess env` 设置 Trial 环境，不改自己的环境，也不执行 reload、monkeypatch 或 `sys.modules` 清理。

Worker 在导入 `hermes.*` 之前验证 cwd、`HERMES_HOME`、`DB_PATH`、请求和结果路径。随后使用 MyHermes 公开的数据库初始化、会话创建、`register_all`、`ToolPolicy`、`build_system_prompt`、Observation sink 与 `run_conversation`。脚本多轮复用同一个 session，固定输入只允许 user turn。

P1 串行执行 Trial。POSIX Worker 使用新 session，Windows 使用新进程组。超时时先发送协作终止信号，短暂等待，再强制清理进程组。Worker 的公开 shutdown 路径负责 process/delegate/session/background review/模型客户端资源，父进程的进程组终止是最后兜底。

Trial 成功要求 Worker completed，且所有 required deterministic 与 tool_trajectory evaluator 都通过。单 Trial 的运行、协议、Validator 或清理失败会形成结构化失败，后续 Case 继续执行；Suite 合同或 subject preflight 失败则不启动任何 Trial。
