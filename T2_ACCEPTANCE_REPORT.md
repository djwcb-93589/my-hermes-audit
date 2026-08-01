# T2 独立验收测试报告 — LLM Judge 与 Langfuse 集成

- 测试阶段：T2（独立测试阶段，非生产修复阶段）
- 执行日期：2026-08-01
- 测试方式：新增测试包 `tests/t2/`（零依赖断言框架 + Fake Judge / Fake Langfuse SDK），运行真实 CLI、真实 Judge、真实 Langfuse（仅 synthetic 数据）
- 结论：**T2 FAILED**（记录 3 个阻断级生产缺陷，本阶段未修复任何生产代码）

---

## 1. 环境基线

| 项目 | 值 |
|---|---|
| Audit commit | `7b19997be40b48b837e4e038582dea936380d8fa` |
| MyHermes commit | `068640c43803dfd04b7dca27fdd27d8fae0a7a77` |
| Audit 仓库 dirty | 是（未跟踪：`local-config.yaml`、`uv.lock`） |
| MyHermes 仓库 dirty | 是（`.gitignore` 已修改） |
| Python | 3.13.5 |
| uv | 0.11.8 |
| OS | Windows 11 Home China 10.0.26200 |
| 执行 Shell | Git Bash（Bash tool）；PowerShell 本轮 **NOT_RUN**（见第 9 节） |
| Langfuse SDK | 4.14.2 |
| OpenAI SDK | 2.52.0 |
| Judge provider | `openai_compatible`（OpenAI SDK responses.parse） |
| Judge model identifier | `deepseek-v4-flash`（`AUDIT_JUDGE_MODEL`） |
| MyHermes model identifier | `deepseek-v4-flash`（`MODEL`；local-config `/model` 同） |
| Suite fingerprint（core_judge_v1） | `96a10855e2f2cbe7a14e910489a4570e2adeaf7a5520ab6e47501540030fce17` |
| 配置 fingerprint（local-config.yaml SHA-256） | `bf5b56140bb3c340a1383f18952ee9becd6dbc1804c2932b175099243ce27d3e` |
| 环境变量存在性 | 9/9 存在（OPENAI×3、AUDIT_JUDGE×3、LANGFUSE×3；仅检查存在性，未打印值） |

---

## 2. 分层总结

| 层 | 状态 | 说明 |
|---|---|---|
| T2-A 纯本地测试 | **FAIL** | A1/A3–A8 全部通过（含 123 项离线断言）；A2 真实 Trial 被生产缺陷 T2-ISSUE-001 阻断（Trial 执行后结果组装崩溃，无报告） |
| T2-B Fake Adapter 契约测试 | **PASS** | B1–B14 全部通过，123/123 断言（见 `tests/t2/t2-results.log`）；B10 symlink 子检查因 Windows 无符号链接权限 SKIP（已注明） |
| T2-C 真实外部验收 | **FAIL** | C1/C2/C4/C7/C9 通过；C3/C5 被 T2-ISSUE-001 阻断；C6 Score 重试被 T2-ISSUE-002/003 阻断；C8 因 Score 提交真实失败不满足设计预期 |

离线测试总计：**123/123 通过**（A1: 12、A3/A4: 7、A8: 10、B1: 15、B2: 6、B3: 7、B4: 4、B5: 3、B6: 5、B7: 6、B8: 4、B9: 11、B10: 8、B11: 3、B12: 9、B13: 5、B14: 8）。

---

## 3. 测试文件变更

新增（全部位于 `tests/t2/`，未修改 `src/`，未修改 MyHermes）：

