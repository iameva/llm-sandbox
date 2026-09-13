"""Check the interactive launch boundary without starting a VM."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qemu.sandbox import USER_DATA, qemu_command, launch_settings
from qemu.proxy_process import Processes
from qemu.guest_verify import sqlite_wal_works, agent_versions


class SandboxTests(unittest.TestCase):
    def test_agent_version_timeout_or_empty_output_fails_but_checks_remaining_agents(self):
        environment = {'HOME': '/private-guest-home'}
        responses = [subprocess.CompletedProcess([], 0, 'claude 1\n'),
                     subprocess.TimeoutExpired('codex', 60),
                     subprocess.CompletedProcess([], 0, ''),
                     FileNotFoundError('omp missing'),
                     subprocess.CompletedProcess([], 0, 'opencode 1\n')]
        with patch('qemu.guest_verify.subprocess.run', side_effect=responses) as run:
            checks, versions = agent_versions(environment)
        self.assertEqual(checks, {'claude_version': True, 'codex_version': False,
                                 'pi_version': False, 'omp_version': False, 'opencode_version': True})
        self.assertEqual(versions['opencode'], 'opencode 1')
        self.assertEqual(run.call_count, 5)
        for call in run.call_args_list:
            self.assertIs(call.kwargs['env'], environment)
            self.assertEqual(call.kwargs['timeout'], 60)
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)

    def test_sqlite_probe_supports_wal_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory)/'state_5.sqlite'
            existing.write_bytes(b'existing state must not be opened or changed')
            self.assertTrue(sqlite_wal_works(directory))
            self.assertEqual(existing.read_bytes(), b'existing state must not be opened or changed')
            self.assertEqual(list(Path(directory).iterdir()), [existing])

    def test_restricted_proxy_forward_uses_explicit_disk(self):
        command = qemu_command('/usr/bin/qemu', Path('/disk.qcow2'),
                               Path('/seed.iso'), Path('/run/fs.sock'), 42123)
        network = command[command.index('-netdev')+1]
        self.assertIn('restrict=on,ipv6=off', network)
        self.assertEqual(network.count('guestfwd='), 1)
        self.assertIn('10.0.2.100:3128-cmd:', network)
        self.assertNotIn('hostfwd=', network)
        self.assertNotIn('-snapshot', command)
        self.assertEqual(command[command.index('-monitor')+1], 'none')
        self.assertNotIn('-enable-kvm', command)  # KVM is selected by machine.
        self.assertIn('q35,accel=kvm', command)
        self.assertEqual(command[command.index('-cpu')+1], 'host')

    def test_guest_boot_script_is_valid_shell(self):
        subprocess.run(['sh', '-n'], input=USER_DATA, text=True, check=True)

    def test_state_validation_rejects_root_and_traversal(self):
        for target in ('/home/fedora', '/etc', '/home/fedora/../../etc'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                launch_settings(['/tmp/state:'+target], [], [], ['codex'])

    def test_launch_settings_keep_literal_arguments_and_do_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'not-created'
            config = launch_settings([str(state)+':/home/fedora/.config/codex'], [],
                                     ['TEST_VALUE=two words'], ['codex', '$(literal)', ''])
            self.assertEqual(config['command'], ['codex', '$(literal)', ''])
            self.assertEqual(config['environment'], {'TEST_VALUE': 'two words'})
            self.assertFalse(state.exists())

    def test_selected_allowlist_is_copied_and_not_replaced_by_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            allow = base/'selected.txt'
            allow.write_text('selected.example\n')
            processes = Processes(base, allow_file=allow)
            self.assertEqual(processes.allow.read_text(), 'selected.example\n')
            allow.write_text('changed.example\n')
            self.assertEqual(processes.allow.read_text(), 'selected.example\n')


if __name__ == '__main__':
    unittest.main()
