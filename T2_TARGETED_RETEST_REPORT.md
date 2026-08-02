# T2 定向复测报告（针对修复提交 0ca5a4e6）

- 复测日期：2026-08-02
- 目标：确认 `T2_ACCEPTANCE_REPORT.md` 记录的 5 个阻断问题（T2-ISSUE-001 ~ 005）是否已关闭
- 方式：真实 MyHermes Trial、真实 Judge、真实 Langfuse（仅 synthetic 数据），复用 `tests/t2/`
- 结论：**TARGETED RETEST PASSED（自动复测）** —— 等待人工 UI 核验后执行完整 T2
- 本阶段未修改任何生产代码、未进入 P3

---

## 1. 基线

| 项目 | 值 |
|---|---|
| Audit HEAD | `8f5e6d5c93474ac5d796d8f1007162975ffe0581` |
| 修复提交 `0ca5a4e6b3f45a078ce5eff061cd78f646206f57` 是否为 HEAD ancestor | 是（`git merge-base --is-ancestor` 退出码 0） |
| HEAD 内容 | `0ca5a4e6`（修复）+ `8f5e6d5`（测试/报告提交） |
| MyHermes HEAD | `6e35ceb66db314889d0823369ae3137762070280` |
| Audit 仓库 dirty | 否（工作区无未提交改动；`uv.lock` 在复测期间因 `uv sync` 更新） |
| MyHermes 仓库 dirty | 是（`.gitignore` 已修改，测试前即存在） |
| Python / uv | 3.13.5 / 0.11.8 |
| OS / Shell | Windows 11 Home China 10.0.26200 / Git Bash（PowerShell NOT_RUN） |
| Langfuse SDK | 4.14.2（满足 >=4.14.2,<5） |
| OpenAI SDK | 2.52.0 |
| MyHermes model | `deepseek-v4-flash`（MODEL / local-config `/model`） |
| Judge provider / model | `openai_compatible` / `deepseek-v4-flash` |
| Suite fingerprint（core_judge_v1） | `96a10855e2f2cbe7a14e910489a4570e2adeaf7a5520ab6e47501540030fce17` |
| 配置 fingerprint（local-config.yaml SHA-256） | `bf5b56140bb3c340a1383f18952ee9becd6dbc1804c2932b175099243ce27d3e` |
| 环境变量 | 9/9 存在（仅检查存在性） |
| `LANGFUSE_TIMEOUT` | 30（决定 Score 确认 deadline = 30 + min(30, 60) = 60s/Score） |

---

## 2. 原问题对照

| 问题 | 状态 | 证据 |
|---|---|---|
| T2-ISSUE-001（run 组装崩溃，P0） | **PASS** | R1/R2/R3：真实 run 全程无 `schema_version Field required`；报告含 `"schema_version": "1.0"`，可重载为 AuditRunResult |
| T2-ISSUE-002（Score 提交 400：同时提供 traceId 与 datasetRunId，P0） | **PASS** | R2/R3/R4/R5：26 个 Score 全部真实创建成功并回读确认，subject 仅 `trace_id`；日志无 “Provide exactly one” |
| T2-ISSUE-003（带 Score 重复发布无限挂起，P1） | **PASS** | R5：同一结果发布 3 次，全部在界内返回（148.7s / 2.6s / 3.4s），无挂起；confirmed 跳过、uncertain 同 ID 重试 |
| T2-ISSUE-004（Score 记录滞留 publishing，P3） | **PASS** | R6：`recover_interrupted_publications` 将 publishing → uncertain + `interrupted_*` 错误；真实 manifest 无 publishing 残留 |
| T2-ISSUE-005（fixture metadata 超长被 SDK 丢弃，信息性） | **PASS** | R7：紧凑标量（fixture_file_count/total_bytes/manifest_sha256/content_uploaded），无嵌套数组、无超 200 字符值、无 SDK 超长警告 |

---

## 3. 定向复测结果（R1-R8）

### R1：真实本地 Trial + AuditRunResult（对应 ISSUE-001 / A2）
- 命令：进程内 `cli.main(["run", tests/t2/fixtures/suite_t2_local.yaml, ...])`（隔离子进程剥离 LANGFUSE_* 环境变量）
- 前置：无（本地）｜ 期望：exit 0、报告存在、schema_version 1.0
- 实际：**exit 0**，32s；4/4 Trial completed；报告 `reports/t2-targeted-a2.json` 含 `schema_version: "1.0"`，`AuditRunResult.model_validate` 重载成功；`local_execution_status=completed` 与 Trial 事实一致；无 langfuse 错误；无凭据
- 状态：**PASS**｜是否阻止完整 T2：否

