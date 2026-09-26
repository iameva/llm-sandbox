# Sandbox hardening: containers to VMs

Plan for moving the LLM sandbox from rootless podman containers to
hardware-isolated VMs, and for cutting network egress to the model APIs.

Written 2026-08-02.

Status: code for phases 1, 2, 4, 5 and 6 is written and unit-tested.
None of it has been run against podman — it was written from inside the
sandbox, which has no podman, no nftables and no /dev/kvm. Everything
below marked "needs host verification" is unproven.

| Phase | State |
| --- | --- |
| 0 recon | partial — /dev/kvm confirmed, package checks outstanding |
| 1 holes | code done, needs host verification |
| 2 collapse | code done, argv verified against the old scripts |
| 3 log-only egress | ready to run; needs a week of wall clock |
| 4 enforce egress | proxy tested; confinement redesigned, unverified |
| 5 vm switch | krun dead — uid mapping unfixable; **gvisor works** 2026-08-06 |
| 6 credentials | partly done; the rest is account-side |
| 7 flip default | deliberately not done — needs soak first |

## Threat model

Four things can go wrong. They need different defences.

1. **Agent goes off-script.** Bad tool call, wrong branch, destructive
   command. Contained by filesystem scope and git review. Unchanged by
   this plan.
2. **Prompt injection.** Fetched web content or a hostile repo directs
   the agent to misuse the access we gave it: the OAuth token, the
   source in `/workspace`, the network. **Contained by egress control
   and credential scope. Not by a VM.**
3. **Hostile code executed inside the sandbox.** An npm postinstall
   script, a dependency's test suite, a downloaded binary.
   **Contained by the VM boundary.**
4. **Kernel LPE from inside.** Today this yields host access as the
   invoking user, because the kernel is the whole boundary.
   **Contained by the VM boundary.**

The VM addresses 3 and 4. Egress control addresses 2, which is the
likelier event. Egress work therefore comes first.

## Decisions

- **Keep `$PWD` bind-mounted.** With no git egress, the mount is the
  only way work leaves the sandbox. Dropping it would strengthen the
  boundary but costs more than it gains under a chat-only allowlist.
- **Egress: LLM chat endpoints only.** Derived empirically in 4a, not
  guessed.
- **No TLS interception.** Hostname allowlist on the CONNECT verb.
  Preserves cert pinning; API traffic stays end-to-end encrypted.
- **No DNS inside the sandbox.** The proxy resolves. Closes DNS
  tunnelling.
- **VM runtime: podman + libkrun** (`--runtime krun`). Minimal diff —
  same image, same wrappers, one flag. Fallback is a persistent
  libvirt VM over ssh if krun proves too rough.
- **Both paths stay live.** The container path is the rollback and
  stays working until the VM path has soaked.

## Known residual risk

Once egress is down to the model APIs, exfiltration via the model API
remains: an injected prompt tells the agent to include source in a chat
request. This cannot be closed without removing the agent's purpose.
Accepted knowingly.

## Phase 0 — host recon

Confirm on the host:

- [x] `/dev/kvm` exists
- [ ] invoking user is in the `kvm` group
- [ ] `crun-krun`, `virtiofsd`, `passt` available
- [ ] host is bare metal, or nested virt is enabled

```sh
ls -l /dev/kvm
id -nG | tr ' ' '\n' | grep -x kvm
dnf list --installed crun-krun virtiofsd passt
rpm -qa | grep -iE 'krun|libkrun'
```

Skipping this step cost a round trip on 2026-08-04:
`--runtime krun` failed with `default OCI runtime "krun" not found`.
Podman resolves a bare runtime name against its configured runtimes and
the package does not reliably register one. The runner now finds the
binary and passes an absolute path, with `SANDBOX_KRUN` to override, so
the remaining cause of that error is the package genuinely being absent.

## Phase 1 — close container-path holes

Independent of everything else. Immediate value, and it keeps the
rollback path healthy.

- `llm-sandbox.sh` uses `--network=host`, dropping the network
  namespace entirely. The agent reaches host `127.0.0.1` services and
  the LAN. The other seven wrappers use `slirp4netns`. Switch to
  pasta.
