For routine image updates, use the installed `,sandbox-image` command and
the [image lifecycle](../../qemu/IMAGE_LIFECYCLE.md). It configures the source
and active version, runs the builder, boot-checks candidates and supports
rollback. The standalone commands below remain useful for development.

The supported launcher runtime has moved to `../../qemu/`. The current resource,
storage, terminal and retention settings are documented in the root README.
The older experiments below describe their original behavior; their `-snapshot`
and artifact-retention descriptions do not apply to the installed launcher.

# QEMU filesystem prototype

This is a first filesystem feasibility test, not the finished sandbox or an
assertion that QEMU is secure. It deliberately has no virtual network card.
It does not implement agents, credential persistence, or proxy networking.

The host UDS experiment demonstrated that gVisor --host-uds=open lets a socket
introduced into a shared workspace relay guest traffic to a host receiver.
That design is rejected. We are evaluating QEMU with virtiofs before building
the privileged Podman/pasta network broker.

## Requirements

Run on the host as your ordinary user. Required installed tools:
qemu-system-x86_64, Rust virtiofsd with --uid-map/--gid-map support, and Python 3.
KVM access is preferred; --accel tcg is a slow functional fallback. The daemon
must support its namespace sandbox under an unprivileged user. Failure is
reported; the prototype never switches to --sandbox=none or disables SELinux.

Supply a trusted, existing BIOS-bootable Linux disk with serial console
(ttyS0), known root login credentials, Python 3, setpriv, and virtiofs guest
support. Cloud images without a configured login do not meet this requirement.
The prototype does not download an image, install packages, or configure host
networking. Preparing a reproducible guest image is the next deliverable.

```sh
python3 prototypes/qemu/run.py --disk /absolute/path/to/test-guest.qcow2 --format qcow2
```

The supplied disk uses QEMU snapshot mode. Do not use a disk currently being
written by another VM. Only a newly created temporary directory is exported;
no real workspace, home directory, or credentials are shared. The guest sees
host-owned files as UID/GID 1000 through explicit virtiofsd mappings. This
first prototype tests that identity, not arbitrary guest users or guest root.

Log in on the serial console and follow the printed mount/check commands.
Root mounts virtiofs; setpriv runs the file and socket tests as UID/GID 1000.
Power off afterward. Ctrl-A X terminates QEMU if the guest cannot boot.

The test checks host-to-guest content, guest create/rename/delete, ownership
on the host, absence of external guest interfaces, and whether a live host
Unix socket can be connected to through the export. The socket has a host
positive control and records received data. It is an echo service, not a
network relay. A successful guest connection is a failure even without data.

Exit 0 means these narrow checks passed; 1 means a failed check; 2 means an
inconclusive/setup error. Daemon logs and results remain in the printed
private temporary directory. QEMU and virtiofsd are stopped on normal exit
and ordinary interruption. SIGKILL cannot guarantee cleanup. Remove the
printed directory after inspection. No global cleanup commands are used.

## Next gates

1. Confirm this boots and performs the filesystem tests on Fedora 44 Atomic.
2. Produce a reproducible guest image and automate guest test execution.
3. Add hostile guest-root, symlink escape, late socket creation, and two-VM
   tests, including separate worktrees and deliberately shared workspaces.
4. Choose a proxy-only network architecture for QEMU; test both IP families,
   direct DNS, host/LAN reachability, concurrent runs and lifecycle failures.
5. Only then integrate launchers, agent state, and install/update operations.

Virtiofs exports a filesystem, rather than explicitly forwarding guest
connect calls to host sockets. That architectural difference is a hypothesis
to test on our exact build, not a substitute for the socket experiments.
Shared files remain an intentional communication channel between VMs using
the same workspace. Prefer separate worktrees for independent agent runs.

