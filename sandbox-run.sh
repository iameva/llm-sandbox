#!/usr/bin/env bash
#
# Common runner for every sandboxed LLM agent.
#
# Invoked either with the agent name as the first argument:
#     sandbox-run.sh claude --resume
# or via one of the installed entry points, which dispatch on $0:
#     ,claude-sandbox.sh --resume
#
# --shell as the first argument runs an interactive shell instead of the
# agent, with that agent's exact mounts, network and isolation:
#     ,claude-sandbox.sh --shell
#     ,claude-sandbox.sh --shell -c 'ip route'
#
# --check runs the sandbox verification suite and exits non-zero if
# anything fails:
#     SANDBOX_ISOLATION=vm ,claude-sandbox.sh --check
#
# Environment:
#   SANDBOX_ISOLATION   container (default) | gvisor | vm
#                       gvisor needs a hand-installed runsc; vm needs
#                       crun-krun and /dev/kvm, and is currently broken
#                       (see the uid note below).
#   SANDBOX_RUNSC       path to runsc, if it is not on PATH
#   SANDBOX_KRUN        path to krun, if it is not on PATH
#   SANDBOX_PROXY       host:port of the egress proxy. When set, the
#                       sandbox gets no DNS and must reach the network
#                       through this proxy. Unset means unrestricted.
#   SANDBOX_LABEL_MODE  z (default, shared SELinux label) | Z (private —
#                       breaks other containers sharing the same paths)
#   SANDBOX_IMAGE       image name, default llm-sandbox
#   SANDBOX_USERNS      passed to --userns verbatim; 'none' omits it.
#                       Default keep-id in container mode,
#                       keep-id:uid=0,gid=0 in vm mode.
#   SANDBOX_USER        <uid>:<gid> for --user. Empty by default —
#                       krun ignores it. Bisecting knob only.
#   SANDBOX_TMP         host directory to bind at /tmp instead of using a
#                       tmpfs. A tmpfs is RAM, and under SANDBOX_ISOLATION=vm
#                       that RAM is charged to the guest's fixed allocation,
#                       so a large build can OOM the VM. Defaults to a
#                       per-run directory in vm mode, tmpfs otherwise.
#                       The literal value 'tmpfs' forces a tmpfs in vm mode.
#   SANDBOX_TMPFS_SIZE  size of the /tmp tmpfs when one is used, default 512M
#   SANDBOX_DRY_RUN     1 to print the podman argv and exit

set -euo pipefail

ROOT="${HOME}/.config/llm-sandbox"
IMAGE="${SANDBOX_IMAGE:-llm-sandbox}"
ISOLATION="${SANDBOX_ISOLATION:-container}"
# SELinux mount label. 'z' is a shared label; 'Z' is private to one
# container.
#
# Default is 'z' despite 'Z' being the stronger choice, because 'Z'
# revokes access for anything else using the same directory: mount a
# tree that a running container also has, and that container loses it
# until the labels are restored. Relabelling is persistent on disk, so
# the damage outlives the run.
#
# Use 'Z' only when nothing else shares these paths. Repair a tree that
# has already been relabelled with:  restorecon -RFv <dir>
LABEL_MODE="${SANDBOX_LABEL_MODE:-z}"
PROXY="${SANDBOX_PROXY:-}"
DRY_RUN="${SANDBOX_DRY_RUN:-0}"

