#!/usr/bin/env python3
"""Test the UDS isolation gate on the host, without sudo or public traffic.

Creates two gVisor sandboxes, two crun holders, and a temporary shared
workspace. No real project or credentials are mounted; images are never
pulled. Opens only host-loopback TCP and private Unix socket receivers.
Exit 1: boundary bypass demonstrated; 2: inconclusive/setup/cleanup error.
This falsification test never certifies isolation or returns success.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time


CLIENT = r'''
import json, socket, sys
family, address, token = json.loads(sys.argv[1])
s = socket.socket(socket.AF_UNIX if family == 'unix' else socket.AF_INET)
s.settimeout(3)
try:
    s.connect(address if family == 'unix' else tuple(address))
    s.sendall((token+'\n').encode())
    reply = s.recv(256).decode().strip()
    print(json.dumps({'reached': reply == token, 'reply': reply}))
except OSError as e:
    print(json.dumps({'reached': False, 'error': str(e)}))
finally:
    s.close()
'''
SERVER = r'''
import os, socket, sys
p = '/workspace/guest.sock'
s = socket.socket(socket.AF_UNIX)
try:
    s.bind(p)
    os.chmod(p, 0o600)
    s.listen(8)
    print('READY', flush=True)
    while True:
        c, _ = s.accept()
        with c:
            c.settimeout(3)
            data = c.recv(256)
            c.sendall(data)
except OSError as e:
    print('UNSUPPORTED: '+str(e), flush=True)
'''


def command(argv):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError(f'{shlex.join(argv[:5])}: {p.stderr.strip()[:500]}')
    return p.stdout.strip()


class Receiver(socketserver.ThreadingTCPServer):
    daemon_threads = True


class UnixReceiver(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Echo(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            data = self.rfile.readline(256)
            self.server.received.append(data.decode().strip())
            self.wfile.write(data)
        except OSError:
            pass


class Relay(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            data = self.rfile.readline(256)
            with socket.create_connection(self.server.target, timeout=3) as upstream:
                upstream.sendall(data)
                self.wfile.write(upstream.recv(256))
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='localhost/llm-sandbox:latest')
    parser.add_argument('--runsc', default=shutil.which('runsc'))
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run as your ordinary host user, without sudo')
    if any(k in os.environ for k in ('CONTAINER_HOST', 'DOCKER_HOST', 'CONTAINER_CONNECTION')):
        parser.error('unset Podman/Docker remote environment settings first')
    podman = shutil.which('podman')
    if not podman or not args.runsc:
        parser.error('host podman and runsc are required')
    pd = [podman, '--remote=false']
    root = None
    owned = []
    servers = []
    result = 2
    runid = secrets.token_hex(12)
    label = f'uds-boundary={runid}'

    def start_server(cls, address, handler):
        server = cls(address, handler)
        server.received = []
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def launch(name, runtime, network, mounts, program):
        # Register unique names before launch so timeouts can be cleaned up;
        # cleanup verifies our cryptographic run label before removing them.
        owned.append(name)
        argv = pd + ['run', '-d', '--pull=never', '--name', name, '--label', label,
                     '--userns=keep-id', '--user', '1000:1000', '--security-opt',
                     'label=disable', '--runtime', runtime, '--network='+network,
                     '--entrypoint', 'python3']
        for source, target in mounts:
            argv += ['-v', f'{source}:{target}:rw']
        command(argv + [args.image, '-u', '-c', program])
        return name

    def probe(name, family, address):
        token = secrets.token_hex(16)
        answer = json.loads(command(pd + ['exec', name, 'python3', '-c', CLIENT,
                                          json.dumps([family, address, token])]))
        return answer, token

    try:
        host = json.loads(command(pd + ['info', '--format', 'json']))['host']
        if host.get('serviceIsRemote') is not False or not host['security']['rootless']:
            raise RuntimeError('local rootless Podman required')
        if os.getuid() != 1000:
            raise RuntimeError('this prototype targets the current UID 1000 image/host mapping')
        command(pd + ['image', 'exists', args.image])
        runsc = str(Path(args.runsc).resolve())
        print(command([runsc, '--version']), flush=True)
        command([runsc, '--host-uds=open', '--version'])
        crun = shutil.which('crun')
        if not crun:
            raise RuntimeError('crun is required for the loopback holders')
        root = Path(tempfile.mkdtemp(prefix='uds-gate-', dir=os.environ.get('XDG_RUNTIME_DIR')))
        workspace = root / 'workspace'
        workspace.mkdir(mode=0o700)
        wrapper = root / 'runsc'
        wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(runsc)+' --ignore-cgroups --host-uds=open "$@"\n')
        wrapper.chmod(0o700)
        guests = []
        for key in ('a', 'b'):
            holder = launch(f'udsg-{runid}-h{key}', crun, 'none', [], 'import time; time.sleep(600)')
            private = root / key
            private.mkdir(mode=0o700)
            endpoint = private / 'allowed.sock'
            start_server(UnixReceiver, str(endpoint), Echo)
            endpoint.chmod(0o600)
            guest = launch(f'udsg-{runid}-{key}', str(wrapper), 'container:'+holder,
                           [(workspace, '/workspace'), (endpoint, '/run/allowed.sock')],
                           'import time; time.sleep(600)')
            guests.append(guest)
            control, _ = probe(guest, 'unix', '/run/allowed.sock')
            if not control['reached']:
                raise RuntimeError(f'{key}: intended UDS control failed: {control}')
        print('CONTROL: both concurrent guests reach their own intended socket', flush=True)

        # This TCP receiver has no guest-facing IP interface or proxy grant.
        target = start_server(Receiver, ('127.0.0.1', 0), Echo)
        direct, _ = probe(guests[1], 'tcp', target.server_address)
        if direct['reached']:
            raise RuntimeError('direct loopback unexpectedly reached host receiver; topology invalid')
        # Introduce an unrelated socket AFTER both guests have started.
        relay_path = workspace / 'extra.sock'
        relay = start_server(UnixReceiver, str(relay_path), Relay)
        relay.target = target.server_address
        relay_path.chmod(0o600)
        with socket.socket(socket.AF_UNIX) as control:
            control.settimeout(3)
            control.connect(str(relay_path))
            control.sendall(b'host-control\n')
            if control.recv(256) != b'host-control\n':
                raise RuntimeError('host relay positive control failed')
        through, token = probe(guests[1], 'unix', '/workspace/extra.sock')
        bypass = through['reached'] and token in target.received
        print(f'HOST RELAY BYPASS: {bypass}; guest={through}; receiver_confirmed={token in target.received}', flush=True)

        # Test guest-created sockets separately: host-uds=open may forbid bind.
        server_process = subprocess.Popen(pd + ['exec', guests[0], 'python3', '-u', '-c', SERVER],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cross = False
        try:
            deadline = time.monotonic() + 5
            while not (workspace / 'guest.sock').exists() and server_process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if (workspace / 'guest.sock').exists():
                own, _ = probe(guests[0], 'unix', '/workspace/guest.sock')
                if not own['reached']:
                    raise RuntimeError('guest socket self-control failed')
                other, _ = probe(guests[1], 'unix', '/workspace/guest.sock')
                cross = other['reached']
                print(f'CROSS-GUEST SOCKET: {cross}; {other}', flush=True)
            else:
                print('CROSS-GUEST SOCKET: inconclusive; guest could not expose a socket in the shared host tree', flush=True)
        finally:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()
                server_process.wait()
        result = 1 if bypass or cross else 2
        print('VERDICT: reject unrestricted host-uds=open with shared mounts' if result == 1 else
              'VERDICT: inconclusive; blocked attempts alone do not prove a socket allowlist', flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        print(f'INCONCLUSIVE: {type(exc).__name__}: {exc}', flush=True)
    finally:
        leaks = []
        for name in reversed(owned):
            try:
                ids = command(pd + ['ps', '-aq', '--filter', 'label='+label, '--filter', 'name=^'+name+'$'])
                for cid in ids.split():
                    command(pd + ['rm', '-f', '--ignore', cid])
            except Exception as exc:
                leaks.append(f'{name}: {exc}')
        for server in reversed(servers):
            server.shutdown()
            server.server_close()
        if root:
            if leaks:
                print(f'Keeping diagnostic directory {root}')
            else:
                shutil.rmtree(root)
        if leaks:
            print('CLEANUP INCOMPLETE:\n'+'\n'.join(leaks))
            result = 2
    return result


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
