#!/bin/bash

ROOT="${HOME}/.config/llm-sandbox"

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/omp:/home/appuser/.omp:z,rw" \
  --tmpfs /tmp:rw,size=64M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  omp
