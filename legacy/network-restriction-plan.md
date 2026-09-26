# Enforced networking for concurrent gVisor sandboxes

Draft for review, 2026-09-12. No networking implementation has changed.

## Current direction: QEMU feasibility prototype

The host boundary test demonstrated an actual relay bypass through a socket
introduced into the shared workspace after launch (receiver-confirmed token).
Unrestricted gVisor host-uds=open with shared mounts is rejected. The user
approved evaluating QEMU before implementing the privileged namespace broker.
The gVisor sequence below is retained as investigation history and fallback
material, not the current implementation order.

Start with [the offline QEMU/virtiofs prototype](prototypes/qemu/README.md):
a private snapshot guest disk, temporary writable export, explicit UID/GID
mapping, and a live host-socket negative test. Each VM will have its own disk
and filesystem daemon. Shared workspaces retain file-based communication;
separate worktrees are preferred. Credential exports are deferred.

Networking stays disabled in this first prototype. Proxy-only networking,
concurrent-VM isolation, lifecycle, and guest-root tests remain acceptance
gates. No claim is made yet that the final QEMU stack needs no privileged
setup. Local tools cannot boot QEMU here; host results are required.

## Goal and trust boundary

Treat guest code, including guest root, as untrusted. Allow external access
only through an enforcing CONNECT proxy. Block direct internet, host, LAN,
metadata, DNS, and cross-sandbox connections. Each invocation must have an
independent boundary, including two launches of the same agent and project.

The host user and installed runtime are trusted. The host user belongs to
wheel: this design protects guest-to-network access, not a compromised host
user. SELinux is enforcing, but the user's shell is unconfined_t and gVisor
mode sets `--security-opt label=disable`. Do not count SELinux as part of this
sandbox boundary. Keep it enforcing on the host without claiming protection
that the launcher does not provide.

Shared files and communication through allowed external services remain
separate channels. A hostname-based CONNECT proxy tunnels arbitrary bytes;
it cannot enforce API paths, accounts, TLS identity, or request content.
Shared upstream hosting and allowed APIs remain possible exfiltration paths.

Unix sockets in mounted trees are a third such channel on the UDS transport,
and unlike the two above it is not a property of proxying — it is a hole in
the guarantee this plan exists to make. Measured 2026-09-12: `--host-uds=open`
is sandbox-wide, not per-mount, so a host socket anywhere the guest can see is
reachable, and without the flag none is. The transport needs the flag.

That means the guest's only exit is *not* the proxy: any host service
listening on a socket in the workspace is reachable with no policy in front
of it. Do not write this down as a caveat and move on. Either bound the
exposure, or choose the namespace fallback, where `--host-uds` stays `none`
and the channel does not exist. Until that is settled the restricted mode is
not safe to enable, however well the transport works.

## Evidence and current gaps

Host: Fedora 44 Sway Atomic 44.20260905.0, kernel 7.1.13-200.fc44,
systemd 259.8, Podman 5.8.4, crun/crun-krun 1.28, libkrun 1.19.0,
netavark 1.17.2, nftables 1.1.6, firewalld 2.4.4, and cgroup v2.
runsc is `release-20260803.0` at /usr/local/bin/runsc, and it accepts
`--host-uds=open` (measured 2026-09-12).

Both inspected sessions use runsc-wrapper, resolving to
`/var/usrlocal/bin/runsc`, with pasta networking. SANDBOX_ISOLATION is gvisor.
Each runsc process has a separate network namespace; both pasta processes
use the host namespace. Podman/pasta and conmon/runtime descendants occupy
separate systemd scopes. Filtering one existing scope would miss processes.

The host has IPv4 and IPv6 default routes. Its supplied firewall rules permit
ordinary host-originated egress. Firewalld is active. The dummy interface
sbxproxy holds 10.99.0.1, with a Python listener on port 9090. Both sbxproxy
and Wi-Fi are in public. Locally generated connections to that local address
can take loopback's INPUT allowance; a new veth connection cannot rely on it.
The listener's identity and enforcing policy remain unverified.