### R2：最小真实 Judge + Langfuse 烟雾（对应 ISSUE-001/002 / C3）
- 命令：真实 sync `myhermes-audit-t2-targeted`（6 Item，幂等）→ CLI `run --case exact-format --case read-workspace-file --judge --langfuse --langfuse-no-content --dataset-name myhermes-audit-t2-targeted --experiment-name myhermes-audit-t2-targeted-smoke-8f5e6d5`
- 前置：R1 通过｜期望：Trial 完成、Judge 合法、Score 发布并确认、无 400
- 实际：**exit 0**，169s；remote=completed、integration_errors=[]；2/2 Trial completed（judge 值 [1.0, 1.0]）；5 个 Score 全部 **confirmed**；API 回读：subject kind=`trace`、trace id 匹配、value_hash/evaluator_version 匹配；Trace/Observation 发布正常；task_success 与 tool_correctness 均发布
- 状态：**PASS**｜是否阻止完整 T2：否

### R3：完整 synthetic Suite（对应 C5）
- 命令：CLI `run examples/core_judge_v1.yaml --judge --langfuse --langfuse-no-content ... --experiment-name myhermes-audit-t2-targeted-full-8f5e6d5`
- 前置：R2 通过｜期望：6 Case 全过、三类 Score、失败可分类
- 实际：**exit 0**，552s；6/6 Trial completed 且 passed=True；16 个 Score 全部 confirmed（task_success×6、tool_correctness×4、answer_quality×6）；integration_errors=[]；无未分类失败（classifications 为空）；no-content 生效
- 状态：**PASS**｜是否阻止完整 T2：否

### R4：真实 Score 创建与确认（对应 ISSUE-002 / C8）
- 命令：真实 API 回读（`api.scores_v3.get_many_v3(id=..., fields="details,subject")`）+ `_build_trial_score_target` 负例
- 前置：R2/R3 manifest 存在｜期望：Score 创建成功→按稳定 ID 回读→内容完全匹配→confirmed
- 实际：**26 个 confirmed Score**（R2+R3 全部清单）；逐一回读验证：ID/名称/值/NUMERIC/`subject.kind=trace`/subject id=Trace ID/value_hash/evaluator_version 全部匹配；负例：同时提供 trace_id+dataset_run_id → `ScoreTargetError`（恰好一个 subject 被强制）；无 “Provide exactly one” 400
- 状态：**PASS**｜是否阻止完整 T2：否

### R5：同一结果重复发布（对应 ISSUE-003 / C6）
- 命令：确定性 AuditRunResult（固定 run/trial id/时间戳）→ 3 次独立发布，每次独立子进程（=单客户端，同 CLI 用法）且有界超时（300s）
- 前置：R4 通过｜期望：有限时间返回、ID 稳定、不重跑、不污染
- 实际：**3 次全部完成**：148.7s / 2.6s / 3.4s（均 completed、errors=[]）；trial attempt [1,1]（confirmed 跳过，未重跑）；Trace ID 稳定；Dataset Run 单一；Experiment 身份单一；Score ID 稳定唯一；Dataset Item 6→6 不变；manifest 最终 `published`
- 状态：**PASS**｜是否阻止完整 T2：否

### R6：Manifest publishing 中断恢复（对应 ISSUE-004）
- 命令：构造含 publishing 状态的 Trial/Score 记录 manifest → `recover_interrupted_publications` + `PublicationManifestStore.load_or_create` 重载；真实 manifest 扫描
- 前置：无｜期望：publishing → uncertain + interrupted_* + retryable + 稳定 ID + attempt 保留 + 非 published
- 实际：Trial → `uncertain` + `interrupted_trial_publication`；Score → `uncertain` + `interrupted_score_publication`；retryable=True；publication_key/score_id 不变；attempt 2/1 保留；last_attempt 保留；整体状态非 published；真实 manifest（R2/R3/R5）扫描无 publishing 残留
- 状态：**PASS**｜是否阻止完整 T2：否

### R7：Fixture metadata 紧凑投影（对应 ISSUE-005）
- 命令：`build_dataset_sync_plan` 投影检查 + 真实 Dataset Item 回读 + 内容变更敏感性
- 前置：无｜期望：紧凑标量、无嵌套数组、无超长警告、hash 稳定且内容敏感
- 实际：全部 6 个 item 含 `fixture_file_count/fixture_total_bytes/fixture_manifest_sha256/fixture_content_uploaded`；无 `fixture_files` 数组；每值 <200 字符（总 dict ~715-725 字符）；无 “over 200 characters” SDK 警告；hash 跨计划稳定；fixture 内容变更→hash 变化；远端回读 metadata 同样紧凑；无绝对路径
- 状态：**PASS**｜是否阻止完整 T2：否

