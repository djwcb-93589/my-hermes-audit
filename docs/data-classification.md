# 数据分类与远端内容策略

Suite 在 `defaults.metadata.data_classification` 声明 `synthetic`、`internal` 或 `sensitive`，默认 `internal`。Case 可提高保护等级，但合同拒绝从 Suite 的较高等级降为较低等级。CLI 的 `--langfuse-no-content` 可进一步关闭内容，Suite 不能覆盖这一选择。

## `synthetic`

可上传经凭据脱敏和受控截断的用户输入、最终输出、Judge rubric/result 与 Validator 摘要。仍不上传 Fixture 正文、隐藏 Prompt、隐藏推理、完整工具参数/结果、config、环境或数据库内容。

## `internal`

默认上传受控头尾截断的输入和输出；单个字符串上限比 synthetic 更低。Fixture 只上传相对标识、SHA-256、content type、大小、synthetic 标志和状态。所有通用禁止项仍适用。

## `sensitive`

输入、输出、rubric 和结果内容都投影为 `content_omitted=true`、分类、canonical SHA-256 与序列化长度。状态、指标、稳定身份和不含内容的效率 metadata 可以上传。不会上传原始文本或所谓“摘要”来绕过分类。

## 强制无内容

`--langfuse-no-content` 对任意分类使用与 sensitive 相同的内容省略投影，但不改变本地报告。该开关同时作用于 Dataset Item 和 Trial/Judge Observation。

## 凭据与错误

Judge 与 Langfuse 使用互不复用的环境变量。外发内容和外部错误至少覆盖当前敏感环境值、Authorization header、Bearer/Basic、常见 API key 前缀、JWT、私钥与 URL credentials。错误写报告前还移除常见本地绝对路径并限制长度。

MyHermes Worker 的环境由独立 allowlist 构造，不继承 `LANGFUSE_*` 或 `AUDIT_JUDGE_*`。Langfuse/ Judge SDK 对象、key、完整 base URL query、完整环境和 config 永不进入 Audit 合同、Artifact 或报告。