References: [virtiofsd options and sandbox](https://gitlab.com/virtio-fs/virtiofsd/-/blob/main/README.md),
[QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html).

## Prepare the guest safely

The preparation script downloads the pinned Fedora Cloud Base Generic 44-1.7
x86_64 qcow2, verifies the signed checksum against Fedora 44's pinned signing
fingerprint and the independently pinned image SHA256, and creates a NoCloud
seed ISO. Verification uses an isolated keyring directory. It never mounts or
boots the downloaded disk and never installs host packages. All output goes
in a new private directory under your home (or an existing --parent directory).
Allow at least 3 GiB free; each run creates a fresh directory, with no cache
replacement. Failed downloads stay there as partial artifacts for inspection.

```sh
python3 prototypes/qemu/prepare_guest.py --check
python3 prototypes/qemu/prepare_guest.py
```

Preparation requires curl, gpgv, and one of genisoimage/xorrisofs/mkisofs.
Missing tools stop the script before writes or downloads. It refuses sudo.
The preparation script prints the exact separate boot command using --seed.
No password or host SSH key is copied: cloud-init runs the check and powers
off automatically, with no guest NIC and no guest package downloads. QEMU
stops after ten minutes if automatic testing hangs. A missing guest tool or
virtiofs mount failure is inconclusive, not a pass. This boot path still needs
validation on the host; preparation alone cannot establish boot compatibility.
The base image remains unchanged through QEMU snapshot mode; the seed is
read-only. Only the newly generated test share is writable from the VM.

The Fedora image URL and hash are pinned from the
[Fedora Cloud download page](https://fedoraproject.org/cloud/download/), and
the signing fingerprint from [Fedora security](https://fedoraproject.org/security/).
The seed uses [cloud-init NoCloud](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html).
No unsigned or alternate-version fallback is allowed. A mirror error stops
preparation rather than weakening verification. Downloads are the only public
network activity; this does not claim the host's own networking is restricted.

## Two-VM boundary test

The first host run passed file content, ownership, absence of external
interfaces, and a blocked host socket connection. Next, reuse that verified
disk with this separate runner (it generates fresh seeds; no download):

```sh
python3 prototypes/qemu/boundary.py --disk /absolute/path/to/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

Two snapshot guests run concurrently with separate virtiofsd processes and
one deliberately shared temporary workspace. Total guest RAM is 4 GiB.
Neither VM has a NIC. The runner introduces a live host socket only after
both guests have booted and demonstrated their own socket listeners work.
It tests guest UID 1000 and guest root against that host socket, the other
VM's socket, absolute/relative symlinks to a private host canary outside the
export, and parent traversal. Receivers record which messages arrive.
Guest root stays unmapped in virtiofsd; permission failures are recorded and
are valid evidence for this mapping, not for configurations mapping root.

Exit 0 means these specific checks passed, 1 means a failed check, and 2
means setup failure or incomplete evidence. Console and daemon logs plus
result.json remain under the printed /tmp/qemu-boundary-* directory. The
script never deletes it automatically. No sudo, host firewall changes,
package installation, downloads, real projects, or credentials are involved.
The VM disks use snapshot mode. Host processes are stopped on normal exit
and ordinary interruption; SIGKILL cannot guarantee cleanup.

This is a set of targeted functional/adversarial probes, not exhaustive
proof against filesystem or hypervisor vulnerabilities. Shared regular-file
communication is intentional here. Proxy networking remains a later gate.

## Restricted user-network transport smoke test

The host reports QEMU 10.2.2 with the user backend and libslirp 4.9.1.
Test the rootless guestfwd path before adding a production proxy:

```sh
python3 prototypes/qemu/boundary.py --network-smoke --disk /absolute/path/to/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

Each VM gets a separate libslirp instance/subnet, restrict=on, ipv6=off,
and a single guestfwd TCP endpoint. A per-connection host Python relay connects
only to that run's fixed private proxy socket, outside all guest mounts.
No hostfwd, bridge, tap, TFTP, or SMB service is configured. There are no
public requests, downloads, credentials, sudo, or host firewall changes.
The test proxy accepts only control.invalid:443 and connects to a fixed
loopback echo receiver; it is not the production allowlist implementation.

Guest root configures the interface and performs all probes. Both VMs must
prove their own TCP listener works, reach their own allowed proxy tunnel,
receive 403 for a denied CONNECT, and fail direct access to the host receiver
and the other VM's address. Listener lifetimes overlap through both attempts.
Host receivers corroborate successful tunnel traffic. This is a transport
smoke test, not certification of egress confinement: public literal-IP,
UDP/DNS, IPv6, arbitrary protocols, runtime failure, proxy failure, and the
production proxy remain separate gates. Preserve result.json and logs.

QEMU documents restrict=on as blocking ordinary host/outside access while
retaining explicit forwarding rules in its
[user networking documentation](https://www.qemu.org/docs/master/system/invocation.html).

## Local network lifecycle test

The first restricted-network host run passed all six smoke-test checks with
QEMU 10.2.2/libslirp 4.9.1. Extend that same topology without public traffic:

```sh
python3 prototypes/qemu/boundary.py --network-lifecycle --disk /absolute/path/to/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

This includes the normal network smoke test, then closes proxy A's listener
and confirms from the host that its stale socket path refuses connections.
Both guests stay alive while retrying allowed tunnels, direct host TCP and
cross-VM TCP, with fresh local-listener controls. B also repeats its explicit
CONNECT denial. After both report, the host terminates only its owned VM A
process group. B repeats allowed/denied proxy calls, direct host access, and
its local control. Host receivers check exact expected phase tokens.

The test fixture proxy is in the host test process: closing its listener
models unavailable service for NEW connections. It does not test killing a
production proxy process, active streaming tunnel interruption, restart, or
reconnection. A missing phase report, timeout, or failed positive control is
not a security pass. Public literal-IP, UDP/DNS, IPv6, and real production
proxy behavior remain unverified. No new host privileges or network changes
are needed. Logs and result.json are retained as in the earlier tests.

## Batched protocol and stream-failure probes

Run the expanded suite with local controls only:

```sh
python3 prototypes/qemu/boundary.py --network-full --disk /absolute/path/to/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

For the same run plus public reachability checks, add `--public-probes`.
This makes a bounded number of TCP connects to Cloudflare's 1.1.1.1 and
2606:4700:4700::1111, DNS requests for example.com to 1.1.1.1, and HTTPS GETs
to example.com. There are no API credentials or project content in requests.
The host establishes positive controls first; unavailable controls produce
SKIP notes. No public traffic is requested without that flag (local built-in
DNS tests intentionally check whether the backend improperly forwards DNS).

This includes the earlier lifecycle test and batches:

- Host UDP and built-in DNS UDP/TCP attempts, with receiver-side UDP evidence.
- Guest-root IPv6 configuration despite the host's ipv6=off setting, a guest
  loopback positive control, host IPv6 listener control where available, and
  IPv4-mapped host alias attempts.
- Concurrent malformed-method/line/port, unlisted-host, wrong-port and private
  literal requests through the real egress-proxy.py request handler. A second
  explicit guestfwd endpoint carries these tests; its host listener is bound
  to loopback. The default profile permits no upstream hosts. Public mode
  permits only example.com and tests verified TLS through that handler.
- Two established echo streams across the fixture proxy A shutdown: A must
  close with EOF/reset, B must still echo. Proxy A's active sockets are closed
  explicitly to model process death; this is not yet killing/restarting a
  standalone production proxy process. New-connection and VM-stop tests also
  repeat, with exact receiver token checks.

The fixture uses separate per-connection relays. No general host socket
access is enabled in the guest. Public mode changes only this run's temporary
proxy profile, never the installed allowlist. Both VMs retain restrict=on,
separate libslirp networks, and ipv6=off. No sudo, package installation,
firewall changes, real workspace mounts, or image modifications are needed.

The report contains individual booleans, raw status/error details and coverage
notes. Failed checks return 1; setup errors or explicitly inconclusive IPv6
setup return 2. Skipped optional coverage does not become a passing check.
DNS silence cannot prove absence of one-way DNS delivery; packet observation
or a controlled external DNS receiver remains needed for that claim. These
probes also do not certify arbitrary protocols, address-policy completeness,
resource exhaustion resistance, WebSocket semantics, or production proxy
restart behavior. In particular, private literals are refused by the test
allowlist; that does not exercise every DNS resolution/address-filter case.

Local regression tests for the fixtures (no QEMU/public networking):

```sh
python3 -B -m unittest discover -s prototypes/qemu -p 'test_*.py' -v
```

### Observe DNS packets on the host

This next stage repeats the full local network suite and observes tagged IPv4
UDP DNS probes. Run as your normal user:

```sh
sudo -v
python3 prototypes/qemu/boundary.py --dns-observation \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

This option requires existing `sudo`, `timeout`, and `tcpdump` commands.
It does not install anything. Only a 35-second tcpdump capture runs with
elevated privileges; Python, QEMU, and virtiofsd remain unprivileged.
The capture uses all host interfaces without promiscuous mode and filters
for this run's random 16-character DNS label. It stores only matching query
packets in the private artifact directory. It changes no host configuration.
An independent timeout stops the capture even if the Python runner dies.

Each guest sends three queries through QEMU's built-in DNS address and three
directly to 1.1.1.1. The host sends two matching positive-control queries to
1.1.1.1, before and after the guest attempts. All names end in `.invalid`.
Missing host controls, capture errors, dropped packets, or an incomplete
guest phase make the result inconclusive. A captured guest query fails the
boundary check. Replies are not required.

Review `result.json`, `dns-observation.json`, `dns-observation.log`, and
`dns-observation.pcap`. This covers the tagged IPv4 UDP DNS paths during
the observation window, not all possible protocols or IPv6 egress.
The existing `--public-probes` option can be added to repeat public TCP
and HTTPS controls too.

The repository proxy now rejects non-global addresses, multicast, and the
well-known NAT64 prefix explicitly. Local address-policy tests cover CGNAT,
private and mapped addresses, multicast, NAT64, and mixed DNS answers:
`python3 -m unittest discover -s tests -p test_proxy.py`.
Running this prototype does not update an installed host proxy.

### Crash and restart the production proxy

Run without sudo:

```sh
python3 prototypes/qemu/boundary.py --proxy-process-lifecycle \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

This starts two separate, unchanged `egress-proxy.py` processes with private
test configuration allowing only example.com:443. Each VM establishes a
verified HTTPS connection and reads a response before the host sends SIGKILL
to its owned proxy A process. A must lose its active tunnel and fail to open
a new one. B must retain its original tunnel and open another. The host
restarts A on its previous port; both guests must then complete HTTPS
requests and continue rejecting an unlisted destination. Direct host and
cross-VM TCP probes are repeated during the outage.

This option explicitly authorizes a few HTTPS requests to example.com. An
unavailable endpoint, nonpersistent response, or stream timeout produces an
inconclusive run. It then repeats the existing full local fixture suite.
It does not enable packet capture, install anything, change host settings,
mount your project, or modify the base disk. It uses two VMs with 4 GiB total
RAM. Only processes created by this test are killed; logs remain in its
private artifact directory. Review the production_processes reports in
result.json and the per-process decision logs.

This test covers the production proxy executable with test configuration,
not a completed sandbox launcher. The next deliverable is the usable QEMU
sandbox prototype, using the established filesystem and network boundary.

### Interactive sandbox prototype

The host production-process test passed: A's active HTTPS tunnel closed on
SIGKILL, new tunnels failed, and access and denials worked after restart.
B's original HTTPS connection survived both events. The earlier tagged DNS
capture also passed with both host controls visible and no kernel drops.

`sandbox.py` now provides an interactive shell using the tested QEMU and
virtiofs configuration. For an initial disposable workspace, run:

```sh
sandbox_demo=$(mktemp -d "$HOME/qemu-demo-XXXXXX")
mkdir "$sandbox_demo/workspace"
printf '%s\n' example.com > "$sandbox_demo/allowlist.txt"

python3 prototypes/qemu/sandbox.py --check \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2 \
  --workspace "$sandbox_demo/workspace" --allow-file "$sandbox_demo/allowlist.txt"

python3 prototypes/qemu/sandbox.py \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2 \
  --workspace "$sandbox_demo/workspace" --allow-file "$sandbox_demo/allowlist.txt"
```

Run as your ordinary user. No sudo, downloads, or host configuration changes.
The launcher requires a terminal. After boot it opens a UID 1000 bash shell
in /workspace; use `exit` to power off. There is no login password to enter.
Try:

```sh
id
printf '%s\n' 'written inside QEMU' > /workspace/hello.txt
curl --max-time 15 -I https://example.com
curl --max-time 15 -I https://denied.invalid
curl --noproxy '*' --connect-timeout 3 --max-time 5 -I https://1.1.1.1
exit
```

The allowed request should succeed, the unlisted CONNECT should return 403,
and the direct request should fail. Check the host workspace for hello.txt
after exit. The base cloud image has not been extended with coding-agent
CLIs yet; this is an interactive shell prototype, not a replacement for
the installed sandbox launcher.

The selected workspace is a real writable export; edits and deletions
persist. Guest disk changes, including packages and home-directory changes,
are discarded. Host credentials and home directories are not automatically
mounted. The allowlist is copied into the private run directory before
starting a dedicated enforcing proxy. Proxy and virtiofsd failure stops this
VM. Logs and launch arguments remain in the printed private directory.

Concurrent invocations have separate proxies, QEMU netstacks, virtiofsd
processes and snapshots. Use separate workspace directories for file
separation. Sharing a workspace intentionally shares its file contents;
the network boundary does not prevent communication through shared files.
Each invocation uses 2 GiB RAM and two virtual CPUs.

### Build the five-agent image

The interactive shell was verified on the host: UID 1000, workspace write,
allowed HTTPS, explicit proxy denial, and normal shutdown all worked.

Prepare a separate image with Claude, Codex, Pi, OMP, and OpenCode:

```sh
python3 prototypes/qemu/build_image.py --check \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2

python3 prototypes/qemu/build_image.py --allow-downloads \
  --disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

No sudo. Requires qemu-img in addition to the existing runtime, 12 GiB free
space, and 2 GiB guest RAM. The script creates a private qemu-agents directory
under your home (or --parent). It converts the verified disk into a new,
independent writable copy; the original is never modified. No project or
credentials are exported. Only an empty report directory is shared.

The explicit --allow-downloads flag permits public HTTPS destinations through
the production proxy in log mode during this build. Private and other
non-global addresses remain rejected. Guest direct networking remains
restricted, IPv6 remains disabled in QEMU, and no host ports are forwarded.
This broader build policy is not copied into normal sandbox launches.

Inside the build VM, signed Fedora packages provide development dependencies.
The same five vendor installer URLs used by Containerfile install current
agent releases as UID 1000. Downloaded installers execute only in that VM.
These releases are not version-pinned; manifest.json records their reported
versions and the resulting image SHA256. The build performs no logins.

The console.log in the printed directory shows progress. Failure retains
building.qcow2 and logs for diagnosis; it does not publish agents.qcow2.
Success checks the qcow2 structure and leaves agents.qcow2 read-only.
Use that new path with sandbox.py --disk. Normal launches still use snapshots.
The host build itself remains unverified until you run it.

### Keep credentials inside the VM

Host home and agent-state directories are not mounted. Use --vm-dir to keep
a dedicated writable VM disk. On first launch it makes an independent copy
of the agent image; subsequent launches reuse it. Credentials, agent homes,
installed packages and session state stay inside that disk.

```sh
python3 prototypes/qemu/sandbox.py \
  --disk /absolute/path/to/agents.qcow2 \
  --vm-dir /absolute/path/to/my-coding-vm \
  --workspace /absolute/path/to/project \
  --allow-file /absolute/path/to/runtime-allowlist.txt
```

The VM storage directory must be outside the shared workspace. The only
directory exported for a coding session is the explicitly selected
workspace. The seed ISO contains generated boot configuration, not host
credentials. VM disk files may be stored under your home; this does not
mount your home inside the guest. During image building, only a new temporary
report directory under /tmp is exported.

Log in to agents from inside the VM. Use --agent claude, codex, pi, omp or
opencode to launch one directly, or use the default shell. Reuse the same
--vm-dir and --disk arguments to resume the VM. An exclusive lock rejects
simultaneous launches of the same VM directory. Use a different --vm-dir for
each concurrent sandbox; their credentials and guest files are separate.

Without --vm-dir the launch remains disposable, including logins. Runtime
allowlists remain mandatory and must include the intended provider and
login endpoints. Device login, token refresh and authenticated requests for
these tools still need host validation. No host credential files or
environment secrets are copied. Agent launches keep their normal permission
settings; no bypass flags are added.

Build failure handling: the host prints the current stage at least every
30 seconds. Guest status and result files are published by atomic rename.
Missing provisioning progress stops the build after ten minutes; a successful
install must shut down within 90 seconds. Installer errors and cloud-init
script failures stop the run promptly. The overall limit remains two hours.
Individual installers have time limits and receive no interactive stdin.
Installer-script downloads have bounded retries; failed installers are not
automatically rerun.

provision.log preserves installer output separately from the serial console;
build-status.json identifies the last stage. Both are copied into the image
artifact directory during cleanup. A successful report must contain all five
nonempty tool versions. Disk checking, hashing and manifest preparation
precede publication of agents.qcow2. Interrupted builds remain disposable
attempts; automatic resume is not implemented.

Claude installation now uses the official @anthropic-ai/claude-code npm
package under the guest user's ~/.local prefix. The native shell bootstrap
stalled after its setup banner in a host build despite accepted proxy
connections; the exact cause remains unknown. npm uses explicit proxy
settings, includes optional platform binaries, logs HTTP operations, and
has a ten-minute overall install limit with bounded fetch retries.
The other four installers are unchanged. This change still needs host
validation and does not relax the QEMU network policy.

The host confirmed OMP's 201319904-byte release downloads successfully,
then omp --version times out. Build and interactive launch now explicitly
use -cpu host with KVM, exposing the host-supported CPU features instead of
leaving QEMU's CPU model implicit. This is a compatibility correction, not
a confirmed diagnosis of the OMP timeout. These images are intended for
local use, not live migration across heterogeneous hosts.

If OMP still times out, the build records guest CPU information and runs a
15-second strace retry inside the guest; the final 80 trace lines go into
provision.log. No host tracing or elevated host privileges are used.

### Integrated launcher migration

The root README now documents SANDBOX_ISOLATION=qemu and the installed
entry points. This supersedes the earlier plan to keep all agent state only
inside VM disks: the agreed default for integrated launches is live shared
sandbox-specific agent state, with an independent disposable disk overlay
per invocation. The standalone sandbox.py --vm-dir option remains an
explicit persistent-VM diagnostic mode; installed launchers never select a
common writable VM disk.

Codex SQLite databases are an exception to live state sharing. The guest sets
CODEX_SQLITE_HOME to /var/lib/llm-sandbox/codex-sqlite, a mode-0700 directory
owned by the guest user on its own disk. This avoids SQLite WAL shared-memory
mapping failures on the virtiofs state export. Config, credentials and session
files remain shared; existing host databases are neither copied nor modified.
Database-only state is private and is discarded when an integrated launch's
overlay exits. It persists when explicitly using the standalone --vm-dir mode.
Separate databases also separate background-job coordination; correctness of
concurrent Codex operations on the remaining shared files is not established.
An explicit sqlite_home setting in Codex config takes precedence over the
environment variable; remove that setting to use the launcher's private path.
This change applies to Codex, not every agent's database storage.

accept_launcher.py exercises two simultaneous installed Codex shell launchers
against a temporary shared project and temporary shared agent state. It
requires the prepared agent image and normal host runtime dependencies,
but no root privileges or public downloads.
It also opens WAL databases at the same guest path in both VMs, checks that a
second connection can read during a write transaction, and verifies that each
database contains only its own VM's marker. These checks cover storage behavior,
not Codex's application-level coordination.

### Launcher hardening acceptance

Run `accept_launcher.py --runtime-checks --disk IMAGE` after reinstalling.
It now exercises terminal initialization and resize through a host pseudo-terminal,
per-run explicit overlays and their cleanup, shared state, private SQLite WAL
files and one guest surviving another's exit. No root or real credentials are used.
Use `--cache-benchmark never` and then `--cache-benchmark auto` to measure the
temporary fixture and host replacement visibility. This does not change the
normal runtime's `never` cache policy. `--long-batch-seconds 960` exercises a
command beyond the former fifteen-minute limit. The long-batch check remains
pending; the short runtime and cache probes have host results below.

A failed acceptance run now prints bounded launcher/guest log tails and saves
failure.json. To inspect a previous run without booting a VM or creating files,
use `accept_launcher.py --diagnose /path/to/acceptance-directory`.

The first hardening host run reached clean guest exits but the launcher
misclassified a helper exiting during QEMU poweroff as an active-session
failure. The supervisor now allows up to two seconds for QEMU to exit when a
helper stops. If QEMU remains running, helper failure still stops the VM.
Actual QEMU exit status and the guest exit report are still checked.
Regression tests cover this ordering, nonzero QEMU exit status and a helper
failing while its VM remains active. The repeat host run below completed
without the shutdown race.

Host run qemu-launch-accept-h0p7126p confirmed A's initial/live terminal size,
both guests' state sharing and private SQLite WAL storage, host replacement
visibility, overlay selection/cleanup, and B surviving A. With cache=never,
500-file scans took 84–95 ms. The two false B terminal checks were a probe bug:
the batch guest still has ttyS0, but receives no host resize events. Terminal
checks now require an explicit --terminal-probe argument passed only to A when
--runtime-checks is requested. A local regression test covers both serial-TTY
guests and explicit selection. The corrected host comparison follows.

Host run qemu-launch-accept-9_uuj6vn passed every reported check with cache=auto
on 2026-09-13. This covered terminal initialization and resize, live shared
state, private guest homes and SQLite WAL files, UID/ownership, independent
listener ports, host file replacement visibility, B surviving A, and explicit
overlay selection and cleanup. Artifacts were retained at:
/var/home/duve/.cache/llm-sandbox/qemu-acceptance/qemu-launch-accept-9_uuj6vn

Mean first-scan times across A/B were 91.0 ms for never and 75.1 ms for auto.
Mean repeat-scan times (the last two scans in each VM) were 86.2 ms for never
and 35.4 ms for auto, about 2.43 times faster in this 500-file fixture.
The runs were sequential, not a controlled cache-coherence stress test.
Normal launchers retain cache=never. Long batch runs, real-agent concurrent
state updates and credential refresh remain separate acceptance items.

Runtime unit tests have moved to ../../qemu/. Run them with
`python3 -m unittest discover -s qemu` from the repository root. Tests specific
to this acceptance harness and the experimental crash helpers stay here.
The supported launcher and acceptance tool now reject writable base images;
use the read-only agents.qcow2 published by the image builder. Historical
building.qcow2 results above describe the manually recovered development image.
