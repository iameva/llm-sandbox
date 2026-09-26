"""Validate build seed and its separation from ordinary launches."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qemu.sandbox import qemu_command
from qemu.build_image import SEED_FILES, build_script
from qemu.provision_agents import (AGENTS, PACKAGES, PLAYWRIGHT_BROWSERS, PLAYWRIGHT_VERSION,
                                   UBUNTU_POOL, WEBKIT_UBUNTU_DEBS, WEBKIT_UBUNTU_LIBRARIES,
                                   X264_DEB, X264_LIBRARY, claude_install_command,
                                   deb_libraries, pi_install_command)


class BuildTests(unittest.TestCase):
    def test_build_script_has_no_interactive_session(self):
        script = build_script()
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        self.assertNotIn('sandbox-session.service', script)
        self.assertIn('--property=After=cloud-final.service', script)
        self.assertNotIn('/sbin/poweroff', script)
        self.assertIn('mount -t virtiofs workspace /workspace', script)

    def test_build_does_not_open_or_stop_the_login_terminal(self):
        script = build_script()
        self.assertNotIn('/dev/tty', script)
        self.assertNotIn('serial-getty', script)
        self.assertIn('exec </dev/null', script)
        self.assertIn('tee /workspace/provision.log', script)

    def test_exact_requested_agents_use_https_installers(self):
        self.assertEqual(set(AGENTS), {'claude', 'codex', 'pi', 'omp', 'opencode'})
        for name, (url, interpreter) in AGENTS.items():
            if name in ('claude', 'pi'):
                continue
            self.assertTrue(url.startswith('https://'))
            self.assertIn(interpreter, (['sh'], ['bash']))

    def test_claude_uses_guest_prefix_proxy_and_optional_native_package(self):
        command = claude_install_command('/home/fedora', 'http://10.0.2.100:3128')
        self.assertEqual(command[command.index('--prefix')+1], '/home/fedora/.local')
        self.assertIn('--https-proxy=http://10.0.2.100:3128', command)
        self.assertIn('--include=optional', command)
        self.assertIn('--fetch-timeout=60000', command)
        self.assertEqual(command[-1], '@anthropic-ai/claude-code')

    def test_pi_installs_outside_shared_state_without_lifecycle_scripts(self):
        command = pi_install_command('/home/fedora', 'http://10.0.2.100:3128')
        self.assertEqual(command[command.index('--prefix')+1], '/home/fedora/.local')
        self.assertIn('@earendil-works/pi-coding-agent', command)
        self.assertIn('--ignore-scripts', command)
        self.assertIn('--https-proxy=http://10.0.2.100:3128', command)

    def test_seed_carries_recipe_inputs(self):
        # guest_verify.py supplies the tool list the recipe checks against.
        for name in SEED_FILES:
            self.assertTrue((Path(__file__).with_name(name)).is_file(), name)
        self.assertIn('guest_verify.py', SEED_FILES)

    def test_recipe_keeps_container_parity(self):
        for package in ('zsh', 'golang', 'caddy', 'gtk3', 'gtk4', 'nss', 'mesa-libgbm',
                        'google-noto-sans-symbols-2-fonts', 'google-noto-color-emoji-fonts'):
            self.assertIn(package, PACKAGES)
        self.assertEqual(len(PACKAGES), len(set(PACKAGES)))
        # Playwright looks for one browser build; a range would drift from it.
        self.assertRegex(PLAYWRIGHT_VERSION, r'^\d+\.\d+\.\d+$')
        self.assertEqual(set(PLAYWRIGHT_BROWSERS), {'firefox', 'chromium', 'webkit'})

    def test_webkit_ubuntu_libraries_are_pinned(self):
        self.assertTrue(UBUNTU_POOL.startswith('https://'))
        for pool_path, digest in [*WEBKIT_UBUNTU_DEBS.items(), X264_DEB]:
            self.assertRegex(pool_path, r'^(main|universe)/.+_amd64\.deb$')
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
        # Each library carries the exact soname WebKit links against.
        for name in [*WEBKIT_UBUNTU_LIBRARIES, X264_LIBRARY]:
            self.assertRegex(name, r'\.so\.\d+$')
        # Ubuntu's own copies would shadow Fedora's if they reached the system path.
        self.assertFalse({'libicu', 'libjpeg-turbo'} & set(PACKAGES))
        self.assertTrue({'binutils', 'zstd', 'vulkan-loader'} <= set(PACKAGES))

    def test_deb_libraries_unpacks_the_data_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root/'tree/usr/lib/x86_64-linux-gnu/libdemo.so.1'
            library.parent.mkdir(parents=True)
            library.write_bytes(b'demo')
            subprocess.run(['tar', '--zstd', '-cf', str(root/'data.tar.zst'), '-C', str(root/'tree'), 'usr'],
                           check=True)
            (root/'debian-binary').write_text('2.0\n')
            subprocess.run(['ar', 'rc', 'demo.deb', 'debian-binary', 'data.tar.zst'], cwd=root, check=True)
            libraries = deb_libraries(root/'demo.deb', root/'out')
            self.assertEqual((libraries/'libdemo.so.1').read_bytes(), b'demo')

    def test_deb_libraries_rejects_a_deb_without_one_data_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'debian-binary').write_text('2.0\n')
            subprocess.run(['ar', 'rc', 'empty.deb', 'debian-binary'], cwd=root, check=True)
            with self.assertRaisesRegex(RuntimeError, 'exactly one data archive'):
                deb_libraries(root/'empty.deb', root/'out')

    def test_command_uses_caller_selected_disk_without_implicit_snapshot(self):
        command = qemu_command('qemu', Path('/base.qcow2'), Path('/seed.iso'),
                               Path('/fs.sock'), 12345)
        self.assertNotIn('-snapshot', command)
        self.assertIn('restrict=on,ipv6=off', command[command.index('-netdev')+1])


if __name__ == '__main__':
    unittest.main()
