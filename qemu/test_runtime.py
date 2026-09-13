"""Exercise disk cleanup, terminal exchange and relay behavior without a VM."""
import fcntl
import json
import contextlib
import io
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qemu.runtime_support import disk_cache, finish_artifacts, prune_artifacts, write_size, validate_base_image
from qemu.sandbox_guest import apply_terminal_size
from qemu.network_relay import relay
from qemu.sandbox import qemu_command, check_helpers


class RuntimeTests(unittest.TestCase):
    def test_helper_exit_before_qemu_exit_preserves_qemu_status(self):
        for status in (0, 3):
            with self.subTest(status=status):
                helper = subprocess.Popen([sys.executable, '-c', 'pass'])
                helper.wait(timeout=5)
                vm = subprocess.Popen([sys.executable, '-c',
                                       f'import time; time.sleep(.1); raise SystemExit({status})'])
                try:
                    self.assertIsNone(vm.poll())
                    check_helpers(vm, [('workspace virtiofsd', helper)])
                    self.assertEqual(vm.returncode, status)
                finally:
                    if vm.poll() is None:
                        vm.kill()
                    vm.wait(timeout=5)

    def test_helper_failure_is_fatal_if_vm_keeps_running(self):
        helper = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])
        helper.wait(timeout=5)
        vm = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            with self.assertRaisesRegex(RuntimeError, r'HTTPS proxy \(exit 7\)'):
                check_helpers(vm, [('HTTPS proxy', helper)], shutdown_grace=.01)
            self.assertIsNone(vm.poll())
        finally:
            vm.kill()
            vm.wait(timeout=5)


    def test_memory_configuration_matches_shared_memory_object(self):
        argv = qemu_command('qemu', '/disk', '/seed', '/sock', 1234, 4096, 4)
        self.assertEqual(argv[argv.index('-m')+1], '4096')
        self.assertEqual(argv[argv.index('-smp')+1], '4')
        self.assertIn('memory-backend-memfd,id=mem,size=4096M,share=on', argv)

    def test_guest_setup_failure_produces_error_report(self):
        from qemu import sandbox_guest
        with tempfile.TemporaryDirectory() as directory:
            def guest_path(value):
                return Path(directory)/Path(value).name
            # Redirect both the pending path and its rename destination.
            original_rename = Path.rename
            def rename(path, target):
                return original_rename(path, guest_path(target))
            with patch.object(sandbox_guest, 'session', side_effect=RuntimeError('mount failed')), \
                 patch.object(sandbox_guest, 'Path', side_effect=guest_path), \
                 patch.object(Path, 'rename', rename), \
                 patch.object(sandbox_guest.os, 'geteuid', return_value=1000), \
                 contextlib.redirect_stderr(io.StringIO()):
                sandbox_guest.main()
            report = json.loads((Path(directory)/'exit.json').read_text())
            self.assertEqual(report, {'returncode': 2, 'error': 'RuntimeError: mount failed'})

    def test_effective_cache_filesystem_is_checked_without_mount_order_assumptions(self):
        for filesystem in ('tmpfs', 'ramfs', 'xfs', 'ext4'):
            with self.subTest(filesystem=filesystem), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)/'new-cache'
                response = subprocess.CompletedProcess([], 0, filesystem+'\n', '')
                with patch('qemu.runtime_support.subprocess.run', return_value=response) as query:
                    if filesystem in ('tmpfs', 'ramfs'):
                        with self.assertRaisesRegex(ValueError, 'disk-backed'):
                            disk_cache(cache)
                    else:
                        self.assertEqual(disk_cache(cache), cache.resolve())
                self.assertEqual(query.call_args.args[0],
                                 ['stat', '--file-system', '--format=%T', '--', directory])
                self.assertFalse(cache.exists())

    def test_cache_filesystem_query_failure_is_rejected(self):
        with patch('qemu.runtime_support.subprocess.run', side_effect=FileNotFoundError('stat missing')):
            with self.assertRaisesRegex(ValueError, 'cannot determine cache filesystem'):
                disk_cache(Path('/tmp'))

    def test_base_image_rejects_any_write_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = Path(directory)/'image.qcow2'
            disk.touch()
            for mode in (0o600, 0o420, 0o402):
                disk.chmod(mode)
                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, 'read-only'):
                    validate_base_image(disk)
            disk.chmod(0o400)
            validate_base_image(disk)
            self.assertEqual(disk.stat().st_mode & 0o777, 0o400)

    def test_terminal_pixel_fields_do_not_trigger_a_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory)
            write_size(report, (100, 30))
            current = struct.pack('HHHH', 30, 100, 1920, 1080)
            with patch.object(os, 'isatty', return_value=True), \
                 patch.object(fcntl, 'ioctl', return_value=current) as ioctl:
                apply_terminal_size(0, report/'terminal.json')
            ioctl.assert_called_once_with(0, termios.TIOCGWINSZ, b'\0'*8)

    def test_terminal_sizes_and_malformed_updates(self):
        master, slave = os.openpty()
        try:
            with tempfile.TemporaryDirectory() as directory:
                report = Path(directory)
                for columns, rows in [(100, 30), (132, 44)]:
                    write_size(report, (columns, rows))
                    apply_terminal_size(slave, report/'terminal.json')
                    self.assertEqual(os.get_terminal_size(slave), (columns, rows))
                (report/'terminal.json').write_text('{"rows": -1, "columns": "bad"}')
                apply_terminal_size(slave, report/'terminal.json')
                self.assertEqual(os.get_terminal_size(slave), (132, 44))
        finally:
            os.close(master)
            os.close(slave)

    def test_terminal_update_does_not_follow_guest_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            canary = base/'canary'
            canary.write_text('unchanged')
            report = base/'report'
            report.mkdir()
            (report/'terminal.json').symlink_to(canary)
            write_size(report, (80, 24))
            self.assertEqual(canary.read_text(), 'unchanged')
            self.assertFalse((report/'terminal.json').is_symlink())

    def test_retention_removes_disks_and_secrets_and_leaves_active_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            active = cache/'run-active'
            active.mkdir()
            for index in range(7):
                base = cache/f'run-{index}'
                base.mkdir()
                (base/'seed').mkdir()
                (base/'seed/session.json').write_text('secret')
                (base/'seed.iso').write_text('secret')
                (base/'overlay.qcow2').write_text('guest credentials')
                (base/'report').mkdir()
                (base/'report/link').symlink_to(active, target_is_directory=True)
                (base/'report/locked').mkdir(mode=0o000)
                (base/'console.log').write_bytes(b'x'*(2*1024*1024))
                finish_artifacts(base, keep=True)
                self.assertFalse((base/'overlay.qcow2').exists())
                self.assertFalse((base/'seed').exists())
                self.assertFalse((base/'report').exists())
                self.assertEqual((base/'console.log').stat().st_size, 1024*1024)
            prune_artifacts(cache)
            self.assertTrue(active.exists())
            self.assertEqual(len(list(cache.glob('run-*'))), 6)
            finish_artifacts(active, keep=False)
            self.assertFalse(active.exists())

    def test_relay_handles_backpressure_and_input_half_close(self):
        incoming_read, incoming_write = os.pipe()
        outgoing_read, outgoing_write = os.pipe()
        proxy, upstream = socket.socketpair()
        errors = []
        def pump():
            try:
                relay(proxy, incoming_read, outgoing_write)
            except Exception as exc:
                errors.append(exc)
            finally:
                os.close(outgoing_write)
        worker = threading.Thread(target=pump, daemon=True)
        worker.start()
        payload = b'x'*(1024*1024)
        def send():
            with os.fdopen(incoming_write, 'wb') as target:
                target.write(payload)
        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        upstream.settimeout(5)
        received = bytearray()
        try:
            time.sleep(.05)
            while True:
                block = upstream.recv(65536)
                if not block:
                    break
                received.extend(block)
            self.assertEqual(bytes(received), payload)
            upstream.sendall(b'final reply')
            upstream.shutdown(socket.SHUT_WR)
            with os.fdopen(outgoing_read, 'rb') as source:
                self.assertEqual(source.read(), b'final reply')
            worker.join(5)
            sender.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        finally:
            proxy.close()
            upstream.close()
            os.close(incoming_read)

    def test_launcher_creates_overlay_and_reports_early_guest_failure(self):
        from qemu import sandbox
        for guest_succeeds in (True, False):
            with self.subTest(guest_succeeds=guest_succeeds), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                disk = root/'base.qcow2'
                disk.write_bytes(b'unchanged base')
                disk.chmod(0o400)
                workspace = root/'workspace'
                workspace.mkdir()
                allow = root/'allow.txt'
                allow.write_text('example.com\n')
                cache = root/'cache'
                commands = []
                class Child:
                    returncode = None
                    def poll(self):
                        return self.returncode
                    def terminate(self):
                        self.returncode = 0
                    def wait(self, timeout=None):
                        return self.returncode
                class Proxy:
                    ports = {'sandbox': 1234}
                    children = {'sandbox': Child()}
                    def __init__(self, *args, **kwargs):
                        pass
                    def start(self, key):
                        pass
                    def close(self):
                        pass
                def run(argv, **kwargs):
                    commands.append(argv)
                    if argv[0].endswith('qemu-img'):
                        Path(argv[-1]).write_bytes(b'private overlay')
                def popen(argv, **kwargs):
                    commands.append(argv)
                    child = Child()
                    if '--socket-path' in argv:
                        Path(argv[argv.index('--socket-path')+1]).touch()
                    else:
                        overlay = Path(argv[argv.index('-drive')+1].split('file=', 1)[1].split(',')[0])
                        self.assertNotEqual(overlay, disk)
                        self.assertTrue(overlay.is_file())
                        if guest_succeeds:
                            (overlay.parent/'report/exit.json').write_text('{"returncode": 0}')
                        child.returncode = 0
                    return child
                stderr = io.StringIO()
                with patch.object(sys, 'argv', ['sandbox', '--disk', str(disk), '--workspace', str(workspace),
                                              '--allow-file', str(allow), '--cache-dir', str(cache), '--batch']), \
                     patch.object(sandbox, 'disk_cache', side_effect=lambda path: path), \
                     patch.object(sandbox.shutil, 'which', side_effect=lambda name: '/fake/'+name), \
                     patch.object(sandbox.os, 'access', return_value=True), \
                     patch.object(sandbox.os, 'geteuid', return_value=1000), \
                     patch.object(sandbox, 'Processes', Proxy), \
                     patch.object(sandbox.subprocess, 'run', side_effect=run), \
                     patch.object(sandbox.subprocess, 'Popen', side_effect=popen), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    result = sandbox.main()
                self.assertEqual(result, 0 if guest_succeeds else 2)
                self.assertEqual(disk.read_bytes(), b'unchanged base')
                creation = next(argv for argv in commands if argv[0].endswith('qemu-img'))
                self.assertEqual(creation[1:7], ['create', '-f', 'qcow2', '-F', 'qcow2', '-b'])
                self.assertFalse(Path(creation[-1]).exists())
                if guest_succeeds:
                    self.assertEqual(list(cache.glob('run-*')), [])
                else:
                    self.assertIn('guest stopped without a valid exit report', stderr.getvalue())
                    self.assertEqual(len(list(cache.glob('run-*/finished'))), 1)
