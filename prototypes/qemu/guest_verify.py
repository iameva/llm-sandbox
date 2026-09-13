"""Small acceptance checks against the actual launcher configuration."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile


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


def verify(mounts, sqlite_home):
    checks = {'uid_1000': os.getuid() == 1000}
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
        'limits': 'Direct TCP failures are smoke checks, not proof of complete isolation; run the host boundary suite.',
    }, indent=2))
    return 0 if all(checks.values()) else 1
