import json
from pathlib import Path
import tempfile
import unittest

from build_support import BuildMonitor, EXPECTED_AGENTS, read_report


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.monitor = BuildMonitor(self.base, self.base, now=0)

    def write_report(self, value):
        (self.base/'build-result.json').write_text(json.dumps(value))

    def test_failure_report_stops_immediately(self):
        self.write_report({'ok': False, 'error': 'installer exit 1'})
        with self.assertRaisesRegex(RuntimeError, 'installer exit 1'):
            self.monitor.poll(now=1)

    def test_missing_boot_progress_times_out(self):
        with self.assertRaisesRegex(TimeoutError, 'did not start'):
            self.monitor.poll(now=601)

    def test_success_requires_every_version(self):
        self.write_report({'ok': True, 'versions': {'codex': '1'}})
        with self.assertRaisesRegex(RuntimeError, 'five tool versions'):
            read_report(self.base/'build-result.json')

    def test_successful_install_cannot_wait_forever_for_shutdown(self):
        self.write_report({'ok': True, 'versions': {name: '1' for name in EXPECTED_AGENTS}})
        self.monitor.poll(now=1)
        with self.assertRaisesRegex(TimeoutError, 'shut down'):
            self.monitor.poll(now=92)

    def test_cloud_init_failure_without_report_stops(self):
        (self.base/'console.log').write_text('Failed to run module scripts_user')
        with self.assertRaisesRegex(RuntimeError, 'cloud-init'):
            self.monitor.poll(now=100)

    def test_stage_progress_does_not_require_console_output(self):
        (self.base/'build-status.json').write_text('{"stage":"Installing codex"}')
        self.monitor.poll(now=700)
        self.assertEqual(self.monitor.stage, 'Installing codex')


if __name__ == '__main__':
    unittest.main()
