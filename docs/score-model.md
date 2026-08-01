# P2 Score 模型

P2 只有三个一级质量指标，不计算跨维度加权总分。

| Score | 本地来源 | 创建条件 | 值 |
| --- | --- | --- | --- |
| `task_success` | Worker 完成且 required deterministic gate 成功的 `TrialResult.task_passed` | 本地任务事实可判定 | 0 或 1 |
| `tool_correctness` | completed、required tool trajectory metrics | Case 声明并完成 tool evaluator | 0～1，多个约束取本地通过率 |
| `answer_quality` | completed `JudgeResult.overall_score` | Judge 实际完成 | 0～1 |

Judge `skipped`、`error` 或 `not_applicable` 时不创建 `answer_quality` Score。没有 tool evaluator 时不创建 `tool_correctness`，不会默认填 1。Evaluator 自身错误也不填 0。

`duration_ms`、`total_tokens`、`iterations` 与 `tool_call_count` 是效率信息，进入 Trace/Score metadata，不参与质量总分。Judge token 与 duration 保留在 `JudgeResult` 和 Judge generation。

每个远端 Score 带 source、evaluator version、简短 comment、Trial ID 和 Case ID。稳定 Score ID 使用 `trace_id + score_name + evaluator_version` 派生，阻止同一 evaluator 版本的有意重复发布。三个分数先在本地计算；Langfuse 不作为唯一计算位置。

`TrialResult.passed` 是 required evaluator 的运行门禁，不等同于新的综合分：required tool 或 required Judge 可令 overall Trial 不通过，同时 `task_passed` 仍保留原始 deterministic 任务事实。