- `:z` to `:Z` on mounts. Lowercase applies a shared SELinux label, so
  concurrent sandboxes can reach each other's relabeled content.
  **Test first** — `:Z` on `$PWD` relabels the real project directory
  and can disrupt host-side tools.
- Delete `Containerfile:32` (`USER appuser2`). That user does not
  exist; the next line overrides it.
- Pin `fedora-minimal` by digest. Pin the five `curl | bash` installer
  versions. Builds are currently non-reproducible and trust five
  upstreams at build time.
- Add `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` to all wrappers.
  Only `deepseek-claude-code.sh` sets it today. Shortens the phase 4
  allowlist.

**Test:** start `python3 -m http.server 8000` on the host. From inside
each sandbox, `curl localhost:8000` must fail. This is the standing
regression test for the `--network=host` fix.

## Phase 2 — collapse the wrappers

Bottleneck for everything after it. Eight near-identical scripts mean
every later change is an eight-file edit — which is exactly how
`--network=host` came to survive in one wrapper and not the others.

One `sandbox-run.sh` taking an agent name, plus a per-agent table
holding the parts that actually differ: extra mounts, env, command.

```sh
# sketch
agent_config() {
  case "$1" in
    claude)  MOUNTS=(claude .claude.json); CMD=(claude --dangerously-skip-permissions) ;;
    codex)   MOUNTS=(codex orca);          CMD=(codex --dangerously-bypass-approvals-and-sandbox) ;;
    ...
  esac
}
```

`install.sh` keeps installing the same eight `,name-sandbox.sh` entry
points; each becomes a one-line call into the common runner. No change
to how they are invoked.

Before writing it, confirm which of the eight are still in use.
Deleting a dead wrapper is cheaper than securing it.

**Test:** each of the eight entry points still launches its agent, sees
`/workspace`, and reads its own config dir. Diff the generated
`podman run` argv against the current scripts — intentional
differences only.

## Phase 3 — egress, log-only (4a)

CONNECT proxy on the host, allowing everything, recording every
hostname each agent contacts. `HTTPS_PROXY` and `HTTP_PROXY` into the
sandbox.

Run for a week of normal work. Output is the real allowlist.

**Test:** proxy log shows model API hostnames during a session. Agents
behave normally — this phase must not change anything a user notices.

## Phase 4 — egress, deny (4b)

Flip the proxy to deny-by-default using the observed list. Remove DNS
from the sandbox.

Enforce at the network layer too, so the proxy is not an honour system
— `HTTPS_PROXY` is trivially ignored by any process that chooses to.
The sandbox's only reachable address must be the proxy. Easier once
phase 5 gives us a tap interface to filter with nftables than it is
under pasta.

Expect these to break. Confirm each is acceptable before flipping:

- `cargo build` / `npm install` / `pip install` / `go mod download` on
  a fresh checkout
- `git push` / `git pull` (push from the host instead, after review)
- Claude Code `WebFetch` (client-side). `WebSearch` survives — it runs
  server-side through the already-allowed API.
- agent auto-updaters — pin versions in the image instead

**Test:** `curl https://example.com` fails. `curl
https://api.anthropic.com` returns 401, not a timeout — proving the
proxy allows it and the deny is at the network layer, not DNS. A real
agent session completes normally.

## Phase 5 — VM runtime behind a switch

`SANDBOX_ISOLATION=container|vm`, defaulting to `container`. Trivial
after phase 2. Both paths live; flip per-invocation and compare.

Expect friction at:

- `--userns=keep-id` semantics, since uid mapping moves into virtiofsd
- networking — passt/pasta or libkrun's socket impersonation rather
  than slirp4netns
- file ownership on the `$PWD` share

### /tmp under a VM

A tmpfs is RAM. Under a container that is host RAM and elastic. Under a
VM it comes out of the guest's fixed allocation, so a build that writes
a few hundred megabytes to `/tmp` becomes an OOM kill rather than a
slow write. `bigger tmpfs` is already a commit in this repo's history,
so the ceiling has been hit once under containers already.

vm mode therefore bind-mounts a per-run directory at `/tmp` instead of
using a tmpfs. It is created under `$ROOT/tmp`, removed when the run
exits, so `/tmp` stays as ephemeral as the tmpfs it replaces. Override
with `SANDBOX_TMP=<host dir>`, which works in container mode too.
`SANDBOX_TMPFS_SIZE` sizes the tmpfs where one is still used.

