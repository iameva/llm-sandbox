"""Exercise the unchanged production proxy over verified public TLS."""
import json
import socket
import ssl


def run(cfg, write, wait_marker, tcp):
    key, prefix = cfg['key'], cfg['prefix']
    address = (prefix+'.100', 3131)

    def connect(host='example.com'):
        raw = socket.create_connection(address, timeout=8)
        raw.settimeout(8)
        raw.sendall(f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode())
        header = b''
        while not header.endswith(b'\r\n\r\n'):
            part = raw.recv(1)
            if not part:
                raw.close()
                raise ConnectionResetError('proxy closed before CONNECT response')
            header += part
            if len(header) > 8192:
                raw.close()
                raise RuntimeError('oversized CONNECT response')
        if host != 'example.com':
            raw.close()
            return b' 403 ' in header.split(b'\r\n')[0]
        if b' 200 ' not in header.split(b'\r\n')[0]:
            raw.close()
            raise RuntimeError('allowed CONNECT failed: '+repr(header))
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)

    def request(stream):
        stream.sendall(b'GET / HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n')
        # Parse a complete response before reusing the persistent tunnel.
        import http.client
        response = http.client.HTTPResponse(stream)
        response.begin()
        status = response.status
        response.read()
        reusable = not response.will_close
        response.close()
        if status != 200 or not reusable:
            raise RuntimeError('public endpoint did not provide a reusable HTTP 200 control')
        return True

    active = connect()
    try:
        checks = {'initial_https': request(active), 'initial_denial': connect('denied.invalid')}
        write(key+'.process-ready', 'ready')
        wait_marker('process-a-killed')
        if key == 'a':
            try:
                data = active.recv(1)
                checks['active_closed'] = data == b''
            except (ConnectionResetError, ssl.SSLEOFError):
                checks['active_closed'] = True
            # A timeout is inconclusive and propagates.
            try:
                extra = connect()
            except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError):
                checks['new_tunnel_blocked'] = True
            else:
                extra.close()
                checks['new_tunnel_blocked'] = False
        else:
            checks['active_survives'] = request(active)
            with connect() as extra:
                checks['new_tunnel_survives'] = request(extra)
        checks['direct_host_blocked'] = not tcp((prefix+'.2', cfg['host_port']))
        checks['other_vm_blocked'] = not tcp((cfg['other_prefix']+'.15', 18080))
        write(key+'.process-stopped', 'checked')
        wait_marker('process-a-restarted')
        with connect() as recovered:
            checks['https_after_restart'] = request(recovered)
        checks['denial_after_restart'] = connect('denied.invalid')
        if key == 'b':
            checks['original_stream_after_restart'] = request(active)
        write(key+'.process.json', json.dumps(checks))
        write(key+'.process-done', 'done')
        wait_marker('process-complete')
    finally:
        active.close()
