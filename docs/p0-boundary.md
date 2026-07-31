# P0 能力边界

## P0 包含

- contracts；
- YAML loader 与 Fixture source resolver；
- Trial Sandbox；
- subject/audit/suite fingerprint；
- Memory 与 Background Review evaluation ports；
- `validate` 和 `schema` CLI；
- 合同示例与边界文档。

## P0 不包含

- Agent 或 AgentLoop 执行；
- Langfuse 或 OpenTelemetry；
- LLM SDK、LLM Judge 或任意模型调用；
- Dense、BM25、Hybrid 或其他 Memory 检索实现；
- Background Review 执行；
- evaluator 与指标计算；
- Trial/Case 真实聚合算法；
- Baseline、报告、比较、同步或 CI；
- MyHermes SQLite schema 初始化；
- Ragas、Inspect AI、DSPy、LangChain 或 LangGraph。

P0 中的 `llm_judge`、`retrieval`、`compression` 与 `background_review` evaluator kind 是未来配置入口。静态校验成功仅表示声明符合合同，不表示相关运行能力存在。

## 被测项目边界

Audit 开发只读参考同级 `my-hermes`，对齐基线 commit `2cb27d72e90365d44cdc2d08953acf882c4ae626`。Audit 不修改被测仓库，不引用 `my-hermes-self-evolution`，也不要求 MyHermes 存在评测特殊分支。

未来 runner 必须像普通调用方一样使用 MyHermes 的公开行为，并把观测结果翻译成 Audit 合同。MyHermes 本身不应获得“正在被评测”的信号。

## 开发阶段边界

P0 构建与测试分离：本阶段不创建或修改测试文件，不运行 pytest、unittest 或集成测试。独立验证阶段可基于本阶段冻结的 schema 与公共 API 建立测试。
