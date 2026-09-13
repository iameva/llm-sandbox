"""Check the interactive launch boundary without starting a VM."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from sandbox import USER_DATA, qemu_command
from process_lifecycle import Processes


class SandboxTests(unittest.TestCase):
    def test_only_restricted_proxy_forward_and_snapshot_are_present(self):
        command = qemu_command('/usr/bin/qemu', Path('/disk.qcow2'),
                               Path('/seed.iso'), Path('/run/fs.sock'), 42123)
        network = command[command.index('-netdev')+1]
        self.assertIn('restrict=on,ipv6=off', network)
        self.assertEqual(network.count('guestfwd='), 1)
        self.assertIn('10.0.2.100:3128-cmd:', network)
        self.assertNotIn('hostfwd=', network)
        self.assertIn('-snapshot', command)
        self.assertEqual(command[command.index('-monitor')+1], 'none')
        self.assertNotIn('-enable-kvm', command)  # KVM is selected by machine.
        self.assertIn('q35,accel=kvm', command)

    def test_guest_boot_script_is_valid_shell(self):
        subprocess.run(['sh', '-n'], input=USER_DATA, text=True, check=True)

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
