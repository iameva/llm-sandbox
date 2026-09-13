#!/usr/bin/env python3
"""Step 0 of network-restriction-plan.md: test every assumption it makes
about the host, before any of it is implemented.

Run this ON THE HOST. It is deliberately not named test_*.py, so
`python3 -m unittest discover -s tests` inside the sandbox skips it: it
cannot pass in here and a skipped run must never look like a passing one.

    python3 tests/host_assumptions.py                  # tier A, no sudo
    sudo -v && python3 tests/host_assumptions.py --sudo-probes
    sudo -v && python3 tests/host_assumptions.py --sudo-probes --reload-firewall
    python3 tests/host_assumptions.py --json report.json

This is not a read-only script. What it does, in full:

* Refuses to start unless it is running as an ordinary user against a
  local rootless podman. Running as root, or with CONTAINER_HOST,
  DOCKER_HOST or CONTAINER_CONNECTION set, exits before anything is
  created — otherwise cleanup itself would act on the wrong machine.
* Every file it creates lives under one directory it makes in
  $XDG_RUNTIME_DIR and removes on exit, including on Ctrl-C.
* Every container carries the label sbxprobe=<runid> and is removed by
  that label, so cleanup cannot touch your running sessions.
* It never reads or writes ~/.config/llm-sandbox, never pulls an image,
  and needs no credentials.
* A9 opens two ephemeral TCP listeners, one on loopback and one on this
  host's own address, and closes both before the probe returns. Reaching
  the host's address is the positive control that makes the guest's
  failure mean something.
* A10 connects to 1.1.1.1:443 and has the guest attempt a DNS lookup.
  --offline skips it; the probe then reports SKIP, not PASS.
* A13 briefly runs one pasta-networked container, to read the pasta
  argv podman actually builds.
* Privileged probes create their own namespace, veth pair and nftables
  table, refusing to continue if a name is already taken, and delete
  exactly what they created. Firewalld changes are RUNTIME ONLY, never
  --permanent, so `firewall-cmd --reload` restores the host.
* --reload-firewall reloads the whole host firewall. Unrelated
  runtime-only rules anywhere on this host are discarded with it.
* --keep leaves every resource in place and prints how to remove them.
* --json backs an existing file up to PATH.bak before overwriting.
* sbxproxy, its listener, the podman default network and firewalld's own
  table are read but never modified.

Results are PASS, FAIL, ERROR, SKIP, or NOTE. ERROR is a broken probe,
not a host verdict, and still fails the run: an unproven assumption must
never be reported as a passing one. NOTE records behaviour the plan does
not assume a direction for but must decide on — read those.

Exit status: 0 every probe that ran passed and cleanup was complete;
1 something failed, errored, or leaked; 2 refused to run here. A 0 with
skips means those assumptions are still unproven — read the summary.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PASS, FAIL, ERROR, SKIP, NOTE, MOOT = "PASS", "FAIL", "ERR", "SKIP", "NOTE", "MOOT"

# Statuses that mean "do not report this run as clean".
BAD = (FAIL, ERROR)

# Statuses that do not leave an assumption unproven. MOOT is not SKIP:
# a skip means nobody answered the question, while MOOT means the
# question stopped applying because another probe answered it.
SETTLED = (PASS, NOTE, MOOT)

# AF_UNIX sun_path is 108 bytes including the terminator. The plan asks
# for per-run socket directories; this is the budget they have to fit.
SUN_PATH_MAX = 107


class ProbeSkip(Exception):
    """Raised by a probe that cannot run, with the reason."""


class MootProbe(Exception):
    """Raised by a probe whose question another probe has settled."""


@dataclass
class Result:
    pid: str
    title: str
    status: str
    detail: str = ""


@dataclass
class Probe:
    pid: str
    tier: str
    title: str
    fn: object
    needs: tuple = ()


PROBES: list[Probe] = []


def probe(pid, tier, title, needs=()):
    def register(fn):
        PROBES.append(Probe(pid, tier, title, fn, tuple(needs)))
        return fn
    return register


# ---------------------------------------------------------------------
# Shelling out
# ---------------------------------------------------------------------

@dataclass
class Run:
    argv: list
    code: int
    out: str
    err: str

    @property
    def ok(self):
        return self.code == 0

    @property
    def text(self):
        return (self.out + self.err).strip()


def run(argv, *, timeout=60, stdin=None, env=None):
    """Never raises on a non-zero exit: a failing command is usually the
    evidence, not an error in the harness."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            input=stdin, env=env,
        )
    except FileNotFoundError:
        return Run(argv, 127, "", f"no such executable: {argv[0]}")
    except subprocess.TimeoutExpired:
        return Run(argv, 124, "", f"timed out after {timeout}s")
    return Run(argv, proc.returncode, proc.stdout, proc.stderr)


def sudo(argv, **kw):
    return run(["sudo", "-n", *argv], **kw)


