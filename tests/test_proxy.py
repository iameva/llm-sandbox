"""Proxy tests use local sockets and mocked DNS, with no public network."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("proxy", Path(__file__).resolve().parents[1] / "egress-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def test_hostname_boundaries(self):
        allowed = {".example.org", "api.example.com"}
        for host in ["example.org", "a.example.org", "API.EXAMPLE.COM."]:
            self.assertTrue(proxy.host_allowed(host, allowed))
        for host in ["badexample.org", "example.org.attacker.com", "127.0.0.1"]:
            self.assertFalse(proxy.host_allowed(host, allowed))

    def test_private_resolution_is_rejected(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"]:
            with self.subTest(address=address), patch.object(proxy.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            ]):
                with self.assertRaises(PermissionError):
                    proxy.resolve_public("api.example.com", 443)

    def test_summary_only_activates_successful_connections(self):
        events = [
            {"decision": "allow-unlisted", "host": "legacy.example"},
            {"decision": "deny", "host": "legacy.example"},
            {"decision": "connected", "host": "working.example"},
            {"decision": "deny", "host": "denied.example"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.log"
            log.write_text("\n".join(json.dumps(e) for e in events) + "\ninvalid json\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                proxy.summarize(log)
            active = [line.split()[0] for line in output.getvalue().splitlines()
                      if line and not line.startswith("#")]
            self.assertEqual(active, ["working.example"])

    def test_failed_resolution_never_logs_success(self):
        with proxy.ProxyServer(("127.0.0.1", 0), proxy.Handler) as server:
            server.mode = "log"
            server.allowlist = set()
            server.allowed_ports = {443}
            server.log_path = None
            events = []
            with patch.object(proxy, "resolve_public", side_effect=PermissionError("private address")), \
                 patch.object(proxy, "log_event", side_effect=lambda path, **event: events.append(event)):
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b"CONNECT private.example:443 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"502", client.recv(4096))
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            self.assertEqual([e["decision"] for e in events], ["deny"])

    def test_successful_tunnel_logs_connection_and_relays_bytes(self):
        with socket.socket() as upstream, proxy.ProxyServer(("127.0.0.1", 0), proxy.Handler) as server:
            upstream.bind(("127.0.0.1", 0))
            upstream.listen(1)
            upstream.settimeout(2)
            server.mode = "log"
            server.allowlist = set()
            server.allowed_ports = {443}
            server.log_path = None
            events = []
            infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", upstream.getsockname())]
            with patch.object(proxy, "resolve_public", return_value=infos), \
                 patch.object(proxy, "log_event", side_effect=lambda path, **event: events.append(event)):
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"200 Connection Established", client.recv(4096))
                    peer, _ = upstream.accept()
                    with peer:
                        peer.settimeout(2)
                        client.sendall(b"hello")
                        self.assertEqual(peer.recv(5), b"hello")
                        peer.sendall(b"world")
                        self.assertEqual(client.recv(5), b"world")
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            self.assertEqual([e["decision"] for e in events], ["connected"])
            self.assertFalse(events[0]["listed"])