The Podman API socket `/run/user/1000/podman/podman.sock` exists. Treat it as
an alternate launch broker to eliminate from restricted launches and mounts.
The repository's proxy environment and --dns=none remain advisory, and the
old SANDBOX_CONFINE option correctly refuses to launch. Historical krun and
scope-filtering claims in vm-migration-plan.md are not current guarantees.

Python 3.14 already classifies mapped loopback/private addresses correctly;
they are not a demonstrated hole in resolve_public. The actual hole is CGNAT:
100.64.0.1 has is_private=False and passes the existing exclusions. Target
this explicit address policy, while retaining checked-address connection:

```python
if not ip.is_global or ip.is_multicast:
    reject()
if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("64:ff9b::/96"):
    reject()
```

Construct the NAT64 network constant once in implementation. is_global alone
also returns True for multicast, so preserve its explicit rejection. Tests
must cover CGNAT, 198.18.0.0/15, 192.0.0.170, mapped private and loopback,
NAT64-encoded private destinations, and existing exclusions. Public controls
include 8.8.8.8, 2606:4700::1111, and ::ffff:8.8.8.8. Deployment-specific
translation prefixes require review too; the named prefix is not all NAT64.

## Ordered implementation plan

0. **Prove the host assumptions before writing anything else.** Delivered:
   `tests/host_assumptions.py`. Deliberately not named `test_*.py`, so sandbox
   discovery skips it; a run that cannot happen must never look like a pass.
   It runs on the host, reports PASS/FAIL/SKIP/NOTE per assumption, and writes
   `--json` evidence for step 6. Tier A needs no privileges and decides step 2:
   Podman is local, rootless and unbrokered; runsc accepts `--host-uds=open`;
   `--dns=none` survives `--network=none`; `--network=none` leaves only `lo`
   with no default route; keep-id maps the socket owner onto the guest user;
   the gofer opens the mounted socket; the same mount fails without the flag;
   host, LAN, internet and DNS stay unreachable against verified positive
   controls; two runs stay separate; a socket-file mount works or a per-run
   directory is required; per-run paths fit sun_path; and pasta accepts the
   step 3 options, recording whether Podman still injects `--dns-forward`.
   Tier B (`--sudo-probes`) decides step 3: a free /30 after route-overlap
   checking, the zone a new veth lands in, firewalld blocking it, an own-table
   accept failing to override that, a runtime rich rule opening it, an
   own-table drop at `filter - 20` pre-empting firewalld, rootless Podman and
   pasta staying inside a root-created namespace, and, with
   `--reload-firewall`, the table surviving a reload.

   The probe is not read-only, so it is built to fail safe. It refuses to
   start as root, or with CONTAINER_HOST, DOCKER_HOST or CONTAINER_CONNECTION
   set, or against a non-rootless or remote Podman — before creating anything,
   because the real hazard is cleanup aiming `podman rm -f` or `nft delete
   table` at the wrong machine. Privileged probes refuse names already in use
   and register an undo only after the resource exists. Cleanup runs newest
   first, names each step, reports what it could not remove, and fails the run
   for it: a leaked namespace or nftables table outlives the process and no
   firewall reload removes either. A broken probe is ERROR, never SKIP, and a
   skip is reported as an unproven assumption rather than folded into a
   passing exit. `--keep` keeps everything and prints how to remove it.

   Scope of what it touches: one temporary directory under
   `$XDG_RUNTIME_DIR`; containers labelled `sbxprobe=<runid>` and removed by
   that label; receivers bound to named addresses, never a wildcard, and
   closed by the probe that opened them; firewalld changes runtime-only, never
   `--permanent`; `~/.config/llm-sandbox` never read or written, so the shared
   runsc wrapper the live sessions use is left alone; no image pull, no
   credentials, sbxproxy untouched. `--offline` drops the one external
   connect. `--reload-firewall` is the exception to all of this: it discards
   unrelated runtime-only rules across the whole host.

   Exit: tier A clean, as an ordinary user, before step 2 starts. Tier B is a
   separate decision — it changes host namespaces, interfaces, routes and
   firewall rules, so it gets its own review before anyone runs it, and must
   be clean before step 3. A failure here revises the plan; it does not get
   worked around in the prototype.