# uid mapping.
#
# container mode: keep-id maps the invoking user to the same uid inside,
# so a file written to /workspace belongs to you on the host, and the
# process agrees about who it is. Verified 2026-08-06: id -u is 1000 and
# files it creates read back as 1000.
#
# vm mode: keep-id, the least broken of three measured options — not a
# working one. vm mode does not currently produce a usable sandbox.
#
# krun ignores the image's USER line, --user and --tmpfs, so the
# entrypoint runs as uid 0 and cannot be moved. Measured 2026-08-06:
#
#   keep-id                writes succeed everywhere, but the process is
#                          uid 0 and every file it creates reads back
#                          uid 1000
#   none                   /workspace not writable at all
#   keep-id:uid=0,gid=0    /workspace not writable at all
#   keep-id + --user 1000  identical to plain keep-id; --user ignored
#
# The reading — inference, not measurement — is that libkrun's virtiofs
# does its own uid translation that podman's --userns does not drive, so
# each mapping lands on a different broken combination rather than
# composing. Under plain keep-id the guest's uid 0 is root within a
# userns that has host 1000 mapped, which is why writes work there and
# nowhere else.
#
# What this costs: any tool that compares its own uid against a file it
# owns will refuse to run. Claude Code's temp directory check is one.
# git's dubious-ownership check on /workspace is the next one waiting.
#
# Value is passed to --userns verbatim; 'none' omits the flag entirely.
USERNS="${SANDBOX_USERNS:-keep-id}"
# Process uid. Container mode honours the image's `USER appuser` and
# needs nothing here. vm mode ignores --user, measured 2026-08-06, so
# this defaults to empty everywhere and stays only as a bisecting knob.
# Setting it under vm while USERNS maps to uid 0 would reintroduce the
# mismatch if krun ever starts honouring it.
RUN_USER="${SANDBOX_USER:-}"

die() { echo "sandbox-run: $*" >&2; exit 1; }

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

# ---------------------------------------------------------------------
# Per-agent configuration
#
# MOUNTS entries are "<path under $ROOT>:<absolute path in sandbox>".
# ENVS entries are "KEY=value".
# CMD is the argv to run inside the sandbox; "$@" is appended.
# ---------------------------------------------------------------------

MOUNTS=()
ENVS=()
CMD=()

HOME_IN_SANDBOX="/home/appuser"

configure_agent() {
    case "$1" in
    claude)
        MOUNTS=(
            "claude:${HOME_IN_SANDBOX}/.claude"
            ".claude.json:${HOME_IN_SANDBOX}/.claude.json"
        )
        CMD=(claude --dangerously-skip-permissions)
        ;;
    codex)
        MOUNTS=(
            "codex:${HOME_IN_SANDBOX}/.codex"
            "orca:${HOME_IN_SANDBOX}/.orca"
        )
        CMD=(codex --dangerously-bypass-approvals-and-sandbox)
        ;;
    aider)
        MOUNTS=("aider:${HOME_IN_SANDBOX}/.aider")
        CMD=(aider)
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
        ENVS=("TODO_USER=${TODO_USER:-${USER:-appuser}}")
        CMD=("${HOME_IN_SANDBOX}/.opencode/bin/opencode")
        ;;
    llm)
        # Interactive shell. Mounts no credentials by default: this used
        # to carry every agent's config at once, which made it the widest
        # credential exposure in the set. Name what you need:
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
                    MOUNTS+=(
                        "claude:${HOME_IN_SANDBOX}/.claude"
                        ".claude.json:${HOME_IN_SANDBOX}/.claude.json"
                    ) ;;
                codex)
                    MOUNTS+=(
                        "codex:${HOME_IN_SANDBOX}/.codex"
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
    deepseek-claude)
        # Claude Code pointed at DeepSeek's Anthropic-compatible API.
        # Separate config dir so a third-party endpoint never touches
        # Anthropic-authenticated state.
        local key_file="${HOME}/.config/deepseek.api"
        [[ -r "$key_file" ]] || die "missing DeepSeek key at $key_file"

        mkdir -p "$ROOT/deepseek-claude"
        # Seed valid JSON on first run so podman mounts a file, not a
        # directory, and Claude does not choke on an empty config. Only
        # when missing or empty, so a populated config is never lost.
        local config="$ROOT/deepseek-claude/.claude.json"
        [[ -s "$config" ]] || echo '{}' > "$config"

        MOUNTS=(
            "deepseek-claude:${HOME_IN_SANDBOX}/.claude"
            "deepseek-claude/.claude.json:${HOME_IN_SANDBOX}/.claude.json"
        )
        ENVS=(
            "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic"
            "ANTHROPIC_AUTH_TOKEN=$(<"$key_file")"
            "ANTHROPIC_MODEL=deepseek-v4-pro[1m]"
            "ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4-pro[1m]"
            "ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro[1m]"
            "ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash"
            "CLAUDE_CODE_SUBAGENT_MODEL=deepseek-v4-flash"
            "CLAUDE_CODE_EFFORT_LEVEL=max"
        )
        CMD=(claude --dangerously-skip-permissions)
        ;;
    *)
        die "unknown agent: $1"
        ;;
    esac
}