class Stack:
    """Undo actions, newest first, each with a name.

    A cleanup step that fails is reported, not swallowed: a leaked
    namespace or nftables table outlives this process, and a reload does
    not remove either. Steps that return a Run are checked by exit code,
    so a failed `ip netns del` cannot pass silently.
    """

    def __init__(self):
        self.items = []

    def defer(self, description, fn):
        self.items.append((description, fn))

    def unwind(self):
        failures = []
        while self.items:
            description, fn = self.items.pop()
            try:
                outcome = fn()
            except Exception as exc:
                failures.append(f"{description}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(outcome, Run) and not outcome.ok:
                failures.append(f"{description}: {outcome.text[:200]}")
        return failures

    def describe(self):
        return [description for description, _ in reversed(self.items)]


# ---------------------------------------------------------------------
# Local receivers. Every "it is blocked" claim needs one of these
# answering somewhere else, or the claim is just a timeout.
# ---------------------------------------------------------------------

class PongHandler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        try:
            self.rfile.readline(64)
            self.wfile.write(b"PONG\n")
        except OSError:
            pass


class UnixPong(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    # Not allow_reuse_address: for AF_UNIX that flag does nothing, which
    # is the same trap egress-proxy.py will hit when it grows --listen-uds.


class TcpPong(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(server_cls, address, stack, description):
    server = server_cls(address, PongHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        # One entry, so shutdown always precedes close. Closing the
        # socket out from under serve_forever wedges the loop.
        server.shutdown()
        server.server_close()

    stack.defer(description, stop)
    return server


def tcp_reachable(host, port, timeout=4):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, "connected"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def primary_address():
    """The address this host would use to reach the internet. A UDP
    connect only consults the routing table, so this works offline."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


# ---------------------------------------------------------------------
# Guest-side probe programs. Passed with python3 -c, so no quoting games
# and nothing is written into the image or a mount.
# ---------------------------------------------------------------------

GUEST_UDS = r'''
import json, os, socket, sys
path = sys.argv[1]
out = {"guest_uid": os.getuid()}
try:
    st = os.stat(path)
    out["sock_uid"] = st.st_uid
    out["sock_mode"] = oct(st.st_mode & 0o777)
    out["is_socket"] = True
except OSError as exc:
    out["stat_error"] = f"{type(exc).__name__}: {exc}"
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(8)
try:
    sock.connect(path)
    sock.sendall(b"PING\n")
    out["reply"] = sock.recv(32).decode("utf-8", "replace").strip()
    out["connected"] = out["reply"] == "PONG"
except OSError as exc:
    out["connected"] = False
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
'''

GUEST_NET = r'''
import json, os, socket, sys
out = {}
# /proc/net/dev, not /sys/class/net: gVisor's sentry serves the former
# and (measured 2026-09-12) not always the latter, so sysfs absence
# would otherwise read as "no interfaces" and pass by accident.
try:
    with open("/proc/net/dev") as fh:
        rows = fh.read().splitlines()[2:]
    out["interfaces"] = sorted(r.split(":")[0].strip() for r in rows if ":" in r)
except OSError as exc:
    out["interfaces"] = f"error: {exc}"
out["sysfs_net"] = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else "absent"
try:
    with open("/proc/net/route") as fh:
        rows = [line.split() for line in fh.read().splitlines()[1:]]
    out["default_routes"] = [r[0] for r in rows if len(r) > 2 and r[1] == "00000000"]
except OSError as exc:
    out["default_routes"] = f"error: {exc}"
try:
    with open("/etc/resolv.conf") as fh:
        out["resolv_conf"] = [l for l in fh.read().split("\n") if l.startswith("nameserver")]
except OSError as exc:
    out["resolv_conf"] = f"absent: {type(exc).__name__}"
results = {}
for name, host, port in json.loads(sys.argv[1]):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((host, port))
        results[name] = "connected"
    except OSError as exc:
        results[name] = f"{type(exc).__name__}({exc.errno})"
    finally:
        sock.close()
out["tcp"] = results
try:
    socket.getaddrinfo("example.com", 443)
    out["dns"] = "resolved"
except OSError as exc:
    out["dns"] = f"{type(exc).__name__}"
# Whether the guest shim can listen on 127.0.0.1 is the question, not
# whether an interface is listed. Bind, connect, exchange bytes.
try:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket()
    client.settimeout(5)
    client.connect(("127.0.0.1", listener.getsockname()[1]))
    accepted, _ = listener.accept()
    accepted.sendall(b"ok")
    out["loopback"] = client.recv(8).decode("utf-8", "replace")
    for sock in (accepted, client, listener):
        sock.close()
except OSError as exc:
    out["loopback"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
'''

# Used from inside the privileged namespace, where the image is not
# involved and only the host's python3 is available.
NS_TCP_PROBE = (
    "import socket, sys\n"
    "sock = socket.socket()\n"
    "sock.settimeout(4)\n"
    "try:\n"
    "    sock.connect((sys.argv[1], int(sys.argv[2])))\n"
    "    print('connected')\n"
    "except OSError as exc:\n"
    "    print(type(exc).__name__, exc.errno)\n"
)


# ---------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------

@dataclass
class Ctx:
    args: object
    runid: str
    tmp: Path
    cleanup: Stack = field(default_factory=Stack)
    facts: dict = field(default_factory=dict)
    passed: set = field(default_factory=set)
    counter: int = 0

    def name(self, what):
        self.counter += 1
        return f"sbxprobe-{self.runid}-{what}-{self.counter}"

    def defer(self, description, fn):
        self.cleanup.defer(description, fn)

    def need(self, *pids):
        for pid in pids:
            if pid not in self.passed:
                raise ProbeSkip(f"{pid} did not pass")


def podman_run(ctx, *, argv, runtime=None, network="none", mounts=(),
               extra=(), dns_none=None, timeout=120, detach=False):
    """One place that decides the container shape, so no probe quietly
    tests a different configuration than the others."""
    cmd = [
        "podman", "run", "--rm", "--pull=never",
        "--label", f"sbxprobe={ctx.runid}",
        "--name", ctx.name("run"),
        "--userns=keep-id",
        "--security-opt", "label=disable",
        f"--network={network}",
    ]
    if dns_none is None:
        dns_none = ctx.facts.get("dns_none_ok", False)
    if dns_none:
        cmd.append("--dns=none")
    if detach:
        cmd.append("-d")
    if runtime:
        cmd += ["--runtime", runtime]
    for source, target in mounts:
        cmd += ["-v", f"{source}:{target}:rw"]
    cmd += list(extra) + [ctx.args.image] + list(argv)
    return run(cmd, timeout=timeout)


def guest_json(result):
    """Guest probes print one JSON line; anything else is a failure we
    want to see verbatim rather than a parse error."""
    for line in reversed(result.out.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ProbeSkip(f"no JSON from guest: {result.text[:400]}")


def uds_mount(ctx, path, target_dir="/run/sbx"):
    """Mount one run's endpoint the way this host turned out to support.

    The plan says "mount only that run's endpoint". Whether podman can
    bind-mount a socket *file* is a host fact, not a preference: if it
    cannot, the endpoint has to be a private directory holding exactly
    one socket, which is the same isolation with a different layout.
    A6 decides which, and every later probe follows it.
    """
    if ctx.facts.get("mount_style", "file") == "file":
        guest = f"{target_dir}/{path.name}"
        return [(str(path), guest)], guest
    return [(str(path.parent), target_dir)], f"{target_dir}/{path.name}"


def uds_wrapper(ctx):
    """The wrapper every UDS probe runs through, made once. A5b may
    replace it if this host needs runsc's netstack forced."""
    if "wrapper_uds" not in ctx.facts:
        ctx.facts["wrapper_uds"] = runsc_wrapper(
            ctx, "--ignore-cgroups --host-uds=open", "uds")
    return ctx.facts["wrapper_uds"]


def runsc_wrapper(ctx, flags, tag):
    """The launcher generates one of these at $ROOT/runsc-wrapper. This
    probe writes its own under its temp directory instead: rewriting the
    shared one would race the sessions already running on this host."""
    path = ctx.tmp / f"runsc-{tag}"
    path.write_text(
        "#!/bin/sh\n"
        "# Generated by tests/host_assumptions.py. Disposable.\n"
        f"exec {shlex.quote(ctx.facts['runsc'])} {flags} \"$@\"\n"
    )
    path.chmod(0o755)
    return str(path)


# ---------------------------------------------------------------------
# Tier A: no privileges. These decide whether the UDS transport in
# step 2 of the plan is real.
# ---------------------------------------------------------------------

@probe("A1", "A", "podman is local, rootless, and not brokered")
def a1(ctx):
    """Already enforced by the startup gate, which runs before anything
    is created: if this could fail, cleanup would be aiming podman at a
    machine this script never touched. Recorded here for the report."""
    host = ctx.facts["podman_host"]
    return PASS, (f"{ctx.facts['podman_version']}, "
                  f"rootless={host['security']['rootless']}, "
                  f"serviceIsRemote={host['serviceIsRemote']}, "
                  f"networkBackend={host.get('networkBackend')}, "
                  f"rootlessNetworkCmd={host.get('rootlessNetworkCmd')}, "
                  f"uid={os.getuid()}")


@probe("A2", "A", "runsc accepts --host-uds=open")
def a2(ctx):
    candidates = [ctx.args.runsc] if ctx.args.runsc else []
    candidates += [shutil.which("runsc"), "/var/usrlocal/bin/runsc",
                   "/usr/local/bin/runsc", str(Path.home() / ".local/bin/runsc")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            ctx.facts["runsc"] = candidate
            break
    else:
        raise ProbeSkip("no runsc binary found; pass --runsc PATH")

    version = run([ctx.facts["runsc"], "--version"])
    ctx.facts["runsc_version"] = version.text.splitlines()[0] if version.ok else "unknown"
    # Global flags parse before the subcommand, so this rejects an
    # unsupported --host-uds without starting anything.
    parsed = run([ctx.facts["runsc"], "--host-uds=open", "--version"])
    detail = f"{ctx.facts['runsc']} — {ctx.facts['runsc_version']}"
    if not parsed.ok:
        return FAIL, detail + f"; --host-uds=open rejected: {parsed.text[:200]}"
    return PASS, detail + "; --host-uds=open accepted"


@probe("A3", "A", "sandbox image is present locally (--pull=never is viable)")
def a3(ctx):
    found = run(["podman", "image", "exists", ctx.args.image])
    if found.ok:
        digest = run(["podman", "image", "inspect", "--format",
                      "{{.Id}} {{.Created}}", ctx.args.image])
        return PASS, f"{ctx.args.image} {digest.text[:60]}"
    return FAIL, f"{ctx.args.image} absent; build it with ./build.sh"


@probe("A4", "A", "podman accepts --dns=none together with --network=none",
       needs=("A3",))
def a4(ctx):
    result = podman_run(ctx, argv=["true"], dns_none=True, network="none",
                        runtime=None, timeout=90)
    ctx.facts["dns_none_ok"] = result.ok
    if result.ok:
        return PASS, "accepted; sandbox-run.sh:866 needs no change for this path"
    return NOTE, ("rejected — sandbox-run.sh:866 adds --dns=none whenever a proxy "
                  f"is set, so the restricted launch must gate it: {result.text[:200]}")


@probe("A5", "A", "--network=none leaves no external interface", needs=("A2", "A3"))
def a5(ctx):
    result = podman_run(ctx, runtime=uds_wrapper(ctx), network="none",
                        argv=["python3", "-c", GUEST_NET, "[]"])
    if not result.ok:
        return FAIL, f"container failed: {result.text[:300]}"
    data = guest_json(result)
    ctx.facts["guest_net_none"] = data
    interfaces = data.get("interfaces")
    routes = data.get("default_routes")
    detail = (f"interfaces={interfaces} sysfs={data.get('sysfs_net')} "
              f"default_routes={routes} resolv={data.get('resolv_conf')}")
    if not isinstance(interfaces, list) or not isinstance(routes, list):
        return FAIL, detail + " — could not read the guest's own network state"
    external = [name for name in interfaces if name != "lo"]
    if external or routes:
        return FAIL, detail
    return PASS, detail


@probe("A5b", "A", "whether plain --network=none leaves a usable loopback",
       needs=("A5",))
def a5b(ctx):
    """Measured 2026-09-12: it does not, and A5f supplies the answer, so
    this records the reason the design carries a holder container rather
    than gating on a configuration the plan no longer ships.

    The shim still needs somewhere to listen — every harness reaches the
    proxy through HTTPS_PROXY=127.0.0.1:PORT. That requirement moved to
    A5f, which measures it in the shape that will actually run."""
    result = ctx.facts["guest_net_none"].get("loopback")
    if result == "ok":
        ctx.facts["loopback_ok"] = True
        return PASS, "bind, connect and exchange on 127.0.0.1 all work"

    forced = runsc_wrapper(ctx, "--ignore-cgroups --host-uds=open --network=sandbox",
                           "sandbox")
    retry = podman_run(ctx, runtime=forced, network="none",
                       argv=["python3", "-c", GUEST_NET, "[]"])
    second = guest_json(retry).get("loopback") if retry.out.strip() else retry.text[:200]
    if second == "ok":
        ctx.facts["wrapper_uds"] = forced
        ctx.facts["loopback_ok"] = True
        return NOTE, ("loopback needs runsc --network=sandbox forced in the wrapper; "
                      f"without it: {result}")
    ctx.facts["loopback_ok"] = False
    return NOTE, (f"none, as expected: default={result!r} "
                  f"--network=sandbox={second!r}. runsc skips a loopback that is down "
                  "and crun brings it up, so the guest needs a prepared namespace — A5f")


def loopback_wanted(ctx):
    if ctx.facts.get("loopback_ok", True):
        raise ProbeSkip("a working guest loopback was already found; "
                        "no alternative needed")
    if ctx.facts.get("holder_ok"):
        raise MootProbe(f"superseded by A5f: {ctx.facts['holder_how']}")


def netns_holder(ctx, key="a"):
    """A crun container doing nothing but holding a namespace open, so
    runsc finds a loopback that is already up.

    Keyed, because the design gives every run its own. Two sandboxes
    sharing one holder share a network namespace and can therefore reach
    each other's loopback services — A15 measures exactly that, so the
    harness must be able to build both arrangements deliberately.
    """
    holders = ctx.facts.setdefault("holders", {})
    if key not in holders:
        started = podman_run(ctx, runtime=None, network="none", detach=True,
                             argv=["sleep", "900"])
        if not started.ok:
            raise ProbeSkip(f"could not start crun holder {key}: {started.text[:200]}")
        holder = started.out.strip()
        holders[key] = holder
        ctx.defer(f"remove netns holder {key} ({holder[:12]})",
                  lambda: run(["podman", "rm", "-f", "--ignore", holder], timeout=30))
    return holders[key]


def guest_network(ctx, key="a"):
    """The network shape the probes should measure: whatever step 2 will
    actually ship on this host. Once A5f proves the holder, every later
    probe uses it — otherwise the isolation results would describe a
    configuration nobody runs."""
    if ctx.facts.get("holder_ok"):
        return f"container:{netns_holder(ctx, key)}"
    return "none"


def lo_up_wrapper(ctx):
    """`unshare -rn <this> <cmd...>`: bring loopback up in the fresh
    namespace, then exec the command. A script beats `sh -c` here — the
    command carries a python program as one argv element."""
    path = ctx.tmp / "lo-up"
    if not path.exists():
        path.write_text("#!/bin/sh\nip link set lo up || exit 97\nexec \"$@\"\n")
        path.chmod(0o755)
    return str(path)


# Holds a namespace open with loopback up. PR_SET_DUMPABLE is the point:
# writing a uid map clears the dumpable flag, which makes /proc/PID/ns/*
# root-owned and unreadable — the "permission denied" the first host run
# hit. Setting it back is the documented way to reopen that path.
NS_HOLDER = (
    "import ctypes, subprocess, sys, time\n"
    "ctypes.CDLL(None, use_errno=True).prctl(4, 1, 0, 0, 0)\n"
    "subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)\n"
    "print('ready', flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


@probe("A5c", "A", "is the missing loopback gVisor's doing or podman's", needs=("A3",))
def a5c(ctx):
    """Diagnostic, not a gate. crun under the same --network=none tells
    us whether to look for a runsc flag or a podman one."""
    loopback_wanted(ctx)
    result = podman_run(ctx, runtime=None, network="none",
                        argv=["python3", "-c", GUEST_NET, "[]"])
    if not result.out.strip():
        return NOTE, f"crun run failed: {result.text[:200]}"
    data = guest_json(result)
    detail = f"crun: loopback={data.get('loopback')!r} interfaces={data.get('interfaces')}"
    if data.get("loopback") == "ok":
        return NOTE, (detail + " — podman's --network=none is not the problem; the "
                      "empty netstack is runsc's behaviour, so look for a runsc answer")
    return NOTE, detail + " — podman's --network=none gives no loopback either"


@probe("A5d", "A", "a prepared loopback-only namespace, joined with --network=ns:",
       needs=("A3",))
def a5d(ctx):
    """The candidate fix that keeps step 2 intact: hand the container a
    namespace that already has lo up and nothing else. Rootless, no
    privileges, and it fails closed — a bad path is an error, not an
    accidental share of the real host namespace."""
    loopback_wanted(ctx)
    holder = subprocess.Popen(
        ["unshare", "-rn", sys.executable, "-c", NS_HOLDER, "180"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ctx.defer("stop the loopback-only namespace holder", holder.kill)
    if (holder.stdout.readline().strip() != "ready"
            or holder.poll() is not None):
        return NOTE, f"could not create the namespace: {holder.stderr.read()[:200]}"

    netns = f"/proc/{holder.pid}/ns/net"
    readable = os.access(netns, os.R_OK)
    if not readable:
        return NOTE, (f"{netns} is unreadable even after PR_SET_DUMPABLE; podman "
                      "cannot join a namespace it cannot open — see A5f")
    result = podman_run(ctx, runtime=uds_wrapper(ctx), network=f"ns:{netns}",
                        argv=["python3", "-c", GUEST_NET, "[]"])
    if not result.out.strip():
        # Measured 2026-09-12: still denied with the flag set, while this
        # process could open the same path. Rootless podman re-execs
        # inside its own user namespace, and from there it has no ptrace
        # access to a namespace owned by a different one. A dead end
        # without a bind-mounted netns file, which needs root.
        return NOTE, (f"podman refused ns:{netns} although this process can read it "
                      f"({readable=}): rootless podman re-execs in its own user "
                      f"namespace and cannot reach another's — {result.text[:200]}")
    data = guest_json(result)
    external = [n for n in data.get("interfaces", []) if n != "lo"]
    detail = (f"loopback={data.get('loopback')!r} interfaces={data.get('interfaces')} "
              f"routes={data.get('default_routes')}")
    if data.get("loopback") == "ok" and not external and not data.get("default_routes"):
        ctx.facts["ns_join_works"] = True
        return PASS, detail + " — step 2 keeps its 127.0.0.1 shim, at the cost of one "\
                              "rootless namespace per run"
    return NOTE, detail


@probe("A5f", "A", "join a crun holder's namespace with --network=container:",
       needs=("A3",))
def a5f(ctx):
    """A5c said crun gets loopback and runsc does not, from the same
    podman flag. The difference is who brings `lo` up: crun does it as
    part of container setup, while runsc builds a netstack from what the
    namespace already has and skips an interface that is down.

    So give runsc a namespace where lo is already up — and let podman
    own it, rather than a /proc path whose permissions depend on the
    dumpable flag. A throwaway crun container holds it open.
    """
    loopback_wanted(ctx)
    holder = netns_holder(ctx)
    result = podman_run(ctx, runtime=uds_wrapper(ctx),
                        network=f"container:{holder}",
                        argv=["python3", "-c", GUEST_NET, "[]"])
    if not result.out.strip():
        return NOTE, f"podman refused --network=container: {result.text[:240]}"
    data = guest_json(result)
    external = [n for n in data.get("interfaces", []) if n != "lo"]
    detail = (f"loopback={data.get('loopback')!r} interfaces={data.get('interfaces')} "
              f"routes={data.get('default_routes')}")
    if data.get("loopback") == "ok" and not external and not data.get("default_routes"):
        ctx.facts["holder_ok"] = True
        ctx.facts["holder_how"] = "--network=container: onto a crun holder"
        return PASS, detail + (" — step 2 keeps its 127.0.0.1 shim; the cost is one "
                               "podman-managed holder container per run. Every "
                               "isolation probe below now runs in this shape")
    return NOTE, detail + " — loopback still unavailable through a shared namespace"


@probe("A5e", "A", "podman --network=host inside an unshared loopback-only namespace",
       needs=("A3",))
def a5e(ctx):
    """The same idea from the other side, if --network=ns: is refused.
    Note what it costs: --network=host is safe here only because the
    namespace around it is empty. Skip the unshare and the same flag
    exposes the real host. A5d fails closed; this fails open, so prefer
    A5d if both work."""
    loopback_wanted(ctx)
    cmd = [
        "unshare", "-rn", lo_up_wrapper(ctx),
        "podman", "run", "--rm", "--pull=never",
        "--label", f"sbxprobe={ctx.runid}", "--name", ctx.name("hostns"),
        "--userns=keep-id", "--security-opt", "label=disable",
        "--runtime", uds_wrapper(ctx), "--network=host",
        ctx.args.image, "python3", "-c", GUEST_NET, "[]",
    ]
    result = run(cmd, timeout=120)
    if "/var/lib/containers" in result.text:
        return NOTE, ("dead end: inside `unshare -r` podman believes it is root and "
                      "switches to rootful storage, where it has no image and no "
                      f"permissions — {result.text[-160:]}")
    if not result.out.strip():
        return NOTE, f"did not run: {result.text[:240]}"
    data = guest_json(result)
    external = [n for n in data.get("interfaces", []) if n != "lo"]
    detail = f"loopback={data.get('loopback')!r} interfaces={data.get('interfaces')}"
    if data.get("loopback") == "ok" and not external:
        return NOTE, detail + " — works, but only while the outer unshare is guaranteed"
    return NOTE, detail


@probe("A6", "A", "keep-id maps the socket owner onto the guest user",
       needs=("A2", "A3"))
def a6(ctx):
    sock_dir = ctx.tmp / "uds-a"
    sock_dir.mkdir(mode=0o700, exist_ok=True)
    path = sock_dir / "p.sock"
    serve(UnixPong, str(path), ctx.cleanup, "close run A's UDS receiver")
    path.chmod(0o600)
    ctx.facts["uds_a"] = path

    if len(str(path).encode()) > SUN_PATH_MAX:
        return FAIL, f"{path} exceeds sun_path ({SUN_PATH_MAX} bytes)"

    data, style = None, None
    for candidate in ("file", "directory"):
        ctx.facts["mount_style"] = candidate
        mounts, guest_path = uds_mount(ctx, path)
        result = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                            mounts=mounts,
                            argv=["python3", "-c", GUEST_UDS, guest_path])
        if result.out.strip():
            data, style = guest_json(result), candidate
            break
        ctx.facts["last_mount_error"] = result.text[:300]
    if data is None:
        return FAIL, ("neither a socket-file nor a directory mount worked: "
                      f"{ctx.facts.get('last_mount_error', '')}")

    ctx.facts["mount_style"] = style
    ctx.facts["uds_guest"] = data
    detail = (f"mount_style={style} guest_uid={data.get('guest_uid')} "
              f"sock_uid={data.get('sock_uid')} mode={data.get('sock_mode')} "
              f"path={len(str(path).encode())}B")
    if data.get("guest_uid") == data.get("sock_uid") and data.get("sock_mode") == "0o600":
        return PASS, detail
    return FAIL, detail + " — mode 0600 would have to be widened"


@probe("A7", "A", "the gofer opens the mounted socket with --host-uds=open",
       needs=("A6",))
def a7(ctx):
    data = ctx.facts["uds_guest"]
    if data.get("connected"):
        return PASS, f"round trip: {data.get('reply')!r}"
    return FAIL, f"connect failed: {data.get('error')}"


@probe("A8", "A", "negative control: the same mount fails without --host-uds",
       needs=("A7",))
def a8(ctx):
    wrapper = runsc_wrapper(ctx, "--ignore-cgroups", "nouds")
    mounts, guest_path = uds_mount(ctx, ctx.facts["uds_a"])
    result = podman_run(ctx, runtime=wrapper, network=guest_network(ctx), mounts=mounts,
                        argv=["python3", "-c", GUEST_UDS, guest_path])
    if not result.ok and not result.out.strip():
        return PASS, f"container refused the mount outright: {result.text[:200]}"
    data = guest_json(result)
    if data.get("connected"):
        return FAIL, ("host sockets are reachable without --host-uds=open, so A7 "
                      "proves nothing about the flag and default runs are exposed")
    return PASS, f"blocked as expected: {data.get('error')}"


@probe("A9", "A", "the guest cannot reach host or LAN services", needs=("A5",))
def a9(ctx):
    # Bound to named addresses, never 0.0.0.0, and closed before this
    # probe returns rather than at the end of the suite. A PONG server
    # is harmless; an exposed one outliving its purpose is sloppy.
    local = Stack()
    try:
        # One target per address. An earlier version also probed
        # 127.0.0.1 under a second label, which was the same connect
        # twice and made the report claim more than it measured.
        controls, targets = {}, []
        for label, addr in (("hosts_loopback_via_127001", "127.0.0.1"),
                            ("host_own_address", primary_address())):
            if not addr:
                continue
            server = serve(TcpPong, (addr, 0), local, f"close {label} control receiver")
            port = server.server_address[1]
            reachable, why = tcp_reachable(addr, port)
            controls[label] = why
            if reachable:
                targets.append([label, addr, port])
        if not targets:
            raise ProbeSkip(f"no positive control reachable from the host: {controls}")

        result = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                            argv=["python3", "-c", GUEST_NET, json.dumps(targets)])
        if not result.ok:
            return FAIL, f"container failed: {result.text[:300]}"
        tcp = guest_json(result)["tcp"]
        escaped = [k for k, v in tcp.items() if v == "connected"]
        detail = (f"network={guest_network(ctx)}; host controls {controls}; "
                  f"guest {tcp}")
        return (FAIL, detail) if escaped else (PASS, detail)
    finally:
        for failure in local.unwind():
            print(f"      cleanup: {failure}")


@probe("A10", "A", "the guest cannot reach the internet or resolve DNS",
       needs=("A5",))
def a10(ctx):
    if ctx.args.offline:
        raise ProbeSkip("--offline: the internet and DNS probes did not run")
    reachable, why = tcp_reachable("1.1.1.1", 443)
    if not reachable:
        raise ProbeSkip(f"host cannot reach 1.1.1.1:443 either ({why}); "
                        "without that control a guest failure proves nothing")
    result = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                        argv=["python3", "-c", GUEST_NET,
                              json.dumps([["public_literal_ip", "1.1.1.1", 443]])])
    if not result.ok:
        return FAIL, f"container failed: {result.text[:300]}"
    data = guest_json(result)
    detail = f"network={guest_network(ctx)}; tcp={data['tcp']} dns={data['dns']}"
    if data["tcp"]["public_literal_ip"] == "connected" or data["dns"] == "resolved":
        return FAIL, detail
    return PASS, detail


@probe("A11", "A", "two runs cannot reach each other's socket", needs=("A7",))
def a11(ctx):
    """Run B reaches its own endpoint and not run A's host path. That is
    ENOENT-level isolation — A's path is not in B's mount set — so it
    confirms the layout rather than the runtime. A12 is the sharp test.

    Run B gets holder B, because that is what the design gives it."""
    sock_dir = ctx.tmp / "uds-b"
    sock_dir.mkdir(mode=0o700, exist_ok=True)
    path_b = sock_dir / "p.sock"
    serve(UnixPong, str(path_b), ctx.cleanup, "close run B's UDS receiver")
    path_b.chmod(0o600)

    mounts, guest_path = uds_mount(ctx, path_b)
    result = podman_run(ctx, runtime=uds_wrapper(ctx),
                        network=guest_network(ctx, "b"), mounts=mounts,
                        argv=["python3", "-c", GUEST_UDS, guest_path])
    if not result.out.strip():
        return FAIL, f"container failed: {result.text[:300]}"
    own = guest_json(result)

    peek = podman_run(ctx, runtime=uds_wrapper(ctx),
                      network=guest_network(ctx, "b"), mounts=mounts,
                      argv=["python3", "-c", GUEST_UDS, str(ctx.facts["uds_a"])])
    other = (guest_json(peek) if peek.out.strip()
             else {"connected": False, "error": peek.text[:120]})
    detail = f"own={own.get('connected')} other_run_host_path={other.get('connected')}"
    if own.get("connected") and not other.get("connected"):
        return PASS, detail + f" ({str(other.get('error', ''))[:80]})"
    return FAIL, detail


@probe("A12", "A", "blast radius of --host-uds=open across other mounts",
       needs=("A7",))
def a12(ctx):
    """Not an assumption with a direction — a fact the plan's step 2 gate
    depends on. A host socket that appears inside any mounted tree, not
    just the proxy endpoint, is what this measures."""
    workspace = ctx.tmp / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    stray = workspace / "stray.sock"
    serve(UnixPong, str(stray), ctx.cleanup, "close the stray workspace receiver")
    stray.chmod(0o600)

    readonly = ctx.tmp / "credentials"
    readonly.mkdir(mode=0o700, exist_ok=True)
    ro_sock = readonly / "cred.sock"
    serve(UnixPong, str(ro_sock), ctx.cleanup, "close the read-only mount receiver")
    ro_sock.chmod(0o600)

    mounts, _ = uds_mount(ctx, ctx.facts["uds_a"])
    ctx.facts["workspace_dir"] = workspace
    result = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                        mounts=[*mounts, (str(workspace), "/workspace")],
                        extra=["-v", f"{readonly}:/creds:ro"],
                        argv=["python3", "-c", GUEST_UDS, "/workspace/stray.sock"])
    data = guest_json(result) if result.out.strip() else {"connected": False}

    ro_result = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                           mounts=mounts, extra=["-v", f"{readonly}:/creds:ro"],
                           argv=["python3", "-c", GUEST_UDS, "/creds/cred.sock"])
    ro_data = guest_json(ro_result) if ro_result.out.strip() else {"connected": False}

    detail = (f"writable workspace mount={data.get('connected')}; "
              f"read-only mount={ro_data.get('connected')}")
    if data.get("connected") or ro_data.get("connected"):
        return FAIL, (detail + " — a host socket in an ordinary mount is reachable, so "
                      "the guest's only exit is not the proxy. By step 2's own gate "
                      "this fails until the exposure is bounded or accepted")
    return PASS, detail + " — no host socket outside the endpoint is reachable"


@probe("A13", "A", "pasta accepts the fallback options, and what podman injects",
       needs=("A3",))
def a13(ctx):
    """Only the step 3 fallback needs pasta, but the answer is free here
    and decides whether --dns-forward can be suppressed at all.

    Tried one option at a time as well as together: a flat rejection says
    the set is wrong without saying which member is, and this build
    already refused --no-dhcp-dns as "passt mode only" once.
    """
    # --map-guest-addr is in this list because the live argv from the
    # first host run carried one: --map-host-loopback none closes the
    # gateway path to the host, and leaves that one open.
    attempts = [
        "--map-host-loopback,none,--map-guest-addr,none,--ipv4-only",
        "--map-host-loopback,none,--map-guest-addr,none",
        "--map-host-loopback,none,--ipv4-only",
        "--ipv4-only",
    ]
    accepted, rejected, argv = None, {}, ""
    for options in attempts:
        before = {p for p in run(["pgrep", "-a", "pasta"]).out.splitlines()}
        started = podman_run(ctx, network=f"pasta:{options}", dns_none=False,
                             detach=True, argv=["sleep", "20"])
        if not started.ok:
            rejected[options] = started.text[-200:]
            continue
        container = started.out.strip()
        ctx.defer(f"remove pasta container {container[:12]}",
                  lambda cid=container: run(["podman", "rm", "-f", "--ignore", cid],
                                            timeout=30))
        time.sleep(2)
        after = {p for p in run(["pgrep", "-a", "pasta"]).out.splitlines()}
        new_argv = " ".join(sorted(after - before))
        if accepted is None:
            accepted, argv = options, new_argv
        run(["podman", "rm", "-f", "--ignore", container], timeout=30)
        if accepted == attempts[0]:
            break  # the full set worked; no need to bisect

    if accepted is None:
        return FAIL, f"this pasta build rejected every option set: {rejected}"
    ctx.facts["pasta_argv"] = argv  # in full, for the JSON report
    detail = f"accepted={accepted!r}"
    if rejected:
        detail += f" rejected={rejected}"
    if not argv:
        return NOTE, detail + "; could not identify the new pasta process to read its argv"

    # Every option here reaches the host or announces a resolver. Report
    # what podman actually built, not what we asked for.
    survivors = [flag for flag in ("--dns-forward", "--map-guest-addr",
                                   "--map-host-loopback", "--no-map-gw")
                 if flag in argv]
    still_mapping = [flag for flag in ("--dns-forward",) if flag in argv]
    if still_mapping or accepted != attempts[0]:
        return NOTE, (detail + f"; live pasta argv carries {survivors}, so step 3 must "
                      f"neutralise what it cannot remove — {argv[:400]}")
    return PASS, detail + f"; live argv: {argv[:400]}"


GUEST_LISTEN = r'''
import socket, sys
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(sys.argv[1])))
listener.listen(8)
print("listening", flush=True)
while True:
    conn, _ = listener.accept()
    conn.sendall(b"NEIGHBOUR\n")
    conn.close()
'''

GUEST_CONNECT = r'''
import json, socket, sys
sock = socket.socket()
sock.settimeout(5)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
    print(json.dumps({"connected": True,
                      "reply": sock.recv(32).decode("utf-8", "replace").strip()}))
except OSError as exc:
    print(json.dumps({"connected": False, "error": f"{type(exc).__name__}({exc.errno})"}))
'''


@probe("A15", "A", "a neighbouring run cannot reach this run's loopback services",
       needs=("A5f",))
def a15(ctx):
    """The plan's neighbouring-sandbox test: a guest development server
    binds the same 127.0.0.1 the shim uses, and no other run may reach it.

    Three measurements, because they can disagree and each says something
    different. The control has to come from *inside the listening
    sandbox* — `podman exec`, not a second container on the same holder.
    Under gVisor every sandbox has its own netstack built from the
    namespace, so loopback traffic may never leave the sentry at all, in
    which case a second container is isolated even when it shares the
    holder. That would make a same-holder container a broken control: it
    would report "unreachable" whether or not the boundary worked.
    """
    port = 18080
    listener = podman_run(ctx, runtime=uds_wrapper(ctx),
                          network=guest_network(ctx, "a"), detach=True,
                          argv=["python3", "-c", GUEST_LISTEN, str(port)])
    if not listener.ok:
        return FAIL, f"could not start the listener: {listener.text[:240]}"
    container = listener.out.strip()
    ctx.defer(f"remove loopback listener {container[:12]}",
              lambda: run(["podman", "rm", "-f", "--ignore", container], timeout=30))

    inside = None
    for _ in range(5):
        time.sleep(2)
        execed = run(["podman", "exec", container,
                      "python3", "-c", GUEST_CONNECT, str(port)], timeout=60)
        if execed.out.strip():
            inside = guest_json(execed)
            if inside.get("connected"):
                break
        else:
            inside = {"connected": False, "error": execed.text[:160]}
    if not inside or not inside.get("connected"):
        return FAIL, (f"positive control failed: the listening sandbox cannot reach "
                      f"its own listener ({inside}). Nothing this probe says about "
                      "any neighbour would mean anything")

    def reach_from(key):
        attempt = podman_run(ctx, runtime=uds_wrapper(ctx),
                             network=guest_network(ctx, key),
                             argv=["python3", "-c", GUEST_CONNECT, str(port)])
        return (guest_json(attempt) if attempt.out.strip()
                else {"connected": False, "error": attempt.text[:120]})

    same_holder = reach_from("a")
    other_holder = reach_from("b")
    detail = (f"from inside={inside.get('reply')!r}; "
              f"second sandbox sharing holder A={same_holder.get('connected')}; "
              f"sandbox with its own holder={other_holder.get('connected')}")

    if other_holder.get("connected"):
        return FAIL, detail + " — a separate run reached this run's loopback service"
    if same_holder.get("connected"):
        return PASS, detail + (" — the namespace is what separates runs, so one holder "
                               "per run is load-bearing and must never be pooled")
    return PASS, detail + (" — gVisor gives each sandbox its own netstack, so guest "
                           "loopback does not cross even a shared holder. Per-run "
                           "holders remain required for modes with a real interface")


def exec_json(container, source, *args, timeout=60):
    """Run a guest program inside an already-running sandbox."""
    result = run(["podman", "exec", container, "python3", "-c", source, *args],
                 timeout=timeout)
    if not result.out.strip():
        return {"connected": False, "error": result.text[:160]}
    return guest_json(result)


@probe("A16", "A", "a host socket appearing after launch is reachable too",
       needs=("A7",))
def a16(ctx):
    """Decides whether a launch-time scan of the mounts could ever be a
    mitigation. If a socket bound after the sandbox starts is reachable,
    a scan checks a condition that does not hold for the rest of the
    session, and the exposure has to be bounded some other way.

    Depends on A7, not A12: this is the probe that matters most when A12
    fails, so it must not be skipped by A12 failing.
    """
    workspace = ctx.facts.get("workspace_dir") or ctx.tmp / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    mounts, guest_path = uds_mount(ctx, ctx.facts["uds_a"])
    started = podman_run(ctx, runtime=uds_wrapper(ctx), network=guest_network(ctx),
                         mounts=[*mounts, (str(workspace), "/workspace")],
                         detach=True, argv=["sleep", "300"])
    if not started.ok:
        return FAIL, f"could not start the sandbox: {started.text[:240]}"
    container = started.out.strip()
    ctx.defer(f"remove late-socket sandbox {container[:12]}",
              lambda: run(["podman", "rm", "-f", "--ignore", container], timeout=30))

    # Bound only now, with the sandbox already running.
    late = workspace / "late.sock"
    serve(UnixPong, str(late), ctx.cleanup, "close the late workspace receiver")
    late.chmod(0o600)

    data = exec_json(container, GUEST_UDS, "/workspace/late.sock")
    detail = f"bound after launch, reachable={data.get('connected')}"
    if data.get("connected"):
        return FAIL, (detail + " — a launch-time scan of the mounts cannot bound this; "
                      "the host user starting a service mid-session opens a path")
    return PASS, detail + f" ({str(data.get('error', ''))[:80]})"


@probe("A17", "A", "whether runsc can scope host UDS access to one path",
       needs=("A2",))
def a17(ctx):
    """The question the whole gate turns on. --host-uds takes a mode, not
    a path list, but check this build rather than the documentation."""
    helped = run([ctx.facts["runsc"], "help"], timeout=30)
    flags = run([ctx.facts["runsc"], "flags"], timeout=30)
    text = helped.text + flags.text
    uds_flags = sorted({line.strip().split()[0] for line in text.splitlines()
                        if "uds" in line.lower() and line.strip().startswith("-")})
    # Does it take a path at all, or only the documented modes?
    attempt = run([ctx.facts["runsc"], f"--host-uds=/run/only/this.sock", "--version"])
    detail = f"uds-related flags: {uds_flags or 'none found'}; a path value is "
    detail += "accepted" if attempt.ok else f"rejected ({attempt.text[:100]})"
    if attempt.ok:
        return NOTE, detail + " — investigate whether it really scopes, or is ignored"
    return NOTE, (detail + " — so the mode is sandbox-wide and scoping must come from "
                  "what the mounts expose, not from the runtime")


@probe("A18", "A", "what a holder's death does to a running sandbox",
       needs=("A5f",))
def a18(ctx):
    """The plan claims losing the holder fails closed. Check it: gVisor
    builds its netstack at startup, so the sandbox may simply carry on
    with a loopback whose namespace no longer exists."""
    holder = netns_holder(ctx, "doomed")
    started = podman_run(ctx, runtime=uds_wrapper(ctx),
                         network=f"container:{holder}", detach=True,
                         argv=["python3", "-c", GUEST_LISTEN, "18081"])
    if not started.ok:
        return NOTE, f"could not start the sandbox: {started.text[:240]}"
    container = started.out.strip()
    ctx.defer(f"remove holder-death sandbox {container[:12]}",
              lambda: run(["podman", "rm", "-f", "--ignore", container], timeout=30))
    time.sleep(3)

    before = exec_json(container, GUEST_CONNECT, "18081")
    if not before.get("connected"):
        return NOTE, f"control failed before killing the holder: {before}"

    killed = run(["podman", "rm", "-f", "--ignore", holder], timeout=60)
    ctx.facts.get("holders", {}).pop("doomed", None)
    time.sleep(3)
    after = exec_json(container, GUEST_CONNECT, "18081")
    escape = exec_json(container, GUEST_NET,
                       json.dumps([["public_literal_ip", "1.1.1.1", 443]]))
    reached = escape.get("tcp", {}).get("public_literal_ip")
    detail = (f"holder removed ({killed.ok}); guest loopback after="
              f"{after.get('connected')}; egress after={reached}")
    if reached == "connected":
        return FAIL, detail + " — losing the holder opened a network path"
    if after.get("connected"):
        return NOTE, (detail + " — the sandbox carries on: gVisor's netstack outlives "
                      "the namespace, so a dead holder is a cleanup leak, not a "
                      "fail-closed event. Correct the plan's claim")
    return NOTE, detail + " — the guest loses its loopback, as the plan assumes"


@probe("A14", "A", "per-run socket paths fit in sun_path")
def a14(ctx):
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    # The shape step 4 proposes: one private directory per run, holding
    # one socket, named by an unguessable run id.
    example = f"{runtime_dir}/llm-sandbox/{'r' * 16}/proxy.sock"
    size = len(example.encode())
    detail = f"{example} = {size}B of {SUN_PATH_MAX}"
    return (PASS, detail) if size <= SUN_PATH_MAX else (FAIL, detail)


# ---------------------------------------------------------------------
# Tier B: needs sudo. These decide whether the step 3 fallback is real.
# Firewalld changes here are runtime-only and reverted; a reload also
# undoes them.
# ---------------------------------------------------------------------

def connect_from_ns(ctx, port):
    """One TCP connect from inside the probe namespace to the host end
    of its veth. Returns the guest's own words, so a refusal and a
    timeout stay distinguishable in the report."""
    return sudo(["ip", "netns", "exec", ctx.facts["ns"], "python3", "-c",
                 NS_TCP_PROBE, ctx.facts["host_ip"], str(port)], timeout=30).text


@probe("B0", "B", "an unused /30 exists and sudo works")
def b0(ctx):
    if not ctx.args.sudo_probes:
        raise ProbeSkip("run with --sudo-probes")
    check = sudo(["true"])
    if not check.ok:
        raise ProbeSkip("passwordless sudo unavailable; run `sudo -v` first")

    routes = run(["ip", "-4", "route", "show", "table", "all"]).out
    taken = []
    for line in routes.splitlines():
        head = line.split()[0]
        try:
            taken.append(ipaddress.ip_network(head, strict=False))
        except ValueError:
            continue
    for _ in range(64):
        candidate = ipaddress.ip_network(f"10.101.{random.randint(1, 254)}.0/30")
        if not any(candidate.overlaps(net) for net in taken):
            ctx.facts["subnet"] = candidate
            hosts = list(candidate.hosts())
            ctx.facts["host_ip"], ctx.facts["ns_ip"] = str(hosts[0]), str(hosts[1])
            return PASS, f"{candidate} free of {len(taken)} existing routes"
    return FAIL, "no free /30 found in 10.101.0.0/16"


@probe("B1", "B", "a new veth lands in firewalld's default zone", needs=("B0",))
def b1(ctx):
    # IFNAMSIZ leaves 15 usable characters, so a 12-hex run id fits with
    # a 3-character prefix. Length is not the safeguard though: nothing
    # is scheduled for deletion until this probe created it, and a name
    # already in use stops the run rather than being adopted.
    ns = f"sbxprobe-{ctx.runid}"
    host_if = f"sb0{ctx.runid}"
    ns_if = f"sb1{ctx.runid}"
    ctx.facts.update(ns=ns, host_if=host_if, ns_if=ns_if)

    taken = []
    if ns in run(["ip", "netns", "list"]).out:
        taken.append(ns)
    for name in (host_if, ns_if):
        if run(["ip", "link", "show", name]).ok:
            taken.append(name)
    if taken:
        return FAIL, f"names already in use, refusing to touch them: {taken}"

    created = sudo(["ip", "netns", "add", ns])
    if not created.ok:
        return FAIL, f"ip netns add {ns}: {created.text[:200]}"
    ctx.defer(f"delete namespace {ns}", lambda: sudo(["ip", "netns", "del", ns]))

    made = sudo(["ip", "link", "add", host_if, "type", "veth", "peer", "name", ns_if])
    if not made.ok:
        return FAIL, f"ip link add {host_if}: {made.text[:200]}"
    ctx.defer(f"delete veth {host_if}", lambda: sudo(["ip", "link", "del", host_if]))

    steps = [
        ["ip", "link", "set", ns_if, "netns", ns],
        ["ip", "addr", "add", f"{ctx.facts['host_ip']}/30", "dev", host_if],
        ["ip", "link", "set", host_if, "up"],
        ["ip", "-n", ns, "addr", "add", f"{ctx.facts['ns_ip']}/30", "dev", ns_if],
        ["ip", "-n", ns, "link", "set", ns_if, "up"],
        ["ip", "-n", ns, "link", "set", "lo", "up"],
        ["ip", "-n", ns, "route", "add", "default", "via", ctx.facts["host_ip"]],
    ]
    for step in steps:
        result = sudo(step)
        if not result.ok:
            return FAIL, f"{' '.join(step)}: {result.text[:200]}"

    zone = sudo(["firewall-cmd", f"--get-zone-of-interface={host_if}"]).text
    ctx.facts["veth_zone"] = zone
    return PASS, (f"{host_if} <-> {ns}:{ns_if} on {ctx.facts['subnet']}; "
                  f"firewalld zone: {zone or 'none'}")


@probe("B2", "B", "firewalld blocks a veth, and an own-table accept does not help",
       needs=("B1",))
def b2(ctx):
    # Bound to this run's own /30 only, and shared with B3 and B5, which
    # test the same path under changing rules.
    server = serve(TcpPong, (ctx.facts["host_ip"], 0), ctx.cleanup,
                   "close the veth control receiver")
    port = server.server_address[1]
    ctx.facts["b_port"] = port

    baseline = connect_from_ns(ctx, port)

    table = f"sbxprobe{ctx.runid}"
    ctx.facts["table"] = table
    if sudo(["nft", "list", "table", "inet", table]).ok:
        return FAIL, f"table inet {table} already exists; refusing to reuse or delete it"
    rule =(f'iifname "{ctx.facts["host_if"]}" ip saddr {ctx.facts["ns_ip"]} '
            f'tcp dport {port} accept')
    ruleset = (f"table inet {table} {{\n"
               f"  chain input {{\n"
               f"    type filter hook input priority filter - 10; policy accept;\n"
               f"    {rule}\n"
               f"  }}\n"
               f"}}\n")
    added = sudo(["nft", "-f", "-"], stdin=ruleset)
    if not added.ok:
        return FAIL, f"nft load failed: {added.text[:300]}"
    ctx.defer(f"delete table inet {table}",
              lambda: sudo(["nft", "delete", "table", "inet", table]))
    with_accept = connect_from_ns(ctx, port)

    detail = f"baseline={baseline!r} own_table_accept={with_accept!r}"
    if "connected" in baseline:
        return NOTE, detail + " — this veth was reachable without a firewalld rule; "\
                              "check which zone accepted it before relying on step 3"
    if "connected" in with_accept:
        return FAIL, detail + " — an accept in a separate table DID override firewalld"
    return PASS, detail + " — as the plan assumes: the permission must come from firewalld"


@probe("B3", "B", "a firewalld runtime permission opens it, and an own-table drop wins",
       needs=("B2",))
def b3(ctx):
    port = ctx.facts["b_port"]
    rich = (f'rule family="ipv4" source address="{ctx.facts["ns_ip"]}/32" '
            f'destination address="{ctx.facts["host_ip"]}/32" '
            f'port port="{port}" protocol="tcp" accept')
    zone = ctx.facts.get("veth_zone") or "public"
    # Runtime only. No --permanent anywhere in this file: a reload is
    # always enough to undo whatever this probe did.
    added = sudo(["firewall-cmd", f"--zone={zone}", f"--add-rich-rule={rich}"])
    if not added.ok:
        return FAIL, f"could not add runtime rich rule: {added.text[:200]}"
    ctx.defer(f"remove runtime rich rule from zone {zone}",
              lambda: sudo(["firewall-cmd", f"--zone={zone}",
                            f"--remove-rich-rule={rich}"]))
    permitted = connect_from_ns(ctx, port)

    # Now the half the plan actually relies on: a drop in our own table,
    # at a lower priority number than firewalld's chains, has to beat
    # both the firewalld accept above and its established-state rule.
    table = ctx.facts["table"]
    drop = (f"table inet {table} {{\n"
            f"  chain drop_first {{\n"
            f"    type filter hook input priority filter - 20; policy accept;\n"
            f'    iifname "{ctx.facts["host_if"]}" drop\n'
            f"  }}\n"
            f"}}\n")
    loaded = sudo(["nft", "-f", "-"], stdin=drop)
    if not loaded.ok:
        return FAIL, f"nft drop chain failed: {loaded.text[:300]}"
    dropped = connect_from_ns(ctx, port)

    detail = f"with_firewalld_rule={permitted!r} then_own_drop={dropped!r}"
    if "connected" not in permitted:
        return FAIL, detail + " — the firewalld permission did not open the path"
    if "connected" in dropped:
        return FAIL, detail + " — the earlier drop did NOT pre-empt firewalld"
    return PASS, detail


@probe("B4", "B", "rootless podman and pasta stay inside a root-created netns",
       needs=("B1",))
def b4(ctx):
    ns = ctx.facts["ns"]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    user = os.environ.get("USER") or str(os.getuid())
    inner_inode = sudo(["ip", "netns", "exec", ns, "readlink", "/proc/self/ns/net"]).text

    before = {line.split()[0] for line in run(["pgrep", "-a", "pasta"]).out.splitlines()}
    name = ctx.name("nsrun")
    launch = sudo([
        "ip", "netns", "exec", ns,
        "runuser", "-u", user, "--",
        "env", f"XDG_RUNTIME_DIR={runtime_dir}", f"HOME={Path.home()}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus",
        "podman", "run", "-d", "--rm", "--pull=never",
        "--label", f"sbxprobe={ctx.runid}", "--name", name,
        "--userns=keep-id", "--security-opt", "label=disable",
        "--network=pasta:--map-host-loopback,none,--ipv4-only,--no-dhcp-dns",
        ctx.args.image, "sleep", "20",
    ], timeout=180)
    ctx.defer(f"remove namespace container {name}",
              lambda: run(["podman", "rm", "-f", "--ignore", name], timeout=30))
    if not launch.ok:
        return FAIL, ("rootless podman would not start inside the namespace — the "
                      f"step 3 fallback needs solving before anything else: {launch.text[:300]}")
    time.sleep(2)
    after = {line.split()[0] for line in run(["pgrep", "-a", "pasta"]).out.splitlines()}
    new = after - before
    if not new:
        return NOTE, "container started, but no new pasta process was identifiable"
    placements = {}
    for pid in new:
        placements[pid] = run(["readlink", f"/proc/{pid}/ns/net"]).text
    outside = {p: v for p, v in placements.items() if v and v != inner_inode}
    detail = f"netns={inner_inode} pasta={placements}"
    if outside:
        return FAIL, detail + " — pasta stayed outside the boundary"
    return PASS, detail


@probe("B5", "B", "the own table survives firewall-cmd --reload", needs=("B3",))
def b5(ctx):
    if not ctx.args.reload_firewall:
        raise ProbeSkip("mutates the whole host briefly; pass --reload-firewall")
    reloaded = sudo(["firewall-cmd", "--reload"], timeout=120)
    if not reloaded.ok:
        return FAIL, f"reload failed: {reloaded.text[:200]}"
    time.sleep(1)
    listed = sudo(["nft", "list", "table", "inet", ctx.facts["table"]])
    zone = sudo(["firewall-cmd",
                 f"--get-zone-of-interface={ctx.facts['host_if']}"]).text
    detail = f"table_present={listed.ok} veth_zone_after_reload={zone or 'none'}"
    if not listed.ok:
        return FAIL, detail + " — a reload removed the sandbox table"
    return PASS, detail + " — runtime rich rule is gone, as expected"


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

def emit(result, use_colour):
    colours = {PASS: "\033[32m", FAIL: "\033[31m", ERROR: "\033[31m",
               SKIP: "\033[90m", NOTE: "\033[33m", MOOT: "\033[90m"}
    tag = (f"{colours.get(result.status, '')}{result.status:<4}\033[0m"
           if use_colour else f"{result.status:<4}")
    print(f"{tag}  {result.pid:<3} {result.title}")
    if result.detail:
        for line in wrap(result.detail):
            print(f"            {line}")


def wrap(text, width=88):
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def startup_gate(facts):
    """Refuse to run at all unless podman here is local, rootless and
    ours. This runs before the first resource exists, because the real
    hazard is not a wrong result: it is cleanup aiming `podman rm -f`
    and `nft delete table` at a machine or account this script never
    meant to touch. Returns a reason to refuse, or None."""
    if os.geteuid() == 0:
        return ("running as root. Every assumption here is about rootless "
                "podman as your ordinary user; run it without sudo.")
    brokered = [name for name in ("CONTAINER_HOST", "DOCKER_HOST",
                                  "CONTAINER_CONNECTION")
                if os.environ.get(name)]
    if brokered:
        return (f"{', '.join(brokered)} set in the environment. That points "
                "podman at another machine, including this script's cleanup.")
    info = run(["podman", "info", "--format", "json"], timeout=60)
    if not info.ok:
        return f"podman info failed: {info.text[:200]}"
    try:
        host = json.loads(info.out)["host"]
    except (ValueError, KeyError) as exc:
        return f"could not parse podman info: {exc}"
    if host.get("serviceIsRemote"):
        return "podman reports serviceIsRemote=true; this must run against local podman."
    if not host.get("security", {}).get("rootless"):
        return "podman is not rootless here; the plan's assumptions are about rootless runs."
    facts["podman_host"] = host
    facts["podman_version"] = run(["podman", "--version"]).text
    return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default="localhost/llm-sandbox:latest")
    parser.add_argument("--runsc", help="path to runsc, if not on PATH")
    parser.add_argument("--sudo-probes", action="store_true",
                        help="also run the tier B probes for the namespace "
                             "fallback; needs sudo, containers stay unprivileged")
    parser.add_argument("--reload-firewall", action="store_true",
                        help="allow B5 to reload firewalld; affects the whole host")
    parser.add_argument("--offline", action="store_true",
                        help="skip the probe that connects to 1.1.1.1 and resolves DNS")
    parser.add_argument("--json", metavar="PATH",
                        help="write results here; an existing file is kept as PATH.bak")
    parser.add_argument("--keep", action="store_true",
                        help="leave every resource in place and print how to remove it")
    args = parser.parse_args()

    if not shutil.which("podman"):
        print("host_assumptions: no podman here. This script must run on the host, "
              "not inside the sandbox.", file=sys.stderr)
        return 2

    facts = {}
    refusal = startup_gate(facts)
    if refusal:
        print(f"host_assumptions: refusing to run — {refusal}", file=sys.stderr)
        return 2

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    base = runtime_dir if Path(runtime_dir).is_dir() else tempfile.gettempdir()
    runid = "".join(random.choices("0123456789abcdef", k=12))
    tmp = Path(tempfile.mkdtemp(prefix=f"sbxprobe-{runid}-", dir=base))
    ctx = Ctx(args=args, runid=runid, tmp=tmp)
    ctx.facts.update(facts)

    print(f"host_assumptions: run {runid}, workspace {tmp}")
    print(f"                  image {args.image}, tier "
          f"{'A+B' if args.sudo_probes else 'A only'}\n")

    results = []
    finished = threading.Event()
    leaks = []

    def cleanup():
        if finished.is_set():
            return
        finished.set()

        if args.keep:
            # --keep means keep: unwinding the stack here would delete
            # the namespace and rules the flag exists to preserve.
            print("\nkept, remove by hand when finished:")
            for description in ctx.cleanup.describe():
                print(f"  - {description}")
            print(f"  - rm -rf {tmp}")
            print(f"  - podman rm -f $(podman ps -aq --filter label=sbxprobe={runid})")
            return

        leaks.extend(ctx.cleanup.unwind())
        stale = run(["podman", "ps", "-aq", "--filter", f"label=sbxprobe={runid}"])
        for cid in stale.out.split():
            removed = run(["podman", "rm", "-f", "--ignore", cid], timeout=30)
            if not removed.ok:
                leaks.append(f"remove container {cid[:12]}: {removed.text[:160]}")
        try:
            shutil.rmtree(tmp)
        except OSError as exc:
            leaks.append(f"remove {tmp}: {exc}")

    def on_signal(signum, _frame):
        cleanup()
        sys.exit(130)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        for item in PROBES:
            if item.tier == "B" and not args.sudo_probes:
                results.append(Result(item.pid, item.title, SKIP, "tier B not requested"))
                emit(results[-1], sys.stdout.isatty())
                continue
            try:
                ctx.need(*item.needs)
                status, detail = item.fn(ctx)
            except ProbeSkip as exc:
                status, detail = SKIP, str(exc)
            except MootProbe as exc:
                status, detail = MOOT, str(exc)
            except Exception as exc:
                # A broken probe is not a host verdict, but it is not a
                # pass either, and it must not be quietly filed with the
                # deliberate skips.
                status, detail = ERROR, f"probe raised {type(exc).__name__}: {exc}"
            if status == PASS:
                ctx.passed.add(item.pid)
            results.append(Result(item.pid, item.title, status, detail))
            emit(results[-1], sys.stdout.isatty())
    finally:
        cleanup()

    counts = {s: sum(1 for r in results if r.status == s)
              for s in (PASS, FAIL, ERROR, SKIP, NOTE, MOOT)}
    print(f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[ERROR]} errored, "
          f"{counts[SKIP]} skipped, {counts[NOTE]} noted, {counts[MOOT]} moot")

    for tier, gates in (("A", "step 2, the UDS transport"),
                        ("B", "step 3, the namespace fallback")):
        members = [r for r in results if r.pid.startswith(tier)]
        unproven = [r.pid for r in members if r.status not in SETTLED]
        verdict = "proven" if not unproven else f"NOT proven — {', '.join(unproven)}"
        print(f"  tier {tier} ({gates}): {verdict}")

    notes = [r for r in results if r.status == NOTE]
    if notes:
        print("\nDecisions these force in the plan:")
        for note in notes:
            print(f"  {note.pid}: {note.title}")

    if leaks:
        print("\nCLEANUP INCOMPLETE — these outlive this process and no firewall "
              "reload will remove them:")
        for leak in leaks:
            print(f"  {leak}")

    if args.json:
        target = Path(args.json)
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup)
            print(f"\nkept the previous report as {backup}")
        target.write_text(json.dumps({
            "runid": runid,
            "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "image": args.image,
            "argv": sys.argv[1:],
            "facts": {k: str(v) for k, v in ctx.facts.items()},
            "leaks": leaks,
            "results": [r.__dict__ for r in results],
        }, indent=2, sort_keys=True) + "\n")
        print(f"wrote {target}")

    return 1 if (counts[FAIL] or counts[ERROR] or leaks) else 0


if __name__ == "__main__":
    sys.exit(main())