1. **Local policy and transport tests.** Deliver address-policy tests and
   CONNECT parsing, malformed-request, bounded-header, and connection-limit
   tests in tests/test_proxy.py. Add ThreadingUnixStreamServer support and a
   --listen-uds PATH option to egress-proxy.py, mutually exclusive with TCP
   listen selection. Replace getpeername()[0] for AF_UNIX: an unnamed peer
   returns an empty address, which currently raises IndexError. Include a
   launcher-assigned run identifier in every log event, independent of peer
   addresses or client-supplied headers. Handle stale sockets before bind;
   allow_reuse_address does not prevent AF_UNIX EADDRINUSE. Under exclusive
   ownership of a private per-run directory, unlink only a verified stale
   socket. Refuse live endpoints, symlinks, and non-socket paths; a failed
   connect alone is not sufficient proof of staleness. Test empty peer names,
   stale/live paths, log attribution, and cleanup ownership.
   Develop a small TCP-to-UDS relay with duplex
   streaming, disconnect handling, and bounded resources; Python is already
   installed, so the shim needs no image change. Exercise the relay against a local fake upstream with dummy
   credentials. Build a disposable two-namespace veth/proxy reachability smoke
   test locally for the fallback. Exit: controlled positive and negative
   results with receivers confirming traffic, not HTTP 000 as proof of denial.
   Local netstack tests do not certify host firewall or runsc gofer behavior.

2. **Test UDS before building a privileged broker.** Deliver a credential-free
   host prototype using Podman --network=none and runsc --host-uds=open
   (verify support in the installed build). Each run gets a unique host UDS
   proxy endpoint, mode 0600, in a private directory outside shared project
   and credential mounts. Mount only that run's endpoint at a fixed guest
   path. With the current host UID 1000, --userns=keep-id and image appuser
   UID 1000 map the socket owner to the shim's guest UID: mode 0600 should
   work without widening permissions. Verify gofer access under that mapping;
   do not treat arbitrary userns or process-user overrides as equivalent.
   A guest shim listens on 127.0.0.1:3128 and relays bytes to the UDS; the host
   proxy enforces CONNECT policy. Proxy variables point to guest loopback.
   That needs a loopback the guest can bind, and `--network=none` alone does
   not provide one: measured 2026-09-12, the guest gets no interfaces at all
   and binding 127.0.0.1 fails with EADDRNOTAVAIL, with or without runsc
   `--network=sandbox`. Under crun the same flag yields a working `lo`, so the
   namespace is fine and runsc is the difference: crun brings loopback up as
   part of container setup, while runsc builds a netstack from what the
   namespace already has and skips an interface that is down.

   So hand runsc a namespace whose loopback is already up. Measured working
   2026-09-12: a throwaway crun container per run, held open with
   `--network=none`, joined by the sandbox with `--network=container:<holder>`.
   The guest then reports `interfaces=[lo]`, no routes, and a working
   127.0.0.1. Podman owns both ends, no privileges are needed, and a missing
   holder is an error rather than a silent downgrade.

   The holder mounts nothing, runs only `sleep`, and carries no workspace or
   credentials — it exists to own a namespace. Note what it is not: it runs
   under crun, so it is not itself a gVisor boundary, and nothing
   attacker-controlled may ever run in it.

   One holder per run, never shared. Measured 2026-09-12, guest loopback does
   not cross a shared holder anyway — gVisor builds a netstack per sandbox, so
   a second sandbox on the same namespace cannot reach the first one's
   listeners — but keep the rule: step 3's fallback puts a real interface in
   that namespace, where sharing would matter, and an allocation rule that is
   only sometimes load-bearing is one a later change pools away for speed.

   Two rejected alternatives, both measured. `--network=ns:` onto an
   `unshare -rn` namespace: podman refuses the /proc path even with
   PR_SET_DUMPABLE set in the holder and the path readable by the caller,
   because rootless podman re-execs inside its own user namespace and has no
   access to one owned by another. A bind-mounted netns file would fix it and
   needs root, which defeats the point. `--network=host` inside an unshared
   namespace: fails open, and podman inside `unshare -r` believes it is root
   and switches to rootful storage where it has neither the image nor
   permissions.
   No external guest interface, pasta, veth, firewall change, or root helper
   is needed if this works. Host proxy code and active policy must be outside
   guest-writable mounts. Prefer one unprivileged proxy process per run.

   Exit: the actual gofer opens the mounted socket; allowed HTTPS/WebSocket
   tunnels work; direct IPv4/IPv6 and cross-run traffic fail; stopping either
   of two runs leaves the other working and restricted. Check loopback-only
   guest networking and absence of networking helpers. Missing UDS, dead
   proxy, or unsupported flags must fail closed. --host-uds=open permits
   access to other reachable host sockets, not just this socket: audit all
   mounted trees, including sockets introduced after launch into shared
   workspaces. Test unintended host sockets and cross-run socket access.
   Mode 0600 alone does not separate sandboxes mapped to the same host UID.
   If mount scope cannot contain UDS access for the promised isolation, this
   path fails the gate even when the relay works. Do not enable create/all.