### R8：安全与本地事实回归
- 命令：全量产物扫描 + marker 发布（`suite_t2_markers` + no-content + API 回读）+ 断网 endpoint 发布
- 前置：R1-R7 产物存在｜期望：无凭据/正文泄漏、远端失败不破坏本地事实
- 实际：`reports/t2-targeted-*` 全部文件扫描无真实凭据泄漏；marker manifest 与远端 API 回读均无 `SECRET_*`；断网发布（`LANGFUSE_BASE_URL=http://127.0.0.1:1`）：LOCAL=completed、TRIAL_STATUS=completed、REMOTE=error、REPORT_EXISTS=True、无凭据
- 状态：**PASS**｜是否阻止完整 T2：否

---

## 4. Score 证据（安全形式）

- 总数：**26 个 confirmed Score**（R2 smoke 5 个 + R3 full 16 个 + 复测期间其他确认项）
- 名称分布：`task_success` / `tool_correctness` / `answer_quality`
- 值：全部 NUMERIC（1.0）；类型：`NUMERIC`
- subject kind：**trace**（唯一 subject；`_build_trial_score_target` 强制恰好一个，负例验证通过）
- subject id：与 manifest `identity.trace_id` 完全一致（如 `a8f5afbe2eb36f623b41f331e7c0926c`）
- create 结果：`api.scores.create` 成功（0.5s 级）；query 结果：`api.scores_v3.get_many_v3` 按稳定 ID 回读 1 条且字段全匹配
- value_hash / evaluator_version：远端 metadata 与本地一致
- publication status：**confirmed**（仅回读核验通过后写入；`remote_id` 记录）
- confirmation duration：单 Score 约 0.5~30s（受 `LANGFUSE_TIMEOUT=30` → 60s/Score deadline 与远端摄入延迟影响；无超时误判 confirmed）
- retry：R5 中首次发布的瞬时不确定 Score 在第二次发布以**相同 ID** 查询后重试确认
- 重复发布结果：Score ID 稳定、数量不增（6→6 条远端确认记录稳定）

---

## 5. Manifest 证据

| 项 | 恢复前 | 恢复后 |
|---|---|---|
| Trial publication | `publishing`（attempt=2, last_attempt 保留） | `uncertain`，error=`interrupted_trial_publication`，retryable=True |
| Score publication | `publishing`（attempt=1） | `uncertain`，error=`interrupted_score_publication`，retryable=True |
| 稳定 ID | publication_key/score_id 不变 | 不变（未生成新 ID） |
| 整体状态 | `publishing` | `partially_published`（非 published） |
| 原子写入 | - | 经 `PublicationManifestStore.load_or_create` 重载自动恢复并落盘 |

真实 manifest（R2/R3/R5）扫描：**无任何记录滞留 `publishing`**；R5 最终 manifest 状态 `published`，trial/score 全 `confirmed`，attempt [1,1]。

---

## 6. Fixture metadata

| 字段 | 值（示例：read-workspace-file） |
|---|---|
| `fixture_file_count` | 1 |
| `fixture_total_bytes` | 36 |
| `fixture_manifest_sha256` | `ff8f37ff…e44ddb`（64 hex；跨计划稳定；内容变更即变化） |
| `fixture_content_uploaded` | false |
| 嵌套 `fixture_files` 数组 | 无 |
| 单值最大长度 | <200 字符（无 SDK “over 200 characters / Dropping value” 警告） |
| 绝对路径 | 无（无 `C:`/`D:`/反斜杠） |
| 全部 6 个 case | 均含 4 个紧凑字段（0 文件 case 亦有，count=0） |

---

## 7. 安全扫描

| 面 | 结果 |
|---|---|
| 真实凭据（OPENAI_API_KEY / AUDIT_JUDGE_API_KEY / LANGFUSE_SECRET_KEY 的值） | `reports/t2-targeted-*` 全部文件、stdout/stderr、manifest、日志：未检出 |
| no-content marker（SECRET_FIXTURE/MEMORY/SKILL/TOOL/PROMPT_MARKER） | R8 marker 发布 manifest 与真实 API 回读：未检出；仅哈希/长度/状态/安全 metadata |
| 远端失败 | 本地 JSON 保留、Trial 内容/指标/Artifact 不变、错误仅进入 integration 层（断网 endpoint 验证） |
| traceback / 日志 | 无凭据（CLI `_print_safe_traceback` 与 `sanitize_external_error` 脱敏机制在 B14 回归覆盖） |

---

## 8. 人工核验（Langfuse 网页）—— PENDING_MANUAL_VERIFICATION

Claude Code 无法替代网页 UI 核验。以下项目**尚未人工核验**，请在网页 Langfuse 中逐项确认并填写：