configure_agent "$AGENT"

# Everything below appends to ENVS, so it must stay after
# configure_agent — that function assigns ENVS wholesale for some agents
# and would discard anything set earlier.

# Applies to every agent: drops telemetry and update-check traffic, which
# keeps the egress allowlist short.
ENVS+=("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")

# HOME. With the guest running as root, the runtime may hand it /root
# rather than the image's /home/appuser — where every credential mount
# lands. Stating it removes the question. vm only, so container argv is
# unchanged.
if [[ "$ISOLATION" == "vm" ]]; then
    ENVS+=("HOME=${HOME_IN_SANDBOX}")
fi

# ---------------------------------------------------------------------
# --shell and --check
#
# Both replace the agent's command while leaving every other setting
# alone: same mounts, same network, same proxy, same isolation. That is
# the point — debugging a different configuration than the agent runs
# under tells you nothing about the agent.
# ---------------------------------------------------------------------

read -r -d '' CHECK_SCRIPT <<'CHECK' || true
fail=0
say() { printf '%-28s %s\n' "$1" "$2"; }
bad() { fail=1; say "$1" "FAIL  $2"; }

# isolation mode is reported so a mode that silently did nothing is
# visible rather than inferred.
say "isolation" "$ISOLATION_MODE"

if [ "$(uname -r)" = "$HOST_KERNEL" ]; then
    if [ "$EXPECT_OWN_KERNEL" = "1" ]; then
        bad "kernel" "same as host ($HOST_KERNEL) — asked for $ISOLATION_MODE, got a plain container"
    else
        say "kernel" "OK  $(uname -r), shared with host (container mode)"
    fi
else
    say "kernel" "OK  guest $(uname -r), host $HOST_KERNEL"
fi

devs=$(ls /sys/class/net 2>/dev/null | tr '\n' ' ')
say "network devices" "${devs:-none}"

# Read routes from /proc directly. iproute2 is not in the image, so
# anything shelling out to `ip` reports a false negative here.
gw=none
while read -r iface dest gwhex rest; do
    [ "$dest" = "00000000" ] || continue
    gw="$((0x${gwhex:6:2})).$((0x${gwhex:4:2})).$((0x${gwhex:2:2})).$((0x${gwhex:0:2})) via $iface"
    break
done < /proc/net/route 2>/dev/null
say "default route" "$gw"

if [ -s /etc/resolv.conf ] && grep -q '^nameserver' /etc/resolv.conf 2>/dev/null; then
    say "dns" "present — expected only when SANDBOX_PROXY is unset"
else
    say "dns" "OK  no resolver"
fi

say "proxy env" "${HTTPS_PROXY:-unset}"

say "process uid" "$(id -u) ($(id -un 2>/dev/null || echo '?'))"
say "home" "${HOME:-unset}"

# Credential mounts must be where the agent will look for them and
# readable by whoever the process turned out to be. Under vm the guest
# runs as root with a HOME the runtime chose, so neither is a given.
# ${=...} because this runs under zsh, which does not word-split.
for m in ${=CHECK_MOUNTS:-}; do
    if [ ! -e "$m" ]; then
        bad "mount $m" "missing"
    elif [ ! -r "$m" ]; then
        bad "mount $m" "present but not readable"
    else
        say "mount $m" "OK  readable"
    fi
done

# A file the process just created must read back owned by that process.
# Anywhere this fails, the uid mapping is wrong, and any tool that checks
# who owns its own files refuses to run. Claude Code does exactly that
# for its temp directory, which is how this was first found.
own_check() {
    dir="$1"
    label="$2"
    probe="$dir/.sandbox-ownership-probe"
    if ! touch "$probe" 2>/dev/null; then
        bad "$label" "cannot write $dir"
        return
    fi
    puid=$(stat -c %u "$probe")
    pgid=$(stat -c %g "$probe")
    rm -f "$probe"
    if [ "$puid" = "$(id -u)" ]; then
        say "$label" "OK  uid $puid gid $pgid — compare on the host"
    else
        bad "$label" "process is uid $(id -u) but its own file reads back uid $puid — uid mapping is inconsistent; try SANDBOX_USERNS"
    fi
}

