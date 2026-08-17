# 真实依赖集成测试

本目录中的测试不使用 SQLite、Mock AI 或内存 Redis。测试会拒绝错误配置，而不是自动降级。

## 必需配置

在项目 `.env` 中设置：

```dotenv
DATABASE_URL=mysql+pymysql://mindbridge:你的密码@127.0.0.1:3306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_MEMORY_REQUIRED=true
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mindbridge-qwen2.5-7b-ft:latest
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=true
OPENAI_API_KEY=你的OpenAI密钥
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

`RUN_REAL_INTEGRATION` 不写入 `.env`，请在运行测试的同一个 PowerShell 窗口中设置：

```powershell
$env:RUN_REAL_INTEGRATION="1"
```

注意：当前 Chroma 的向量生成只实现了 OpenAI Embeddings。Docker Compose 中的 `OLLAMA_EMBEDDING_MODEL` 尚未被代码使用。如果不准备使用 OpenAI Key，需要在后续 RAG 阶段增加 Ollama Embedding Provider，不能只修改环境变量。

## 启动依赖

可以使用项目 Compose 启动 MySQL 和 Redis：

```powershell
docker compose up -d mysql redis
```

当前 Compose 暴露的是 MySQL `13306` 和 Redis `16379`；如果采用 Compose，请将 `.env` 改为：

```dotenv
DATABASE_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:13306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:16379/0
```

启动或确认 Ollama：

```powershell
ollama list
ollama create mindbridge-qwen2.5-7b-ft -f models/mindbridge-qwen2.5-7b-ft/Modelfile
ollama serve
```

## 执行

```powershell
$env:RUN_REAL_INTEGRATION="1"
.\.venv310\Scripts\python.exe -m unittest tests.integration.test_real_dependencies -v
```

该测试会向真实 MySQL 写入项目表和默认种子数据，会在 Redis 中创建后立即删除一个唯一探针 Key，并会调用真实 Embedding 和 Ollama API。
