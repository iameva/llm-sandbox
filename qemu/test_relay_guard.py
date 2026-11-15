"""Real relay subprocesses: bounded admission, owner death and full-duplex EOF."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qemu.network_relay import relay
from qemu.relay_guard import RelayGuard

REPO = Path(__file__).resolve().parents[1]


class RelayGuardTests(unittest.TestCase):
    def child(self, args, **kwargs):
        process = subprocess.Popen([sys.executable, '-B', *args], cwd=REPO,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **kwargs)
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(stop)
        return process

    def relay_child(self, path, port):
        return self.child(['qemu/network_relay.py', '--guard', str(path),
                           '--tcp', '127.0.0.1', str(port)])

    def lease(self, path):
        lease = socket.socket(socket.AF_UNIX)
        self.addCleanup(lease.close)
        lease.settimeout(3)
        lease.connect(str(path))
        return lease

    def test_guard_cancels_idle_relay_after_guest_eof_without_stopping_host(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            with RelayGuard(Path(directory)/'guard') as guard:
                child = self.relay_child(guard.path, listener.getsockname()[1])
                with listener.accept()[0] as host:
                    host.settimeout(3)
                    # Equivalent to a disappearing QEMU channel while upstream stays open.
                    child.stdin.close()
                    child.stdout.close()
                    self.assertEqual(host.recv(1), b'')
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=.2)
                    guard.close()
                    self.assertEqual(child.wait(timeout=3), 0, child.stderr.read())
                self.assertFalse(guard.path.exists())
            # The separately managed MCP listener is untouched.
            with socket.create_connection(listener.getsockname(), timeout=3):
                listener.accept()[0].close()

    def test_guard_cancels_relay_when_upstream_accept_queue_is_full(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(0)
            with socket.create_connection(listener.getsockname(), timeout=3):
                with RelayGuard(Path(directory)/'guard') as guard:
                    child = self.relay_child(guard.path, listener.getsockname()[1])
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=.2)
                    guard.close()
                    # Cancellation must not wait for the ten-second connect timeout.
                    self.assertEqual(child.wait(timeout=3), 0, child.stderr.read())

    def test_owner_sigkill_cancels_relay_without_a_finally_block(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            path = Path(directory)/'guard'
            ready = Path(directory)/'ready'
            owner = self.child(['-c',
                'from qemu.relay_guard import RelayGuard; from pathlib import Path; '
                'import sys,time; g=RelayGuard(sys.argv[1]); '
                'Path(sys.argv[2]).write_text("ready"); time.sleep(60)', str(path), str(ready)])
            deadline = time.monotonic()+5
            while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), 'guard owner did not become ready')
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            child = self.relay_child(path, listener.getsockname()[1])
            with listener.accept()[0] as host:
                host.settimeout(3)
                owner.kill()
                owner.wait(timeout=3)
                self.assertEqual(child.wait(timeout=3), 0, child.stderr.read())
                self.assertEqual(host.recv(1), b'')

    def test_limit_is_enforced_on_host_before_connecting_to_mcp(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(.2)
            with RelayGuard(Path(directory)/'guard', limit=1) as guard:
                self.assertEqual(guard.path.stat().st_mode & 0o777, 0o600)
                first = self.lease(guard.path)
                self.assertEqual(first.recv(1), b'1')
                rejected = self.relay_child(guard.path, listener.getsockname()[1])
                self.assertEqual(rejected.wait(timeout=3), 1)
                self.assertIn(b'limit reached', rejected.stderr.read())
                with self.assertRaises(socket.timeout):
                    listener.accept()
                first.close()
                # The released slot becomes reusable; allow the event loop to observe EOF.
                deadline = time.monotonic()+3
                while True:
                    again = self.lease(guard.path)
                    if again.recv(1) == b'1':
                        break
                    again.close()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                guard.check()

    def test_stopping_one_run_does_not_cancel_another(self):
        with tempfile.TemporaryDirectory() as directory:
            with RelayGuard(Path(directory)/'one') as first, RelayGuard(Path(directory)/'two') as second:
                one, two = self.lease(first.path), self.lease(second.path)
                self.assertEqual(one.recv(1), b'1')
                self.assertEqual(two.recv(1), b'1')
                first.close()
                self.assertEqual(one.recv(1), b'')
                two.settimeout(.1)
                with self.assertRaises(socket.timeout):
                    two.recv(1)
                second.check()
                with self.assertRaisesRegex(RuntimeError, 'guard stopped'):
                    first.check()

    def test_unavailable_server_has_port_diagnostic_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            port = unavailable.getsockname()[1]
            with RelayGuard(Path(directory)/'guard', limit=1) as guard:
                child = self.relay_child(guard.path, port)
                self.assertEqual(child.wait(timeout=3), 1)
                self.assertIn(f'127.0.0.1:{port}'.encode(), child.stderr.read())
                deadline = time.monotonic()+3
                while True:
                    lease = self.lease(guard.path)
                    if lease.recv(1) == b'1':
                        break
                    lease.close()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)

    def test_legacy_unix_proxy_command_still_relays_after_input_eof(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = Path(directory)/'proxy.sock'
            listener.bind(str(path))
            listener.listen()
            listener.settimeout(3)
            child = self.child(['qemu/network_relay.py', str(path)])
            with listener.accept()[0] as host:
                host.settimeout(3)
                child.stdin.write(b'ping')
                child.stdin.close()
                received = bytearray()
                while data := host.recv(1024):
                    received.extend(data)
                self.assertEqual(bytes(received), b'ping')
                host.sendall(b'pong')
                host.shutdown(socket.SHUT_WR)
                self.assertEqual(child.wait(timeout=3), 0, child.stderr.read())
                self.assertEqual(child.stdout.read(), b'pong')

    def test_command_propagates_reverse_eof_with_separate_stdio_pipes(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            child = self.child(['qemu/network_relay.py', '--tcp', '127.0.0.1',
                                str(listener.getsockname()[1])])
            with listener.accept()[0] as host:
                host.settimeout(3)
                host.sendall(b'ready')
                host.shutdown(socket.SHUT_WR)
                # Bound the EOF read so a regression cannot hang the test suite.
                output = []
                reader = threading.Thread(target=lambda: output.append(child.stdout.read()), daemon=True)
                reader.start()
                reader.join(3)
                self.assertFalse(reader.is_alive())
                self.assertEqual(output, [b'ready'])
                child.stdin.write(b'after EOF')
                child.stdin.close()
                received = bytearray()
                while data := host.recv(1024):
                    received.extend(data)
                self.assertEqual(bytes(received), b'after EOF')
                self.assertEqual(child.wait(timeout=3), 0, child.stderr.read())

    def test_reverse_half_close_preserves_backpressured_client_upload(self):
        downstream, client = socket.socketpair()
        upstream, host = socket.socketpair()
        for peer in (downstream, client, upstream, host):
            self.addCleanup(peer.close)
        client.settimeout(5)
        host.settimeout(5)
        errors = []
        def pump():
            try:
                relay(upstream, downstream.fileno(), downstream.fileno())
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=pump, daemon=True)
        worker.start()
        host.sendall(b'ready')
        host.shutdown(socket.SHUT_WR)
        self.assertEqual(client.recv(5), b'ready')
        self.assertEqual(client.recv(1), b'')
        payload = b'x'*(1024*1024)
        def send():
            try:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
            except Exception as exc:
                errors.append(exc)
        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        received = bytearray()
        while True:
            chunk = host.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        sender.join(5)
        worker.join(5)
        self.assertFalse(sender.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(bytes(received), payload)


if __name__ == '__main__':
    unittest.main()