own_check /workspace "workspace write"
own_check /tmp "tmp write"

say "tmp backing" "$(stat -f -c %T /tmp 2>/dev/null || echo unknown)"

if [ -n "${HTTPS_PROXY:-}" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://api.anthropic.com/v1/messages 2>/dev/null || echo 000)
    [ "$code" = "000" ] && bad "egress allowed host" "no response (proxy unreachable?)" \
                        || say "egress allowed host" "OK  http $code"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://example.com/ 2>/dev/null || echo 000)
    [ "$code" = "000" ] && say "egress denied host" "OK  refused" \
                        || bad "egress denied host" "reachable, http $code — allowlist not enforced"
fi

echo
[ "$fail" = 0 ] && echo "all checks passed" || echo "SOME CHECKS FAILED"
exit "$fail"
CHECK

if [[ "${SANDBOX_SHELL:-}" == "1" || "${1:-}" == "--shell" ]]; then
    [[ "${1:-}" == "--shell" ]] && shift
    CMD=(zsh)
elif [[ "${SANDBOX_CHECK:-}" == "1" || "${1:-}" == "--check" ]]; then
    # HOST_KERNEL lets the guest tell whether it is really a guest.
    # EXPECT_OWN_KERNEL decides whether a shared kernel is a failure or
    # just container mode working as asked. Both vm and gvisor report a
    # kernel of their own — gVisor's Sentry answers uname itself — so a
    # kernel matching the host means the runtime silently did nothing.
    ENVS+=("HOST_KERNEL=$(uname -r)")
    ENVS+=("ISOLATION_MODE=$ISOLATION")
    ENVS+=("EXPECT_OWN_KERNEL=$([[ "$ISOLATION" == "container" ]] && echo 0 || echo 1)")
    check_mounts=""
    for m in ${MOUNTS[@]+"${MOUNTS[@]}"}; do
        check_mounts+="${m#*:} "
    done
    ENVS+=("CHECK_MOUNTS=${check_mounts% }")
    CMD=(zsh -c "$CHECK_SCRIPT")
    set --
fi

# ---------------------------------------------------------------------
# Build the podman argv
# ---------------------------------------------------------------------

argv=(podman run --rm -it)
if [[ "$USERNS" != "none" ]]; then
    argv+=("--userns=$USERNS")
fi
if [[ -n "$RUN_USER" ]]; then
    argv+=(--user "$RUN_USER")
fi
argv+=(--workdir /workspace)

# Find an OCI runtime and return something podman will accept.
#
# Normally an absolute path. But an override with no '/' is passed
# through as a runtime *name*, because that is the only way to attach
# arguments to a runtime: podman's --runtime takes a path with no room
# for flags, so a runtime needing them has to be declared in
# containers.conf and referenced by name.
#
#   [engine.runtimes]
#   runsc = ["/home/you/.local/bin/runsc", "--ignore-cgroups"]
#
#   SANDBOX_RUNSC=runsc ,claude-sandbox.sh --check
#
#
# Always an absolute path, never a bare name: podman resolves a bare name
# against its configured runtimes, and neither crun-krun nor a
# hand-installed runsc reliably registers one. That failure reads as
#   Error: default OCI runtime "krun" not found: invalid argument
# which looks like the runtime is missing when it is merely unregistered.
#
# /var/usrlocal/bin is where /usr/local/bin points on Fedora Atomic, and
# ~/.local/bin is where an unprivileged install lands. Both are searched
# because on an image-based OS a hand-installed runtime cannot go in
# /usr/bin.
find_runtime() {
    local override="$1"; shift
    local candidate dir
    if [[ -n "$override" ]]; then
        echo "$override"
        return
    fi
    for candidate in "$@"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return
        fi
    done
    for candidate in "$@"; do
        for dir in /usr/bin /usr/local/bin /var/usrlocal/bin "$HOME/.local/bin"; do
            if [[ -x "$dir/$candidate" ]]; then
                echo "$dir/$candidate"
                return
            fi
        done
    done
    echo ""
}

case "$ISOLATION" in
    container)
        ;;
    gvisor)
        # gVisor serves the guest's syscalls from a userspace kernel, so
        # reaching the host kernel needs a Sentry bug plus defeating its
        # seccomp filter. Weaker than the KVM boundary vm mode was after,
        # much stronger than a shared kernel.
        #
        # Chosen over krun because it implements the OCI spec rather than
        # ignoring parts of it. krun drops USER, --user and --tmpfs, which
        # is what left vm mode unusable. Whether runsc honours them is the
        # thing --check is for; do not assume it.
        runsc_bin="$(find_runtime "${SANDBOX_RUNSC:-}" runsc)"
        [[ "$runsc_bin" != */* || -x "$runsc_bin" ]] || \
            die "SANDBOX_RUNSC=$runsc_bin is not an executable file"
        [[ -n "$runsc_bin" ]] || die \
"no runsc runtime found.

  Look for it:      command -v runsc; ls -l /var/usrlocal/bin/runsc ~/.local/bin/runsc
  Point at it:      SANDBOX_RUNSC=/path/to/runsc

gVisor is not packaged for Fedora, so this is a hand-installed binary.
Container mode still works: unset SANDBOX_ISOLATION."

        argv+=(--runtime "$runsc_bin")
        ;;
    vm)
        # libkrun boots each run as a microVM with its own kernel, so a
        # container escape is a VMM bug rather than a kernel LPE.
        # vm mode is not usable yet: krun runs the entrypoint as uid 0
        # while virtiofs stamps files uid 1000, and no --userns mapping
        # reconciles them. See the note above and vm-migration-plan.md.
        # Loud on purpose — silently handing back a broken sandbox is how
        # the 2026-08-04 "verified" claim happened.
        if [[ "${SANDBOX_VM_ACK:-}" != "1" && "${SANDBOX_CHECK:-}" != "1" && "${CMD[0]}" != "zsh" ]]; then
            echo "sandbox-run: WARNING — vm mode has a known uid mismatch (process 0, files 1000)." >&2
            echo "sandbox-run: agents that check ownership will refuse to start. Set SANDBOX_VM_ACK=1 to silence." >&2
        fi

        [[ -e /dev/kvm ]] || die "SANDBOX_ISOLATION=vm needs /dev/kvm"
        [[ -r /dev/kvm && -w /dev/kvm ]] || \
            die "/dev/kvm is not readable and writable by you; add yourself to the 'kvm' group"

        # Pass an absolute path rather than the name. Podman resolves a
        # bare "krun" against its configured runtimes, and the package
        # does not always register one, which fails as:
        #   Error: default OCI runtime "krun" not found: invalid argument
        krun_bin="$(find_runtime "${SANDBOX_KRUN:-}" krun crun-krun)"
        [[ "$krun_bin" != */* || -x "$krun_bin" ]] || \
            die "SANDBOX_KRUN=$krun_bin is not an executable file"
        [[ -n "$krun_bin" ]] || die \
