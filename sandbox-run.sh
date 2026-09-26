#!/usr/bin/env bash
#
# Common runner for every sandboxed LLM agent. Each launch boots a
# disposable QEMU VM from the image selected by ,sandbox-image.
#
# Invoked either with the agent name as the first argument:
#     sandbox-run.sh claude --resume
# or via one of the installed entry points, which dispatch on $0:
#     ,claude-sandbox.sh --resume
#
# --shell as the first argument runs an interactive shell instead of the
# agent, with that agent's exact mounts and network:
#     ,claude-sandbox.sh --shell
#     ,claude-sandbox.sh --shell -c 'ip route'
#
# --check boots the same configuration and runs the guest smoke checks:
#     ,claude-sandbox.sh --check
#
# Backend options (before harness arguments):
#   --backend NAME   Choose a profile from backends.json or a built-in profile.
#   --model MODEL    Override the profile model for this launch.
# Sessions remain in the harness directory when the backend changes.
#
# Environment:
#   SANDBOX_BACKEND   Default backend for this invocation
#   SANDBOX_MODEL     Model override for this invocation
#   SANDBOX_BACKENDS_FILE  Alternate host path to backends.json
#   SANDBOX_QEMU_CONFIG    Image selection, default ~/.config/llm-sandbox/qemu.json
#   SANDBOX_QEMU_DISK      Explicit base image; overrides the selection
#   SANDBOX_ALLOW_FILE     Egress allowlist, default the installed copy
#   SANDBOX_QEMU_MEMORY_MIB, _CPUS, _BOOT_TIMEOUT, _BATCH_TIMEOUT,
#   _IDLE_TIMEOUT, _CACHE_DIR, _KEEP_ARTIFACTS
#                     Passed to qemu/sandbox.py; see README.
#   SANDBOX_BATCH     1 for a noninteractive run
#   SANDBOX_LLM_AGENTS  Agent state for ,llm-sandbox.sh, e.g. claude,codex
#   SANDBOX_DRY_RUN   1 to print the runtime argv and exit

set -euo pipefail

ROOT="${HOME}/.config/llm-sandbox"
DRY_RUN="${SANDBOX_DRY_RUN:-0}"
HOME_IN_SANDBOX="/home/fedora"

die() { echo "sandbox-run: $*" >&2; exit 1; }

# The Podman modes (container, gvisor, vm) were removed. Refuse them rather
# than quietly run something other than what was asked for.
case "${SANDBOX_ISOLATION:-qemu}" in
    qemu) ;;
    *) die "SANDBOX_ISOLATION=${SANDBOX_ISOLATION} is no longer supported; QEMU is the only mode. Unset it." ;;
esac

# ---------------------------------------------------------------------
# Agent name
# ---------------------------------------------------------------------

# Entry points are copies of this script named ,<agent>-sandbox.sh. Strip
# the decoration to recover the agent name. An explicit first argument
# always wins.
agent_from_argv0() {
    local base="${0##*/}"
    base="${base#,}"
    case "$base" in
        deepseek-claude-code.sh) echo "deepseek-claude" ;;
        sandbox-run.sh)          echo "" ;;
        *-sandbox.sh)            echo "${base%-sandbox.sh}" ;;
        *)                       echo "" ;;
    esac
}

AGENT="$(agent_from_argv0)"
if [[ -z "$AGENT" ]]; then
    [[ $# -ge 1 ]] || die "no agent given; usage: sandbox-run.sh <agent> [args...]"
    AGENT="$1"
    shift
fi

# The legacy entry point is an alias, not a separate harness or session store.
BACKEND="${SANDBOX_BACKEND:-}"
MODEL_OVERRIDE="${SANDBOX_MODEL:-}"
if [[ "$AGENT" == "deepseek-claude" ]]; then
    AGENT=claude
    BACKEND="${BACKEND:-deepseek}"
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backend|--model)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 requires a value"
            if [[ "$1" == "--backend" ]]; then BACKEND="$2"; else MODEL_OVERRIDE="$2"; fi
            shift 2 ;;
        --backend=*)
            BACKEND="${1#*=}"
            [[ -n "$BACKEND" ]] || die "--backend requires a value"
            shift ;;
        --model=*)
            MODEL_OVERRIDE="${1#*=}"
            [[ -n "$MODEL_OVERRIDE" ]] || die "--model requires a value"
            shift ;;
        *) break ;;
    esac
done

# ---------------------------------------------------------------------
# Per-agent configuration
#
# MOUNTS entries are "<path under $ROOT>:<absolute path in sandbox>".
# ENVS entries are "KEY=value", or a bare KEY inherited from this process.
# CMD is the argv to run inside the sandbox; "$@" is appended.
# ---------------------------------------------------------------------

