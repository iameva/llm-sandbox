#!/bin/bash

# Run Claude Code inside the sandbox, but pointed at DeepSeek's
# Anthropic-compatible API instead of Anthropic's own endpoint.
#
# Requires a DeepSeek API key on the host:
#   export DEEPSEEK_API_KEY=sk-...
# Model/effort mapping follows DeepSeek's Claude Code documentation.

set -euo pipefail

ROOT="${HOME}/.config/llm-sandbox"

DEEPSEEK_API_KEY=$(cat "${HOME}/.config/deepseek.api")

# Separate config dir so a third-party endpoint never touches the
# real Anthropic-authenticated ~/.claude config.
mkdir -p "$ROOT/deepseek-claude"
# Seed a valid-JSON config on first run so podman mounts a file (not a
# directory) and Claude doesn't choke on an empty/EOF config. Only write
# when missing or empty, so a populated config is never clobbered.
CONFIG="$ROOT/deepseek-claude/.claude.json"
if [[ ! -s "$CONFIG" ]]; then
  echo '{}' > "$CONFIG"
fi

podman run --rm -it \
  --userns=keep-id \
  --workdir /workspace \
  -v "$PWD:/workspace:z,rw" \
  -v "$ROOT/deepseek-claude:/home/appuser/.claude:z,rw" \
  -v "$ROOT/deepseek-claude/.claude.json:/home/appuser/.claude.json:z,rw" \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro[1m]" \
  -e ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro[1m]" \
  -e ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro[1m]" \
  -e ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash" \
  -e CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash" \
  -e CLAUDE_CODE_EFFORT_LEVEL="max" \
  -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  --tmpfs /tmp:rw,size=512M \
  --tmpfs /run:rw,size=16M \
  --network=slirp4netns \
  llm-sandbox \
  claude --dangerously-skip-permissions "$@"
