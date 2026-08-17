# mindbridge-qwen2.5-7b-ft:latest

这是 MindBridge 项目使用的本地 Ollama 模型目录。`Modelfile` 会加载本目录下的 GGUF 权重文件：

```text
mindbridge-qwen2.5-7b-ft-q4_k_m.gguf
```

创建本地模型：

```bash
/Applications/Ollama.app/Contents/Resources/ollama create mindbridge-qwen2.5-7b-ft:latest -f models/mindbridge-qwen2.5-7b-ft/Modelfile
```
