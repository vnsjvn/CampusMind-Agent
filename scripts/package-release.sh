#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="mindbridge-py"
STAMP="$(date +%Y%m%d-%H%M%S)"
DIST_DIR="$ROOT_DIR/dist"
STAGE_DIR="$DIST_DIR/stage-$STAMP"
ARCHIVE="$DIST_DIR/${PROJECT_NAME}-app-$STAMP.tar.gz"

mkdir -p "$DIST_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/$PROJECT_NAME"

rsync -a "$ROOT_DIR/" "$STAGE_DIR/$PROJECT_NAME/" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude '.env.production' \
  --exclude '.env.development' \
  --exclude '.env.test' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'target/' \
  --exclude 'dist/' \
  --exclude 'data/' \
  --exclude 'docs/' \
  --exclude 'output/' \
  --exclude 'tmp/' \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '*secret*' \
  --exclude '*token*' \
  --exclude '*.db' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite3' \
  --exclude '*.xlsx' \
  --exclude '*.gguf' \
  --exclude '*.gguf.zip' \
  --exclude '*.zip' \
  --exclude '*.tar.gz'

(
  cd "$STAGE_DIR"
  COPYFILE_DISABLE=1 tar \
    --no-xattrs \
    --uid 0 --gid 0 \
    --uname root --gname root \
    -czf "$ARCHIVE" "$PROJECT_NAME"
)

rm -rf "$STAGE_DIR"

echo "Created release package:"
echo "$ARCHIVE"
echo "Model GGUF is intentionally excluded. Send the model zip separately."
du -sh "$ARCHIVE"
