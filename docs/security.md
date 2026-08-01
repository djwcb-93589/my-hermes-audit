# P1 安全边界

## 环境与凭据

父进程集中定义 Suite 可覆盖白名单：`MODEL`、`MAX_ITERATIONS`、`MODEL_TIMEOUT_SECONDS`、`MODEL_MAX_OUTPUT_TOKENS`。Suite 不能覆盖路径、Python 导入、用户目录、虚拟环境或任何名称包含 API key/token/password/secret/credential 语义的变量。模型凭据仅从启动 Audit 的父环境继承，实际值会从日志和安全摘要中替换。

生成的 `config.yaml` 使用 safe YAML、有限 JSON 类型和“仅覆盖已存在路径”的深合并。明文 secret 字段被拒绝，白名单环境占位符允许；background review、browser 与 plugins 被强制关闭。配置尽量设置为 `0600`，不注册为 Artifact，也不复制真实 `.env`。

## 路径、Fixture 与 Artifact

每个 Trial 拥有独立 workspace、`HERMES_HOME`、SQLite 与 artifacts。Fixture 仅支持内联内容或相对 Suite 的只读普通文件，目标只能位于 workspace/`HERMES_HOME`；配置、数据库、所有权 marker 与 manifest 名称受保护。Fixture manifest 只记录目标相对路径、大小和 SHA-256，不记录 source 绝对路径。

Validator 与 Artifact 路径拒绝绝对路径、`..`、符号链接逃逸和 Trial 根外目标。ArtifactRef 仅保存 Trial 相对路径、摘要和大小。日志读取有界，超限保留头尾；报告不包含完整环境、Sandbox 路径、API key、完整工具正文或模型隐藏推理。

## 子进程

只有 Worker 在隔离边界验证后导入 MyHermes。子进程 cwd 固定为 Trial workspace，使用 Python 安全路径模式，禁用 bytecode 写入；`PYTHONPATH` 只存在于该子进程，并只加入 subject repo 与 Audit 包根。超时和异常均走进程组清理；MyHermes 公共生命周期关闭先于进程退出。

Worker 的结构化结果与转录先完成协议一致性校验，再由父进程按当前敏感环境值和常见凭据格式重写为等结构脱敏 Artifact；本地 `AuditRunResult`、Validator 与后续外发层只消费该脱敏投影。工具参数、工具结果正文和隐藏 Prompt 不在 Worker 公共 Observation 合同中。

## P2 外部适配层

Judge 与 Langfuse 只在父进程初始化，使用不同环境变量，且都不在 Worker 继承白名单中。Suite 不能保存 key 或 base URL。普通本地 P1 路径不导入相应 SDK。

Judge Prompt 把 rubric/criteria 标为 trusted rule，把任务、候选输出、会话和 runtime evidence 标为 untrusted data；工具 evidence 不含参数与结果正文。Prompt 采用透明头尾截断和 48,000 字符硬上限，不保存 raw response 或隐藏推理。

Langfuse 内容由 `synthetic`、`internal`、`sensitive` 和 CLI `--langfuse-no-content` 共同约束。Fixture、Memory、Skill 和数据库正文、完整工具内容、隐藏 Prompt、config 与绝对路径不上传。外部内容与错误在进入 SDK/报告前覆盖环境敏感值、Authorization、Bearer/Basic、常见 key、JWT、私钥与 URL credentials；外部错误还移除常见绝对路径并有长度上限。

P2.1 的发布清单与 JSON 报告并列写入，采用原子替换并尽量设置为 `0600`。清单只保存本地/远端身份、摘要哈希、稳定时间戳、尝试次数、状态和脱敏错误；不保存凭据、完整输入输出、Fixture 正文、隐藏 Prompt、工具正文或绝对 Sandbox 路径。网络超时不会被标成 confirmed。

Experiment Runner 的回放载荷继续经过现有数据分类投影。回放代码位于 Audit 父进程，只消费已经脱敏的本地合同；Langfuse 凭据、Judge 凭据和模型凭据都不会传入 MyHermes Worker。运行时只使用官方公开 SDK 方法，不读取 transport、下划线成员或自动生成客户端资源。
