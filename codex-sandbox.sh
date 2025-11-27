#!/bin/bash

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:Z,rw" \
  -v "$HOME/.config/llm-sandbox/codex:/home/appuser/.codex:Z,rw" \
  --tmpfs /tmp:rw,size=64M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  codex