3. **Only if UDS fails, prototype outer namespaces.** Deliver a disposable
   host test with one outer network namespace per run, containing Podman,
   runsc, and every pasta/helper process that opens sockets for the guest.
   Prove actual placement, including processes created through systemd.
   Before Podman starts, bring up and address the outer veth, and install a
   default route so pasta has an explicit usable template interface. Give
   the host proxy a distinct reachable address that is not the gateway.
   Pass Podman's comma-separated pasta options explicitly:
   `--network=pasta:--map-host-loopback,none,--map-guest-addr,none,--ipv4-only`.
   Not `--no-dhcp-dns`: measured 2026-09-12, this build answers "--no-dhcp-dns
   is for passt mode only" and refuses to start. Pasta mode configures the
   namespace directly instead of serving DHCP, so the resolver comes from
   Podman and `--dns-forward`, not from a DHCP option. `--map-guest-addr` is
   in that list because the live argv carried one: closing the gateway path
   with `--map-host-loopback none` leaves a second address mapped to the host.
   Read the real argv rather than the requested options — Podman's injected
   `--dns-forward 169.254.1.1` survives everything asked of it, so step 3 has
   to blackhole it. Podman already passes `-t none -u none -T none -U none`,
   so automatic port forwarding is off by default; confirm it stays off. Suppress Podman's injected --dns-forward option and resolver
   announcements; inspect the actual pasta argv to prove it. Do not rely on
   an unreachable copied 127.0.0.53 to block resolution. Pasta-originated DNS
   sockets must also stay inside the filtered outer namespace. Do not rely
   on default gateway/loopback mapping behavior.

   Use a separate inet nftables table with scoped INPUT and FORWARD base
   chains at priority `filter - 10`, before firewalld's `filter + 10` chains.
   Permit only the exact proxy address and TCP port from each allocated link;
   drop other host access, all cross-link forwarding, and direct egress for
   both families. Match ingress interface, not just a spoofable source IP.
   Keep explicit forward drops independent of ip_forward: unrelated bridge
   network setup can enable forwarding after launch. IPv4-only pasta is not
   a substitute for IPv6 firewall coverage.

   Bind each host veth to a dedicated firewalld zone with target DROP and
   only the proxy TCP port open; the earlier nftables policy narrows that
   port allowance to the exact destination. Do not leave veths in public.
   Verify binding and restriction survive firewall-cmd --reload. Never flush
   the host ruleset or edit firewalld's generated table. Exit: two concurrent
   sandboxes pass the acceptance matrix, including reload and forwarding
   enabled on the host, before building the long-lived broker. If this path
   cannot reliably contain helpers, evaluate a conventional filtered VM.

