"""Check owned process crash/restart with the real proxy, without public traffic."""
from pathlib import Path
import socket
import tempfile
import unittest
from process_lifecycle import Processes


class LifecycleTest(unittest.TestCase):
    def test_crash_restart_keeps_other_proxy_running(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = Processes(Path(directory))
            def denial(key):
                with socket.create_connection(('127.0.0.1', processes.ports[key]), timeout=2) as client:
                    client.sendall(b'CONNECT denied.invalid:443 HTTP/1.1\r\n\r\n')
                    self.assertIn(b' 403 ', client.recv(4096))
            try:
                processes.start('a')
                processes.start('b')
                denial('a')
                denial('b')
                port = processes.ports['a']
                processes.kill('a')
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(('127.0.0.1', port), timeout=2)
                denial('b')
                processes.start('a')
                self.assertEqual(port, processes.ports['a'])
                denial('a')
                denial('b')
            finally:
                processes.close()


if __name__ == '__main__':
    unittest.main()
