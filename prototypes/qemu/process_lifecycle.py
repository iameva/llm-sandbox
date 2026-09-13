"""Owned production proxy processes for the QEMU lifecycle test."""
from pathlib import Path
import socket
import subprocess
import sys
import time


class Processes:
    def __init__(self, base, allow_file=None, mode="enforce"):
        self.base = base
        self.mode = mode
        self.ports = {}
        self.children = {}
        self.logs = []
        self.allow = base/'process-allowlist.txt'
        self.allow.write_text(allow_file.read_text() if allow_file is not None else 'example.com\n')

    def start(self, key):
        if key not in self.ports:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                self.ports[key] = reservation.getsockname()[1]
        log = (self.base/f'{key}.process-console.log').open('a')
        self.logs.append(log)
        child = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve().parents[2]/'egress-proxy.py'),
            '--mode', self.mode, '--allow-file', str(self.allow),
            '--listen', f'127.0.0.1:{self.ports[key]}',
            '--log', str(self.base/f'{key}.process-decisions.jsonl'),
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.children[key] = child
        # A port collision makes the child exit; do not accept another
        # listener as a successful startup.
        time.sleep(.3)
        for _ in range(50):
            if child.poll() is not None:
                raise RuntimeError('production proxy exited during startup; inspect logs')
            try:
                with socket.create_connection(('127.0.0.1', self.ports[key]), timeout=.2):
                    return
            except OSError:
                time.sleep(.1)
        raise RuntimeError('production proxy did not become ready')

    def kill(self, key):
        child = self.children[key]
        if child.poll() is not None:
            raise RuntimeError('proxy exited before deliberate crash')
        child.kill()
        child.wait(timeout=5)

    def close(self):
        for child in self.children.values():
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for log in self.logs:
            log.close()