4. **Implement lifecycle for the successful transport.** For UDS, deliver
   per-run socket allocation, proxy supervision, namespace holder, guest relay
   startup, and cleanup. Keep directories private, paths unique and short
   enough for UDS, and wrapper files per-run or immutable to prevent
   concurrent rewrites. The holder container is part of the run: start it
   before the sandbox, remove it after, and never recycle one between runs.
   A sandbox must refuse to start if its holder is absent. Do not assume that
   losing one mid-session fails closed: gVisor builds its netstack at startup,
   so the sandbox may carry on with a loopback whose namespace is gone. A18
   measures it. Until then treat a dead holder as unknown rather than safe —
   and note which answer would matter. A session surviving its holder is
   untidy; a session *gaining* network access when the holder dies is the
   outcome that would sink this design.
   A guest launch wrapper starts the shim, verifies readiness, and then
   starts the agent, shell, or check; it owns relay shutdown on exit. Killing
   the shim removes that run's normal proxy path and restores no direct
   network access. Guest code can still use its authorized UDS directly;
   the enforcing host proxy, not the shim, is the policy boundary.
   A proxy restart must not leave a stale bind-mounted socket pretending to
   work; either coordinate restart or terminate that run closed. Close active
   tunnels on teardown. Cleanup must affect only the owning run.

   Only the namespace fallback needs host/sandbox-network.py and a root-owned
   service. Install code under /usr/local/libexec/llm-sandbox, policy under
   /etc/llm-sandbox, and transient state under /run/llm-sandbox-network.
   Authenticate callers by host UID; accept structured operations, never
   arbitrary privileged shell commands. Drop privileges/groups and close
   inherited descriptors before executing user code. Allocate unique subnets
   and interface names under a lock after checking route/VPN overlap. Keep
   rules until workloads/helpers stop, disconnect links before rule removal,
   and do not recycle allocations with stale connections. Proxy instances
   may be shared only if per-run access and resource limits remain isolated.
   Exit: crashes, concurrent allocation, restart, and cleanup cannot widen
   access or disrupt another run. For the fallback also test service restart,
   reboot, and firewall reload; missing policy must leave links disconnected.

5. **Integrate launch, policy, and checks.** Deliver sandbox-run.sh and
   install.sh changes for the chosen path. Assert CONTAINER_HOST and
   DOCKER_HOST are absent, reject remote options, use local Podman explicitly,
   and require podman info serviceIsRemote=false. Do not mount Podman/Docker
   API sockets or contact a socket-activated host broker to launch workloads.
   Pin or validate --userns=keep-id and the effective guest user for UDS
   access. Reject incompatible SANDBOX_USERNS and SANDBOX_USER overrides
   rather than passing them through or widening socket permissions. The
   restricted path owns the runsc flag string too: SANDBOX_RUNSC_FLAGS
   replaces it wholesale rather than merging, and an empty value skips the
   wrapper entirely, so `--host-uds=open` would vanish silently. Reject both
   overrides with a clear message instead of failing inside the gofer.
   Drop --dns=none when the network mode is none: measured 2026-09-12, Podman
   5.8.4 refuses the combination outright with "conflicting options: dns and
   the network mode: none", so sandbox-run.sh:866 must gate it rather than
   add it whenever a proxy is set.
   Treat runtime flags, socket mounts, and inherited descriptors as part of
   the launch audit. Apply the same restrictions to agents, shells, and
   --check. Use --pull=never; builds and pulls happen outside sessions.
   SANDBOX_PROXY overrides and preflight bypasses cannot weaken this mode.
   For UDS, host preflight connects to the allocated host socket, never to
   guest-visible 127.0.0.1:3128 (which could be an unrelated host listener).
   Guest readiness separately checks the shim and an enforcing proxy request.
   Gate the pasta -T construction and gVisor loopback warning to pasta mode;
   neither applies to --network=none. Update their comments accordingly.

   Use exact model/login endpoints verified for each harness. Package
   registries are an explicit broader policy; discovery logs never become
   permissions automatically. Restricted mode always uses explicit enforce
   mode, not the proxy's current log-mode default. Dry runs report the whole
   launch path without creating resources. Exit: unsupported configurations
   refuse to launch, and integration checks verify the boundary rather than
   merely seeing a responding proxy. Make restricted gVisor the default only
   after host acceptance. Krun requires independent verification.

6. **Publish evidence and operating instructions.** Deliver
   tests/host_network_integration.py, README updates, and a historical marker
   on vm-migration-plan.md. Record tested host/runtime versions and transport,
   local tests, host results, and outstanding limitations. Exit: reproducible
   two-sandbox acceptance passes, with no unsupported claim of enforcement.
   Keep current host sessions and sbxproxy unchanged during the prototype.

## Acceptance matrix

Use controlled reachable receivers and positive controls. A timeout, DNS
failure, or HTTP 000 alone cannot establish confinement.

