"""Local-only MCP acceptance tests; no VM, image, installer or public network."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from accept_mcp import Fixture, GUEST, environment, guard_path, positive_control, relay_command, wait_for


class AcceptanceTests(unittest.TestCase):
    def test_environment_is_fresh_and_bounded(self):
        with patch.dict(os.environ, {'REAL_SECRET': 'do-not-copy', 'SANDBOX_LLM_AGENTS': 'claude',
                                     'HTTPS_PROXY': 'http://public.invalid', 'XDG_CONFIG_HOME': '/real/state'}):
            env = environment(Path('/private/run'), Path('/images/agents.qcow2'), 'dummy')
        self.assertFalse(set(env) & {'REAL_SECRET', 'SANDBOX_LLM_AGENTS', 'HTTPS_PROXY', 'XDG_CONFIG_HOME'})
        self.assertEqual(env['HOME'], '/private/run/home')
        self.assertEqual(env['TMPDIR'], '/private/run/tmp')
        self.assertEqual(env['SANDBOX_QEMU_CACHE_DIR'], '/private/run/runs')
        self.assertEqual(env['SANDBOX_QEMU_DISK'], '/images/agents.qcow2')
        self.assertEqual(env['SANDBOX_ALLOW_FILE'], '/private/run/allowlist')
        self.assertEqual(env['SANDBOX_QEMU_MEMORY_MIB'], '2048')
        self.assertTrue(0 < int(env['SANDBOX_QEMU_BATCH_TIMEOUT']) <= 300)
        self.assertTrue(0 < int(env['SANDBOX_QEMU_BOOT_TIMEOUT']) <= 600)
        self.assertEqual(env['MCP_ACCEPT_HOST_ONLY_KEY'], 'dummy')

    def test_guard_parsing_and_process_matching_are_run_specific(self):
        guard = Path('/tmp/private sockets/mcp.guard')
        command = ['python3', '/installed/network_relay.py', '--guard', str(guard), '--tcp', '127.0.0.1', '32123']
        argv = ['qemu-system-x86_64', '-netdev', 'user,id=n,guestfwd=tcp:10.0.2.101:32123-cmd:'+shlex.join(command)]
        self.assertEqual(guard_path(argv, 32123), guard)
        self.assertTrue(relay_command(command, guard))
        for other in [argv, ['sh', '-c', shlex.join(command)], command[:2]+command[4:],
                      ['python3', '/other.py', *command[2:]], command[:3]+[str(guard)+'-other']+command[4:]]:
            self.assertFalse(relay_command(other, guard))
        with self.assertRaises(ValueError):
            guard_path(argv, 32124)
        for replacement in ['relative/mcp.guard', '/tmp/not-the-guard']:
            with self.assertRaises(ValueError):
                guard_path([argv[0], argv[1], argv[2].replace(shlex.quote(str(guard)), replacement)], 32123)
        with self.assertRaises(TimeoutError):
            wait_for(lambda: False, seconds=0)

    def test_actual_guest_protocol_streaming_restart_and_fixture_cleanup(self):
        compile(GUEST, 'guest-probe', 'exec')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            fixtures, errors = [], []
            try:
                server = Fixture(); fixtures.append(server)
                neighbor = Fixture(); fixtures.append(neighbor)
                self.assertEqual(server.server_address[0], '127.0.0.1')
                self.assertGreaterEqual(server.port, 1024)
                self.assertNotEqual(server.port, neighbor.port)
                positive_control(neighbor.port)
                scope = {}
                with patch.object(sys, 'argv', ['probe.py', str(server.port), str(neighbor.port), 'digest']):
                    exec(compile(GUEST.split('exercise(1)', 1)[0], 'guest-protocol', 'exec'), scope)
                scope['workspace'] = workspace
                def exercise():
                    try:
                        scope['exercise'](1)
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=exercise)
                worker.start()
                try:
                    wait_for(lambda: (workspace/'chunk-seen').exists() or errors, seconds=5)
                    self.assertFalse(errors)
                    self.assertTrue(worker.is_alive(), 'result arrived before the host released it')
                    self.assertFalse(server.release.is_set())
                finally:
                    server.release.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors)
                stale, response = scope['request']('GET')
                self.assertEqual(scope['event'](response)['method'], 'notifications/message')
                port = server.port
                server.close(); fixtures.remove(server)
                self.assertFalse(server.thread.is_alive())
                self.assertFalse(server.clients)
                with self.assertRaises(http.client.IncompleteRead):
                    response.read()
                stale.close()
                server = Fixture(port, generation=2); fixtures.append(server)
                positive_control(port)
                server.release.set()
                scope['exercise'](2)
                held, response = scope['request']('GET')
                self.assertEqual(scope['event'](response)['method'], 'notifications/message')
                response.close(); held.close()
                self.assertTrue(server.eof.wait(3))
            finally:
                for fixture in reversed(fixtures):
                    fixture.close()
                    self.assertFalse(fixture.thread.is_alive())
                    self.assertFalse(fixture.clients)

    def test_guest_canary_scanner_checks_contents_across_read_boundaries(self):
        key = 'accept-mcp-'+'a'*32
        scope = {}
        with patch.object(sys, 'argv', ['probe.py', '32123', '32124', hashlib.sha256(key.encode()).hexdigest()]):
            exec(GUEST.split('exercise(1)', 1)[0], scope)
        exec('pattern = '+GUEST.split('pattern = ', 1)[1].split("checks['no_key_in_guest_environment']", 1)[0], scope)
        self.assertFalse(scope['clean'](json.dumps({'KEY': key}).encode()))
        self.assertTrue(scope['clean'](b'no canary here'))
        scanner = 'for root in roots:'+GUEST.split('for root in roots:', 1)[1].split("held, response = request('GET')", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.hidden').write_bytes(b'x'*65530+key.encode()+b'y'*70000)
            os.mkfifo(root/'not-a-file')
            (root/'dangling').symlink_to(root/'absent')
            scope.update(roots=[root], checks={'no_key_in_guest_files': True})
            exec(scanner, scope)
            self.assertFalse(scope['checks']['no_key_in_guest_files'])


if __name__ == '__main__':
    unittest.main()