| 文件 | 用途 |
|---|---|
| `harness.py` | 零依赖断言框架、子进程 CLI 运行器、环境隔离、AuditRunResult/TrialResult 构造（走生产 `model_validate` 路径）、marker 扫描 |
| `fake_langfuse.py` | Fake Langfuse SDK（仅实现公开合同面）+ 内存后端 + 故障注入 + 真实 `LangfuseV4Adapter` 绑定工厂 |
| `fake_judge.py` | Fake JudgePort（脚本化结果/错误 + 计数器） |
| `fake_runner.py` | CountingTrialRunner（TrialRunnerPort 计数器：Agent/模型/工具/Validator/Sandbox/Session） |
| `fake_openai.py` | Fake OpenAI 客户端（responses.parse / chat.completions）驱动真实 Judge 适配器 |
| `b_helpers.py` | B 层共享构造器（指标、Trial、publisher 注入） |
| `block_langfuse.py` | A6 用 langfuse import blocker |
| `fixtures/suite_t2_local.yaml` | 本地执行套件（single/multi/tool/file + 可选 Judge） |
| `fixtures/suite_t2_required.yaml` | 必需 Judge 套件 |
| `fixtures/suite_t2_markers.yaml` | no-content marker 套件（SECRET_*） |
| `test_a1.py` `test_a3_a4.py` `test_a8_regression.py` | T2-A 用例 |
| `test_b1_judge_structure.py` … `test_b14_credentials.py` | T2-B 用例（14 个模块） |
| `run_a2_real.py` `run_c_real.py` `run_all.py` | 真实 Trial 运行 / 真实外部验收 / 全量运行器 |

确认：**未修改 `src/` 下任何生产文件；未修改 MyHermes 仓库**（两仓库 dirty 状态与测试开始前一致）。

---

## 4. 命令清单（摘要，无密钥）

| 命令 | 退出码 |
|---|---|
| `uv run python -m tests.t2.run_all`（123 项离线断言） | 0 |
| `uv run myhermes-audit validate examples/core_judge_v1.yaml`（含未知字段/重复 case 负例） | 0 / 2(负例) |
| `uv run myhermes-audit sync examples/core_judge_v1.yaml --dataset-name myhermes-audit-t2-synthetic --langfuse-no-content --dry-run`（C1/A5） | 0 |
| 真实 sync（C2，进程内 `LangfuseV4Adapter.sync_dataset`，两轮） | 0 |
| `uv run myhermes-audit run … --case exact-format --case read-workspace-file --judge --langfuse …`（C3 冒烟） | 3（T2-ISSUE-001 复现） |
| A2 真实 Trial（无 langfuse，进程内 cli.main） | 3（T2-ISSUE-001 复现） |
| 真实 Judge `OpenAICompatibleJudgeAdapter.evaluate`（C4） | 0 |
| C4 受控错误（子进程 `AUDIT_JUDGE_BASE_URL=http://127.0.0.1:1`） | 0（脚本内验证 evaluator_error） |
| C6 真实发布+重发（无 Score 结果，进程内 publish_audit_result×2） | 0 |
| C6 Score 提交证据（真实发布，进程内） | 0（验证 score_idempotency_error） |
| C6 Score 重发（有界 150s 子进程） | 124（timeout，T2-ISSUE-003 复现） |
| C7 断网（子进程 `LANGFUSE_BASE_URL=http://127.0.0.1:1`） | 0（脚本内验证 error 状态） |
| C9 marker 发布 + API 回读扫描 | 0 |

完整命令与逐项输出见各测试模块 stdout 与 `tests/t2/t2-results.log`、`reports/t2-c-evidence.json`。

---

## 5. 用例详情

### T2-A