MOUNTS=()
ENVS=()
CMD=()

# Mounts carrying Claude Code's credentials, appended to MOUNTS.
#
# Claude Code writes its global config as temp-file + rename beside the
# target. CLAUDE_CONFIG_DIR puts that config inside the directory mount, so
# the rename stays on one filesystem and the write stays atomic. (A single-
# file mount of ~/.claude.json made the rename fail EXDEV and fall back to
# truncate-then-write, which wiped a config on 2026-09-01.)
#
# The legacy host file is copied, never moved, when the new one is absent
# or empty, so a populated config is never lost.
claude_config_mounts() {
    MOUNTS+=("claude:${HOME_IN_SANDBOX}/.claude")
    [[ "$DRY_RUN" == "1" ]] && return

    local legacy="$ROOT/.claude.json"
    local config="$ROOT/claude/.claude.json"
    mkdir -p "$ROOT/claude"
    if [[ ! -s "$config" ]]; then
        if [[ -s "$legacy" ]]; then
            cp -p "$legacy" "$config"
            echo "sandbox-run: seeded $config from $legacy" >&2
        else
            echo '{}' > "$config"
        fi
    fi
}

configure_agent() {
    case "$1" in
    claude)
        MOUNTS=()
        claude_config_mounts
        CMD=(claude --dangerously-skip-permissions)
        ;;
    codex)
        # Not ~/.codex: that is codex's install root as well as its default
        # CODEX_HOME, so mounting over it would hide the binary. The guest
        # sets CODEX_HOME=~/.config/codex to separate the two.
        MOUNTS=(
            "codex:${HOME_IN_SANDBOX}/.config/codex"
            "orca:${HOME_IN_SANDBOX}/.orca"
        )
        CMD=(codex --dangerously-bypass-approvals-and-sandbox)
        ;;
    omp)
        MOUNTS=("omp:${HOME_IN_SANDBOX}/.omp")
        CMD=(omp)
        ;;
    pi)
        MOUNTS=("pi:${HOME_IN_SANDBOX}/.pi")
        CMD=(pi)
        ;;
    opencode)
        MOUNTS=(
            "opencode:${HOME_IN_SANDBOX}/.config/opencode"
            "local-opencode:${HOME_IN_SANDBOX}/.local/share/opencode"
        )
        ENVS=("TODO_USER=${TODO_USER:-${USER:-fedora}}")
        CMD=(opencode)
        ;;
    llm)
        # Interactive shell. Mounts no credentials by default; name what
        # you need:
        #     SANDBOX_LLM_AGENTS=claude,codex ,llm-sandbox.sh
        MOUNTS=()
        local requested="${SANDBOX_LLM_AGENTS:-}"
        if [[ -z "$requested" ]]; then
            echo "sandbox-run: llm shell started with no agent credentials." >&2
            echo "sandbox-run: add them with SANDBOX_LLM_AGENTS=claude,codex,opencode" >&2
        fi
        local want
        IFS=, read -ra want <<< "$requested"
        for w in "${want[@]}"; do
            case "$w" in
                claude)
                    claude_config_mounts ;;
                codex)
                    MOUNTS+=(
                        "codex:${HOME_IN_SANDBOX}/.config/codex"
                        "orca:${HOME_IN_SANDBOX}/.orca"
                    ) ;;
                opencode)
                    MOUNTS+=(
                        "opencode:${HOME_IN_SANDBOX}/.config/opencode"
                        "local-opencode:${HOME_IN_SANDBOX}/.local/share/opencode"
                    ) ;;
                "") ;;
                *) die "SANDBOX_LLM_AGENTS: unknown agent '$w'" ;;
            esac
        done
        CMD=(zsh)
        ;;
    *)
        die "unknown agent: $1"
        ;;
    esac
}

# Resolve the profile before creating mounts. The helper emits NUL-delimited
# records, never shell code. A private temporary file preserves its exit status.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
backend_helper="$script_dir/backend-config.py"
[[ -f "$backend_helper" ]] || backend_helper="$ROOT/backend-config.py"
[[ -f "$backend_helper" ]] || die "backend-config.py is missing; rerun install.sh"
backend_command=(python3 "$backend_helper" --harness "$AGENT"
                 --backend "$BACKEND" --model "$MODEL_OVERRIDE")
if [[ -n "${SANDBOX_BACKENDS_FILE:-}" ]]; then
    [[ -f "$SANDBOX_BACKENDS_FILE" ]] || die "SANDBOX_BACKENDS_FILE does not exist"
    backend_command+=(--config "$SANDBOX_BACKENDS_FILE")
