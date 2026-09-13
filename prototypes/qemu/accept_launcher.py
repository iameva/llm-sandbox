#!/usr/bin/env python3
"""Run two concurrent installed-launcher probes with disposable state.

No sudo, downloads, real credentials, or project exports. Each VM uses 2 GiB.
The disk must be the already built five-agent image.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import struct
import sys
import termios
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qemu.runtime_support import disk_cache, private_directory

GUEST = r"""
import json, os, pathlib, socket, sqlite3, sys, time
key = sys.argv[1]
other = 'b' if key == 'a' else 'a'
terminal_checks = {}
# Both guests have ttyS0, including the guest whose host console is a log
# file. Only the explicitly selected interactive probe receives host resizes.
if '--terminal-probe' in sys.argv[3:]:
    if not os.isatty(0):
        raise RuntimeError('interactive terminal probe has no guest TTY')
    terminal_checks['initial_terminal_size'] = os.get_terminal_size(0) == (100, 30)
    pathlib.Path('/workspace/terminal-ready').write_text('ready')
    deadline = time.monotonic()+30
    while os.get_terminal_size(0) != (132, 44) and time.monotonic() < deadline:
        time.sleep(.2)
    terminal_checks['live_terminal_resize'] = os.get_terminal_size(0) == (132, 44)
share = pathlib.Path('/workspace')
state = pathlib.Path.home()/'.config/codex'
home_marker = pathlib.Path.home()/('private-'+key)
home_marker.write_text(key)
(state/key).write_text(key)
sqlite_home = pathlib.Path(os.environ['CODEX_SQLITE_HOME'])
database = sqlite3.connect(sqlite_home/'concurrent-probe.sqlite')
assert database.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
database.execute('CREATE TABLE markers (value TEXT)')
database.execute('INSERT INTO markers VALUES (?)', (key,))
database.commit()
reader = sqlite3.connect(sqlite_home/'concurrent-probe.sqlite')
database.execute('INSERT INTO markers VALUES (?)', (key+'-pending',))
wal_reader_ok = reader.execute('SELECT value FROM markers').fetchall() == [(key,)]
database.rollback()
reader.close()
listener = socket.socket()
listener.bind(('0.0.0.0',18080))
listener.listen()
def wait(path):
    deadline = time.monotonic()+120
    while not path.exists():
        if time.monotonic()>deadline:
            raise TimeoutError(str(path))
        time.sleep(.2)
wait(state/other)
checks = {
    **terminal_checks,
    'shared_agent_state': (state/other).read_text() == other,
    'private_home': not (pathlib.Path.home()/('private-'+other)).exists(),
    'uid_1000': os.getuid()==1000,
    'same_port_in_each_vm': True,
    'private_codex_sqlite': database.execute('SELECT value FROM markers').fetchall() == [(key,)],
    'codex_sqlite_wal_reader': wal_reader_ok,
    'codex_sqlite_outside_shared_state': str(sqlite_home.resolve()).startswith('/var/lib/llm-sandbox/'),
}
if (share/'benchmark').is_dir():
    checks['host_initial_contents'] = (share/'host-update').read_text() == 'before'
    (share/(key+'.host-ready')).write_text('ready')
    deadline = time.monotonic()+30
    while (share/'host-update').read_text() != 'after' and time.monotonic() < deadline:
        time.sleep(.1)
    checks['host_replacement_visible'] = (share/'host-update').read_text() == 'after'
    timings = []
    for iteration in range(3):
        started = time.monotonic()
        total = sum(len(path.read_bytes()) for path in sorted((share/'benchmark').iterdir()))
        timings.append(time.monotonic()-started)
    checks['benchmark_contents'] = total == 500*4096
    (share/(key+'.timings.json')).write_text(json.dumps(timings))
(share/(key+'.ready')).write_text('ready')
wait(share/(other+'.ready'))
if key == 'b':
    wait(share/'a-exited')
    delay = int(sys.argv[2])
    if delay:
        time.sleep(delay)
        checks['long_batch_survives'] = True
    checks['survives_a_exit'] = (state/'a').read_text()=='a'