"no krun runtime found.

  Look for it:      rpm -qa | grep -iE 'krun|libkrun'
                    ls -l /usr/bin/krun /usr/bin/crun-krun 2>/dev/null
  Install it:       sudo dnf install crun-krun
  Point at it:      SANDBOX_KRUN=/path/to/krun

Container mode still works: unset SANDBOX_ISOLATION."

        argv+=(--runtime "$krun_bin")
        ;;
    *)
        die "SANDBOX_ISOLATION must be 'container', 'gvisor' or 'vm', got '$ISOLATION'"
        ;;
esac

# pasta, never host. --network=host would drop the network namespace and
# expose every service on the host's loopback and LAN.
argv+=(--network=pasta)

argv+=(-v "$PWD:/workspace:${LABEL_MODE},rw")
for m in "${MOUNTS[@]}"; do
    argv+=(-v "$ROOT/${m%%:*}:${m#*:}:${LABEL_MODE},rw")
done

if [[ -n "$PROXY" ]]; then
    # The proxy resolves hostnames on the sandbox's behalf, so the
    # sandbox needs no resolver of its own. That closes DNS tunnelling.
    argv+=(--dns=none)
    ENVS+=(
        "HTTPS_PROXY=http://${PROXY}"
        "HTTP_PROXY=http://${PROXY}"
        "https_proxy=http://${PROXY}"
        "http_proxy=http://${PROXY}"
        "NO_PROXY=localhost,127.0.0.1"
    )