This costs some speed — `/tmp` is now disk rather than RAM — and buys
not having to guess the guest's memory ceiling in advance.

**Verified 2026-08-04:**
- guest kernel 6.12.91 against host 7.0.12-101.fc43.x86_64
- `/tmp` reports `fuse`, so the disk-backed mount is in effect

### uid mapping is broken under krun

The 2026-08-04 entry also claimed `keep-id` mapped ownership correctly,
on the strength of `--check` reporting uid 1000 on `/workspace`. That
was wrong. `--check` never compared that number against the guest's own
uid, so it read a mismatch as a pass.

The first real session in vm mode failed at startup:

```
Temp directory /tmp/claude-0 is owned by uid 1000, expected 0.
```

Measured 2026-08-06:

| | `id -u` | file it creates | consistent |
| --- | --- | --- | --- |
| container | 1000 | 1000 | yes |
| vm | 0 | 1000 | no |

`/run` is also not writable by that nominal root. The guest runs its
entrypoint as uid 0, and virtiofsd — unprivileged, running as the
invoking user — can only present files as uid 1000. So a process writes
a file as root and reads it back owned by someone else. Claude Code
creates `/tmp/claude-0` and then refuses to trust it, correctly.

Nothing is wrong with `/workspace` ownership on the host. The break is
that the guest disagrees with itself, which any tool checking who owns
its own files will refuse to run under.

Dropping `--userns=keep-id` was the first guess. It is wrong, and
measurably worse:

| vm mode | writes | ownership |
| --- | --- | --- |
| `keep-id` | succeed everywhere | process uid 0, files uid 1000 |
| `none` | `/workspace` not writable at all | n/a |

Guest root has no authority over virtiofs, so under `none` only
world-writable paths — the image's sticky `/tmp` — accept a write. The
mapping was never the problem.

### krun ignores parts of the podman spec

Three things the runtime silently drops, all found on 2026-08-06:

- **`USER` in the image.** The entrypoint runs as uid 0 despite
  `USER appuser`. This is the cause of the uid split.
- **`--user`.** Passing `--user 1000:1000` changes nothing; the process
  is still uid 0. So the uid cannot be set from outside either.
- **`--tmpfs`.** `SANDBOX_TMP=tmpfs` passes `--tmpfs /tmp` and `/tmp`
  still reports `fuse`. The same applies to `--tmpfs /run`, which is why
  `/run` is not writable in vm mode.

The second one retires the `/tmp` reasoning above for vm mode: every
path is virtiofs whatever podman is asked for, so the guest cannot OOM
on a `/tmp` write and the bind mount is not buying anything the runtime
would not have done anyway. It stays because container mode honours
`--tmpfs` and still needs it.

Anything else in the podman spec that matters should be assumed dropped
until `--check` proves otherwise.

### No `--userns` mapping reconciles it

Three tried on 2026-08-06:

| `--userns` | writes | ownership |
| --- | --- | --- |
| `keep-id` | succeed everywhere | process 0, files 1000 |
| `none` | `/workspace` not writable | — |
| `keep-id:uid=0,gid=0` | `/workspace` not writable | — |

Inference, not measurement: libkrun's virtiofs does its own uid
translation that podman's `--userns` does not drive, so each mapping
lands on a different broken combination rather than composing. Under
plain `keep-id` the guest's uid 0 is root within a userns that has host
1000 mapped, which is why writes work there and nowhere else.

Two smaller things were fixed along the way and do now pass: `HOME` is
set explicitly to `/home/appuser` in vm mode, since the runtime
otherwise hands a root process `/root` — nowhere near the credential
mounts — and `--check` verifies every mount exists and is readable.

**Phase 5 is blocked.** The default is back to plain `keep-id`, the
least broken of the three, and vm mode warns before starting an agent.
It is not a working sandbox: anything comparing its own uid against a
file it owns refuses to run. Claude Code's temp directory check is one.
git's dubious-ownership check on `/workspace` is the next one waiting,
and that one matters more, since reviewing the agent's commits is a
control this plan depends on.

### Where that leaves the plan

