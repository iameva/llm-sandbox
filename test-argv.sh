#!/usr/bin/env bash
# Compare the argv sandbox-run.sh builds against the eight wrapper
# scripts it replaced, recovered from git.
#
# Runs no containers. Substitutes a stub for podman and diffs what each
# side would have executed. Every difference should be one of the
# intended phase-1 changes, which are listed per agent below.
#
# Usage: ./test-argv.sh [git-ref]
#
# Default ref is the parent of whichever commit deleted the wrappers,
# found at run time. Hardcoding HEAD went stale the moment the deletion
# was committed, and the failure was silent — every agent SKIPped and
# the script still exited 0.

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"

default_ref() {
    local del
    del="$(git -C "$REPO" rev-list -1 HEAD -- claude-sandbox.sh)"
    [[ -n "$del" ]] || { echo "HEAD"; return; }
    if git -C "$REPO" cat-file -e "$del:claude-sandbox.sh" 2>/dev/null; then
        echo "$del"          # still present there
    else
        echo "${del}^"       # that commit removed it
    fi
}

REF="${1:-$(default_ref)}"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

# Stub podman: print argv one token per line, do nothing else.
mkdir -p "$WORK/stub"
printf '#!/bin/sh\nprintf "%%s\\n" "$@"\n' > "$WORK/stub/podman"
chmod +x "$WORK/stub/podman"

# Isolated HOME so the comparison does not depend on real config, and so
# the deepseek path has a key to read.
export HOME="$WORK/home"
mkdir -p "$HOME/.config"
echo "sk-stub" > "$HOME/.config/deepseek.api"

export PATH="$WORK/stub:$PATH"

# agent name -> old script name
declare -A OLD=(
    [claude]=claude-sandbox.sh
    [codex]=codex-sandbox.sh
    [aider]=aider-sandbox.sh
    [omp]=omp-sandbox.sh
    [pi]=pi-sandbox.sh
    [opencode]=opencode-sandbox.sh
    [llm]=llm-sandbox.sh
    [deepseek-claude]=deepseek-claude-code.sh
)

# Differences that are meant to be there.
expected_note() {
    case "$1" in
        llm)  echo "network=host -> pasta (the phase 1 fix), telemetry env, no creds by default" ;;
        deepseek-claude) echo "slirp4netns -> pasta (telemetry env already present)" ;;
        *)    echo "slirp4netns -> pasta, telemetry env" ;;
    esac
}

fail=0
for agent in "${!OLD[@]}"; do
    script="${OLD[$agent]}"
    if ! git -C "$REPO" show "$REF:$script" > "$WORK/$script" 2>/dev/null; then
        # A skip is a failure. The whole point is comparing against the
        # old wrappers; if they cannot be found, nothing was compared.
        echo "SKIP $agent — $script not found at $REF" >&2
        fail=1
        continue
    fi
    chmod +x "$WORK/$script"

    # SANDBOX_LLM_AGENTS makes the new llm shell mount what the old one
    # mounted unconditionally, so the credential change does not drown
    # out the rest of the diff.
    ( cd "$REPO" && "$WORK/$script" ) 2>/dev/null | sort > "$WORK/$agent.old" || true
    ( cd "$REPO" && SANDBOX_LLM_AGENTS=claude,codex,opencode "$REPO/sandbox-run.sh" "$agent" ) \
        2>/dev/null | sort > "$WORK/$agent.new" || true

    echo "=== $agent ==="
    echo "    expected: $(expected_note "$agent")"
    # Explicit line formats, so a removed "--network=..." cannot be
    # mistaken for a "---" diff header.
    if diff --unchanged-line-format='' \
            --old-line-format='    old only: %L' \
            --new-line-format='    new only: %L' \
            "$WORK/$agent.old" "$WORK/$agent.new" > "$WORK/$agent.diff"; then
        echo "    IDENTICAL — no intended changes applied, which is itself suspicious"
        fail=1
    else
        cat "$WORK/$agent.diff"
    fi
    echo
done

exit "$fail"
