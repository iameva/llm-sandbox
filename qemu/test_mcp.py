"""Host MCP forwarding boundary and real local HTTP/SSE transport, without a VM."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qemu.network_relay import MCP_ADDRESS, loopback_forward
from qemu.sandbox import qemu_command
from qemu.guest_verify import mcp_http_checks


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.server.requests.append((self.path, self.headers, {'method': 'HEAD'}))
        self.send_response(self.server.head_status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((self.path, self.headers, request))
        if self.headers['Host'] != self.server.authority:
            self.send_error(421)
            return
        if request['method'] == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'host-test', 'version': '1'}}
        else:
            result = {'content': [{'type': 'text', 'text': 'host tool result'}]}
        body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Mcp-Session-Id', 'test-session')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'event: message\ndata: first\n\n')
        self.wfile.flush()
        self.server.finish_stream.wait(5)
        self.wfile.write(b'event: message\ndata: last\n\n')
        self.wfile.flush()
        self.close_connection = True


class MCPTests(unittest.TestCase):
    def command(self, ports=()):
        return qemu_command('qemu', '/disk', '/seed', '/fs', 49123, mcp_ports=ports,
                            mcp_guard='/private run/mcp.guard')

    def test_default_has_only_the_existing_enforcing_proxy_forward(self):
        command = self.command()
        network = command[command.index('-netdev')+1]
        self.assertIn('restrict=on,ipv6=off', network)
        self.assertEqual(network.count('guestfwd='), 1)
        self.assertNotIn(MCP_ADDRESS, network)
        self.assertNotIn('hostfwd=', network)

    def test_opt_in_only_forwards_fixed_host_loopback_ports(self):
        command = self.command([8000, 8765, 8000, 3128])
        network = command[command.index('-netdev')+1]
        self.assertIn('restrict=on,ipv6=off', network)
        forwards = [item for item in network.split(',') if item.startswith('guestfwd=')]
        self.assertEqual(len(forwards), 4)
        self.assertTrue(forwards[0].startswith('guestfwd=tcp:10.0.2.100:3128-cmd:'))
        for forward, port in zip(forwards[1:], (8000, 8765, 3128)):
            endpoint, command = forward.split('-cmd:', 1)
            self.assertEqual(endpoint, f'guestfwd=tcp:{MCP_ADDRESS}:{port}')
            args = shlex.split(command)
            self.assertEqual(args[0], sys.executable)
            self.assertEqual(Path(args[1]).name, 'network_relay.py')
            self.assertEqual(args[2:], ['--guard', '/private run/mcp.guard',
                                       '--tcp', '127.0.0.1', str(port)])
        self.assertNotIn('hostfwd=', network)
        self.assertNotIn('10.0.2.2', network)

    def test_invalid_ports_cannot_inject_destinations_or_qemu_options(self):
        for port in (0, -1, 1023, 65536, True, '8000', '8000,restrict=off', '$(touch /tmp/no)'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.command([port])
        self.command([1024, 65535])

    def test_mcp_forward_requires_guard_and_rejects_qemu_separator(self):
        with self.assertRaisesRegex(ValueError, 'lifetime guard'):
            qemu_command('qemu', '/disk', '/seed', '/fs', 49123, mcp_ports=[8000])
        with self.assertRaisesRegex(ValueError, 'commas'):
            qemu_command('qemu', '/disk', '/seed', '/fs', 49123, mcp_ports=[8000],
                         mcp_guard='/private,unsafe/mcp.guard')

    def host_server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), MCPHandler)
        server.daemon_threads = True
        server.requests = []
        server.head_status = 404
        server.finish_stream = threading.Event()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.finish_stream.set()
            server.shutdown()
            server.server_close()
            worker.join(5)
        self.addCleanup(close)
        return server

    def test_http_initialize_and_tool_call_preserve_host_and_session_headers(self):
        host = self.host_server()
        with loopback_forward(0, host.server_address) as address:
            self.assertEqual(address[0], '127.0.0.1')
            host.authority = f'127.0.0.1:{address[1]}'
            client = http.client.HTTPConnection(*address, timeout=5)
            try:
                for index, method in enumerate(('initialize', 'tools/call')):
                    body = json.dumps({'jsonrpc': '2.0', 'id': index, 'method': method,
                                       'params': {'name': 'lookup'} if index else {}})
                    client.request('POST', '/mcp', body,
                                   {'Content-Type': 'application/json',
                                    'Accept': 'application/json, text/event-stream',
                                    'Origin': 'http://'+host.authority,
                                    'Mcp-Session-Id': 'test-session'})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader('Mcp-Session-Id'), 'test-session')
                    result = json.loads(response.read())
                    self.assertEqual(result['id'], index)
                self.assertEqual(result['result']['content'][0]['text'], 'host tool result')
                for path, headers, request in host.requests:
                    self.assertEqual(path, '/mcp')
                    self.assertEqual(headers['Host'], host.authority)
                    self.assertEqual(headers['Origin'], 'http://'+host.authority)
                    self.assertEqual(headers['Mcp-Session-Id'], 'test-session')
                    self.assertNotIn('Authorization', headers)
            finally:
                client.close()
        # Closing the guest listener does not manage or stop the external host server.
        with socket.create_connection(host.server_address, timeout=5):
            pass
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=1)

    def test_sse_is_streamed_before_upstream_closes(self):
        host = self.host_server()
        with loopback_forward(0, host.server_address) as address:
            client = http.client.HTTPConnection(*address, timeout=5)
            try:
                client.request('GET', '/mcp', headers={'Accept': 'text/event-stream'})
                response = client.getresponse()
                self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
                self.assertEqual(response.readline(), b'event: message\n')
                self.assertEqual(response.readline(), b'data: first\n')
                self.assertEqual(response.readline(), b'\n')
                self.assertFalse(host.finish_stream.is_set())
                host.finish_stream.set()
                self.assertEqual(response.read(), b'event: message\ndata: last\n\n')
            finally:
                host.finish_stream.set()
                client.close()

    def test_check_validates_http_through_forward_without_auth_or_tool_calls(self):
        host = self.host_server()
        with loopback_forward(0, host.server_address) as address:
            for status in (200, 401, 404, 405):
                host.head_status = status
                self.assertEqual(mcp_http_checks([address[1]]),
                                 {f'mcp_http_transport:{address[1]}': True})
        for path, headers, request in host.requests:
            self.assertEqual((path, request), ('/', {'method': 'HEAD'}))
            self.assertNotIn('Authorization', headers)
        self.assertEqual(mcp_http_checks([]), {})

    def test_check_fails_when_only_guest_listener_is_reachable(self):
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            with loopback_forward(0, unavailable.getsockname()) as address:
                self.assertEqual(mcp_http_checks([address[1]]),
                                 {f'mcp_http_transport:{address[1]}': False})

    def test_context_exit_closes_existing_streams_not_only_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            with loopback_forward(0, listener.getsockname()) as address:
                client = socket.create_connection(address, timeout=3)
                self.addCleanup(client.close)
                host, _ = listener.accept()
                self.addCleanup(host.close)
                host.settimeout(3)
                host.sendall(b'hello')
                self.assertEqual(client.recv(5), b'hello')
            # Neither peer had closed; exiting the context must cancel both legs.
            self.assertEqual(client.recv(1), b'')
            self.assertEqual(host.recv(1), b'')

    def test_unavailable_host_closes_connection_without_fallback(self):
        # Hold the port without listening, so no unrelated process can take it.
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            with loopback_forward(0, unavailable.getsockname()) as address:
                with socket.create_connection(address, timeout=5) as client:
                    try:
                        self.assertEqual(client.recv(1), b'')
                    except ConnectionResetError:
                        pass


if __name__ == '__main__':
    unittest.main()
