#!/usr/bin/env python3
"""Run as guest UID/GID 1000 after root mounts the virtiofs export."""
import json
import os
from pathlib import Path
import socket

root = Path('/workspace')
if (os.geteuid(), os.getegid()) != (1000, 1000):
    raise SystemExit('Run this check as guest UID/GID 1000.')
results = {'interfaces': sorted(os.listdir('/sys/class/net'))}
for label, path in [('host_socket', '/workspace/host.sock')]:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(3)
    try:
        sock.connect(path)
        sock.sendall(b'guest-probe\n')
        results[label] = {'connected': True, 'reply': sock.recv(100).decode()}
    except OSError as exc:
        results[label] = {'connected': False, 'error': str(exc)}
    finally:
        sock.close()
# Exercise ownership under the explicitly mapped, unprivileged identity.
text = (root / 'host.txt').read_text()
assert text == 'host control\n'
path = root / 'guest.tmp'
path.write_text('guest control\n')
path.rename(root / 'guest.txt')
probe = root / 'delete.tmp'
probe.write_text('delete me')
probe.unlink()
(root / 'guest-result.json').write_text(json.dumps(results, indent=2)+'\n')
print(json.dumps(results, indent=2))
print('File operations: PASS')
print('Power off the guest to let the host verify content and ownership.')