| 编号 | 目标 | 命令/方式 | 期望 | 实际 | 状态 | 证据 | 阻断 P3 |
|---|---|---|---|---|---|---|---|
| A1 | Suite 静态校验 | validate + 负例 YAML | 加载成功、Case 无重复、未知字段拒绝、fingerprint 稳定、不联网 | 全部符合；fingerprint 两次一致 | **PASS** | `test_a1.py` | 否 |
| A2 | Langfuse 未启用真实 Trial | 进程内 cli.main（剥离 LANGFUSE_*） | Trial 完成、报告生成、无 langfuse 导入、退出码由本地决定 | Trial 执行（真实模型），但 `AuditRunResult` 组装崩溃：exit 3、无报告 | **FAIL**（T2-ISSUE-001） | `reports/t2-a2.json` 不存在；stderr 快照见下 | **是** |
| A3 | Judge disabled | JudgeService(None) + 结果构造 | 不发请求、skipped、不伪造 0 分、task/tool 指标不受影响、JSON 往返 | 全部符合 | **PASS** | `test_a3_a4.py` | 否 |
| A4 | Judge not applicable | JudgeService + failed trial | not_applicable、不调用、不产生虚假得分、非 evaluator error | 全部符合（汇总层可区分） | **PASS** | `test_a3_a4.py` | 否 |
| A5 | dry-run 不联网 | `sync --dry-run`（进程内，剥离 env） | 只生成计划、退出 0、不导入 langfuse | 符合；`Remote write: no`；`langfuse` 未进 sys.modules | **PASS** | `test_a1.py` | 否 |
| A6 | SDK 不可用 | import blocker + 真实 publisher（子进程） | 本地报告先存在、integration_errors 记录、remote=error、无凭据 | 符合（`langfuse_capability_error`）；另验证无本地报告时 publisher 拒绝并记录 publication_state_error | **PASS** | `test_a1.py` | 否 |
| A7 | 本地报告先于远端发布 | spy 验证 from_environment 调用时报告已落盘 | 报告先落盘、远端失败不改写 Trial | 符合（spy 证据 + publisher 无报告即拒绝的守卫） | **PASS** | `test_a1.py` | 否 |
| A8 | P1/P1.1 回归 | 10 项回归断言 | Sandbox/HERMES_HOME/workspace/SQLite 隔离、进程树清理、Toolset 开关、5 类 Validator、失败保留 Sandbox、凭据不进报告、capability preflight、run_conversation 不再误判、Probe 不发模型请求不建 Sandbox | 全部符合（真实 capability probe 对 D:\my-hermes 只读执行） | **PASS** | `test_a8_regression.py` | 否 |

### T2-B（全部离线；Fake SDK 仅模拟公开合同面）

| 编号 | 目标 | 状态 | 关键证据 |
|---|---|---|---|
| B1 | Judge 严格结构（合法/缺字段/未知字段/类型/越界/非 JSON/截断/空响应/timeout/provider error/criterion 缺失/重复/本地加权/无隐藏推理） | **PASS**（15） | 合法→completed 且 0.77 加权均值精确；非法→evaluator_error，无伪造 0 分；`answer_quality` 是唯一一级语义指标 |
| B2 | MetricStatus 合同 + 汇总区分 | **PASS**（6） | completed 必带值；skipped/N/A 序列化为 null 非 0；error 必带结构化错误；Judge 错误不覆盖确定性 Validator |
| B3 | Dataset 映射与幂等 | **PASS**（7） | 一 Suite 一 Dataset；uuid5 稳定 item id；重复同步 unchanged=6；内容变化→新 id+updated；suite 冲突→报错；dry-run 零远端调用 |
| B4 | 单 Experiment 生命周期 | **PASS**（4） | 多 Trial 同一 run/experiment；ID/URL 来自 Adapter 返回；run id 分裂立即失败；不静默多 Run |
| B5 | 回放不重跑本地工作 | **PASS**（3） | 计数器全 0；replay task 仅读取既有 Trial；无第二份 Trial；无 Sandbox 创建 |
| B6 | Trace/Observation 映射 | **PASS**（5） | 父子结构正确；身份一致；不跨 Trial；无隐藏 prompt/推理/凭据；local/remote trace id 区分且稳定 |
| B7 | 一级 Score 映射 | **PASS**（6） | task_success/tool_correctness/answer_quality 名称值类型正确；不适用不生成；Judge 未完成不上传 0；Score ID 稳定（64-hex 确定性哈希）；时间戳稳定 |
| B8 | Score 提交/确认语义 | **PASS**（4） | 提交成功仍为 uncertain（无确认能力记录在 manifest）；广告确认能力也不伪造 confirmed；提交后超时→uncertain 且重试复用原 ID；合同拒绝无身份 confirmed |
| B9 | 发布状态机 | **PASS**（11） | 11 类故障注入；明确失败→failed、结果不明→uncertain；从不伪造 confirmed；shutdown 失败不删本地报告 |
| B10 | Manifest | **PASS**（8） | publication key 稳定；confirmed+同 fingerprint 跳过、uncertain 重试同 ID；fingerprint 冲突失败；原子写中断保留旧版；symlink 拒绝（子检查 SKIP：环境无符号链接权限）；run 隔离；无凭据 |
| B11 | 远端失败不覆盖本地事实 | **PASS**（3） | 9 个注入节点逐一验证：JSON 可重读、Trial 数量/内容/指标/Artifact 不变、integration_errors 明确、远端错误不写成本地任务失败 |
| B12 | CLI 退出码 | **PASS**（9） | 见下方退出码表 |
| B13 | no-content 不泄漏 | **PASS**（5） | 5 个 SECRET_* marker 扫描 Dataset/Item/Trace/Observation/Score/manifest/错误面；仅哈希/长度/状态/安全 metadata 上传 |
| B14 | 凭据隔离 | **PASS**（8） | T2_FAKE_API_KEY/SECRET_TOKEN marker 不进入远端 payload/manifest/错误/traceback；`sanitize_external_error`、CLI `_print_safe_traceback` 均脱敏 |

