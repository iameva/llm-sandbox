#!/bin/bash

ROOT="${HOME}/.config/llm-sandbox"

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/claude:/home/appuser/.claude:z,rw" \
  -v "$ROOT/.claude.json:/home/appuser/.claude.json:z,rw" \
  --tmpfs /tmp:rw,size=64M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  claude --dangerously-skip-permissions
