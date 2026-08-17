#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8080}"
export AI_PROVIDER="${AI_PROVIDER:-ollama}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-mindbridge-qwen2.5-7b-ft:latest}"
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
