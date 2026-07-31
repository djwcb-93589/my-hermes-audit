# P1 安全边界

## 环境与凭据

父进程集中定义 Suite 可覆盖白名单：`MODEL`、`MAX_ITERATIONS`、`MODEL_TIMEOUT_SECONDS`、`MODEL_MAX_OUTPUT_TOKENS`。Suite 不能覆盖路径、Python 导入、用户目录、虚拟环境或任何名称包含 API key/token/password/secret/credential 语义的变量。模型凭据仅从启动 Audit 的父环境继承，实际值会从日志和安全摘要中替换。

生成的 `config.yaml` 使用 safe YAML、有限 JSON 类型和“仅覆盖已存在路径”的深合并。明文 secret 字段被拒绝，白名单环境占位符允许；background review、browser 与 plugins 被强制关闭。配置尽量设置为 `0600`，不注册为 Artifact，也不复制真实 `.env`。

## 路径、Fixture 与 Artifact

每个 Trial 拥有独立 workspace、`HERMES_HOME`、SQLite 与 artifacts。Fixture 仅支持内联内容或相对 Suite 的只读普通文件，目标只能位于 workspace/`HERMES_HOME`；配置、数据库、所有权 marker 与 manifest 名称受保护。Fixture manifest 只记录目标相对路径、大小和 SHA-256，不记录 source 绝对路径。

Validator 与 Artifact 路径拒绝绝对路径、`..`、符号链接逃逸和 Trial 根外目标。ArtifactRef 仅保存 Trial 相对路径、摘要和大小。日志读取有界，超限保留头尾；报告不包含完整环境、Sandbox 路径、API key、完整工具正文或模型隐藏推理。

## 子进程

只有 Worker 在隔离边界验证后导入 MyHermes。子进程 cwd 固定为 Trial workspace，使用 Python 安全路径模式，禁用 bytecode 写入；`PYTHONPATH` 只存在于该子进程，并只加入 subject repo 与 Audit 包根。超时和异常均走进程组清理；MyHermes 公共生命周期关闭先于进程退出。