Four podman guarantees do not hold under krun — `USER`, `--user`,
`--tmpfs`, and `--userns` composing with the guest's file ownership.
That is a pattern, not a run of bad luck, and the next thing to break is
unlikely to announce itself as clearly as an ownership check did.

### gVisor works — verified 2026-08-06

`SANDBOX_ISOLATION=gvisor` passes every check on the host:

```
isolation                    gvisor
kernel                       OK  guest 4.19.0-gvisor, host 7.0.12-101.fc43.x86_64
process uid                  1000 (appuser)
home                         /home/appuser
mount /home/appuser/.claude  OK  readable
workspace write              OK  uid 1000 gid 1000
tmp write                    OK  uid 1000 gid 1000
tmp backing                  tmpfs
```

Every krun failure is absent. `USER` is honoured, so the process is uid
1000 and agrees with the files it writes. `--tmpfs` is honoured, so
`/tmp` is a real tmpfs. The kernel differs from the host, so the runtime
genuinely engaged rather than silently falling back.

That is the difference that mattered: gVisor implements the OCI spec
where krun ignores parts of it.

It also keeps the property the two bigger options below both cost — one
flag, same image, same eight entry points, same `--check`.

Three accommodations were needed, all in `sandbox-run.sh`:
`--security-opt label=disable`, `--ignore-cgroups` via a generated
wrapper under `$ROOT`, and a hand-installed `runsc` since Fedora does
not package it.

`--check` had a blind spot here: it read `/sys/class/net`, which gVisor
does not populate, and reported `network devices none` on a sandbox with
a working route. It now reads `/proc/net/dev` with sysfs as fallback.
Same class of mistake as the 2026-08-04 ownership claim — a check that
looked in one place and reported absence as fact.

What it does not give: a hardware boundary. The Sentry is a userspace
kernel, and escaping it needs a Sentry bug plus defeating its seccomp
filter — much harder than a kernel LPE, weaker than KVM. And it does
nothing for egress enforcement: same rootless pasta, same problem.

**It also costs SELinux.** runsc refuses any OCI spec carrying an
SELinux label:

```
FetchSpec failed: reading spec: SELinux is not supported:
system_u:system_r:container_t:s0:c181,c430
```

podman sets that label by default, so gvisor mode has to pass
`--security-opt label=disable`. Not a preference — the mode does not
start otherwise. Type enforcement therefore no longer confines the
sandbox process on the host; gVisor's own seccomp filter on the Sentry
remains. So the comparison against container mode is not a clean
upgrade: it trades a host-side MAC layer for a much smaller guest-side
kernel surface. Volume relabelling is left alone, so host labels stay
as container mode leaves them.

Cost is speed on syscall-heavy work, which means builds. gVisor's own
overlay (`--overlay2`) recovers much of it, and is separately
interesting because it is the copy-on-write behaviour the injected
filesystem wants.

### The two bigger options

Two ways forward, and they are not equal:

1. **Drop the VM for now and finish egress.** The threat model already
   says prompt injection is likelier than hostile code execution, and
   that a VM does nothing for it. Phase 3 is unblocked, needs no new
   code, and costs a week of wall clock that has not started. Container
   mode passes every check.
2. **Persistent VM over ssh**, the fallback named on 2026-08-02. The uid
   question becomes ordinary Unix instead of a runtime's mapping policy,
   and it restores the real network device that phase 4's enforcement
   was originally designed around. Cost is writing lifecycle management
   and losing the one-flag property that made phase 5 attractive.

Doing 1 does not prevent 2. Doing 2 first spends the week that 1 needs
anyway.

`--check` now prints the process uid and compares it against a file it
creates, in both `/workspace` and `/tmp`. That is the reason this
section can be trusted next time; the 2026-08-04 entry could not be,
because the check read a file's uid and never asked whether the process
agreed.
- build throughput over virtiofs, which is the reason to soak before
  phase 7. A `cargo build` on a cold target directory is the honest
  measure, since it is metadata-heavy and `/tmp` is no longer RAM
- a large write to `/tmp` in vm mode does not OOM the guest
- `$ROOT/tmp` has no leftover directories after a run, including a
  failed one
- `dmesg | head` shows a fresh boot
- no host `/proc`, no host `$HOME` beyond mounted config dirs
- phase 1 and 4 tests still pass under `vm`

