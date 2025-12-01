#!/bin/bash

ROOT="${HOME}/.config/llm-sandbox"

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/codex:/home/appuser/.codex:z,rw" \
  -v "$ROOT/orca:/home/appuser/.orca:z,rw" \
  --tmpfs /tmp:rw,size=64M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  codex --dangerously-bypass-approvals-and-sandbox "$@"
