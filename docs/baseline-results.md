# 现状测试与评测基线

记录日期：2026-07-31

## 代码级基线

- `python -m compileall -q app tests`：通过。
- `python -m unittest discover -s tests -v`：23 项被发现，18 项通过，5 项真实依赖测试因未设置 `RUN_REAL_INTEGRATION=1` 而跳过。
- Risk Safety Harness：4 个场景通过。
- Agent Routing Harness：CHAT、CONSULT、RISK 三条链路通过。
- Standard Skills Harness：7 个 Skill 加载和高风险强制选择通过。
- Tool Queue Harness：Excel、风险个案、预警依赖和 DLQ 通过。

这些 Harness 使用 SQLite、Mock AI 和内存短期记忆，只证明代码逻辑基线，不证明真实服务部署成功。

## RAG 降级基线

环境：SQLite、向量检索关闭、BM25 + 本地重排、60 条固定数据、Top K=4。

| 指标 | 结果 |
|---|---:|
| Recall@K | 0.9667 |
| Precision@K | 0.6458 |
| MRR | 0.9083 |
| NDCG@K | 0.9053 |
| Hit Rate | 0.9667 |

重要结论：简历中的 `0.9667` 与当前评测的 Recall@K/Hit Rate 相同，并不是当前脚本计算出的 Context Precision；当前 `precisionAtK` 为 `0.6458`。在定义 Context Precision 的计算方式并运行真实 Chroma 基线前，不能写“Context Precision 0.9667”。

评测产物位于 `target/harness/rag-eval-report.json`。

## 真实服务基线

2026-08-03 重新验收结果：

| 依赖 | 结果 | 证据 |
|---|---|---|
| MySQL | 通过 | 连接 `mindbridge` 成功，14 张业务表齐全，包含 `long_term_memories` |
| Redis | 通过 | `PING=true`，真实 Redis DB 可读取 |
| DashScope Embedding | 通过 | `text-embedding-v3` 返回 1024 维真实向量 |
| Chroma | 通过 | Persistent Collection 可读取，当前包含 34 条向量 |
| Ollama TCP | 通过 | `10.10.19.12:11434` 在约 15ms 内建立连接 |
| Ollama HTTP | 通过 | `/api/tags` 约 0.17 秒，目标模型存在；最小推理约 4.63 秒 |
| FastAPI 完整聊天 | 通过 | 真实 SSE 请求包含 `meta`、`token`、`done`，并完成 MySQL/Redis 持久化 |

真实依赖测试共 5 项，全部通过。当前 `.env` 已切换为 MySQL、Redis 强依赖、Ollama、Chroma 强依赖和 DashScope Embedding，不再是 SQLite/Mock 配置。测试入口位于 `tests/integration/test_real_dependencies.py`。

真实聊天验收后的 MySQL 证据：`chat_sessions=3`、`chat_messages=6`、`long_term_memories=3`、`agent_run_traces=4`、`knowledge_chunks=34`。这些数量是验收时快照，会随运行继续增长。

## 真实 Chroma 混合检索基线

环境：MySQL、Chroma、DashScope `text-embedding-v3`、BM25 融合、本地重排、60 条固定数据、Top K=4。

| 指标 | 真实结果 |
|---|---:|
| Recall@K | 0.9833 |
| Precision@K | 0.7500 |
| MRR | 0.9542 |
| NDCG@K | 0.9398 |
| Hit Rate | 0.9833 |
| Average First Relevant Rank | 1.0847 |

其中 30 条以 `risk-` 开头的场景：Hit Rate `1.0000`，Recall@K `1.0000`。报告位于 `target/rag-eval-report.json`。

这组指标说明当前真实混合检索优于此前 BM25 降级基线，但它仍不是 RAGAS 定义的 Context Precision。简历中的 `Context Precision=0.9667` 仍不能由当前评测脚本支持。

## RAGAS生成质量基线

独立使用12条带人工参考答案的数据集，真实Ollama生成回答，DashScope `qwen-plus`作为Judge，结果如下：

| RAGAS指标 | 结果 |
|---|---:|
| Context Precision | 0.9282 |
| Context Recall | 1.0000 |
| Faithfulness | 0.8739 |
| Answer Relevancy | 0.8211 |

正式报告位于 `target/ragas-report.json`。这证明项目现在可以使用RAGAS定义的Context Precision，但实测值是0.9282，不是原简历中的0.9667。
