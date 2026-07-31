#!/bin/bash

ROOT="${HOME}/.config/llm-sandbox"
TODO_USER_VALUE="${TODO_USER:-${USER:-appuser}}"

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -e "TODO_USER=${TODO_USER_VALUE}" \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/opencode:/home/appuser/.config/opencode:z,rw" \
  -v "$ROOT/local-opencode:/home/appuser/.local/share/opencode:z,rw" \
  --tmpfs /tmp:rw,size=512M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  /home/appuser/.opencode/bin/opencode "$@"
