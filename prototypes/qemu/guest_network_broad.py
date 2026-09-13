"""Additional guest-root probes. All public requests require host opt-in."""
import concurrent.futures
import socket
import ssl
import struct
import subprocess
import threading


def dns_packet():
    return struct.pack('!6H', 0x51A7, 0x100, 1, 0, 0, 0) + b'\x07example\x03com\0\0\x01\0\x01'


def udp(address, payload):
    family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as client:
        client.settimeout(3)
        try:
            client.sendto(payload, address)
            data, _ = client.recvfrom(1024)
            return {'reply': True, 'matched': data == payload, 'bytes': len(data)}
        except OSError as exc:
            return {'reply': False, 'error': str(exc)}


def status(address, request):
    with socket.create_connection(address, timeout=5) as client:
        client.settimeout(8)
        client.sendall(request)
        stream = client.makefile('rb')
        line = stream.readline(1024)
        parts = line.split()
        return int(parts[1]) if len(parts) > 1 else 0


def tcp(address):
    try:
        with socket.create_connection(address, timeout=3):
            return True
    except OSError:
        return False


def run(cfg, interface):
    prefix = cfg['prefix']
    proxy = (prefix+'.100', 3128)
    production = (prefix+'.100', 3129)
    checks, notes, details = {}, {}, {}
    # A real local UDP receiver inside this VM is a positive control for the
    # guest socket code. Host UDP receivers independently confirm delivery.
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('0.0.0.0', 18081))
    listener.settimeout(.2)
    stopped = threading.Event()
    def echo():
        while not stopped.is_set():
            try:
                data, addr = listener.recvfrom(1024)
                listener.sendto(data, addr)
            except socket.timeout:
                continue
    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    try:
        checks['udp_guest_positive_control'] = udp((prefix+'.15', 18081), b'self-control')['matched']
        details['host_udp'] = udp((prefix+'.2', cfg['udp_port']), ('guest-'+cfg['key']).encode())
        checks['host_udp_no_reply'] = not details['host_udp']['reply']
        details['builtin_dns'] = udp((prefix+'.3', 53), dns_packet())
        checks['builtin_dns_no_reply'] = not details['builtin_dns']['reply']
        checks['builtin_dns_tcp_blocked'] = not tcp((prefix+'.3', 53))
        # An off-list port on the permitted forwarding address must not open.
        checks['proxy_address_other_port_blocked'] = not tcp((prefix+'.100', 3130))
        requests = {
            'plain_http': (b'GET http://example.com/ HTTP/1.1\r\n\r\n', 405),
            'malformed_line': (b'nonsense\r\n\r\n', 400),
            'unlisted_host': (b'CONNECT denied.invalid:443 HTTP/1.1\r\n\r\n', 403),
            'wrong_port': (b'CONNECT example.com:80 HTTP/1.1\r\n\r\n', 403),
            'malformed_port': (b'CONNECT example.com:bad HTTP/1.1\r\n\r\n', 400),
            'private_literal': (b'CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n', 403),
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(status, production, request) for name, (request, _) in requests.items()}
            for name, future in futures.items():
                actual = future.result()
                details[name] = actual
                checks['production_'+name] = actual == requests[name][1]
        # Attempt to enable IPv6 in the guest while the host backend remains
        # ipv6=off. No host sysctl is changed. Failed setup is inconclusive.
        setup = [
            ['sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=0'],
            ['sysctl', '-w', f'net.ipv6.conf.{interface}.disable_ipv6=0'],
            ['ip', '-6', 'addr', 'replace', 'fec0::15/64', 'dev', interface, 'nodad'],
            ['ip', '-6', 'route', 'replace', 'default', 'via', 'fec0::2', 'dev', interface],
        ]
        outcomes = [subprocess.run(cmd, capture_output=True, text=True) for cmd in setup]
        details['ipv6_setup'] = [{'code': p.returncode, 'error': p.stderr.strip()} for p in outcomes]
        if all(p.returncode == 0 for p in outcomes):
            with socket.socket(socket.AF_INET6) as control:
                control.bind(('::1', 0));control.listen(1)
                checks['ipv6_guest_positive_control'] = tcp(('::1', control.getsockname()[1]))
            checks['ipv4_mapped_host_alias_blocked'] = not tcp(('::ffff:'+prefix+'.2', cfg['host_port']))
            if cfg['ipv6_control']['reachable']:
                checks['ipv6_host_alias_blocked'] = not tcp(('fec0::2', cfg['ipv6_control']['port']))
            else:
                notes['host_ipv6'] = 'SKIP: host IPv6 receiver control unavailable'
        else:
            notes['ipv6'] = 'INCONCLUSIVE: guest could not configure IPv6'
        if cfg['public']:
            for name, info in cfg['public_controls'].items():
                if name == 'https':
                    continue
                if not info['reachable']:
                    notes[name] = 'SKIP: host positive control unavailable: '+info.get('error','')
                elif name == 'dns_udp':
                    details[name] = udp(('1.1.1.1', 53), dns_packet())
                    checks['public_dns_no_reply'] = not details[name]['reply']
                elif ':' in info['address'] and 'ipv6' in notes:
                    notes[name] = 'SKIP: guest IPv6 setup failed'
                else:
                    checks['direct_public_'+name+'_blocked'] = not tcp((info['address'], info['port']))
            # Public end-to-end HTTPS through the actual production handler.
            if cfg['public_controls']['https']['reachable']:
                with socket.create_connection(production, timeout=5) as client:
                    client.settimeout(20)
                    client.sendall(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
                    stream = client.makefile('rb')
                    line = stream.readline()
                    if b' 200 ' not in line:
                        checks['production_https'] = False
                        details['production_https'] = line.decode(errors='replace')
                    else:
                        while stream.readline() not in (b'\r\n', b'\n', b''): pass
                        stream.close()
                        with ssl.create_default_context().wrap_socket(client, server_hostname='example.com') as tls:
                            tls.sendall(b'GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n')
                            response = tls.recv(1024)
                            checks['production_https'] = response.startswith(b'HTTP/1.')
            else:
                notes['production_https'] = 'SKIP: host HTTPS control unavailable'
        else:
            notes['public'] = 'SKIP: --public-probes not requested'
    finally:
        stopped.set();thread.join(timeout=2);listener.close()
    return {'checks': checks, 'notes': notes, 'details': details}