fi

for e in "${ENVS[@]}"; do
    argv+=(-e "$e")
done

# /tmp. A tmpfs lives in RAM. Under a container that RAM is the host's,
# and elastic. Under a VM it comes out of the guest's fixed allocation,
# where a build that writes a few hundred megabytes to /tmp turns into an
# OOM kill. So vm mode gets a disk-backed /tmp by default.
#
# Measured 2026-08-06: krun ignores --tmpfs. SANDBOX_TMP=tmpfs passes it
# and /tmp still reports `fuse`. The same is true of the --tmpfs /run
# below, which is why /run is not writable in vm mode. So under vm the
# OOM argument above is moot — every path is virtiofs whatever we ask
# for — and the bind mount is doing nothing the runtime would not have
# done anyway. Keep it for container mode, where --tmpfs is honoured.
TMP_DIR="${SANDBOX_TMP:-}"
TMPFS_SIZE="${SANDBOX_TMPFS_SIZE:-512M}"
cleanup_tmp=""

# SANDBOX_TMP=tmpfs forces the tmpfs branch. Without it that branch is
# unreachable once ISOLATION=vm, which makes it impossible to tell a
# /tmp problem caused by the bind mount from one caused by the VM.
# Under vm this is diagnostic only: krun ignores --tmpfs, so it changes
# which mount is requested, not what the guest ends up with.
if [[ "$TMP_DIR" == "tmpfs" ]]; then
    TMP_DIR=""
elif [[ -z "$TMP_DIR" && "$ISOLATION" == "vm" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        TMP_DIR="$ROOT/tmp/${AGENT}.XXXXXX"
    else
        mkdir -p "$ROOT/tmp"
        # Per-run and removed on exit, so /tmp stays as ephemeral as the
        # tmpfs it replaces.
        TMP_DIR="$(mktemp -d "$ROOT/tmp/${AGENT}.XXXXXX")"
        cleanup_tmp="$TMP_DIR"
    fi
fi

if [[ -n "$TMP_DIR" ]]; then
    argv+=(-v "$TMP_DIR:/tmp:${LABEL_MODE},rw")
else
    argv+=(--tmpfs "/tmp:rw,size=${TMPFS_SIZE}")
fi

argv+=(--tmpfs /run:rw,size=16M
       "$IMAGE"
       "${CMD[@]}" "$@")

# ---------------------------------------------------------------------
# Network confinement
#
# The original plan was to filter a tap interface with nftables. That is
# not available: libkrun uses transparent socket impersonation, so the
# guest has only a dummy interface and no route, and the real sockets are
# opened by the krun process on the host. There is no tap to filter.
#
# A systemd scope filters at the cgroup instead, via BPF on socket
# operations. That catches the host-side krun process no matter how the
# guest's networking is arranged, so it works under TSI and would work
# just as well with a virtio-net device.
# ---------------------------------------------------------------------

if [[ "${SANDBOX_CONFINE:-}" == "1" ]]; then
    [[ -n "$PROXY" ]] || die "SANDBOX_CONFINE=1 needs SANDBOX_PROXY; denying all addresses without a proxy leaves no network at all"
    command -v systemd-run >/dev/null 2>&1 || die "SANDBOX_CONFINE=1 needs systemd-run"

    proxy_addr="${PROXY%:*}"
    [[ -n "$proxy_addr" ]] || die "cannot read an address out of SANDBOX_PROXY='$PROXY'"

    argv=(systemd-run --user --scope --quiet --collect
          -p IPAddressDeny=any
          -p "IPAddressAllow=${proxy_addr}"
          -- "${argv[@]}")
fi

if [[ "$DRY_RUN" == "1" ]]; then
    printf '%q ' "${argv[@]}"
    printf '\n'
    exit 0
fi

if [[ -z "$cleanup_tmp" ]]; then
    exec "${argv[@]}"
fi

trap 'rm -rf -- "$cleanup_tmp"' EXIT
rc=0
"${argv[@]}" || rc=$?
exit "$rc"