`SANDBOX_TMP=tmpfs` now forces the tmpfs branch. Without it that branch
was unreachable once `ISOLATION=vm`, so a `/tmp` problem caused by the
bind mount could not be told apart from one caused by the VM. That is
how the uid failure first looked like a `/tmp` problem. Under vm it is
diagnostic only, since krun ignores `--tmpfs`.

## Phase 6 — shrink credential scope

Highest-value target in the box: full `~/.claude` mounted rw, and a
live API key passed as an env var, into agents running with permissions
disabled.

- Read-only config mounts wherever the agent tolerates it
- Separate revocable API keys per agent, with spend caps
- Mounted key files rather than env vars where supported

`deepseek-claude-code.sh` already splits its config dir so a
third-party endpoint never touches Anthropic-authenticated state. Same
instinct, applied wider.

## Phase 7 — flip the default

`SANDBOX_ISOLATION` defaults to `vm`. Container path stays as rollback
until the VM path has soaked.

Not done. Flipping the default to a path that has never been run would
be worse than leaving it. Do it after phase 5 verifies on the host.

## Enforcement caveat

The proxy is only half of phase 4. The other half is making it the
*only* way out, and that half is weak under rootless pasta.

What holds today in container mode:

* `--dns=none` means the sandbox has no resolver, so anything that
  needs a hostname must use the proxy. DNS tunnelling is closed.
* `HTTPS_PROXY` covers every well-behaved tool.

What does not hold: a process that opens a raw socket to a literal IP
still gets out. Env vars are advisory, and rootless pasta gives no
convenient place to hang a default-deny rule.

Phase 5 was meant to fix this by giving the guest a real network device
with a host-side tap that nftables could filter.

**Ruled out on 2026-08-04.** A `--check` run in vm mode reported:

```
kernel            OK  guest 6.12.91, host 7.0.12-101.fc43.x86_64
network devices   dummy0 lo
default route     none
```

`dummy0` and no route is the signature of transparent socket
impersonation. libkrun intercepts the guest's socket calls and reissues
them from the krun process on the host, so the guest never has a real
network stack. There is no tap interface, and nothing for an
interface-based nftables rule to match. The plan was wrong.

### What replaces it

Filter at the cgroup rather than at an interface. A systemd scope with
`IPAddressDeny=any` plus `IPAddressAllow=<proxy>` applies a BPF filter
to socket operations for every process in the scope — which includes
the host-side krun process doing the impersonation. It does not care
how the guest's networking is arranged, so it survives the TSI question
entirely and would still work if libkrun switched to virtio-net.

`SANDBOX_CONFINE=1` wraps the run this way. It requires
`SANDBOX_PROXY`, since denying every address without a proxy to reach
leaves no network at all.

**Answered 2026-08-06, and the answer is no.** `IPAddressDeny` has no
effect on a rootless `systemd-run --user --scope` on this host. With
`SANDBOX_CONFINE=1` and the proxy environment stripped inside the
sandbox, `--check` reached `https://1.1.1.1/` and got HTTP 301:

```
raw egress (confined)  FAIL  reached 1.1.1.1 directly, http 301
                             — IPAddressDeny is NOT holding
```

The properties are accepted without error and silently do nothing. That
is the worst failure shape available: it looks confined and is not.
Likely cause is that attaching a cgroup BPF filter needs privileges the
user manager does not have, so the setting is parsed and dropped.

`--check` now runs this probe automatically whenever `SANDBOX_CONFINE=1`,
and the runner warns on every confined run until `--check` says
otherwise. The probe must use a literal IP: with `--dns=none`, a hostname
fails to resolve before any connect is attempted, which measures DNS and
reads as a pass. An earlier hand-run test made exactly that mistake and
looked like success.

### So egress control is advisory today

Both modes. The proxy stops an agent that follows instructions. It does
not stop code that opens a socket to a literal IP — a dependency's
install script, a downloaded binary, anything in threat 3. That is worth
stating plainly because the rest of this document could otherwise read as
though phase 4 were done.

Three ways forward:

1. **nftables rule matching the scope's cgroup.** Needs `sudo` once, then
   persists. Requires a stable cgroup path, so the scope needs a fixed
   name via `systemd-run --unit=`, not the random `run-r<hex>.scope` it
   currently gets.
