#!/usr/bin/env python3
"""Opt-in installed-launcher MCP acceptance: --disk READ_ONLY_AGENT_IMAGE.
Needs KVM/QEMU: one disposable 2-GiB VM, private temporary home, loopback
fixtures, no real credentials/public egress. Stops the VM with idle SSE open.
Local unit tests are not a substitute for running this acceptance on the host.
"""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from accept_launcher import collect_diagnostics
from qemu.runtime_support import disk_cache, private_directory, read_report, remove_tree, validate_base_image

GUEST = r'''
import hashlib, http.client, json, os, pathlib, re, socket, stat, sys, time
port, neighbor = map(int, sys.argv[1:3])
digest = sys.argv[3]
workspace = pathlib.Path('/workspace')
def mark(name):
    (workspace/name).write_text('ready')
def request(method, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    connection.request(method, '/mcp', json.dumps(body) if body else None,
                       {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
    return connection, connection.getresponse()
def event(response):
    assert response.readline(65536) == b'event: message\n'
    line = response.readline(65536)
    assert line.startswith(b'data: ') and response.readline(65536) == b'\n'
    return json.loads(line[6:])
def exercise(generation):
    connection, response = request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'acceptance', 'version': '1'}}})
    assert response.status == 200
    result = json.loads(response.read())['result']
    assert result['protocolVersion'] == '2024-11-05' and result['serverInfo']['version'] == str(generation)
    connection.close()
    connection, response = request('POST', {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert response.status == 202
    response.read(); connection.close()
    connection, response = request('POST', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'echo', 'arguments': {'text': 'harmless'}, '_meta': {'progressToken': 2}}})
    assert response.status == 200 and response.getheader('Content-Type') == 'text/event-stream'
    assert event(response)['method'] == 'notifications/progress'
    mark('chunk-seen')  # Host cannot emit the result until this is visible.
    result = event(response)
    assert result['id'] == 2 and result['result']['content'] == [{'type': 'text', 'text': 'harmless:'+str(generation)}]
    assert response.read() == b''
    connection.close()
exercise(1)
stale, response = request('GET')
assert response.status == 200 and event(response)['method'] == 'notifications/message'
mark('restart-request')
deadline = time.monotonic()+45
while not (workspace/'restarted').exists():
    assert time.monotonic() < deadline, 'host did not restart'
    time.sleep(.1)
try:
    assert response.read(1) == b'', 'old connection survived host restart'
except http.client.IncompleteRead:
    pass  # Restart cuts off the old chunked response, not a completed stream.
stale.close()
exercise(2)
checks = {'initialize_tool_stream': True, 'restart_reconnect': True}
for address in ('10.0.2.101', '10.0.2.2'):
    try:
        with socket.create_connection((address, neighbor), timeout=3):
            checks['neighbor_blocked_'+address] = False
    except OSError:
        checks['neighbor_blocked_'+address] = True
pattern = rb'accept-mcp-[0-9a-f]{32}'
def clean(data):
    return all(hashlib.sha256(match.group()).hexdigest() != digest for match in re.finditer(pattern, data))
checks['no_key_in_guest_environment'] = clean(json.dumps(dict(os.environ)).encode())
# Scan actual contents, including dotfiles and seed/session settings; never embed
# the canary itself in this probe. Baked-in binaries are streamed, not loaded.
checks['no_key_in_guest_files'] = True
roots = [pathlib.Path.home(), workspace, pathlib.Path('/mnt'), pathlib.Path('/var/lib/llm-sandbox'), pathlib.Path('/tmp')]
for root in roots:
    for path in root.rglob('*'):
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                continue
            with path.open('rb') as source:
                tail = b''
                while data := source.read(65536):
                    checks['no_key_in_guest_files'] &= clean(tail+data)
                    tail = data[-64:]
        except (PermissionError, FileNotFoundError):
            pass  # Unreadable or dangling image links expose no key.
held, response = request('GET')
assert response.status == 200 and event(response)['method'] == 'notifications/message'
(workspace/'result.pending').write_text(json.dumps(checks))
(workspace/'result.pending').rename(workspace/'result.json')
# Keep the TCP stream alive, with no heartbeat, until the host terminates the VM.
while True:
    time.sleep(1)
'''


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status, stream=False, length=0):
        self.send_response(status)
        self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
        self.send_header('Transfer-Encoding' if stream else 'Content-Length', 'chunked' if stream else str(length))
        self.end_headers()

    def event(self, value):
        data = ('event: message\ndata: '+json.dumps({'jsonrpc': '2.0', **value})+'\n\n').encode()
        self.wfile.write(f'{len(data):x}\r\n'.encode()+data+b'\r\n')
        self.wfile.flush()

    def do_HEAD(self):
        self.respond(200 if self.path == '/' else 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        if self.path != '/mcp' or not 0 < length <= 65536:
            self.send_error(400); return
        body = json.loads(self.rfile.read(length))
        method = body['method']
        if method == 'initialize':
            data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': {
                'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'loopback-acceptance', 'version': str(self.server.generation)}}}).encode()
            self.respond(200, length=len(data)); self.wfile.write(data)
        elif method == 'notifications/initialized':
            self.respond(202)
        elif method == 'tools/call' and body['params']['name'] == 'echo' and body['params']['arguments'] == {'text': 'harmless'}:
            self.respond(200, stream=True)
            self.event({'method': 'notifications/progress', 'params': {'progressToken': 2, 'progress': 1, 'total': 2}})
            if not self.server.release.wait(45):
                self.close_connection = True; return
            self.event({'id': body['id'], 'result': {'content': [{'type': 'text', 'text': 'harmless:'+str(self.server.generation)}]}})
            self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
        else:
            self.send_error(400)

    def do_GET(self):
        if self.path != '/mcp':
            self.send_error(404); return
        self.respond(200, stream=True)
        self.event({'method': 'notifications/message', 'params': {'level': 'info', 'data': 'holding'}})
        if self.connection.recv(1) == b'':
            self.server.eof.set()
        self.close_connection = True


class Fixture(ThreadingHTTPServer):
    daemon_threads = False  # close() joins handlers after shutting down their sockets.
    allow_reuse_address = True

    def __init__(self, port=0, generation=1):
        self.clients, self.release, self.eof = set(), threading.Event(), threading.Event()
        self.generation = generation
        super().__init__(('127.0.0.1', port), Handler)
        self.port = self.server_port
        if self.port < 1024:
            self.server_close()
            raise ValueError('fixture needs an unprivileged port')
        self.thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': .05})
        self.thread.start()

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(45)
        self.clients.add(client)
        return client, address

    def shutdown_request(self, client):
        super().shutdown_request(client)
        self.clients.discard(client)

    def handle_error(self, *args):
        pass  # Expected disconnects during restart/VM termination.

    def close(self):
        self.shutdown(); self.thread.join(timeout=2)
        self.release.set()
        for client in tuple(self.clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        self.server_close()


def positive_control(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('HEAD', '/')
        if connection.getresponse().status != 200:
            raise RuntimeError('host fixture positive control failed')
    finally:
        connection.close()


def environment(base, disk, key):
    # Do not inherit proxies, backend profiles, SSH agents, XDG state or secrets.
    return dict(PATH='/usr/bin:/bin:/usr/sbin:/sbin', LANG='C.UTF-8', HOME=str(base/'home'),
                TMPDIR=str(base/'tmp'), MCP_ACCEPT_HOST_ONLY_KEY=key, SANDBOX_ISOLATION='qemu',
                SANDBOX_QEMU_DISK=str(disk), SANDBOX_BATCH='1', SANDBOX_QEMU_MEMORY_MIB='2048',
                SANDBOX_QEMU_CPUS='2', SANDBOX_QEMU_BOOT_TIMEOUT='300', SANDBOX_QEMU_BATCH_TIMEOUT='240',
                SANDBOX_QEMU_CACHE_DIR=str(base/'runs'), SANDBOX_QEMU_KEEP_ARTIFACTS='1',
                SANDBOX_ALLOW_FILE=str(base/'allowlist'))


def guard_path(argv, port):
    prefix = f'guestfwd=tcp:10.0.2.101:{port}-cmd:'
    network = argv[argv.index('-netdev')+1].split(',')
    command, = [shlex.split(item[len(prefix):]) for item in network if item.startswith(prefix)]
    guard = Path(command[3]) if len(command) == 7 else Path('.')
    if not guard.is_absolute() or guard.name != 'mcp.guard' or not relay_command(command, guard) or command[-1] != str(port):
        raise ValueError('unexpected MCP guard command')
    return guard


def relay_command(argv, guard):
    return (len(argv) == 7 and Path(argv[0]).name.startswith('python') and Path(argv[1]).name == 'network_relay.py'
            and argv[2:6] == ['--guard', str(guard), '--tcp', '127.0.0.1'] and argv[6].isdigit())


def processes(base, guard=None, relays_only=False, kill=False):
    """Exact per-run argv matching, never pgrep; pidfds prevent PID-reuse kills."""
    found = []
    for entry in Path('/proc').glob('[0-9]*'):
        fd = None
        try:
            if kill:
                fd = os.pidfd_open(int(entry.name))
            argv = (entry/'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            relay = guard is not None and relay_command(argv, guard)
            binary = Path(argv[0]).name
            owned = binary.startswith('python') and len(argv) > 1 and argv[1] in {
                str(base/'home/.config/llm-sandbox/qemu'/name) for name in ('sandbox.py', 'network_relay.py', 'egress-proxy.py')}
            owned |= binary == 'qemu-system-x86_64' and any(arg.startswith('file='+str(base/'runs')+'/run-') for arg in argv)
            owned |= binary == 'virtiofsd' and any(argv[i] == '--shared-dir' and argv[i+1].startswith(str(base)+'/')
                                                  for i in range(len(argv)-1))
            if relay or (not relays_only and owned):
                found.append(int(entry.name))
                if kill:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return found


def wait_for(predicate, child=None, seconds=30):
    deadline = time.monotonic()+seconds
    while not predicate():
        if child is not None and child.poll() is not None:
            raise RuntimeError(f'launcher exited early: {child.returncode}')
        if time.monotonic() > deadline:
            raise TimeoutError('acceptance phase timed out')
        time.sleep(.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disk', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run without sudo')
    try:
        disk = args.disk.resolve(strict=True)
        validate_base_image(disk)
        if not disk.is_file() or not shutil.which('qemu-system-x86_64') or not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise ValueError('an existing agent disk, QEMU and KVM access are required')
        cache = disk_cache(Path.home()/'.cache/llm-sandbox/qemu-acceptance')
        private_directory(cache)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    base = Path(tempfile.mkdtemp(prefix='mcp-accept-', dir=cache))
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    fixtures, children, checks = [], [], {}
    guard, failure = None, None
    print(f'Acceptance artifacts: {base}; one VM (2 GiB RAM)', flush=True)
    try:
        for name in ('home', 'workspace', 'tmp'):
            (base/name).mkdir(mode=0o700)
        key = 'accept-mcp-'+secrets.token_hex(16)
        (base/'home/host-only.key').write_text(key)
        (base/'allowlist').write_text('127.0.0.1\n')  # Proxy rejects private IPs; no public hosts allowed.
        (base/'workspace/probe.py').write_text(GUEST)
        env = environment(base, disk, key)
        with (base/'a.log').open('w') as log:
            install = subprocess.Popen(['sh', 'install.sh'], cwd=Path(__file__).resolve().parents[2], env=env,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            children.append(install)
            if install.wait(timeout=30):
                raise RuntimeError('temporary install failed')
            (base/'home/.config/llm-sandbox/backends.json').write_text('{}')
            server = Fixture(); fixtures.append(server)
            neighbor = Fixture(); fixtures.append(neighbor)
            positive_control(neighbor.port)
            checks['host_neighbor_positive_control'] = True
            child = subprocess.Popen([str(base/'home/.local/bin/,llm-sandbox.sh'), '--mcp-port', str(server.port),
                '--shell', '-c', f'exec python3 /workspace/probe.py {server.port} {neighbor.port} {hashlib.sha256(key.encode()).hexdigest()}'],
                cwd=base/'workspace', env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            children.append(child)
            wait_for(lambda: (base/'workspace/chunk-seen').exists(), child, 360)
            run, = (base/'runs').glob('run-*')
            argv = read_report(run/'launch.json')['argv']
            guard = guard_path(argv, server.port)
            checks['disposable_overlay'] = '-snapshot' not in argv and f'file={run}/overlay.qcow2,format=qcow2,if=virtio' in argv
            server.release.set()
            wait_for(lambda: (base/'workspace/restart-request').exists(), child)
            port = server.port
            server.close(); fixtures.remove(server)
            server = Fixture(port, generation=2); fixtures.append(server)
            positive_control(server.port)
            server.release.set()
            (base/'workspace/restarted').write_text('ready')
            wait_for(lambda: (base/'workspace/result.json').exists(), child, 180)
            checks.update(read_report(base/'workspace/result.json'))
            positive_control(neighbor.port)
            checks['active_real_relay'] = bool(processes(base, guard, relays_only=True))
            child.terminate()  # Normal launcher SIGTERM cleanup while guest still holds SSE open.
            checks['controlled_vm_shutdown'] = child.wait(timeout=30) == 2
            wait_for(lambda: not processes(base, guard, relays_only=True), seconds=10)
            checks['relays_exited'] = True
            checks['host_stream_closed_by_vm_exit'] = server.eof.wait(5)
            checks['vm_and_helpers_exited'] = not processes(base, guard)
            checks['overlay_and_guard_removed'] = not (run/'overlay.qcow2').exists() and not guard.exists()
    except (Exception, KeyboardInterrupt) as exc:
        failure = f'{type(exc).__name__}: {exc}'[:2000]
    finally:
        for child in reversed(children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)
                except ProcessLookupError:
                    child.wait(timeout=5)
        # Also recover run-owned helpers if the launcher failed before its finally.
        if guard is None:
            for launch in (base/'runs').glob('run-*/launch.json'):
                try:
                    guard = guard_path(read_report(launch)['argv'], server.port)
                except (OSError, ValueError, NameError, KeyError):
                    pass
        processes(base, guard, kill=True)
        try:
            wait_for(lambda: not processes(base, guard), seconds=5)
        except TimeoutError:
            failure = failure or 'run-owned processes survived cleanup; retaining disk and exports for recovery'
        for fixture in reversed(fixtures):
            fixture.close()
        checks['host_fixtures_closed'] = all(not fixture.thread.is_alive() and not fixture.clients for fixture in fixtures)
        # Never invalidate a surviving VM's disk or exports; retain them for recovery.
        if not processes(base, guard):
            for name in ('home', 'workspace', 'tmp'):
                remove_tree(base/name)
            for overlay in (base/'runs').glob('run-*/overlay.qcow2'):
                overlay.unlink(missing_ok=True)
        # Replace, rather than truncate: even a surviving writer cannot enlarge the retained log.
        if (base/'a.log').exists():
            with (base/'a.log').open('rb') as source:
                source.seek(0, os.SEEK_END); source.seek(max(0, source.tell()-1024*1024))
                (base/'a.log.tail').write_bytes(source.read(1024*1024))
            (base/'a.log.tail').replace(base/'a.log')
    report = {'checks': checks, 'error': failure}
    if failure or not checks or not all(checks.values()):
        report['logs'] = collect_diagnostics(base)
    (base/'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f'Diagnostics retained at {base}')
    return 0 if not failure and checks and all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
