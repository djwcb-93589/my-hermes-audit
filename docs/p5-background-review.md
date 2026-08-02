# P5：Background Review 评测

## 范围与运行方式

P5 评测 MyHermes 对 Memory Review 和 Skill Review 的决策、证据窗口、真实状态变化、治理拒绝和重复执行安全；它不是普通 Agent 项目的人工审批评测，也不实现 MyHermes 的后台队列性能或端到端回归闭环。后两项属于 P6。

每个声明了 `fixture.background_review_plans` 的 Trial，才会在隔离 Worker 内创建一个 trial-local 的 `MyHermesBackgroundReviewAdapter`。它复用 MyHermes 的公开 Driver、Registry、`ReviewAgentLoop`、`ToolPolicy`、`ToolRegistry` 和公开 Memory/Skill API，以同步方式完成一次可收口的评测：

```text
foreground turn 完成
→ Driver.record_progress / claim_due
→ prepare_run 捕获 Subject 证据窗口
→ claim 再验证
→ 真实 ReviewAgentLoop 同步执行
→ Driver.complete 或 Driver.fail
→ after live snapshot 与 observed state diff
```

同步仅服务于 P5 的确定性、trial-local 评测；它不替代 MyHermes 的真实异步 Runtime，也不使用全局 Background Review singleton。每个 Review 有显式 timeout，Worker cleanup 前会收口 executor，因此 Review 不会越过 Trial 生命周期。

未声明 Review Plan 的 P0–P4 Case 不创建 Review Driver、Loop、Tool Registry 或 Coordinator；其 `background_review_results`、`background_review_errors` 为空，`review_gate_passed` 为 `null`。

## Plan、状态与事实

`BackgroundReviewPlan` 显式包含 `review_id`、kind、逻辑 foreground session、trigger turn、timeout、lifecycle 和有限的 `repeat_count`。P5 支持：

| lifecycle | 含义 |
| --- | --- |
| `normal` | 领取并同步执行一次 Subject Review。 |
| `duplicate_execute` | 首次完成后检查同一 claim；第二次不得运行模型、工具或写状态。 |
| `stale_before_execute` | 需要 Subject 的公开治理 revision/claim 验证证明旧 claim 已失效。 |

`completed`、`failed`、`rejected` 和 `stale` 是不同状态。`completed` 可以是有变更，也可以是 no-op；`failed`、`rejected`、`stale` 仍会保留 before/after snapshot 和 `observed_changes`，因为错误或 no-op 不保证没有半写入。

`ReviewOutcome.changes` 是 Subject 结果的规范化表示；`observed_changes` 是由 live before/after snapshot 计算出的事实。两者不能互相替代。任何 protected、非目标或终态后的意外变化都必须被记录，并由安全门禁判失败。

Memory snapshot 继续复用 P3 的公共 Memory Adapter。Skill snapshot 只使用公开 Skill inventory 和治理字段：`skill_id`、name、source、managed_by、pinned、revision、governance_revision；不读取 Skill 文件或私有治理数据库。

## 证据边界

P5 区分两份安全投影：

- foreground source projection：当前 Trial 实际前台会话与公开工具轨迹；
- Subject prepared review projection：`ReviewDriver.prepare_run().messages` 实际交给 `ReviewAgentLoop` 的窗口。

支持的 evidence kind 是 `user_message`、`tool_observation`、`tool_error`、`assistant_decision_unverified` 和 `assistant_report_unverified`。Assistant 决策和报告始终是 unverified；`tool_observation` 的 `ok=true` 只证明调用完成，不自动证明用户目标或长期事实成立。Suite 不能把自定义正文写入 Review 持久化层冒充前台事实。

每个投影都有稳定 ID、顺序、hash、长度与来源关联；不包含 Review Prompt、隐藏推理、claim token、凭据、Memory/Skill 正文或完整工具正文。

## Validator 与 Review gate

`background_review` evaluator 对每个 expectation 分别生成六类结构化 Metric，而不生成第四个一级分数：

1. decision correctness；
2. evidence completeness；
3. update correctness；
4. stale rejection；
5. side-effect safety；
6. idempotency。

required Background Review evaluator 任意失败或 error 时，`review_gate_passed=false`，并使 `task_passed=false` 与 `passed=false`。没有 required Review evaluator 时 gate 为 `null`；一级质量分数仍只有 `task_success`、`tool_correctness`、`answer_quality`。

## Capability-negative stale 场景

stale 不能根据“没有变化”猜测。它必须由 Subject 公共 claim validation 或 governance revision API 证明。当前公开 MyHermes 表面不能绑定 Skill governance revision 到 claim validity，所以默认 Suite 不包含 stale。

[`../examples/background_review_capability_negative_v1.yaml`](../examples/background_review_capability_negative_v1.yaml) 仅声明这一 stale 场景，预期在创建 Sandbox 前以 capability preflight 拒绝，而不是伪造 stale 结果。

## Artifacts、Langfuse 与示例

Worker v4 为 P5 输出 `background-review-results.json`、`background-review-evidence.json` 与 `background-review-snapshots.json`，并通过既有同目录原子写入发布。它们只含结构化、安全事实。

Langfuse 只回放持久化的 `TrialResult`，不重新运行 Review、模型或工具。即使未启用 `--langfuse-no-content`，P5 evidence、Review Prompt、Memory、Skill 和工具正文都不会上传；`sensitive` 同样强制无正文投影。

[`../examples/background_review_v1.yaml`](../examples/background_review_v1.yaml) 是默认 synthetic P5 Suite：Memory no-op、Skill verified update、冲突证据 no-op、duplicate idempotency、user-managed 保护和 pinned 无半写入。它不包含 stale；stale 位于独立 capability-negative Suite。示例仅声明合约，开发构建阶段不执行 Trial、模型、Judge 或远端发布。