**B12 退出码表（实际 → 预期）**：

| 场景 | 实际 | 预期 | 说明 |
|---|---|---|---|
| 1. 本地成功，无 langfuse | 0 | 0 | ✓ |
| 2. 本地成功，远端全部 confirmed | 0 | 0 | 仅无 Score 结果可达（orchestrator 恒设 task_passed ⇒ 真实运行必有 Score，已注明为合成构造） |
| 3. 本地成功，远端 uncertain | 1 | 1 | ✓ |
| 4. 本地成功，远端 failed | 1 | 1 | ✓ |
| 5. 本地 Trial 失败，远端未启用 | 1 | 1 | ✓ |
| 6. 本地失败 + 远端失败 | 1 | 1 | ✓ |
| 7. Judge evaluator error（required） | 1 | 1 | ✓ |
| 8. Judge disabled（optional） | 0 | 0 | ✓ |
| 9. Judge not applicable（Trial 失败） | 1 | 1 | ✓ |

> 注：B12 通过替换 orchestrator 为“返回预构建本地结果”的替身来绕过 T2-ISSUE-001，CLI 的写报告→发布→退出码计算路径本身未修改。

### T2-C

| 编号 | 目标 | 状态 | 证据 |
|---|---|---|---|
| C1 | 真实 Dataset dry-run | **PASS** | 退出 0，`Remote write: no` |
| C2 | 真实 Dataset 同步 | **PASS** | `myhermes-audit-t2-synthetic` 存在 6 项；第一轮 unchanged=6（复用）、第二轮 unchanged=6（幂等）；远端身份记录 |
| C3 | 最小真实烟雾运行 | **BLOCKED** | 真实 2 Trial 执行后 exit 3、无报告 —— T2-ISSUE-001 复现（19s） |
| C4 | 真实 Judge | **PASS** | deepseek-v4-flash 得分 1.000；answer-quality-v1；严格结构；本地加权校验通过；provider/model 记录；无隐藏推理；受控 provider 错误→`judge_timeout_error`→evaluator_error；disabled 对照 skipped 且 0 调用 |
| C5 | 完整 synthetic Suite | **BLOCKED** | 与 C3 同一代码路径（`run`→orchestrator 组装崩溃），未重复执行以节省真实模型调用；由 C3/A2 证据覆盖 |
| C6 | 同一结果重复发布 | **FAIL**（部分通过） | 无 Score 结果：Trace ID 稳定、attempt 1→1（不重跑）、fingerprint 不变、remote=completed ✓；Score 重试路径被 T2-ISSUE-002/003 阻断 |
| C7 | 断网/不可达 endpoint | **PASS** | 本地 Trial completed、报告存在、`langfuse_connection_error`、remote=error、无凭据 |
| C8 | 当前 Score 确认语义 | **FAIL** | 非“已提交不可确认”的设计可接受态：Score 提交本身被真实 API 400 拒绝（T2-ISSUE-002），远端 Score 不可见；本地状态机仍安全（uncertain，不伪造 confirmed） |
| C9 | 真实 no-content | **PASS** | manifest 与**真实 API 回读**（get_dataset 读取远端 item）均无 SECRET_* marker；`content_omitted`/哈希/长度存在 |

