#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_NAME="${FINETUNED_MODEL_NAME:-mindbridge-qwen2.5-7b-ft:latest}"
MODEL_DIR="${FINETUNED_MODEL_DIR:-$ROOT_DIR/models/mindbridge-qwen2.5-7b-ft}"
GGUF_FILE="${FINETUNED_MODEL_FILE:-mindbridge-qwen2.5-7b-ft-q4_k_m.gguf}"
UPSTREAM_GGUF="${UPSTREAM_GGUF:-}"

DEFAULT_OLLAMA_BIN="$(command -v ollama || true)"
if [ -z "$DEFAULT_OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  DEFAULT_OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi
OLLAMA_BIN="${OLLAMA_BIN:-$DEFAULT_OLLAMA_BIN}"

if [ ! -x "$OLLAMA_BIN" ]; then
  echo "Cannot find Ollama."
  echo "Install Ollama or set OLLAMA_BIN to the ollama executable path."
  exit 1
fi

mkdir -p "$MODEL_DIR"

if [ ! -f "$MODEL_DIR/$GGUF_FILE" ]; then
  if [ -n "$UPSTREAM_GGUF" ] && [ -f "$UPSTREAM_GGUF" ]; then
    echo "Linking GGUF from upstream MindBridge project..."
    ln -sf "$UPSTREAM_GGUF" "$MODEL_DIR/$GGUF_FILE"
  else
    echo "Missing GGUF model file:"
    echo "  $MODEL_DIR/$GGUF_FILE"
    echo
    echo "Put the model file there, or set UPSTREAM_GGUF to an existing GGUF path."
    exit 1
  fi
fi

"$OLLAMA_BIN" create "$MODEL_NAME" -f "$MODEL_DIR/Modelfile"

echo "Created $MODEL_NAME"
echo "Run MindBridge Python with: AI_PROVIDER=ollama ./scripts/run-dev.sh"
