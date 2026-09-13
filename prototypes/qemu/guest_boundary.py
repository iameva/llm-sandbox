#!/usr/bin/env python3
"""Guest-root coordinator for the offline, two-VM boundary experiment."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

SHARE = Path('/workspace')


def wait_for(path):
    deadline = time.monotonic() + 180
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.2)


def connect(path, message):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        try:
            client.connect(str(path))
            client.sendall(message.encode())
            return {'connected': True, 'reply': client.recv(100).decode()}
        except OSError as exc:
            return {'connected': False, 'error': str(exc)}


def escapes(config):
    out = {}
    for name, path in [('absolute_symlink', SHARE/'absolute-link'),
                       ('relative_symlink', SHARE/'relative-link'),
                       ('parent_traversal', SHARE/'..'/'outside.txt')]:
        try:
            out[name] = {'read': path.read_text() == config['outside_token']}
        except OSError as exc:
            out[name] = {'read': False, 'error': str(exc)}
    return out


def user_tests(key, config, ready, release):
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    other = 'b' if key == 'a' else 'a'
    received = []
    stop = threading.Event()
    path = SHARE/f'{key}.sock'
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(4)
        listener.settimeout(0.2)
        def serve():
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                with peer:
                    peer.settimeout(3)
                    try:
                        data = peer.recv(100).decode()
                        received.append(data)
                        peer.sendall(data.encode())
                    except OSError:
                        pass
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        own = connect(path, 'self-'+key)
        if own.get('reply') != 'self-'+key:
            raise RuntimeError('same-guest socket control failed')
        (SHARE/f'{key}.ready').write_text('ready')
        wait_for(SHARE/'late.ready')
        result = {'own_socket': own, 'interfaces': sorted(os.listdir('/sys/class/net')),
                  'late_host_socket': connect(SHARE/'late.sock', 'guest-'+key),
                  'neighbour_socket': connect(SHARE/f'{other}.sock', 'cross-'+key),
                  'escapes': escapes(config)}
        # Keep both receivers alive until both guests have tried connecting.
        (SHARE/f'{key}.attempted').write_text('done')
        wait_for(SHARE/f'{other}.attempted')
        os.write(ready, b'1')
        if os.read(release, 1) != b'1':
            raise RuntimeError('root coordinator failed')
        (SHARE/f'{key}.root-done').write_text('done')
        wait_for(SHARE/f'{other}.root-done')
        time.sleep(0.5)
        stop.set()
        thread.join(timeout=4)
        result['received'] = received
        (SHARE/f'{key}.user.json').write_text(json.dumps(result))


def main():
    key = sys.argv[1]
    config = json.loads(Path('/mnt/seed/config.json').read_text())
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ready_read)
            os.close(release_write)
            user_tests(key, config, ready_write, release_read)
            os._exit(0)
        except Exception as exc:
            print(f'USER TEST ERROR: {exc}', flush=True)
            os._exit(1)
    os.close(ready_write)
    os.close(release_read)
    if os.read(ready_read, 1) != b'1':
        raise RuntimeError('unprivileged tests failed before root probes')
    root_results = {'uid': os.getuid(),
                    'late_host_socket': connect(SHARE/'late.sock', 'root-'+key),
                    'neighbour_socket': connect(SHARE/('b.sock' if key == 'a' else 'a.sock'), 'root-cross-'+key),
                    'escapes': escapes(config)}
    os.write(release_write, b'1')
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError('unprivileged tests did not finish')
    # Root is deliberately unmapped in virtiofsd. Persist reports using the
    # authorized filesystem UID, without weakening the host mapping.
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    (SHARE/f'{key}.root.json').write_text(json.dumps(root_results))
    print(json.dumps(root_results), flush=True)


if __name__ == '__main__':
    main()
