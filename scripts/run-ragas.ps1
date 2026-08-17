$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv-ragas\Scripts\python.exe")) {
    .\.venv310\Scripts\python.exe -m venv .venv-ragas
    .\.venv-ragas\Scripts\python.exe -m pip install -r requirements-ragas.txt
}

.\.venv310\Scripts\python.exe -m app.rag_eval.ragas_dataset
.\.venv-ragas\Scripts\python.exe scripts\evaluate-ragas.py
