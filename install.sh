#!/bin/sh
# Install the sandbox entry points into ~/.local/bin.
#
# Every agent entry point is a copy of sandbox-run.sh, which recovers the
# agent name from $0. One file, so a change to the run arguments cannot
# apply to some agents and miss others.

set -eu

BIN="${HOME}/.local/bin"
mkdir -p "$BIN"

for agent in claude codex llm opencode aider pi omp; do
    install -m 0755 sandbox-run.sh "$BIN/,${agent}-sandbox.sh"
done

install -m 0755 sandbox-run.sh "$BIN/,deepseek-claude-code.sh"
install -m 0755 sandbox-run.sh "$BIN/,sandbox-run.sh"

install -m 0755 copy-session.sh "$BIN/,copy-session.sh"
install -m 0755 egress-proxy.py "$BIN/,egress-proxy.py"