- Two runs can independently use allowed HTTPS and WebSocket APIs.
- Prove the shim relays an allowed request, then kill it: proxy-variable
  clients in that run lose egress, direct networking remains blocked, and
  the other run still works. Its loopback listener is unreachable from the
  host and other guests. Direct use of the authorized UDS remains filtered.
- With proxy variables removed, literal-IP TCP, UDP/TCP DNS, UDP 443, ICMP,
  IPv6, and direct connections to otherwise allowed API IPs cannot escape.
- Neither run reaches host services, LAN, metadata, CGNAT, another run's TCP
  or UDP development servers, or its proxy endpoint, directly or via CONNECT.
- Unlisted hosts, disallowed ports, private/mapped/NAT64 destinations, and
  changed DNS answers cannot redirect tunnels inward. Connect only to checked
  addresses without a second hostname resolution.
- Guest root cannot restore access through route, DNS, firewall, proxy, or
  mount changes. For UDS, test hostile sockets appearing in shared mounts;
  for namespaces, test address spoofing and host forwarding enabled.
- Simultaneous launches, one run stopping/crashing, and proxy failure leave
  the survivor's connectivity and restrictions intact. No stale tunnels or
  allocation reuse grant access to later runs.
- No agent runs before its boundary and proxy are ready. Dead infrastructure
  leaves it offline, never on an advisory fallback.

## Step 0 results, host runs 2026-09-12

Tier A, `runsc release-20260803.0`, Podman 5.8.4. Second run: 11 passed,
1 failed, 3 noted. The UDS transport itself is proven; its guest-side
entry point is not.

Proven, and step 2 can rely on it:

- A6/A7: the gofer opens a bind-mounted socket with `--host-uds=open`, as a
  socket *file* mount — no per-run directory needed. `--userns=keep-id` maps
  host uid 1000 to guest uid 1000, so mode 0600 works unwidened.
- A8: without the flag the same mount is refused. The transport is
  attributable to `--host-uds=open`, not to gVisor passing everything.
- A9/A10: the guest reaches nothing — host loopback, the host's own address,
  a public literal IP and DNS all fail, against host controls that succeeded
  first. Measured under plain `--network=none`; see the re-measurement note
  below.
- A11: one run cannot reach another's host socket path.

The guest-side entry point, settled over three runs:

- A5b: no guest loopback under plain `--network=none`. Binding 127.0.0.1
  fails with EADDRNOTAVAIL, with and without runsc `--network=sandbox`.
  Recorded behaviour now, not a gate: it is the reason the design carries a
  holder, and the plan no longer ships that configuration.
- A5c narrowed it: crun under the same flag gets `lo` and works. The
  namespace podman builds is fine; runsc skips a loopback that is down, and
  crun brings it up itself.
- A5f, PASS: `--network=container:<crun holder>` gives the guest
  `interfaces=[lo]`, no routes, and a working 127.0.0.1. This is the shape
  step 2 ships.
- A5d and A5e are dead ends, measured rather than assumed. `--network=ns:` on
  a /proc path stays denied even with PR_SET_DUMPABLE set and the path
  readable by the caller, because rootless podman re-execs in its own user
  namespace. `--network=host` inside `unshare -r` sends podman to rootful
  storage where it has no image, and fails open besides.

Re-measured in the holder shape: A9 and A10 hold. The guest's 127.0.0.1 is
its own — a host listener on that exact port, reachable from the host, is
refused from the guest — and the host's own address and the public internet
are ENETUNREACH.

A15, with per-run holders and a control taken from inside the listening
sandbox by `podman exec`: the listener answers `NEIGHBOUR` to its own
sandbox, and is unreachable both from a second sandbox sharing holder A and
from one with its own holder. So gVisor builds a netstack per sandbox and
guest loopback does not cross a shared namespace at all. Cross-run loopback
isolation therefore does not depend on the allocation rule under this
transport. Keep one holder per run regardless: step 3's fallback puts a real
interface in that namespace, where sharing would matter.

