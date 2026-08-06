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
| 5 vm switch | **works** — guest kernel 6.12.91 confirmed 2026-08-04 |
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
- `/workspace` writes land as uid 1000 gid 1000 on the host, matching
  the guest — virtiofs plus `keep-id` maps ownership correctly
- `/tmp` reports `fuse`, so the disk-backed mount is in effect

**Still untested:**
- build throughput over virtiofs, which is the reason to soak before
  phase 7. A `cargo build` on a cold target directory is the honest
  measure, since it is metadata-heavy and `/tmp` is no longer RAM
- a large write to `/tmp` in vm mode does not OOM the guest
- `$ROOT/tmp` has no leftover directories after a run, including a
  failed one
- `dmesg | head` shows a fresh boot
- no host `/proc`, no host `$HOME` beyond mounted config dirs
- a real task's edits land in host `$PWD` with correct ownership —
  this is where virtiofs plus keep-id will bite
- phase 1 and 4 tests still pass under `vm`

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

Unverified: whether `IPAddressDeny` takes effect for a rootless
`systemd-run --user --scope`. These are cgroup BPF properties and the
user manager may need delegation. If it silently does nothing, the
fallback is an nftables rule matching the scope's cgroup path, which is
stable and known because the scope names it.

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
,egress-proxy.py --summarize > egress-allowlist.txt   # then review it
,egress-proxy.py --mode enforce --allow-file egress-allowlist.txt &
```

The proxy address must be reachable from inside the sandbox. Under
pasta the host is normally reachable, but the exact address needs
checking on the host — `127.0.0.1` inside the sandbox is the sandbox
itself, not the host.

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
