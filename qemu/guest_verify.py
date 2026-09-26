"""Small acceptance checks against the actual launcher configuration."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import subprocess


def sqlite_wal_works(directory):
    """Exercise WAL mapping and a reader while a writer has uncommitted data."""
    with tempfile.TemporaryDirectory(prefix='wal-check-', dir=directory) as probe:
        database = str(Path(probe)/'probe.sqlite')
        writer = sqlite3.connect(database)
        reader = sqlite3.connect(database)
        try:
            if writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                return False
            writer.execute('CREATE TABLE probe (value TEXT)')
            writer.execute("INSERT INTO probe VALUES ('committed')")
            writer.commit()
            writer.execute("INSERT INTO probe VALUES ('pending')")
            before = reader.execute('SELECT value FROM probe').fetchall()
            writer.commit()
            after = reader.execute('SELECT value FROM probe').fetchall()
            return before == [('committed',)] and after == [('committed',), ('pending',)]
        finally:
            reader.close()
            writer.close()


def agent_versions(environment):
    checks = {}
    versions = {}
    for agent in ('claude', 'codex', 'pi', 'omp', 'opencode'):
        try:
            result = subprocess.run([agent, '--version'], env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=60, check=True)
            versions[agent] = result.stdout.strip()
            checks[agent+'_version'] = bool(versions[agent])
        except (OSError, subprocess.SubprocessError) as exc:
            checks[agent+'_version'] = False
            print(f'{agent} version check failed: {exc}', flush=True)
    return checks, versions


# Everything the retired container image provided besides the agents. The
# build records these versions and the candidate boot check re-runs them,
# so a tool that silently fails to install blocks activation.
TOOLS = {
    'bash': ['bash', '--version'], 'zsh': ['zsh', '--version'],
    'git': ['git', '--version'], 'rg': ['rg', '--version'],
    'jq': ['jq', '--version'], 'tree': ['tree', '--version'],
    'vim': ['vim', '--version'], 'nvim': ['nvim', '--version'],
    'task': ['task', '--version'], 'sqlite3': ['sqlite3', '--version'],
    'caddy': ['caddy', 'version'], 'bwrap': ['bwrap', '--version'],
    'ip': ['ip', '-V'], 'openssl': ['openssl', 'version'],
    'shasum': ['shasum', '--version'], 'curl': ['curl', '--version'],
    'gcc': ['gcc', '--version'], 'make': ['make', '--version'],
    'python3': ['python3', '--version'], 'pip': ['pip', '--version'],
    'go': ['go', 'version'], 'node': ['node', '--version'], 'npm': ['npm', '--version'],
    'rustup': ['rustup', '--version'], 'rustc': ['rustc', '--version'],
    'cargo': ['cargo', '--version'], 'playwright': ['playwright', '--version'],
}
TOOLS_RECORD = Path('/etc/sandbox-tools.json')
SYMBOL_FONT_RULE = Path('/etc/fonts/conf.d/99-symbol-fallback.conf')
SYMBOL_FONTS = ('Noto Sans Symbols', 'Noto Sans Symbols 2', 'Noto Color Emoji',
                'DejaVu Sans', 'Liberation Sans')


def first_line(result):
    for line in (result.stdout+'\n'+result.stderr).splitlines():
        if line.strip():
            return line.strip()
    return ''


def tool_versions(environment):
    checks, versions = {}, {}
    for name, command in TOOLS.items():
        try:
            result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=60, check=True)
            versions[name] = first_line(result)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f'{name} version check failed: {exc}', flush=True)
        checks['tool:'+name] = bool(versions.get(name))
    try:
        components = subprocess.run(['rustup', 'component', 'list', '--installed'], env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=60, check=True).stdout.split()
        checks['rust_src'] = any(item.startswith('rust-src') for item in components)
    except (OSError, subprocess.SubprocessError):
        checks['rust_src'] = False
    try:
        families = subprocess.run(['fc-list', ':', 'family'], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=60, check=True).stdout
        installed = {name.strip() for line in families.splitlines() for name in line.split(',')}
        checks['symbol_fonts'] = SYMBOL_FONT_RULE.is_file() and set(SYMBOL_FONTS) <= installed
    except (OSError, subprocess.SubprocessError):
        checks['symbol_fonts'] = False
    return checks, versions


def browser_smoke(environment, output):
    """Launch both baked browsers; the screenshots are for a human to inspect."""
    output.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(['browser-smoke.mjs', str(output)], env=environment, stdin=subprocess.DEVNULL,
                       timeout=300, check=True)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'browser smoke test failed: {exc}', flush=True)
        return False


def omp_sqlite_checks(mounts):
    checks = {}
    for mount in mounts:
        target = Path(mount['target'])
        if target == Path('/home/fedora/.omp'):
            # Use a disposable database on the actual state filesystem;
            # never open credentials or change an existing database.
            try:
                checks['omp_sqlite_wal'] = sqlite_wal_works(target)
            except (OSError, sqlite3.Error) as exc:
                checks['omp_sqlite_wal'] = False
                print(f'OMP SQLite WAL check failed: {exc}', flush=True)
    return checks


def verify(mounts, sqlite_home, environment=None, image=False):
    checks, versions = agent_versions(environment) if image else ({}, {})
    tools = {}
    # Images built before the toolchain was added have no record; the image
    # manager requires these checks only for manifests that list tools.
    if image and TOOLS_RECORD.is_file():
        tool_checks, tools = tool_versions(environment)
        checks.update(tool_checks)
        checks['browser_smoke'] = browser_smoke(environment, Path('/workspace/browser-smoke'))
    checks.update(omp_sqlite_checks(mounts))
    checks['uid_1000'] = os.getuid() == 1000
    try:
        checks['codex_sqlite_wal'] = sqlite_wal_works(sqlite_home)
    except (OSError, sqlite3.Error):
        checks['codex_sqlite_wal'] = False
    rows = Path('/proc/net/route').read_text().splitlines()[1:]
    checks['one_default_route'] = sum(row.split()[1] == '00000000' for row in rows) == 1
    try:
        with socket.create_connection(('10.0.2.100', 3128), timeout=5) as proxy:
            proxy.sendall(b'CONNECT denied.invalid:443 HTTP/1.1\r\n\r\n')
            checks['unlisted_connect_denied'] = b' 403 ' in proxy.recv(4096)
    except OSError:
        checks['unlisted_connect_denied'] = False
    for name, address in [('direct_public_tcp_blocked', ('1.1.1.1', 443)),
                          ('host_alias_tcp_blocked', ('10.0.2.2', 22))]:
        try:
            with socket.create_connection(address, timeout=2):
                checks[name] = False
        except OSError:
            checks[name] = True
    for target in ['/workspace', *[m['target'] for m in mounts]]:
        checks['writable:'+target] = os.access(target, os.W_OK)
    (Path('/mnt/report')/'verify.json').write_text(json.dumps({
        'checks': checks,
        'versions': versions,
        'tools': tools,
        'limits': 'Direct TCP failures are smoke checks, not proof of complete isolation; run the host boundary suite.',
    }, indent=2))
    return 0 if all(checks.values()) else 1
