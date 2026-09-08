#!/usr/bin/env bash
# Kept for old shortcuts. Claude backends now use the same session store.
set -euo pipefail
cat <<'MESSAGE'
Claude and DeepSeek now share sessions; copying is no longer needed.
Resume with either:
  ,claude-sandbox.sh --backend claude --resume
  ,claude-sandbox.sh --backend deepseek --resume
Legacy DeepSeek sessions are imported on the next Claude harness launch.
Original files are retained, and existing shared files are never overwritten.
MESSAGE
