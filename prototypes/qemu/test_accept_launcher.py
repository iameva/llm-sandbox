"""Check the disposable host acceptance harness without booting a VM."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class AcceptanceTests(unittest.TestCase):
    def test_serial_tty_does_not_select_batch_guest_for_resize_probe(self):
        from accept_launcher import GUEST
        # Execute the actual guest preflight, stopping before workspace and
        # database setup. Both guests have a serial TTY; only A is selected.
        preflight = compile(GUEST.split("share = pathlib.Path('/workspace')", 1)[0],
                            'guest-preflight', 'exec')
        for key, flags in [('a', []), ('b', []), ('a', ['--terminal-probe'])]:
            with self.subTest(key=key, flags=flags):
                scope = {}
                with patch.object(sys, 'argv', ['probe.py', key, '0', *flags]), \
                     patch.object(os, 'isatty', return_value=True), \
                     patch.object(os, 'get_terminal_size', side_effect=[(100, 30), (132, 44), (132, 44)]) as size, \
                     patch.object(Path, 'write_text') as write_marker:
                    exec(preflight, scope)
                if flags:
                    self.assertEqual(scope['terminal_checks'], {
                        'initial_terminal_size': True, 'live_terminal_resize': True})
                    write_marker.assert_called_once_with('ready')
                else:
                    self.assertEqual(scope['terminal_checks'], {})
                    size.assert_not_called()
                    write_marker.assert_not_called()

    def test_acceptance_diagnostics_are_bounded_and_do_not_follow_links(self):
        from accept_launcher import collect_diagnostics
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'canary').write_text('must not be read')
            (base/'a.log').symlink_to(base/'canary')
            (base/'b.log').write_text('x'*10000+'failure details')
            run = base/'runs/run-example'
            run.mkdir(parents=True)
            (run/'exit.json').write_text('{"returncode": 0}')
            os.mkfifo(run/'console.log')
            result = collect_diagnostics(base)
            self.assertNotIn('must not be read', json.dumps(result))
            self.assertEqual(len(result['b.log']), 8192)
            self.assertTrue(result['b.log'].endswith('failure details'))
            self.assertIn('runs/run-example/exit.json', result)
            self.assertNotIn('runs/run-example/console.log', result)