Two controls in this probe had to be replaced before it measured anything.
A second container on the same holder reports ECONNREFUSED whether or not
the boundary works, because it has its own netstack — as a control it would
have passed on a broken boundary. The same mistake, in the other direction,
made an earlier A9 probe 127.0.0.1 under two labels and report one connect
as two results.

**Where this actually stands, 2026-09-12**, on Podman 5.8.4 and runsc
release-20260803.0: the UDS transport is verified, and the network isolation
gate is unresolved because other mounted host sockets are reachable.

Those are separate claims and an earlier draft of this section ran them
together. Every probe passing is not the same as step 2's gate passing.
A12 is a policy bypass, not an operational note: the proxy endpoint is
filtered, but any other host socket in a mounted tree is a different service
with no policy at all, and A11 only shows that an *unmounted* path is absent.
Mode 0600 does not separate sandboxes that map to the same host user.

By step 2's own wording — "if mount scope cannot contain UDS access for the
promised isolation, this path fails the gate even when the relay works" —
A12 fails the gate. It is now scored FAIL rather than NOTE.

What decides it: A16 (a socket bound after launch — if reachable, a
launch-time scan is not a mitigation), A17 (whether this runsc can scope
host UDS to one path at all), A18 (what a holder's death does to a running
sandbox, testing this plan's fail-closed claim). If the exposure cannot be
bounded, the recorded isolation failure triggers the namespace fallback,
where `--host-uds` stays `none` and the channel does not exist. That is the
real cost of the simpler design, and it should be chosen deliberately.

The passing results still hold on their own terms: the gofer round trip at
mode 0600 with a working negative control, the prepared-loopback holder, and
blocked host, public-IPv4 and neighbouring-loopback paths with positive
controls. They are a subset of the acceptance matrix, not the whole of it.

Tier B is untouched and gets its own review before anyone runs it. Resolve
the UDS exposure question first — it decides whether the simple design
survives, and the privileged probes are only needed if it does not.

Decisions forced:

- A4: Podman rejects `--dns=none` with `--network=none`. Folded into step 5.
- A12: `--host-uds=open` is sandbox-wide. A host socket placed in `/workspace`
  is reachable from the guest. Folded into the trust boundary as a stated
  scope limit, not a defect to design around.
- A13: `--no-dhcp-dns` is passt-mode only and refused. The accepted set is
  `--map-host-loopback,none,--ipv4-only`, and the live argv showed Podman
  still injecting `--dns-forward 169.254.1.1` and a `--map-guest-addr`, while
  already disabling port forwarding with `-t none -u none -T none -U none`.
  All folded into step 3.

Probe changes from these runs: A5 no longer asserts on an interface listing
when the requirement is a working loopback; A6 through A12 depend on A2/A3
rather than A5, so one wrong assertion cannot hide eight answers; A13 bisects
the option set and records the full live argv.

## Verification and references

Rechecked locally: 35 tests, 3 failures (Pi, OMP, OpenCode model expectations),
1 skipped. Python 3.14.7 confirms the address behavior above. unshare -rn and
veth creation/deletion succeed locally in a disposable namespace. The full
local two-namespace proxy smoke test remains to be written and run.

The parts of `tests/host_assumptions.py` that need no Podman were exercised
here: the AF_UNIX and TCP receivers, both guest probe programs, JSON parsing,
mount-style selection, and cleanup. Two measurements came out of that. gVisor
serves `/proc/net/dev` but not `/sys/class/net`, so interface checks read
/proc; trusting sysfs would have reported "no interfaces" and passed by
accident. Today's gvisor+pasta guest reports interfaces `[lo, wlp1s0]` with a
default route via a copy of the host address — the baseline step 2 must reduce
to `[lo]` with no default route. Everything else in the file is unrun until it
executes on the host.

This session is inside gVisor: local policy, parser, relay, and netstack tests
are feasible. Host nftables filtering needs host execution; nft is absent
here. Installed runsc/gofer UDS transport and Podman launch behavior also
need host verification. Host topology comes from supplied read-only output.

- [gVisor flags](https://github.com/google/gvisor/blob/master/runsc/config/flags.go)
  defines host-uds permissions none, open, create, and all.
- [pasta manual](https://passt.top/builds/latest/web/passt.1.html) documents
  mapping and IPv4-only options; verify installed-build behavior.
- [Podman run](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
  documents network modes and pasta option syntax.

Fallback order: UDS with no guest external network -> outer network namespace
-> conventional VM. Each transition requires a recorded failure of the prior
compatibility or isolation gate, rather than speculative complexity.

## QEMU prototype progress

Host results now establish the tested virtiofs socket boundary, concurrent
restricted QEMU user networks, and tagged IPv4 UDP DNS non-delivery with
working capture controls. The actual production proxy was then killed and
restarted: A failed closed and recovered, while B retained its original
HTTPS tunnel. These are targeted results, not an exhaustive security claim.

The next deliverable is now implemented as prototypes/qemu/sandbox.py:
an interactive ordinary-user shell, explicitly selected writable workspace,
per-run enforcing proxy and disposable guest disk. Host console validation
is pending. The cloud image still needs coding-agent tooling before this
can replace the installed launcher. See prototypes/qemu/README.md for the
initial launch and the exact persistence behavior.

The host interactive shell gate passed. The staged next build installs
Claude, Codex, Pi, OMP and OpenCode into a separate disk using vendor
installers, with no project or credentials. Build-only public HTTPS is
explicitly enabled; runtime launches retain enforcing allowlists.
Per the user’s preference, there are no host agent-state exports. A dedicated
VM disk retains agent homes and credentials; only the selected workspace is
shared. Direct agent launch is implemented.
The image build and authenticated agent flows still require host validation.

## Integrated QEMU launcher acceptance

The user selected live sharing of the current ~/.config/llm-sandbox agent
directories. This supersedes the prior per-VM-only credential design.
The selectable qemu branch now consumes the existing agent/backend plan,
uses a fresh snapshot and enforcing proxy per launch, and shares only the
workspace and selected per-agent state. No project lock or shared writable
VM disk is used. Container, gVisor and legacy krun modes remain selectable.

Installer support, argument forwarding, guest exit reporting and a minimal
--check path are implemented. The host acceptance script runs two copies
of the installed Codex launcher with isolated temporary configuration and
one shared workspace. Host validation and real authenticated concurrency
remain gates before changing the default.

Local validation: five QEMU dispatch tests passed, including installed
entry points and concurrent-launch configuration. Runtime/build unit tests
and syntax checks passed. The full repository suite ran 42 tests and retains
the three known Pi/OMP/OpenCode model-expectation failures and one skip.

Host acceptance passed on 2026-09-13 with artifacts at
/tmp/qemu-launch-accept-f97ilcr3. All ten checks passed: both installed Codex
shell launchers observed shared agent state and separate guest homes, ran as
UID 1000, bound the same port independently, and wrote host-owned workspace
files. Instance B survived instance A exiting. This validates launcher
concurrency with temporary state; authenticated agent concurrency remains
untested. Next: installed --check and two real Codex sessions in the same
project, followed by the other agents and login/refresh persistence.


Launcher hardening (2026-09-13): supported runtime now lives in qemu/.
Installed launches use per-run explicit overlays on disk-backed cache storage,
configurable RAM/CPU/deadlines, initial/live terminal sizing, and no established
proxy idle timeout by default. Diagnostics are bounded and completed runs are
pruned; disposable overlays and seeds are removed on normal cleanup. Cache
policy remains never; the small host cache comparison is recorded below. The expanded
accept_launcher.py stages resize, overlay cleanup, SQLite separation, optional
long batch runs and temporary never/auto cache benchmarks. Earlier boundary
results are historical evidence; current launcher results follow.

Local validation for this hardening pass: 37 QEMU tests passed. The main suite
ran 44 tests, with the same three model-expectation subtest failures and one
skip. Shell syntax, Python compilation and diff whitespace checks passed.

Host hardening acceptance: qemu-launch-accept-9_uuj6vn passed every reported
check with cache=auto, following the never baseline in
qemu-launch-accept-h0p7126p. Runtime resize, storage separation and cleanup,
shared-file visibility and B surviving A were confirmed. Auto improved mean
repeated 500-file scans from 86.2 to 35.4 ms. Production cache policy remains
never; this fixture does not establish correctness of concurrent application
state updates. Long batch and authenticated agent acceptance remain pending.
