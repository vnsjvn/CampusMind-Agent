# MindBridge 评测体系

## 两层评测

项目将检索回归与 RAGAS 生成质量评测分开，避免混淆指标口径。

### 检索层

- 数据集：`app/rag_eval/mindbridge-rag-eval.json`，60条。
- 指标：Recall@K、Precision@K、MRR、NDCG@K、Hit Rate。
- 命令：`.\.venv310\Scripts\python.exe -m app.rag_eval.runner`。
- 产物：`target/rag-eval-report.json`。

### RAGAS层

- 数据集：`app/rag_eval/mindbridge-ragas-eval.json`，12条人工参考答案。
- Generator：项目真实 Ollama 模型。
- Retriever：真实 Chroma + BM25 + 本地重排。
- Judge：DashScope `qwen-plus`，temperature=0，max_tokens=4096。
- Embedding：DashScope `text-embedding-v3`。
- 指标：Context Precision、Context Recall、Faithfulness、Answer Relevancy。
- 产物：`target/ragas-input.json`、`target/ragas-report.json`。

RAGAS 0.4.3 使用独立 `.venv-ragas`，不安装到生产虚拟环境，因为它的 LangChain依赖与项目的 LangGraph 0.4运行时版本不同。

## 安装与运行

```powershell
.\.venv310\Scripts\python.exe -m venv .venv-ragas
.\.venv-ragas\Scripts\python.exe -m pip install -r requirements-ragas.txt
.\scripts\run-ragas.ps1
```

也可以分两步运行：

```powershell
.\.venv310\Scripts\python.exe -m app.rag_eval.ragas_dataset
.\.venv-ragas\Scripts\python.exe scripts\evaluate-ragas.py
```

必要配置来自 `.env`：`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_EMBEDDING_MODEL`、`RAGAS_JUDGE_MODEL`。可用 `RAGAS_JUDGE_BASE_URL` 和 `RAGAS_JUDGE_API_KEY` 单独覆盖Judge服务。

## 2026-08-03真实结果

| 指标 | 12条RAGAS结果 |
|---|---:|
| Context Precision | 0.9282 |
| Context Recall | 1.0000 |
| Faithfulness | 0.8739 |
| Answer Relevancy | 0.8211 |

已发现的薄弱点：

- `ragas-procrastination` Context Precision为0.50，说明相关块排序不够靠前。
- `ragas-counseling-referral` Faithfulness为0.4286。
- `ragas-privacy-boundary` Faithfulness为0.50。

这些结果应作为后续检索策略和Prompt优化的输入，不应删除困难样本或只报告高分子集。

## 指标口径

- Context Precision判断相关上下文是否排在更靠前的位置，需要人工参考答案。
- Context Recall判断参考答案中的信息是否被检索上下文覆盖。
- Faithfulness判断生成回答中的陈述能否从检索上下文得到支持。
- Answer Relevancy判断回答是否直接回应用户问题。
- MRR属于原60条检索回归集，不是RAGAS指标。

12条人工集适合作为工程基线，但规模仍小。简历使用指标时必须同时注明样本数、Judge模型、评测日期和数据集版本。
