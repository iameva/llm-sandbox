#!/bin/bash

ROOT="${HOME}/.config/llm-sandbox"

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/claude:/home/appuser/.claude:z,rw" \
  -v "$ROOT/.claude.json:/home/appuser/.claude.json:z,rw" \
  -v "$ROOT/codex:/home/appuser/.codex:z,rw" \
  -v "$ROOT/orca:/home/appuser/.orca:z,rw" \
  -v "$ROOT/opencode:/home/appuser/.config/opencode:z,rw" \
  -v "$ROOT/local-opencode:/home/appuser/.local/share/opencode:z,rw" \
  --tmpfs /tmp:rw,size=512M \
  --tmpfs /run:rw,size=16M \
  --network=host \
  llm-sandbox \
  zsh "$@"