2. **A system scope instead of a user scope.** `sudo systemd-run --scope
   --uid=$(id -u) -p IPAddressDeny=any -- podman run …`. The system
   manager has the privileges the user manager lacks. Costs a `sudo` per
   run, or a sudoers rule, and needs `XDG_RUNTIME_DIR` and `HOME`
   preserved for rootless podman.
3. **Accept it.** Keep the proxy for observability and for well-behaved
   tools, and rely on the gVisor boundary for hostile code. Given the
   threat model puts prompt injection first — where the agent is
   following instructions and the proxy does bind — this is defensible,
   but it should be a decision, not a default.

Before building any of them, confirm the diagnosis with a two-line test
that involves no sandbox at all:

```sh
systemd-run --user --scope -q -p IPAddressDeny=any -- \
  curl -sS --max-time 5 -o /dev/null -w '%{http_code}\n' https://1.1.1.1/
sudo systemd-run --scope -q -p IPAddressDeny=any -- \
  curl -sS --max-time 5 -o /dev/null -w '%{http_code}\n' https://1.1.1.1/
```

If the first returns 301 and the second fails to connect, the privilege
theory holds and option 2 is the cheap fix.

**Test it, do not assume it.** With the proxy in enforce mode and
`SANDBOX_CONFINE=1`, from inside the sandbox:

```sh
curl -sS --max-time 10 https://example.com/          # must fail
curl -sS --max-time 10 https://api.anthropic.com/    # must reach the proxy
```

then repeat with `HTTPS_PROXY` unset inside the sandbox. That is the
real test: it proves the deny survives a process that ignores the proxy
environment, which is the whole point of confinement.

Until confinement is verified, treat egress control in either mode as
protection against an agent that follows instructions, not against code
actively trying to get out.

## Getting a shell, and checking a sandbox

Only the `llm` entry point runs a shell, and since phase 6 it is a
different configuration from the agents — different mounts, no
credentials. Debugging it tells you nothing about the sandbox `claude`
actually runs in. So every agent takes two overrides that change the
command and nothing else:

```sh
,claude-sandbox.sh --shell               # interactive shell, claude's exact config
,claude-sandbox.sh --shell -c 'ip route' # one command
SANDBOX_ISOLATION=vm ,claude-sandbox.sh --check
```

`--check` runs inside the sandbox and reports: guest kernel versus
host kernel, network devices and default route, whether a resolver is
present, proxy environment, `/workspace` writability with the uid and
gid to compare against the host, and what `/tmp` is backed by. With
`SANDBOX_PROXY` set it also tries one allowlisted and one denied host.
It exits non-zero on failure, so it can gate a change.

The kernel comparison is the one that matters most: if the guest
kernel equals the host kernel, `--runtime krun` silently did nothing
and you are in a container. That is the failure most likely to look
like success.

## Operating it

Log-only, to learn the real allowlist:

```sh
,egress-proxy.py --mode log --listen 127.0.0.1:8080 &
SANDBOX_PROXY=127.0.0.1:8080 ,claude-sandbox.sh
```

After a week:

```sh
# Review every line before enforcing.
,egress-proxy.py --summarize > ~/.config/llm-sandbox/egress-allowlist.txt
,egress-proxy.py --mode enforce &   # reads that path by default
```

The proxy address must be reachable from inside the sandbox, and
`127.0.0.1` inside the sandbox is the sandbox, not the host. Host
loopback is unreachable by design — that is exactly what the phase 1
regression test proves when `curl localhost:8000` fails.

Resolved for container mode 2026-08-06. A loopback `SANDBOX_PROXY` adds
pasta's `-T` port forward, so `127.0.0.1:<port>` inside reaches host
loopback on the same port and nothing else does. Verified end to end —
the proxy log shows real CONNECT tunnels to `api.anthropic.com` and
`example.com` from inside the sandbox.

**Not resolved for gvisor, and it cannot be.** Measured, probing the
proxy from inside each mode:

| mode | target | result |
| --- | --- | --- |
| container | `http://127.0.0.1:9090` | 405 — reached it |
| gvisor | `http://127.0.0.1:9090` | 000 |
| gvisor | `http://192.168.86.1:9090` | 000 |

