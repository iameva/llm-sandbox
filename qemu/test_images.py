"""Exercise image publication and selection without QEMU or downloads."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qemu import images


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # These tests use fake disks in /tmp. Filesystem placement has its own
        # tests; bypass it here so we reach the publication and boot gates.
        cache = patch.object(images, 'disk_cache', side_effect=lambda path: Path(path).resolve())
        cache.start()
        self.addCleanup(cache.stop)
        self.config_path = self.root/'qemu.json'
        self.versions = {name: '1.0' for name in images.AGENTS}
        self.first = self.published('first')
        self.second = self.published('second')
        self.config = {'version': 1, 'active_image': str(self.first), 'image_store': str(self.root)}
        images.atomic_json(self.config_path, self.config)
        self.original_config = self.config_path.read_bytes()

    def published(self, name):
        directory = self.root/name
        directory.mkdir()
        image = directory/'agents.qcow2'
        image.write_bytes(name.encode())
        image.chmod(0o400)
        images.atomic_json(directory/'manifest.json', {
            'ok': True, 'versions': self.versions, 'sha256': images.digest(image)})
        return image

    def main(self, *args):
        with patch.object(sys, 'argv', ['images.py', '--config', str(self.config_path), *args]), \
                patch.object(images.os, 'geteuid', return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return images.main()

    def test_selection_and_rollback_preserve_resolved_old_path(self):
        started_with = Path(images.load_config(self.config_path)['active_image'])
        with patch.object(images, 'image_tool'), patch.object(images, 'probe', return_value=self.versions):
            self.assertEqual(self.main('activate', str(self.second)), 0)
            config = images.load_config(self.config_path)
            self.assertEqual(config['active_image'], str(self.second))
            self.assertEqual(config['previous_image'], str(self.first))
            self.assertEqual(started_with.read_bytes(), b'first')
            self.assertEqual(self.main('rollback'), 0)
        config = images.load_config(self.config_path)
        self.assertEqual(config['active_image'], str(self.first))
        self.assertEqual(config['previous_image'], str(self.second))
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_failed_boot_or_version_mismatch_never_switches(self):
        for outcome in (RuntimeError('boot failed'), {**self.versions, 'codex': 'different'}):
            with self.subTest(outcome=outcome), patch.object(images, 'image_tool'), \
                    patch.object(images, 'probe', side_effect=[outcome]) as boot:
                self.assertEqual(self.main('activate', str(self.second)), 2)
                boot.assert_called_once()
                self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_changed_image_rejected_before_boot(self):
        self.second.chmod(0o600)
        self.second.write_bytes(b'changed')
        self.second.chmod(0o400)
        with patch.object(images, 'probe') as boot:
            self.assertEqual(self.main('activate', str(self.second)), 2)
            boot.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_writable_image_rejected_before_check_or_boot(self):
        self.second.chmod(0o600)
        with patch.object(images, 'image_tool') as tool, patch.object(images, 'probe') as boot:
            self.assertEqual(self.main('activate', str(self.second)), 2)
            tool.assert_not_called()
            boot.assert_not_called()

    def test_failed_update_preserves_active(self):
        with patch.object(images, 'build_candidate', side_effect=RuntimeError('installer failed')):
            self.assertEqual(self.main('update', '--allow-downloads'), 2)
        self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_build_stages_without_activation(self):
        with patch.object(images, 'build_candidate', return_value=self.second), \
                patch.object(images, 'image_tool'), patch.object(images, 'probe') as boot:
            self.assertEqual(self.main('build', '--allow-downloads'), 0)
            boot.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_downloads_require_explicit_flag(self):
        before = set(self.root.iterdir())
        with patch.object(images, 'build_candidate') as build:
            self.assertEqual(self.main('update'), 2)
            build.assert_not_called()
        self.assertEqual(set(self.root.iterdir()), before)

    def test_concurrent_operation_cannot_replace_config(self):
        with images.config_lock(self.config_path):
            self.assertEqual(self.main('activate', str(self.second)), 2)
        self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_adoption_copies_original_and_checks_only_the_copy(self):
        recovered = self.root/'building.qcow2'
        recovered.write_bytes(b'recovered data')
        original_mode = recovered.stat().st_mode
        def image_tool(*args):
            if args[0] == 'convert':
                shutil.copyfile(args[-2], args[-1])
        with patch.object(images, 'image_tool', side_effect=image_tool), \
                patch.object(images, 'probe', return_value=self.versions) as boot:
            self.assertEqual(self.main('adopt', str(recovered)), 0)
        active = Path(images.load_config(self.config_path)['active_image'])
        self.assertEqual(active.name, 'agents.qcow2')
        self.assertNotEqual(active, recovered)
        self.assertEqual(active.read_bytes(), recovered.read_bytes())
        self.assertEqual(active.stat().st_mode & 0o777, 0o400)
        self.assertEqual(recovered.stat().st_mode, original_mode)
        boot.assert_called_once()
        self.assertNotEqual(boot.call_args.args[0], recovered)

    def test_read_only_commands_create_nothing(self):
        before = set(self.root.iterdir())
        for action in ('status', 'path', 'list'):
            self.assertEqual(self.main(action), 0)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertEqual(self.config_path.read_bytes(), self.original_config)

    def test_preflight_is_read_only_and_targets_source_not_active_image(self):
        source = self.root/'cloud.qcow2'
        source.write_bytes(b'clean source')
        config = dict(self.config, source_disk=str(source), image_store=str(self.root/'new-store'))
        images.atomic_json(self.config_path, config)
        before = set(self.root.iterdir())
        with patch.object(images, 'builder_path', return_value=Path('/builder.py')), \
                patch.object(images, 'run_owned') as run:
            self.assertEqual(self.main('update', '--check'), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--disk')+1], str(source))
        self.assertIn('--check', command)
        self.assertNotIn('--allow-downloads', command)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertEqual(images.load_config(self.config_path), config)

    def test_probe_requires_successful_runtime_checks_and_all_versions(self):
        def fake_boot(command, timeout):
            directory = Path(command[command.index('--cache-dir')+1])/'run-test'
            directory.mkdir(parents=True)
            self.assertIn('--verify-agents', command)
            self.assertNotIn('--mount', command)
            self.assertNotIn('--env', command)
            images.atomic_json(directory/'verify.json', self.probe_report)
        reports = [
            {'checks': {'uid_1000': False}, 'versions': self.versions},
            {'checks': {'uid_1000': True}, 'versions': {'codex': '1.0'}},
            {'checks': {}, 'versions': self.versions},
            {'checks': {'uid_1000': True}, 'versions': self.versions},
        ]
        with patch.object(images, 'run_owned', side_effect=fake_boot):
            for self.probe_report in reports:
                with self.assertRaises(RuntimeError):
                    images.probe(self.first, self.root)
            self.probe_report = {'checks': dict.fromkeys(images.REQUIRED_CHECKS, True), 'versions': self.versions}
            self.assertEqual(images.probe(self.first, self.root), self.versions)

    def test_malformed_configuration_and_manifest_fail_cleanly(self):
        self.second.with_name('manifest.json').write_text('[]')
        self.assertEqual(self.main('activate', str(self.second)), 2)
        images.atomic_json(self.config_path, {'version': 1, 'active_image': 42})
        self.assertEqual(self.main('path'), 2)


if __name__ == '__main__':
    unittest.main()
