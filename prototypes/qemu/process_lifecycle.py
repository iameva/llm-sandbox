"""Proxy supervisor with deliberate crash support for acceptance tests."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qemu.proxy_process import Processes as ProxyProcesses

class Processes(ProxyProcesses):
    def __init__(self, base, allow_file=None, mode='enforce', idle_timeout=0):
        if allow_file is None:
            allow_file = base/'fixture-allowlist.txt'
            allow_file.write_text('example.com\n')
        super().__init__(base, allow_file, mode, idle_timeout)

    def kill(self, key):
        child = self.children[key]
        if child.poll() is not None:
            raise RuntimeError('proxy exited before deliberate crash')
        child.kill()
        child.wait(timeout=5)
