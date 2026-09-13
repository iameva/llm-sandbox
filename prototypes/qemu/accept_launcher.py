#!/usr/bin/env python3
"""Run two concurrent installed-launcher probes with disposable state.

No sudo, downloads, real credentials, or project exports. Each VM uses 2 GiB.
The disk must be the already built five-agent image.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

GUEST = r"""
import json, os, pathlib, socket, sqlite3, sys, time
key = sys.argv[1]
other = 'b' if key == 'a' else 'a'
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
    'shared_agent_state': (state/other).read_text() == other,
    'private_home': not (pathlib.Path.home()/('private-'+other)).exists(),
    'uid_1000': os.getuid()==1000,
    'same_port_in_each_vm': True,
    'private_codex_sqlite': database.execute('SELECT value FROM markers').fetchall() == [(key,)],
    'codex_sqlite_wal_reader': wal_reader_ok,
    'codex_sqlite_outside_shared_state': str(sqlite_home.resolve()).startswith('/var/lib/llm-sandbox/'),
}
(share/(key+'.ready')).write_text('ready')
wait(share/(other+'.ready'))
if key == 'b':
    wait(share/'a-exited')
    checks['survives_a_exit'] = (state/'a').read_text()=='a'
(share/(key+'.json')).write_text(json.dumps(checks))
listener.close()
database.close()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run without sudo')
    disk = args.disk.resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    base = Path(tempfile.mkdtemp(prefix='qemu-launch-accept-'))
    home, workspace = base/'home', base/'workspace'
    home.mkdir()
    workspace.mkdir()
    (workspace/'probe.py').write_text(GUEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SANDBOX_')}
    env.update(HOME=str(home), SANDBOX_ISOLATION='qemu', SANDBOX_QEMU_DISK=str(disk),
               SANDBOX_BATCH='1')
    children, logs = [], []
    print(f'Acceptance artifacts: {base}; starting two VMs (4 GiB RAM)', flush=True)
    try:
        subprocess.run(['sh', 'install.sh'], cwd=repo, env=env, check=True, timeout=30)
        (home/'.config/llm-sandbox/backends.json').write_text('{}')
        for key in ('a', 'b'):
            log = (base/f'{key}.log').open('w')
            logs.append(log)
            child = subprocess.Popen([
                str(home/'.local/bin/,codex-sandbox.sh'), '--shell', '-c',
                f'exec python3 /workspace/probe.py {key}',
            ], cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
               start_new_session=True)
            children.append(child)
        if children[0].wait(timeout=600):
            raise RuntimeError('instance A failed; inspect a.log')
        (workspace/'a-exited').write_text('exited')
        if children[1].wait(timeout=180):
            raise RuntimeError('instance B failed; inspect b.log')
        reports = {key: json.loads((workspace/f'{key}.json').read_text()) for key in ('a','b')}
        checks = {f'{key}_{name}': value for key, report in reports.items() for name,value in report.items()}
        checks['host_workspace_ownership'] = all((workspace/f'{key}.json').stat().st_uid == os.getuid() for key in ('a','b'))
        (base/'result.json').write_text(json.dumps(checks, indent=2))
        print(json.dumps(checks, indent=2))
        return 0 if all(checks.values()) else 1
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        for log in logs:
            log.close()
        print(f'Artifacts retained at {base}')


if __name__ == '__main__':
    raise SystemExit(main())