---

## 6. 远端对象证据（安全形式）

| 对象 | 值（可核对） |
|---|---|
| Dataset `myhermes-audit-t2-synthetic` | 6 个 Item（C2 同步/复用，幂等确认） |
| Dataset `myhermes-audit-t2-markers-20260801` | 1 个 Item（C9 创建，API 回读无 marker） |
| Dataset Run ID（示例，真实 API 返回） | `234779ac-296e-4304-b332-e9306a60df35`、`53a4fa79-913f-4d04-a542-4aa98080412d`、`5e745c02-0553-49b5-9c04-115931e4d1e5`、`a899eab0-e160-4b2d-8373-ea4ed84c97a2` |
| Experiment ID | 与对应 Dataset Run ID 相同（真实 SDK 语义） |
| Experiment URL | 由 SDK 返回（`dataset_run_url`；C6 发布正常） |
| Trace ID（示例） | `9c512238f80570b3998b459e973ff662`、`8f4ecc38fb494826d3bf27f46839dded`、`4bb1616cc38b425bddac8eb87e0b27ac`、`d6cd31ad80792b560ea3b5a48368bc94` |
| Experiment 名称 | `myhermes-audit-t2-republish-20260801`、`-noscore-`、`-scores-`、`-hangprobe-`、`-markers-`（均为 synthetic） |
| Score ID | 未成功提交任何 Score（T2-ISSUE-002） |
| 重复发布前后对象数量 | Trial/Trace：不新增（C6 证据：attempt 1→1、Trace 稳定）；Dataset Item：不变 |

---

## 7. Score 状态

| 状态 | 出现情况 | 说明 |
|---|---|---|
| confirmed | 0 个 Score | 生产代码无“可靠远端确认”路径（仅提交），且提交本身被真实 API 拒绝（T2-ISSUE-002） |
| uncertain | 真实发布中的 Score（`score_idempotency_error`） | 状态机正确：提交失败→uncertain+retryable，从不伪造 confirmed；但**非**“已提交不可确认”的设计可接受态 —— 远端未收到 Score |
| failed | 0 个 Score（真实）；B9 注入场景验证过 failed 分支 | 真实提交错误被映射为 uncertain（`_is_retryable` 对 SDK 400 判定为 True）；B9 中显式失败→failed 语义已用 Fake 验证 |

- 是否获得可靠远端确认：否（SDK 无公开确认方法；且提交失败）
- CLI 退出码：Score 相关失败 → 1（integration failure）
- 本地结果完整性：发布失败不影响本地 Trial/报告（B11/C7 验证）
- 远端 UI 是否待人工核验：是（见第 8 节 Manual 清单）

---

## 8. 安全扫描

| 面 | 结果 |
|---|---|
| 凭据 marker（T2_FAKE_API_KEY / T2_FAKE_SECRET_TOKEN） | stdout/stderr：CLI `_print_safe_traceback` 脱敏 ✓；JSON：本地报告保留原始内容（Fake runner 注入内容绕过真实 worker 脱敏，见 B14 注），真实报告脱敏由 A2 扫描真实敏感值验证 ✓；manifest/logs/Artifact/异常/traceback/远端 payload：均未检出 ✓ |
| 真实敏感环境值扫描（A2/A6/B13/B14/C7） | 报告、stderr、integration_errors 均未检出 ✓ |
| no-content marker（SECRET_FIXTURE/MEMORY/SKILL/TOOL/PROMPT_MARKER） | Dataset 计划、Dataset Item、Trace、Observation、Score、manifest、错误面、真实 API 回读：全部未检出 ✓；仅哈希/长度/状态/安全 metadata 上传 ✓ |
| traceback | `sanitize_external_error` 截断+脱敏（含 Windows 本地路径 → `[LOCAL_PATH]`）✓ |
| Fake Adapter payload / 真实 Langfuse payload 投影 | Fake 后端全量扫描 ✓；真实侧以 API 回读 + manifest 扫描验证 ✓ |