(share/(key+'.json')).write_text(json.dumps(checks))
listener.close()
database.close()
"""


def collect_diagnostics(base):
    """Read bounded log tails without following links or opening special files."""
    paths = [base/'a.log', base/'b.log']
    for run in sorted((base/'runs').glob('run-*')):
        if not run.is_symlink():
            paths.extend(run/name for name in ('exit.json', 'console.log'))
    logs = {}
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    continue
                source.seek(max(0, info.st_size-8192))
                content = source.read(8192).decode(errors='replace')
            # JSON escapes control characters when printed, including terminal
            # escape sequences from the serial console.
            logs[str(path.relative_to(base))] = content
        except OSError as exc:
            logs[str(path.relative_to(base))] = f'Unavailable: {exc.strerror}'
    return logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', type=Path)
    parser.add_argument('--diagnose', type=Path,
                        help='read existing acceptance logs as JSON; no VM or files are created')
    parser.add_argument('--runtime-checks', action='store_true', help='also exercise initial terminal size, live resize and explicit overlay cleanup')
    parser.add_argument('--cache-benchmark', choices=['never', 'auto'],
                        help='benchmark a temporary installed runtime; normal launcher cache policy is unchanged')
    parser.add_argument('--long-batch-seconds', type=int, default=0,
                        help='keep guest B running after A exits; use 960 to exceed the old limit')
    args = parser.parse_args()
    if args.diagnose:
        print(json.dumps(collect_diagnostics(args.diagnose.resolve(strict=True)), indent=2))
        return 0
    if args.disk is None:
        parser.error('--disk is required unless --diagnose is used')
    if not 0 <= args.long_batch_seconds <= 86400:
        parser.error('--long-batch-seconds must be between 0 and 86400')
    if os.geteuid() == 0:
        parser.error('run without sudo')
    disk = args.disk.resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    cache = disk_cache(Path.home()/'.cache/llm-sandbox/qemu-acceptance')
    private_directory(cache)
    base = Path(tempfile.mkdtemp(prefix='qemu-launch-accept-', dir=cache))
    home, workspace = base/'home', base/'workspace'
    home.mkdir()
    workspace.mkdir()
    (workspace/'probe.py').write_text(GUEST)
    if args.cache_benchmark:
        (workspace/'host-update').write_text('before')
        (workspace/'benchmark').mkdir()
        for index in range(500):
            (workspace/'benchmark'/str(index)).write_bytes(b'x'*4096)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SANDBOX_')}
    env.update(HOME=str(home), SANDBOX_ISOLATION='qemu', SANDBOX_QEMU_DISK=str(disk),
               SANDBOX_BATCH='1', SANDBOX_QEMU_CACHE_DIR=str(base/'runs'),
               SANDBOX_QEMU_KEEP_ARTIFACTS='1')
    children, logs = [], []
    terminal = None
    drain = None
    failure = None
    print(f'Acceptance artifacts: {base}; starting two VMs (4 GiB RAM)', flush=True)
    try:
        subprocess.run(['sh', 'install.sh'], cwd=repo, env=env, check=True, timeout=30)
        (home/'.config/llm-sandbox/backends.json').write_text('{}')
        if args.cache_benchmark:
            runtime = home/'.config/llm-sandbox/qemu/sandbox.py'
            text = runtime.read_text()
            if text.count("'--cache=never'") != 2:
                raise RuntimeError('cache benchmark needs updating for this runtime')
            runtime.write_text(text.replace("'--cache=never'", f"'--cache={args.cache_benchmark}'"))
        for key in ('a', 'b'):
            log = (base/f'{key}.log').open('w')
            logs.append(log)
            child_env = dict(env)
            input_stream, output_stream = subprocess.DEVNULL, log
            if args.runtime_checks and key == 'a':
                terminal, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
                child_env['SANDBOX_BATCH'] = '0'
                input_stream = output_stream = slave
            child = subprocess.Popen([
                str(home/'.local/bin/,codex-sandbox.sh'), '--shell', '-c',
                f'exec python3 /workspace/probe.py {key} {args.long_batch_seconds if key == "b" else 0}'
                + (' --terminal-probe' if args.runtime_checks and key == 'a' else ''),
            ], cwd=workspace, env=child_env, stdin=input_stream, stdout=output_stream, stderr=output_stream,
               start_new_session=True)
            children.append(child)
            if args.runtime_checks and key == 'a':
                os.close(slave)
                def copy_console(target=log):
                    try:
                        while True:
                            data = os.read(terminal, 65536)
                            if not data:
                                break
                            target.write(data.decode(errors='replace'))
                            target.flush()
                    except OSError:
                        pass
                drain = threading.Thread(target=copy_console, daemon=True)
                drain.start()
        if args.runtime_checks:
            deadline = time.monotonic()+600
            while not (workspace/'terminal-ready').exists():
                if children[0].poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('terminal probe did not become ready; inspect a.log')
                time.sleep(.2)
            fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack('HHHH', 44, 132, 0, 0))
            os.kill(children[0].pid, signal.SIGWINCH)
        if args.cache_benchmark:
            deadline = time.monotonic()+600
            while not all((workspace/(key+'.host-ready')).exists() for key in ('a', 'b')):
                if any(child.poll() is not None for child in children) or time.monotonic() > deadline:
                    raise RuntimeError('cache probes did not become ready; inspect guest logs')
                time.sleep(.2)
            (workspace/'host-update.pending').write_text('after')
            (workspace/'host-update.pending').replace(workspace/'host-update')
        a_status = children[0].wait(timeout=600)
        if a_status:
            raise RuntimeError(f'instance A exited with status {a_status}')
        (workspace/'a-exited').write_text('exited')
        b_status = children[1].wait(timeout=180+args.long_batch_seconds)
        if b_status:
            raise RuntimeError(f'instance B exited with status {b_status}')
        reports = {key: json.loads((workspace/f'{key}.json').read_text()) for key in ('a','b')}
        checks = {f'{key}_{name}': value for key, report in reports.items() for name,value in report.items()}
        checks['host_workspace_ownership'] = all((workspace/f'{key}.json').stat().st_uid == os.getuid() for key in ('a','b'))
        runs = list((base/'runs').glob('run-*'))
        checks['disposable_overlays_removed'] = len(runs) == 2 and all(not (run/'overlay.qcow2').exists() for run in runs)
        for index, run in enumerate(runs):
            argv = json.loads((run/'launch.json').read_text())['argv']
            checks[f'explicit_overlay_{index}'] = ('-snapshot' not in argv and
                f'file={run}/overlay.qcow2,format=qcow2,if=virtio' in argv)
        if args.cache_benchmark:
            timings = {key: json.loads((workspace/(key+'.timings.json')).read_text()) for key in ('a', 'b')}
            print(json.dumps({'cache_policy': args.cache_benchmark, 'scan_seconds': timings}, indent=2))
            (base/'benchmark.json').write_text(json.dumps({'cache_policy': args.cache_benchmark, 'scan_seconds': timings}, indent=2))
        (base/'result.json').write_text(json.dumps(checks, indent=2))
        print(json.dumps(checks, indent=2))
        return 0 if all(checks.values()) else 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        failure = f'{type(exc).__name__}: {exc}'
        return 2
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        if terminal is not None:
            os.close(terminal)
        if drain:
            drain.join(timeout=5)
        for log in logs:
            log.close()
        if failure:
            report = {'error': failure, 'logs': collect_diagnostics(base)}
            (base/'failure.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2), file=sys.stderr)
        print(f'Artifacts retained at {base}')


if __name__ == '__main__':
    raise SystemExit(main())
