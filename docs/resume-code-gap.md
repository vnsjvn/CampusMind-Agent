# 简历声明—代码—测试—指标证据对照表

审计日期：2026-07-31

状态说明：`已有` 表示主链路存在；`部分` 表示存在骨架但声明范围更大；`缺失` 表示没有足够代码或可复现证据。

| 简历声明 | 状态 | 代码证据 | 当前测试证据 | 缺口 |
|---|---|---|---|---|
| FastAPI 服务 | 已有 | `app/main.py`、`app/api/routes.py` | API Harness 使用模拟依赖 | 缺少真实依赖端到端测试 |
| Agent 执行生命周期 | 部分 | `app/agents/harness.py`、`runtime.py` | Routing Harness | 生命周期接口和错误/超时事件不完整 |
| LangGraph 多 Agent | 部分 | `langgraph_runtime.py` | 当前 Harness 强制 custom runtime | 需真实 LangGraph 集成验收 |
| CoordinatorAgent | 缺失 | 当前由 Supervisor 方法和 LangGraph controller 分担 | 无 | 没有独立 Coordinator/Registry |
| 事件驱动 Runtime | 缺失 | 当前为同步循环或 LangGraph 状态图 | 无 | 没有 Event Bus 和生命周期事件 |
| Context 管理 | 部分 | `AgentContext`、`prompt_builder.py` | Prompt Builder 单测 | 缺 Context Builder、Token 预算和来源标识 |
| Redis 短期记忆 | 已有 | `RedisShortTermMemoryStore` | 真实 Redis Ping/临时 Key/完整聊天通过 | 故障恢复和多会话压力测试留待后续 |
| MySQL 长期记忆 | 已有 | `LongTermMemory`、`MySqlLongTermMemoryStore` | 真实 MySQL 表、完整聊天和长期记忆落库通过 | 重启恢复和长期事实提取留待后续 |
| 动态路由 RAG | 部分 | 非 CHAT 检索；Chroma + BM25 + rerank | RAG Harness 入口 | CONSULT/RISK 参数未区分，评测 JSON 无效 |
| Chroma 向量索引 | 已有 | `ChromaKnowledgeStore` | DashScope 1024 维向量、34 chunks、60 条真实评测通过 | 当前 Provider 命名仍偏 OpenAI，后续需抽象 |
| 路由准确率 97% | 缺失 | 无 150 条固定路由数据集 | 无 | 必须建立数据集并由脚本计算 |
| Risk 场景召回率 99% | 当前实测更高 | 60 条 RAG 数据中含 30 条 `risk-*` 场景 | 真实混合检索 Risk Recall@K/Hit Rate 均为 `1.0000` | 简历应注明样本数、Top K 和指标定义 |
| Context Precision 0.9667 | 数值不成立，能力已补齐 | 独立RAGAS 0.4.3评测、12条人工参考答案 | RAGAS Context Precision实测`0.9282` | 简历应改为0.9282并注明12条人工评测集 |
| MRR 0.9083 | 已有且实测更高 | `rag_eval/runner.py` 和 60 条数据存在 | BM25 `0.9083`；真实混合检索 `0.9542` | 简历应说明数据集和评测时间 |
| MCP 工具 | 部分 | `mcp_tools/server.py`、`mcp_client.py` | 无真实 MCP 进程测试 | 默认队列路径绕过 MCP |
| 异步 Tool Queue | 部分 | `tool_queue.py`、`ToolJob`、`DeadLetterRecord` | Queue 单测与 Harness | 没有 Redis 调度、取消和超时 |
| 幂等、限流、重试、DLQ | 部分 | 原子抢占、指数退避、RateLimiter、DLQ | 相关单测通过 | 需真实 MySQL 多 Worker 故障测试 |
| Skill Registry | 部分 | `services/skills.py`、7 个 `SKILL.md` | Skill 单测与 Harness | 缺版本、启停、Schema、Tool 白名单 |
| 高风险安全 Skill | 已有 | `high_risk_safety_plan/SKILL.md` 和强制选择逻辑 | Skill Harness | Trace 尚未记录 Skill 版本 |
| Qwen2.5-7B + QLoRA | 部分 | GGUF、Modelfile 和启动脚本存在 | 模型资产状态接口 | 缺训练代码、参数、数据划分和评测报告 |
| 风险识别准确率 90% | 缺失 | 无训练/基线评测结果 | 无 | 不能保留为已验证指标 |
| GGUF + Ollama 部署 | 部分 | GGUF、Modelfile、Ollama client | 当前 `AI_PROVIDER=mock` | 需实际导入模型并完成聊天验收 |
| Docker | 已有 | `Dockerfile`、`docker-compose.yml` | 未在本次真实启动 | Compose 中 embedding 配置与代码不一致 |

## 当前可复现基线

- Python 编译：通过。
- 单元测试：18 项通过。
- Agent Routing Harness：通过，但使用 SQLite、Mock AI 和内存短期记忆。
- Standard Skills Harness：通过。
- Tool Queue Harness：通过，但数据库为 SQLite。
- RAG BM25 基线（60 条，Top K=4）：Recall@K `0.9667`、Precision@K `0.6458`、MRR `0.9083`、NDCG@K `0.9053`、Hit Rate `0.9667`。
- 上述结果来自 SQLite + BM25 降级路径，不是 Chroma 混合检索结果。
- 真实 MySQL/Redis/Chroma/DashScope Embedding/Ollama：5 项集成测试全部通过。

## 简历当前可安全保留的表述

可以描述已经实现 FastAPI API、LangGraph 工作流骨架、Redis/MySQL 分层 Memory 代码、Chroma/BM25 混合检索代码、Skill Registry、MCP 工具定义、数据库持久化 Tool Queue、重试/限流/DLQ，以及 GGUF/Ollama 部署资产。

在真实环境和评测脚本通过前，不应把“事件驱动”“CoordinatorAgent”“Redis/MySQL 已生产接入”以及 97%、99%、0.9667、0.9083、90% 写成已经验证的事实。
