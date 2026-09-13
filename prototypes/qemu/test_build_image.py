"""Validate build seed and its separation from ordinary launches."""
import subprocess
import unittest

from sandbox import qemu_command
from build_image import build_script
from provision_agents import AGENTS
from pathlib import Path


class BuildTests(unittest.TestCase):
    def test_build_script_has_no_interactive_session(self):
        script = build_script()
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        self.assertNotIn('sandbox-session.service', script)
        self.assertIn('mount -t virtiofs workspace /workspace', script)

    def test_build_does_not_open_or_stop_the_login_terminal(self):
        script = build_script()
        self.assertNotIn('/dev/tty', script)
        self.assertNotIn('serial-getty', script)
        self.assertIn('exec </dev/null', script)
        self.assertIn('tee /workspace/provision.log', script)

    def test_exact_requested_agents_use_https_installers(self):
        self.assertEqual(set(AGENTS), {'claude', 'codex', 'pi', 'omp', 'opencode'})
        for url, interpreter in AGENTS.values():
            self.assertTrue(url.startswith('https://'))
            self.assertIn(interpreter, (['sh'], ['bash']))

    def test_normal_launch_remains_disposable(self):
        command = qemu_command('qemu', Path('/base.qcow2'), Path('/seed.iso'),
                               Path('/fs.sock'), 12345)
        self.assertIn('-snapshot', command)
        self.assertIn('restrict=on,ipv6=off', command[command.index('-netdev')+1])


if __name__ == '__main__':
    unittest.main()