---

## 9. 跨平台范围

| 平台 | 状态 | 说明 |
|---|---|---|
| Windows | **通过** | Windows 11，路径、`taskkill` 进程树、CTRL_BREAK_EVENT 等均已实测（A8 进程树清理） |
| PowerShell | **NOT_RUN** | 本轮执行环境为 Git Bash（Bash tool），PowerShell 语法命令未实际执行 |
| Git Bash | **通过** | 全部命令经 Git Bash 执行；非 ASCII（中文）路径/文本经 YAML/JSON UTF-8 往返验证 |
| `uv run` | **通过** | 全部测试经 `uv run python` |
| JSON 编码 / manifest 路径 / Sandbox 路径 / 子进程退出 / 日志编码 | **通过** | 见 A1/A8/B10/B14 |

> 后续正式验收仍需补验 PowerShell 环境。

---

## Manual Langfuse Verification Required

Claude Code 无法替代网页 UI 核验。以下项目**尚未人工核验**，状态均为 `PENDING_MANUAL_VERIFICATION`，请逐项在网页 Langfuse UI 中确认并填写 PASS/FAIL：

| # | 项目 | 预期结果 | 状态（用户填写） |
|---|---|---|---|
| M1 | Dataset `myhermes-audit-t2-synthetic` | 存在，6 个 Item | PENDING_MANUAL_VERIFICATION |
| M2 | Dataset `myhermes-audit-t2-markers-20260801` | 存在，1 个 Item，input 为 `content_omitted` 投影 | PENDING_MANUAL_VERIFICATION |
| M3 | Experiment `myhermes-audit-t2-republish-20260801`（及 -noscore/-scores/-hangprobe/-markers） | 各 1 个 Dataset Run；Trial Trace 可见 | PENDING_MANUAL_VERIFICATION |
| M4 | Trace（如 `9c512238…`、`d6cd31ad…`） | 根 span `myhermes.audit.trial` + turn/model/tool/validator/judge 子观察 | PENDING_MANUAL_VERIFICATION |
| M5 | Score | 预期：不应存在（T2-ISSUE-002 阻止提交）；若 UI 可见任何 Score，请记录 | PENDING_MANUAL_VERIFICATION |
| M6 | 重复发布前后对象数量 | Dataset Item 6 个不变；Experiment Run 数量不因 republish 增加 | PENDING_MANUAL_VERIFICATION |
| M7 | no-content marker | 网页中搜索 `SECRET_*`：应无任何命中 | PENDING_MANUAL_VERIFICATION |

---

## 10. 问题清单（只记录，未修复）

### T2-ISSUE-001（严重级别：P0，阻断 `run` 全部路径）
- 复现步骤：`myhermes-audit run <suite> --subject-repo ..\my-hermes --subject-config .\local-config.yaml`（任何 suite）
- 期望行为：Trial 执行完毕后构造 AuditRunResult 并写报告
- 实际行为：`pydantic.ValidationError: schema_version Field required`；CLI exit 3；无报告（Trial 本身已执行；真实复现见 C3/A2）
- 影响范围：`run` 命令全部路径（P1/P2/P2.1 的核心执行链路）；或chestrator 在组装阶段崩溃
- 相关生产文件：`src/myhermes_audit/contracts/result.py:413-415`（`schema_version: SchemaVersion = Field(description=…)` 无默认值，覆盖基类默认）；`src/myhermes_audit/runners/orchestrator.py:142`（构造未传 schema_version）
- 建议修复方向：orchestrator 组装时显式传 `schema_version="1.0"`（或改回继承基类默认），并补一条 run 端到端测试
- 是否阻止进入 P3：**是**