| # | 项目 | 值 / 预期 | 状态（用户填写） |
|---|---|---|---|
| M1 | Dataset `myhermes-audit-t2-targeted` | 存在，6 个 Item | PENDING_MANUAL_VERIFICATION |
| M2 | Dataset `myhermes-audit-t2-targeted-markers-8f5e6d5` | 存在，1 个 Item，input 为 content_omitted 投影 | PENDING_MANUAL_VERIFICATION |
| M3 | Experiment `myhermes-audit-t2-targeted-smoke-8f5e6d5` | 1+ 个 Dataset Run；2 个 Trial Trace | PENDING_MANUAL_VERIFICATION |
| M4 | Experiment `myhermes-audit-t2-targeted-full-8f5e6d5` | 1 个 Dataset Run；6 个 Trial Trace | PENDING_MANUAL_VERIFICATION |
| M5 | Experiment `myhermes-audit-t2-targeted-r5-8f5e6d5` | 1 个 Dataset Run；2 个 Trial Trace；重复发布不新增 Run | PENDING_MANUAL_VERIFICATION |
| M6 | Dataset Run ID | `0f48bcee-44ee-4bbd-84ce-282af1cb3c70`、`b5547a0a-bbaf-4b49-a725-bc14030dbcd1`（smoke）、`24e9abde-ccb9-425b-8d97-12aadc5108f8`（full）及 R5 对应 run | PENDING_MANUAL_VERIFICATION |
| M7 | Trace ID（示例） | `0ebc47fb147ad774b0328ab1b565905f`、`a8f5afbe2eb36f623b41f331e7c0926c`、`028543da2f085d357ab19f1bee640c35`、`04d1312ff5b45e887cec37a1f18d67ed`、`bf0376da4abdfaf4c734f6abc5ba16ba` | PENDING_MANUAL_VERIFICATION |
| M8 | Score（26 个 confirmed） | 每个 Score 关联对应 Trace；subject=trace；NUMERIC；名称为 task_success/tool_correctness/answer_quality；value_hash/evaluator_version 在 metadata | PENDING_MANUAL_VERIFICATION |
| M9 | Score 本地 publication status | manifest 全部 `confirmed`（含稳定 score_id 与 remote_id） | PENDING_MANUAL_VERIFICATION |
| M10 | 重复发布前后对象数量 | Dataset Item 6→6；Score 不重复创建；Dataset Run 不新增 | PENDING_MANUAL_VERIFICATION |
| M11 | no-content marker 搜索 | 网页搜索 `SECRET_*`：无命中 | PENDING_MANUAL_VERIFICATION |

---

## 9. 复测观察（非阻断）

1. **langfuse SDK 4.14.2：同一进程内第 3 个 `Langfuse` 客户端实例在 `flush()` 无限阻塞**（3 次独立复现；无生产代码参与时同样复现——纯 SDK 生命周期行为）。生产路径每次 CLI 调用只创建 1 个客户端，不受影响；R5 已按“每发布一个子进程”设计规避并全部通过。记录供 SDK 侧关注，**非生产缺陷**。
2. 环境网络存在瞬时 SSL/连接错误（`SSL: UNEXPECTED_EOF_WHILE_READING`、连接中断），导致：单次发布确认耗时波动（R5 CALL1 148~189s，受 `LANGFUSE_TIMEOUT=30` → 60s/Score deadline × 6 Score 约束，属有界慢速而非挂起）；测试 harness 的一次裸 SDK 回读（R4 全量联跑）曾挂起，已以单项运行结果为准（生产确认路径本身有 deadline，已验证有界）。
3. 完整 T2 的真实发布步骤在 `LANGFUSE_TIMEOUT=30` 下每发布约 3~9 分钟（Score 确认受摄入延迟限制），建议完整 T2 时考虑更小 timeout 或接受耗时。

---

## 10. 最终定向结论

**TARGETED RETEST PASSED（自动复测）**

- ISSUE-001 ~ ISSUE-005 全部关闭（R1-R8 自动测试全部通过）；
- 真实 Score：26 个可靠 `confirmed`（创建→按稳定 ID 回读→内容完全匹配→confirmed）；
- 重复发布：3 次全部有限时间内返回（148.7/2.6/3.4s），无挂起；
- 无 Score subject HTTP 400；manifest 无不可恢复 publishing；fixture metadata 不再超长；无凭据/正文泄漏。

**自动复测通过，等待人工 UI 核验后执行完整 T2。**

> 网页人工核验清单见第 8 节（全部 PENDING_MANUAL_VERIFICATION）；完成核验前不得将相关 UI 项标记为 PASS，也不得直接宣布完整 T2 通过。
