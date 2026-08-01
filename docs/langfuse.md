# Langfuse 集成

P2 锁定 `langfuse>=4,<5`。Langfuse Python SDK 在 v4 重写；当前构造参数使用 `base_url`，同时兼容旧环境名 `LANGFUSE_HOST`。实现依据当前官方 [Langfuse Python SDK](https://github.com/langfuse/langfuse-python)、[v4 client 源码](https://github.com/langfuse/langfuse-python/blob/main/langfuse/_client/client.py) 与 [Dataset Run Item API](https://github.com/langfuse/langfuse-python/blob/main/langfuse/api/dataset_run_items/client.py)，不使用 v2/v3 示例。

## 配置

安装：

```bash
python -m pip install -e ".[langfuse]"
```

环境变量：

- `LANGFUSE_PUBLIC_KEY`，必填；
- `LANGFUSE_SECRET_KEY`，必填；
- `LANGFUSE_BASE_URL`，可选且优先；
- `LANGFUSE_HOST`，兼容环境名，仅在没有 `LANGFUSE_BASE_URL` 时使用；
- `LANGFUSE_TIMEOUT`，可选的 1～600 秒整数。

Host 只能是无认证信息、query 或 fragment 的 HTTP(S) URL。Suite、报告和 Artifact 不保存密钥。普通 P1 导入路径不会导入 SDK。

## Dataset

一个 Suite 对应一个 Dataset，一个 Case 内容版本对应一个 Dataset Item。Item ID 是 `dataset_name + suite_id + case_id + case_sha256` 的稳定 UUID5。SDK 的 `create_dataset_item(..., id=...)` 具有 upsert 语义；Case hash 与发布投影 hash 都不变时不写，分类、`--langfuse-no-content` 或外部 Fixture 摘要变化时更新同一 Case 版本的受控投影，Case 内容变化时创建新的稳定版本。历史 Item 和历史 Experiment 保留，P2 没有 prune 或删除命令。

Item input 包含 Case ID、模式、tags 和受分类控制的用户输入。expected output 是 Judge rubric/criteria 与确定性期望的结构化投影。Fixture 正文永不上传；文件 Fixture metadata 只含受控相对标识、SHA-256、content type、字节数、synthetic 标志和 `content_uploaded=false`。Memory、Skill 与数据库 Fixture 正文不上传。

`sync --dry-run` 只构造本地身份和内容投影，不导入 SDK、不连接、不创建 Dataset。由于不读远端，新增/更新/不变三个远端计数明确显示为 unknown，而不是伪造数量。

## Experiment 与 Trace

Audit 自己串行执行 MyHermes，随后通过 `api.dataset_run_items.create` 将稳定 Trial Trace 与已同步 Dataset Item、Experiment name 关联。映射如下：

- `AuditRunResult` → Langfuse Dataset Run / Experiment identity；
- `TrialResult` → 根 span `myhermes.audit.trial`；
- scripted turn → `myhermes.audit.turn`；
- 公共 Model Observation → generation `myhermes.agent.model`；
- 公共 Tool Observation → tool observation `myhermes.agent.tool`；
- deterministic/runtime metric → evaluator `myhermes.audit.validator`；
- 实际完成的 Judge → generation `myhermes.audit.judge`；
- 未调用的 Judge → 同名 evaluator 状态记录，不伪造 generation。

Trace metadata 保存 Audit/Suite/Case/Trial 身份、subject commit/dirty、Audit version/commit、subject model（安全可得时）、Judge model/prompt version、分类、tags、runtime status、worker protocol 和效率数据。不保存 config、绝对路径、隐藏系统 Prompt、隐藏推理或工具正文。

## Score

只发布 `task_success`、`tool_correctness`、`answer_quality`。Score ID 由 `trace_id + score_name + evaluator_version` 稳定派生，因此重试不会有意创建同一版本的重复 Score。每个 Score metadata 包含 source、evaluator version、Trial ID 和 Case ID。duration、tokens、iterations、tool call count 只作为效率 metadata。

## 生命周期与可见性

一个 CLI run 使用一个 adapter。连接检查使用 `auth_check()`；所有 Trial 后先完成本地 Experiment identity，再调用 `flush()` 和 `shutdown()`。SDK v4 明确说明 flush 保证交付但不保证立即可读，因此 P2 不在 flush 后立即反查并把短暂读取不到误判为上传失败。

Trace URL 只有 SDK 实际返回并通过安全 URL 检查时才进入 receipt；Experiment URL 不推测构造。发布错误先映射成 Audit 异常并脱敏。无 `--langfuse` 时，本地 P1 路径完全独立。

## 事实来源

`AuditRunResult` 是 task gate、Judge 结果、效率数据和发布状态的事实来源。Langfuse 是 Dataset 管理、Experiment 观测、Trace 展示和 Score 分析层，不在远端重新计算或取代本地 `task_success`、`tool_correctness`、`answer_quality`。
