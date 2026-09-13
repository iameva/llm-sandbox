"""Local-only transport controls for QEMU's restricted user network probe.

The fixture permits exactly control.invalid:443 and relays to a fixed local
receiver. It is not the production egress proxy or a general network service.
"""
import importlib.util
import json
import ssl
import struct
from pathlib import Path
import shlex
import socket
import socketserver
import sys
import threading


class Echo(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(180)
        try:
            while data := self.rfile.readline(128):
                self.server.received.append(data.decode().strip())
                self.wfile.write(data)
        except OSError:
            pass


class Proxy(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        with self.server.active_lock:
            self.server.active.add(self.request)

    def finish(self):
        with self.server.active_lock:
            self.server.active.discard(self.request)
        super().finish()

    def handle(self):
        self.request.settimeout(5)
        try:
            line = self.rfile.readline(256)
            for _ in range(16):
                if self.rfile.readline(256) in (b'\r\n', b'\n', b''):
                    break
            else:
                return
            if line != b'CONNECT control.invalid:443 HTTP/1.1\r\n':
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                return
            with socket.create_connection(self.server.target, timeout=5) as upstream:
                upstream.settimeout(180)
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.wfile.flush()
                self.request.settimeout(180)
                while data := self.rfile.readline(128):
                    upstream.sendall(data)
                    self.wfile.write(upstream.recv(128))
        except OSError:
            pass


class TCP(socketserver.ThreadingTCPServer):
    daemon_threads = True


class TCP6(TCP):
    address_family = socket.AF_INET6


class UDS(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class UDP(socketserver.ThreadingUDPServer):
    daemon_threads = True


class DatagramEcho(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        self.server.received.append(data.decode(errors='replace'))
        sock.sendto(data, self.client_address)


class Controls:
    def __init__(self, base, lifecycle=False, broad=False, public=False):
        self.base = base
        self.lifecycle = lifecycle
        self.broad = broad
        self.public = public
        self.datagrams = {}
        self.production_ports = {}
        self.process_ports = {}
        self.ipv6_targets = {}
        self.public_controls = None
        self.proxies = {}
        self.servers = []
        self.targets = {}

    def start(self, server):
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def configure(self, key):
        target = self.start(TCP(('127.0.0.1', 0), Echo))
        target.received = []
        self.targets[key] = target
        path = self.base/f'{key}.proxy.sock'
        proxy = UDS(str(path), Proxy)
        proxy.active = set()
        proxy.active_lock = threading.Lock()
        self.start(proxy)
        path.chmod(0o600)
        proxy.target = target.server_address
        self.proxies[key] = proxy
        # Host positive control before interpreting guest failures.
        with socket.create_connection(target.server_address, timeout=3) as c:
            c.sendall(b'host-control\n')
            if c.recv(128) != b'host-control\n':
                raise RuntimeError('host receiver control failed')
        extra = {}
        if self.broad:
            datagram = UDP(('127.0.0.1', 0), DatagramEcho)
            datagram.received = []
            self.datagrams[key] = self.start(datagram)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
                control.settimeout(3)
                control.sendto(b'host-control', datagram.server_address)
                if control.recv(128) != b'host-control':
                    raise RuntimeError('host UDP control failed')
            spec = importlib.util.spec_from_file_location('production_proxy', Path(__file__).resolve().parents[2]/'egress-proxy.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            production = module.ProxyServer(('127.0.0.1', 0), module.Handler)
            production.mode = 'enforce'
            production.allowed_ports = {443}
            production.allowlist = {'example.com'} if self.public else set()
            production.log_path = str(self.base/f'{key}.production-proxy.log')
            self.production_ports[key] = production.server_address[1]
            self.start(production)
            if self.public and self.public_controls is None:
                self.public_controls = public_controls()
            ipv6_control = {'reachable': False}
            try:
                ipv6_target = TCP6(('::1', 0), Echo)
                ipv6_target.received = []
                self.ipv6_targets[key] = self.start(ipv6_target)
                with socket.create_connection(('::1', ipv6_target.server_address[1]), timeout=3) as control:
                    control.sendall(b'host-ipv6-control\n')
                    if control.recv(128) != b'host-ipv6-control\n':
                        raise RuntimeError('host IPv6 receiver control failed')
                ipv6_control = {'reachable': True, 'port': ipv6_target.server_address[1]}
            except OSError as exc:
                ipv6_control['error'] = str(exc)
            extra = {'ipv6_control': ipv6_control, 'udp_port': datagram.server_address[1], 'public': self.public,
                     'public_controls': self.public_controls or {}}
        return {**extra, 'broad': self.broad, 'key': key, 'lifecycle': self.lifecycle, 'host_port': target.server_address[1],
                'prefix': '10.0.2' if key == 'a' else '10.0.3',
                'other_prefix': '10.0.3' if key == 'a' else '10.0.2'}

    def argv(self, key):
        prefix = '10.0.2' if key == 'a' else '10.0.3'
        cmd = shlex.join([sys.executable, str(Path(__file__).with_name('network_relay.py').resolve()),
                          str(self.base/f'{key}.proxy.sock')])
        if ',' in cmd:
            raise ValueError('QEMU guestfwd command paths must not contain commas')
        options = f'user,id=n,net={prefix}.0/24,restrict=on,ipv6=off,guestfwd=tcp:{prefix}.100:3128-cmd:{cmd}'
        if self.broad:
            production_cmd = shlex.join([sys.executable, str(Path(__file__).with_name('network_relay.py').resolve()),
                                         '--tcp', '127.0.0.1', str(self.production_ports[key])])
            options += f',guestfwd=tcp:{prefix}.100:3129-cmd:{production_cmd}'
        if key in self.process_ports:
            command = shlex.join([sys.executable, str(Path(__file__).with_name('network_relay.py').resolve()),
                                  '--tcp', '127.0.0.1', str(self.process_ports[key])])
            options += f',guestfwd=tcp:{prefix}.100:3131-cmd:{command}'
        return ['-netdev', options, '-device', 'virtio-net-pci,netdev=n']

    def evaluate(self, share):
        reports = {k: json.loads((share/f'{k}.network.json').read_text()) for k in ('a', 'b')}
        checks = {
            'both_allowed_tunnels_work': all(r['allowed'] for r in reports.values()),
            'both_denied_connects_refused': all(r['denied'] for r in reports.values()),
            'direct_host_tcp_blocked': all(not r['direct_host'] for r in reports.values()),
            'cross_vm_tcp_blocked': all(not r['other_vm'] for r in reports.values()),
            'both_guest_listener_controls_work': all(r['self_listener'] for r in reports.values()),
            'host_receivers_only_expected_data': all(v.received == (['host-control', 'allowed-'+k] + (['after-stop-b', 'after-vm-stop-b'] if self.lifecycle and k == 'b' else [])) for k, v in self.targets.items()),
        }
        if self.lifecycle:
            stages = {k: json.loads((share/f'{k}.lifecycle.json').read_text()) for k in ('a', 'b')}
            a, b = stages['a'], stages['b']
            checks.update({
                'a_proxy_failure_blocks_new_tunnels': not a['allowed_after_proxy_a_stop'],
                'both_local_controls_survive_proxy_stop': all(r['self_listener_after_proxy_a_stop'] for r in stages.values()),
                'no_direct_host_fallback_after_proxy_stop': all(not r['direct_host_after_proxy_a_stop'] for r in stages.values()),
                'cross_vm_still_blocked_after_proxy_stop': all(not r['other_vm_after_proxy_a_stop'] for r in stages.values()),
                'b_proxy_unaffected_by_a_proxy_stop': b['allowed_after_proxy_a_stop'] and b['denied_after_proxy_a_stop'],
                'b_proxy_unaffected_by_a_vm_stop': b['allowed_after_vm_a_stop'] and b['denied_after_vm_a_stop'],
                'b_direct_host_still_blocked_after_a_vm_stop': not b['direct_host_after_vm_a_stop'],
                'b_local_control_survives_a_vm_stop': b['self_listener_after_vm_a_stop'],
            })
            reports['lifecycle'] = stages
        if self.broad:
            broad = {k: json.loads((share/f'{k}.broad.json').read_text()) for k in ('a', 'b')}
            for key, report in broad.items():
                checks.update({key+'_'+name: value for name, value in report['checks'].items()})
            checks['host_ipv6_receivers_only_controls'] = all(s.received == ['host-ipv6-control'] for s in self.ipv6_targets.values())
            checks['host_udp_receivers_only_controls'] = all(s.received == ['host-control'] for s in self.datagrams.values())
            checks['a_active_stream_closed_on_proxy_stop'] = reports['lifecycle']['a']['active_stream_after_stop'] == 'closed'
            checks['b_active_stream_survives_a_proxy_stop'] = reports['lifecycle']['b']['active_stream_after_stop'] == 'echo'
            # Streams add phase tokens beyond the original single requests.
            for key, server in self.targets.items():
                expected = ['host-control', 'allowed-'+key, 'stream-before-'+key]
                if key == 'b':
                    expected += ['stream-after-b', 'after-stop-b', 'after-vm-stop-b']
                checks[key+'_receiver_phase_tokens'] = sorted(server.received) == sorted(expected)
            checks.pop('host_receivers_only_expected_data')
            reports['broad'] = broad
        return checks, reports

    def stop_proxy(self, key):
        proxy = self.proxies[key]
        proxy.shutdown()
        if self.broad:
            with proxy.active_lock:
                for connection in list(proxy.active):
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        proxy.server_close()
        self.servers.remove(proxy)
        # A stale pathname remains intentionally: connection must fail closed.
        with socket.socket(socket.AF_UNIX) as control:
            control.settimeout(3)
            try:
                control.connect(str(self.base/f'{key}.proxy.sock'))
            except ConnectionRefusedError:
                return
            raise RuntimeError('stopped proxy still accepts connections')

    def close(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()


def public_controls():
    """Explicit opt-in: a few TCP connects, one DNS query, and one HTTPS GET."""
    result = {}
    for name, address, port in [('ipv4_tcp', '1.1.1.1', 443),
                                ('ipv6_tcp', '2606:4700:4700::1111', 443),
                                ('dns_tcp', '1.1.1.1', 53),
                                ('https', 'example.com', 443)]:
        entry = {'address': address, 'port': port, 'reachable': False}
        try:
            with socket.create_connection((address, port), timeout=5) as conn:
                if name == 'https':
                    with ssl.create_default_context().wrap_socket(conn, server_hostname=address) as tls:
                        tls.settimeout(10)
                        tls.sendall(b'GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n')
                        entry['reachable'] = tls.recv(1024).startswith(b'HTTP/1.')
                else:
                    entry['reachable'] = True
        except OSError as exc:
            entry['error'] = str(exc)
        result[name] = entry
    packet = struct.pack('!6H', 0x51A7, 0x100, 1, 0, 0, 0) + b'\x07example\x03com\0\0\x01\0\x01'
    entry = {'reachable': False, 'address': '1.1.1.1', 'port': 53}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(5)
            client.sendto(packet, ('1.1.1.1', 53))
            reply, addr = client.recvfrom(1024)
            entry['reachable'] = addr == ('1.1.1.1', 53) and len(reply) >= 12 and reply[:2] == packet[:2] and bool(reply[2] & 128)
    except OSError as exc:
        entry['error'] = str(exc)
    result['dns_udp'] = entry
    return result
