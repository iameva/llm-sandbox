"""Local tests of the broad probe fixtures; no QEMU or public networking."""
import socket
import tempfile
import unittest
from pathlib import Path

from network_support import Controls
from guest_network_broad import dns_packet, status, udp


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.controls = Controls(Path(self.directory.name), lifecycle=True, broad=True)
        self.a = self.controls.configure('a')
        self.b = self.controls.configure('b')

    def tearDown(self):
        self.controls.close()
        self.directory.cleanup()

    def stream(self, key):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(3)
        client.connect(str(Path(self.directory.name)/f'{key}.proxy.sock'))
        client.sendall(b'CONNECT control.invalid:443 HTTP/1.1\r\n\r\n')
        file = client.makefile('rb')
        self.assertIn(b' 200 ', file.readline())
        self.assertEqual(file.readline(), b'\r\n')
        client.sendall(b'before\n')
        self.assertEqual(file.readline(), b'before\n')
        return client, file

    def test_active_failure_is_scoped_to_one_proxy(self):
        a, af = self.stream('a')
        b, bf = self.stream('b')
        try:
            self.controls.stop_proxy('a')
            self.assertEqual(af.readline(), b'')
            b.sendall(b'after\n')
            self.assertEqual(bf.readline(), b'after\n')
            with socket.socket(socket.AF_UNIX) as stopped:
                with self.assertRaises(ConnectionRefusedError):
                    stopped.connect(str(Path(self.directory.name)/'a.proxy.sock'))
        finally:
            af.close(); a.close(); bf.close(); b.close()

    def test_real_handler_denies_without_upstream_connections(self):
        address = ('127.0.0.1', self.controls.production_ports['a'])
        requests = [
            (b'GET / HTTP/1.1\r\n\r\n', 405),
            (b'nonsense\r\n\r\n', 400),
            (b'CONNECT denied.invalid:443 HTTP/1.1\r\n\r\n', 403),
            (b'CONNECT example.com:80 HTTP/1.1\r\n\r\n', 403),
            (b'CONNECT example.com:bad HTTP/1.1\r\n\r\n', 400),
            (b'CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n', 403),
        ]
        for request, expected in requests:
            with self.subTest(request=request):
                self.assertEqual(status(address, request), expected)
        self.assertEqual(self.controls.targets['a'].received, ['host-control'])

    def test_udp_positive_control_and_payload(self):
        packet = dns_packet()
        self.assertEqual(len(packet), 29)
        reply = udp(('127.0.0.1', self.a['udp_port']), packet)
        self.assertTrue(reply['matched'])

    def test_guest_forwarding_keeps_restrictions(self):
        argv = self.controls.argv('a')
        self.assertIn('restrict=on,ipv6=off', argv[1])
        self.assertIn('10.0.2.100:3128-cmd:', argv[1])
        self.assertIn('10.0.2.100:3129-cmd:', argv[1])
        self.assertNotIn('hostfwd=', argv[1])


if __name__ == '__main__':
    unittest.main()
