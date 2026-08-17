# MindBridge 当前架构与调用链基线

审计日期：2026-07-31

本文只描述当前代码实际行为，不代表目标架构。

## 1. 启动链路

`app.main:create_app()` 创建 FastAPI 应用。启动事件依次执行：

1. `create_schema()` 通过 SQLAlchemy `Base.metadata.create_all()` 建表；当前没有数据库迁移工具。
2. `seed_data()` 创建默认 `admin/admin123`、`student/student123`，并把 `app/knowledge/*.md` 写入数据库。
3. `ToolQueueWorker.start()` 启动进程内调度线程和线程池。

关闭事件只停止 Tool Queue Worker。SQLAlchemy Engine、Redis 客户端和 Chroma 客户端没有统一的应用生命周期管理。

## 2. 聊天请求完整调用链

```text
POST /api/chat/stream
  -> current_user：HTTP Basic 认证并从数据库加载用户
  -> ChatService.stream_chat()
  -> MindBridgeAgentHarness.run()
     -> 输入脱敏
     -> 创建或读取 ChatSession
     -> create_agent_runtime()
        -> LangGraphAgentRuntimeService（配置为 langgraph 且依赖可用）
        -> AgentRuntimeService（否则回退）
     -> MemoryAgent
        -> Redis 最近窗口/摘要
        -> 未命中时 MySQL chat_messages/long_term_memories 回填
     -> SupervisorAgent：CHAT / CONSULT / RISK 路由
     -> KnowledgeAgent：非 CHAT 时执行 Chroma + BM25 混合检索
     -> RiskGuardianAgent：规则快速筛查 + 模型结构化评估
     -> CompanionAgent 或 CounselorAgent
        -> Skill 选择
        -> AgentPromptBuilder 组装 Prompt
     -> 保存用户消息、心理报告和 AgentRunTrace
  -> AiClient.stream()：Ollama / OpenAI / Mock 流式输出
  -> 保存助手消息
  -> ToolQueueService.enqueue_report()
  -> SSE done
```

工具队列在响应文本生成完后入队，Excel、风险个案和预警通知不会阻塞 SSE 文本生成。

## 3. 数据与外部依赖

| 组件 | 当前代码实现 | 2026-07-31 实际配置 | 审计结论 |
|---|---|---|---|
| MySQL | SQLAlchemy + PyMySQL，默认 URL 指向 MySQL | `.env` 实际为 `sqlite:///data/mindbridge-dev.db` | 代码支持，当前运行未使用 MySQL |
| Redis | 最近消息、摘要缓存、TTL、MySQL 回填 | 指向 `127.0.0.1:6379`，端口不可达 | 代码支持，当前运行会降级 MySQL |
| Chroma | 本地 PersistentClient | `KNOWLEDGE_VECTOR_ENABLED=false` | 当前只使用 BM25，不是完整混合检索 |
| Embedding | OpenAI `/embeddings` | 向量检索关闭 | `OLLAMA_EMBEDDING_MODEL` 在 Compose 中存在，但 Settings 和实现未使用它 |
| Ollama | `/api/chat` 和流式 `/api/chat` | `AI_PROVIDER=mock` | 当前聊天未使用 Qwen/Ollama |
| MCP | MCP server 和 stdio client 均存在 | Tool Queue 开启时主链路直接调用业务 Service | MCP 仅在关闭 Tool Queue 时进入聊天工具链，不是默认主路径 |
| Tool Queue | MySQL 任务事实表 + 进程内轮询线程池 | 当前任务表落在 SQLite | 不是 Redis 队列；生产多实例尚未实测 |

## 4. 当前 Agent 与调度方式

- `MemoryAgent`、`SupervisorAgent`、`KnowledgeAgent`、`RiskGuardianAgent`、`CompanionAgent`、`CounselorAgent` 是 Runtime 中的方法，不是独立注册的 Agent 对象。
- LangGraph 负责节点路由，但主要状态判断仍是 controller 中的条件分支。
- 当前没有 Event Bus，也没有独立 `CoordinatorAgent` 类。
- 自研 Runtime 是有界同步循环，不是事件驱动 Runtime。

因此简历中的“多 Agent 协作”有工作流证据；“事件驱动”和“CoordinatorAgent”暂时没有完整代码证据。

## 5. RAG 当前实现

- MySQL/SQLite 保存 `KnowledgeChunk`。
- Chroma 保存向量索引。
- 向量召回与 BM25 召回进行加权融合。
- 本地规则执行重排。
- CHAT 不检索，CONSULT/RISK 使用同一套检索参数，尚未实现按路由配置不同知识库、Top K 和阈值。
- 当前向量生成只支持 OpenAI Embeddings，不支持 Compose 中声明的 Ollama Embedding 模型。

## 6. 已识别的技术债与未接入代码

1. `/actuator/health` 无条件返回 `UP`，没有验证 MySQL、Redis、Chroma 或 Ollama。
2. `.env` 当前选择 SQLite、Mock AI 并关闭向量检索，与简历技术栈不一致。
3. `OLLAMA_EMBEDDING_MODEL` 只出现在 Docker Compose，业务配置与向量实现没有读取。
4. Tool Queue 默认直接调用 `ToolOrchestrationService`，没有通过 MCP。
5. MCP client 只有在 `TOOL_QUEUE_ENABLED=false` 时被聊天主链路使用。
6. RAG 评测数据文件是有效 JSON，共 60 条；当前仍需分别记录 BM25 降级基线和真实 Chroma 混合检索基线。
7. 没有路由 150 条数据集和风险微调评测产物，97%、99%、90% 暂无可复现证据。
8. 没有数据库迁移机制，当前依赖 `create_all()`，只能创建缺失表，不能可靠演进已有表结构。
9. 默认账号密码硬编码在 seed 逻辑中，不适合生产环境。
10. 源码及评测数据存在明显乱码内容，需要单独确认原始 UTF-8 数据是否已经损坏。