### T2-ISSUE-002（严重级别：P0，阻断 Score 发布功能）
- 复现步骤：任何带 Score 的真实发布（C6 score 证据：真实 publish_audit_result）
- 期望行为：Score 提交成功
- 实际行为：`create_score` 同时携带 `dataset_run_id` 与 `trace_id`，真实 API 返回 400（调试日志原文：“Provide exactly one of the following: traceId (with optional observationId), sessionId or datasetRunId”）；SDK 仅打印通用 “Bad request” 日志；本地记录 `score_idempotency_error` → uncertain；远端无 Score
- 影响范围：全部 Score 发布（task_success/tool_correctness/answer_quality）；C8 设计可接受态不成立
- 相关生产文件：`src/myhermes_audit/integrations/langfuse/client.py:1112-1121`（`_publish_one_score` 同时传 dataset_run_id 与 trace_id）
- 建议修复方向：按 API 约束二选一（Score 关联实验运行应传 `dataset_run_id` 或仅 `trace_id`），并对照真实 API 验证
- 是否阻止进入 P3：**是**

### T2-ISSUE-003（严重级别：P1，阻断重复发布）
- 复现步骤：对同一带 Score 结果二次 `publish_audit_result`（真实 Langfuse；3 次独立复现，150s+ 不返回）
- 期望行为：uncertain Score 以原 ID 重试
- 实际行为：`publish_scores` → `create_score` 无限挂起（SDK 对失败事件的内部重试队列阻塞）；无 Score 结果的重发正常（C6 无 Score 路径 6s 完成）
- 影响范围：所有带 Score 的重复发布/重试
- 相关生产文件：交互发生在 `client.py:_publish_one_score`（SDK 行为触发）
- 建议修复方向：修复 ISSUE-002 后重测；考虑对失败 Score 不重发同 ID 或对 create_score 调用加有界超时
- 是否阻止进入 P3：**是**

### T2-ISSUE-004（严重级别：P3，状态机残留）
- 复现步骤：真实发布中首个 Score 失败后查看 manifest（5 次运行中 3 次复现）
- 期望行为：所有 Score 记录收敛到 failed/uncertain
- 实际行为：后续 Score 记录停留在 `publishing`（`publish_scores` 在首个错误处中断，剩余记录未收尾）
- 影响范围：manifest 状态可读性；重试语义
- 相关生产文件：`src/myhermes_audit/integrations/langfuse/client.py:publish_scores`
- 建议修复方向：失败路径将剩余 publishing 记录收尾为 failed/uncertain
- 是否阻止进入 P3：否

### T2-ISSUE-005（严重级别：信息性）
- 现象：SDK 日志 “Propagated attribute 'experiment_item_metadata.fixture_files' value is over 200 characters (237 chars). Dropping value.” —— fixture 摘要 metadata 超过 SDK 属性长度上限，远端被丢弃（no-content 场景下影响有限，但 fixture_files 摘要不可见）
- 相关生产文件：`src/myhermes_audit/integrations/langfuse/dataset_sync.py:_fixture_file_summaries`（经 experiment_item_metadata 传播）
- 是否阻止进入 P3：否

---

## 11. 最终结论

**T2 FAILED**

判定依据（满足任务定义的 FAILED 情形）：
1. 阻断级生产缺陷 T2-ISSUE-001：本地执行被结果组装崩溃阻断（`run` 从 P1 起无法产出报告），T2-A2、T2-C3/C5 无法验收；
2. 阻断级生产缺陷 T2-ISSUE-002：真实 Score 提交被 API 400 拒绝，P2.1 核心功能（Score 发布）未达成，C8 不满足；
3. 阻断级生产缺陷 T2-ISSUE-003：带 Score 的重复发布无限挂起，C6 Score 重试路径不可用。

同时满足的正面结果（供修复后回归参考）：
- 离线契约层 T2-B 全部通过（123/123），覆盖 Judge 严格结构、MetricStatus、Dataset/Experiment/Trace/Score 映射、幂等、状态机、manifest、退出码、no-content、凭据隔离；
- 真实链路中 Trial→Experiment→Trace 发布正常（confirmed）、真实 Judge 正常（1.000）、断网行为正确、no-content 与凭据隔离在真实侧成立；
- 本地事实完整性保障（B11/C7）成立：远端失败从不改写本地 Trial 与报告。

**本阶段未修改任何生产代码、未进入 P3。**
