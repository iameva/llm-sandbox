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

install -m 0755 backend-config.py "$CONF/backend-config.py"
install -m 0644 backend-provider.mjs "$CONF/backend-provider.mjs"
install -m 0644 backends.example.json "$CONF/backends.example.json"
# Set up working defaults on the first install; preserve user changes on reinstall.
if [ ! -e "$CONF/backends.json" ] && [ ! -L "$CONF/backends.json" ]; then
    install -m 0644 backends.example.json "$CONF/backends.json"
fi

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

# Keep the QEMU runtime beside its dependencies; no VMs or images are built.
mkdir -p "$CONF/qemu"
for file in sandbox.py sandbox_guest.py network_relay.py proxy_process.py guest_verify.py runtime_support.py images.py; do
    install -m 0644 "qemu/$file" "$CONF/qemu/$file"
done
for file in build_image.py build_support.py provision_agents.py; do
    install -m 0644 "prototypes/qemu/$file" "$CONF/qemu/$file"
done
install -m 0644 egress-proxy.py "$CONF/qemu/egress-proxy.py"
cat > "$BIN/,sandbox-image" <<'SH'
#!/bin/sh
exec python3 "$HOME/.config/llm-sandbox/qemu/images.py" "$@"
SH
chmod 0755 "$BIN/,sandbox-image"
