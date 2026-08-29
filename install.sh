#!/bin/sh
# Install the sandbox entry points into ~/.local/bin.
#
# Every agent entry point is a copy of sandbox-run.sh, which recovers the
# agent name from $0. One file, so a change to the run arguments cannot
# apply to some agents and miss others.

set -eu

BIN="${HOME}/.local/bin"
CONF="${HOME}/.config/llm-sandbox"
mkdir -p "$BIN" "$CONF"

for agent in claude codex llm opencode aider pi omp; do
    install -m 0755 sandbox-run.sh "$BIN/,${agent}-sandbox.sh"
done

install -m 0755 sandbox-run.sh "$BIN/,deepseek-claude-code.sh"
install -m 0755 sandbox-run.sh "$BIN/,sandbox-run.sh"

install -m 0755 copy-session.sh "$BIN/,copy-session.sh"
install -m 0755 egress-proxy.py "$BIN/,egress-proxy.py"

# The proxy reads this path by default. The repo copy is the source of
# truth, so a reinstall overwrites it. Keep a .bak of a differing copy
# first: overwriting can only ever widen or narrow egress, and doing that
# silently is how you end up unable to explain what the proxy is doing.
ALLOW="$CONF/egress-allowlist.txt"
if [ -e "$ALLOW" ] && ! cmp -s egress-allowlist.txt "$ALLOW"; then
    cp -p "$ALLOW" "$ALLOW.bak"
    echo "install.sh: replaced $ALLOW (previous copy saved as $ALLOW.bak)"
fi
install -m 0644 egress-allowlist.txt "$ALLOW"
