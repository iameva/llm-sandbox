#!/usr/bin/env python3
"""Root guest probes of restricted libslirp; uses local test endpoints only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time


def wait_marker(name):
    subprocess.run([
        'setpriv', '--reuid=1000', '--regid=1000', '--clear-groups',
        'python3', '-c',
        'import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); '
        '\nfor i in range(900):\n if p.exists(): break\n time.sleep(.2)\nelse: raise TimeoutError()',
        '/workspace/'+name,
    ], check=True, timeout=185)


def tcp(address):
    try:
        with socket.create_connection(address, timeout=3):
            return True
    except OSError:
        return False


def main():
    cfg = json.loads(Path('/mnt/seed/config.json').read_text())
    key, prefix = cfg['key'], cfg['prefix']
    interface, = [x for x in os.listdir('/sys/class/net') if x != 'lo']
    for argv in [['ip','link','set',interface,'up'],
                 ['ip','addr','replace',prefix+'.15/24','dev',interface],
                 ['ip','route','replace','default','via',prefix+'.2']]:
        subprocess.run(argv, check=True)
    # Guest root configures its own network and performs all network probes.
    # Only report-file writes use the mapped filesystem identity.
    def write(name, text):
        subprocess.run(['setpriv','--reuid=1000','--regid=1000','--clear-groups',
                        'python3','-c','import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())',
                        '/workspace/'+name], input=text, text=True, check=True)
    listener = socket.socket()
    listener.bind(('0.0.0.0', 18080));listener.listen(8);listener.settimeout(0.2)
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            try:
                c,_=listener.accept();c.close()
            except socket.timeout:
                continue
    thread = threading.Thread(target=serve,daemon=True);thread.start()
    active = None
    active_file = None
    try:
        own = tcp((prefix+'.15',18080))
        if not own:
            raise RuntimeError('guest listener self-control failed')
        write(key+'.ready','ready')
        if cfg.get('dns_tag'):
            from dns_observation import query
            wait_marker('dns-start')
            for label, address in [('direct', '1.1.1.1'), ('builtin', prefix+'.3')]:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    for _ in range(3):
                        sender.sendto(query(cfg['dns_tag'], key+'-'+label), (address, 53))
                        time.sleep(.1)
            write(key+'.dns-sent', 'six tagged queries submitted')
        wait_marker('late.ready')
        if cfg.get('process_lifecycle'):
            from guest_process_lifecycle import run
            run(cfg, write, wait_marker, tcp)
        def tunnel(host, allowed, phase=None):
            with socket.create_connection((prefix+'.100',3128),timeout=5) as c:
                c.settimeout(5)
                c.sendall(f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode())
                stream=c.makefile('rb')
                status=stream.readline()
                while stream.readline() not in (b'\r\n',b'\n',b''): pass
                if allowed and b' 200 ' in status:
                    token=('allowed-'+key) if phase is None else phase+'-'+key
                    c.sendall((token+'\n').encode())
                    return stream.readline().decode().strip()==token
                return not allowed and b' 403 ' in status
        result={'self_listener':own,'allowed':tunnel('control.invalid', True),
                'denied':tunnel('denied.invalid', False),
                'direct_host':tcp((prefix+'.2',cfg['host_port'])),
                'other_vm':tcp((cfg['other_prefix']+'.15',18080))}
        if cfg.get('broad'):
            from guest_network_broad import run
            broad = run(cfg, interface)
            write(key+'.broad.json', json.dumps(broad))
            active = socket.create_connection((prefix+'.100', 3128), timeout=5)
            active.settimeout(8)
            active.sendall(b'CONNECT control.invalid:443 HTTP/1.1\r\n\r\n')
            active_file = active.makefile('rb')
            if b' 200 ' not in active_file.readline():
                raise RuntimeError('active stream CONNECT control failed')
            while active_file.readline() not in (b'\r\n', b'\n', b''): pass
            token = 'stream-before-'+key
            active.sendall((token+'\n').encode())
            if active_file.readline().decode().strip() != token:
                raise RuntimeError('active stream echo control failed')
        write(key+'.network.json',json.dumps(result))
        write(key+'.attempted','done')
        other='b' if key=='a' else 'a'
        wait_marker(other+'.attempted')
        if cfg.get('lifecycle'):
            wait_marker('proxy-a-stopped')
            stream_result = None
            if active is not None:
                try:
                    active.sendall(('stream-after-'+key+'\n').encode())
                    reply = active_file.readline()
                    stream_result = 'closed' if reply == b'' else ('echo' if reply.decode().strip() == 'stream-after-'+key else 'unexpected')
                except (ConnectionResetError, BrokenPipeError):
                    stream_result = 'closed'
                # A timeout is deliberately not accepted as proof of closure.
                active_file.close()
                active.close()
                active_file = active = None
            failure_error = None
            try:
                allowed_after_stop = tunnel('control.invalid', True, 'after-stop')
            except OSError as exc:
                if key != 'a':
                    raise
                allowed_after_stop = False
                failure_error = str(exc)
            lifecycle = {
                'active_stream_after_stop': stream_result,
                'allowed_after_proxy_a_stop': allowed_after_stop,
                'proxy_failure_error': failure_error,
                'direct_host_after_proxy_a_stop': tcp((prefix+'.2',cfg['host_port'])),
                'other_vm_after_proxy_a_stop': tcp((cfg['other_prefix']+'.15',18080)),
                'self_listener_after_proxy_a_stop': tcp((prefix+'.15',18080)),
            }
            if key == 'b':
                lifecycle['denied_after_proxy_a_stop'] = tunnel('denied.invalid', False)
            write(key+'.lifecycle.json', json.dumps(lifecycle))
            write(key+'.proxy-stop-checked', 'done')
            if key == 'a':
                # Host deliberately kills VM A while it is still running.
                wait_marker('never-release-a')
            else:
                wait_marker('vm-a-stopped')
                lifecycle.update({
                    'allowed_after_vm_a_stop': tunnel('control.invalid', True, 'after-vm-stop'),
                    'denied_after_vm_a_stop': tunnel('denied.invalid', False),
                    'direct_host_after_vm_a_stop': tcp((prefix+'.2',cfg['host_port'])),
                    'self_listener_after_vm_a_stop': tcp((prefix+'.15',18080)),
                })
                write(key+'.lifecycle.json', json.dumps(lifecycle))
    finally:
        if active_file is not None:
            active_file.close()
        if active is not None:
            active.close()
        stop.set();thread.join(timeout=2);listener.close()


if __name__=='__main__':
    main()