fi
[[ "$DRY_RUN" != "1" ]] || backend_command+=(--dry-run)
backend_plan="$(mktemp)"
trap 'rm -f -- "$backend_plan"' EXIT
if ! "${backend_command[@]}" > "$backend_plan"; then
    rm -f -- "$backend_plan"
    exit 1
fi
mapfile -d '' -t backend_records < "$backend_plan"
rm -f -- "$backend_plan"
trap - EXIT

configure_agent "$AGENT"
BACKEND_ASSETS=()
for ((i=0; i<${#backend_records[@]}; i+=2)); do
    value="${backend_records[i+1]}"
    case "${backend_records[i]}" in
        arg) CMD+=("$value") ;;
        env) ENVS+=("$value") ;;
        secret)
            # The runtime inherits only this named value. Neither argv nor
            # dry-run output contains the key, and it is not persisted to
            # agent config.
            export "$value"
            ENVS+=("${value%%=*}") ;;
        asset) BACKEND_ASSETS+=("${backend_helper%/*}/$value:/opt/sandbox/$value") ;;
        *) die "invalid backend helper output" ;;
    esac
done
unset backend_records value

# Everything below appends to ENVS, so it must stay after
# configure_agent — that function assigns ENVS wholesale for some agents
# and would discard anything set earlier.

# Applies to every agent: drops telemetry and update-check traffic, which
# keeps the egress allowlist short.
ENVS+=("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")
ENVS+=("CLAUDE_CONFIG_DIR=${HOME_IN_SANDBOX}/.claude")

# ---------------------------------------------------------------------
# --shell and --check
#
# Both replace the agent's command while leaving every other setting
# alone: same mounts, same network, same proxy. Debugging a different
# configuration than the agent runs under tells you nothing about the agent.
# ---------------------------------------------------------------------

VERIFY=0
if [[ "${SANDBOX_SHELL:-}" == "1" || "${1:-}" == "--shell" ]]; then
    [[ "${1:-}" == "--shell" ]] && shift
    CMD=(zsh)
elif [[ "${SANDBOX_CHECK:-}" == "1" || "${1:-}" == "--check" ]]; then
    VERIFY=1
    CMD=(true)
    set --
fi

# ---------------------------------------------------------------------
# Launch
#
# Each invocation gets a private overlay, network stack and enforcing
# proxy; agent state directories are shared live.
# ---------------------------------------------------------------------

qemu_runner="$script_dir/qemu/sandbox.py"
[[ -f "$qemu_runner" ]] || qemu_runner="$ROOT/qemu/sandbox.py"
[[ -f "$qemu_runner" ]] || die "QEMU launcher is missing; rerun install.sh"
qemu_disk="${SANDBOX_QEMU_DISK:-}"
if [[ -z "$qemu_disk" ]]; then
    qemu_config="${SANDBOX_QEMU_CONFIG:-$ROOT/qemu.json}"
    qemu_disk=$(python3 "${qemu_runner%/*}/images.py" --config "$qemu_config" path) || exit 1
fi
qemu_allow="${SANDBOX_ALLOW_FILE:-$ROOT/egress-allowlist.txt}"
qargv=(python3 "$qemu_runner" --disk "$qemu_disk"
       --workspace "$PWD" --allow-file "$qemu_allow")
[[ "${SANDBOX_BATCH:-0}" != "1" ]] || qargv+=(--batch)
for setting in MEMORY_MIB CPUS BOOT_TIMEOUT BATCH_TIMEOUT IDLE_TIMEOUT CACHE_DIR; do
    variable="SANDBOX_QEMU_$setting"
    value="${!variable:-}"
    option="${setting,,}"
    [[ -z "$value" ]] || qargv+=("--${option//_/-}" "$value")
done
[[ "${SANDBOX_QEMU_KEEP_ARTIFACTS:-0}" != "1" ]] || qargv+=(--keep-artifacts)
for m in ${MOUNTS[@]+"${MOUNTS[@]}"}; do
    qargv+=(--mount "$ROOT/${m%%:*}:${m#*:}")
done
for e in "${ENVS[@]}"; do qargv+=(--env "$e"); done
for asset in ${BACKEND_ASSETS[@]+"${BACKEND_ASSETS[@]}"}; do qargv+=(--asset "$asset"); done
[[ "$VERIFY" != "1" ]] || qargv+=(--verify)
qargv+=(--command "${CMD[@]}" "$@")
if [[ "$DRY_RUN" == "1" ]]; then
    printf '%q ' "${qargv[@]}"
    printf '\n'
    exit 0
fi
exec "${qargv[@]}"
