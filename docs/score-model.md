# P2 Score 模型

P2 只有三个一级质量指标，不计算跨维度加权总分。

| Score | 本地来源 | 创建条件 | 值 |
| --- | --- | --- | --- |
| `task_success` | Worker 完成且 required 本地 deterministic/tool/retrieval/state gate 全部成功的 `TrialResult.task_passed` | 本地任务事实可判定 | 0 或 1 |
| `tool_correctness` | completed、required tool trajectory metrics | Case 声明并完成 tool evaluator | 0～1，多个约束取本地通过率 |
| `answer_quality` | completed `JudgeResult.overall_score` | Judge 实际完成 | 0～1 |

Judge `skipped`、`error` 或 `not_applicable` 时不创建 `answer_quality` Score。没有 tool evaluator 时不创建 `tool_correctness`，不会默认填 1。Evaluator 自身错误也不填 0。

`duration_ms`、`total_tokens`、`iterations`、`tool_call_count`、Recall@K、MRR、Memory query duration/item count/size/write count 是诊断或效率信息，不参与质量总分。Recall/MRR 只有 Suite 显式阈值时才成为对应 retrieval evaluator 的确定性门禁，也不创建第四个一级 Score。Judge token 与 duration 保留在 `JudgeResult` 和 Judge generation。

每个远端 Score 带 source、evaluator version、简短 comment、Trial ID 和 Case ID。稳定 Score ID 是 `trace_id + score_name + evaluator_version + trial_id` 的 SHA-256；时间戳优先使用 `TrialResult.finished_at`，否则使用首次写入发布清单的时间。三个分数先在本地计算；Langfuse 不作为计算位置。

Score identity 同时记录 `case_id` 与规范化 `value_hash`。confirmed 重复项跳过；pending、failed 或 uncertain 重试必须复用相同 ID、name、Trace、evaluator version 和 timestamp。相同 identity 的 `value_hash` 改变会抛出结构化冲突，调用方必须提升 evaluator version，不能借重试静默覆盖评分含义。详见 [发布幂等合同](publication-idempotency.md)。

`TrialResult.passed` 是全部 required evaluator（含 Judge）的运行门禁，不等同于新的综合分。`task_passed` 不包含 Judge，但 P3 会纳入 required tool、retrieval evidence 和 Memory state；`retrieval_gate_passed`、`final_answer_gate_passed`、`memory_state_gate_passed` 仍独立保留，避免把上下文暴露与答案使用合并。