gVisor runs its own network stack. `127.0.0.1` inside the sandbox is
gVisor's loopback, not the namespace's, and pasta's `-T` listener sits in
the kernel netns that gVisor never touches. Everything the sandbox sends
goes out the tap to pasta, which then opens the connection from the host.
So only an address the sandbox can *route* to is reachable, and the
runner now warns when a loopback proxy is combined with gvisor.

### Reaching the proxy under gvisor

Three options, in order of how much they widen the sandbox's reach:

1. **A dummy interface on the host.** Bind the proxy to an address the
   host owns that is not reachable from the LAN. NetworkManager can hold
   it, so it survives reboots without a unit file — which matters on an
   image-based OS:

   ```sh
   sudo nmcli connection add type dummy ifname sbxproxy con-name sbxproxy \
        ipv4.method manual ipv4.addresses 10.99.0.1/32 \
        ipv6.method disabled connection.autoconnect yes
   sudo nmcli connection up sbxproxy
   ip -4 addr show sbxproxy          # expect 10.99.0.1/32

   ,egress-proxy.py --mode log --listen 10.99.0.1:9090
   SANDBOX_PROXY=10.99.0.1:9090
   ```

   The sandbox has no route for `10.99.0.1`, so it goes out the default
   route to pasta; pasta opens the connection from the host, where the
   address is local. Exactly one address and port become reachable — as
   tight as the loopback arrangement was meant to be — and it composes
   with `SANDBOX_CONFINE`, which then allows only that address.

   Pick a range the LAN does not use. Here the LAN is 192.168.86.0/24,
   so 10.99.0.0/24 is free.

   **Verified 2026-08-06.** With the dummy interface up, gvisor passes
   every check including `proxy reachable`, and the proxy log records
   real CONNECT tunnels from inside the sandbox. Both isolation modes now
   have a working egress path.

   Note when reading `--check` output: `egress allowed host OK http 405`
   is Anthropic answering a GET on a POST-only endpoint, not the proxy's
   own 405 for a non-CONNECT request. The two are indistinguishable by
   code alone; the proxy log disambiguates.

2. **Bind to the host's LAN address.** Was listed as a working-but-
   exposed option. It is probably not working at all: pasta copies the
   host's address into the namespace, so the host's own LAN address is
   local from inside and never routes out — the same failure as
   loopback, and consistent with the gateway probe returning 000.
   Rejected on both counts.

3. **pasta `--map-gw`.** No setup, but it maps the gateway to host
   loopback wholesale, so every service on host loopback becomes
   reachable at the gateway address. That undoes what the phase 1
   `--network=host` fix was for. Rejected.

Option 1 is the recommendation. Options 2 and 3 both widen egress, which
is the opposite of this phase's purpose.

`SANDBOX_PASTA_FORWARD=0` suppresses the forward; a routable proxy gets
no forward, since it needs none.

Changing the port means changing it in both places, and nowhere else:

```sh
,egress-proxy.py --mode log --listen 127.0.0.1:9090
SANDBOX_PROXY=127.0.0.1:9090 ,claude-sandbox.sh
```

`--ports` on the proxy is a different knob: it limits which *destination*
ports the sandbox may reach, default 443.

## What is still open

* Package pinning for the codex, claude and aider installers. Their
  version arguments could not be verified from inside the sandbox, so
  they remain unpinned. This is the last build-time trust of a third
  party.
* `:Z` was tried as the default and reverted on 2026-08-04. It gives a
  mount a private SELinux label, which revokes access for anything else
  using the same directory — running containers sharing a path lost
  their files until the labels were restored. The relabel is persistent
  on disk, so `restorecon -RFv <dir>` is the repair. Default is `z`
  again; `SANDBOX_LABEL_MODE=Z` opts in where nothing else shares the
  paths.
* Credentials in the environment. `ANTHROPIC_AUTH_TOKEN` is visible in
  `/proc/*/environ` to every process in the sandbox, including anything
  a dependency's install script runs. Claude Code's `apiKeyHelper`
  setting looks like the fix, but it was not verified and guessing at
  it would break a working path.
* Per-agent revocable keys with spend caps. Account-side work, not
  code.
* Whether all eight entry points are still wanted. Nothing was deleted
  because that question went unanswered.
