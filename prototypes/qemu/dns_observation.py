"""Bounded host DNS observation. Only tcpdump/timeout run with sudo.

Captures UDP DNS packets bearing this run's random first query label. The
pcap is written through an already-open user-owned descriptor, not a root
path. No firewall, interface, resolver, or installed service is changed.
"""
import json
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time


def wire_name(tag, label):
    return bytes([len(tag)])+tag.encode()+bytes([len(label)])+label.encode()+b'\x07invalid\0'


def query(tag, label):
    return struct.pack('!6H', 0x7261, 0x100, 1, 0, 0, 0)+wire_name(tag, label)+b'\0\x01\0\x01'


def packet_records(data):
    magic = data[:4]
    formats = {b'\xd4\xc3\xb2\xa1': '<', b'\xa1\xb2\xc3\xd4': '>',
               b'\x4d\x3c\xb2\xa1': '<', b'\xa1\xb2\x3c\x4d': '>'}
    if magic not in formats or len(data) < 24:
        raise ValueError('missing or unsupported pcap header')
    offset = 24
    while offset < len(data):
        if offset+16 > len(data):
            raise ValueError('truncated pcap record')
        _, _, length, original = struct.unpack_from(formats[magic]+'4I', data, offset)
        offset += 16
        if length > original or length > 65535 or offset+length > len(data):
            raise ValueError('invalid pcap packet length')
        yield data[offset:offset+length]
        offset += length


class Observation:
    def __init__(self, base):
        self.base = base
        self.tag = secrets.token_hex(8)  # Fixed 16-byte first DNS label.
        self.process = None
        self.output = None
        self.error = None
        self.result = None

    @staticmethod
    def preflight():
        for name in ('sudo', 'timeout', 'tcpdump'):
            if not shutil.which(name):
                raise RuntimeError(f'{name} missing; no package will be installed')
        subprocess.run(['sudo', '-n', 'true'], check=True, timeout=5)

    def start(self):
        # UDP header 8 bytes + DNS header 12 bytes, then the first label.
        clauses = ['udp dst port 53', 'udp[20] = 16']
        for offset in range(0, 16, 4):
            value = self.tag[offset:offset+4].encode().hex()
            clauses.append(f'udp[{21+offset}:4] = 0x{value}')
        self.output = (self.base/'dns-observation.pcap').open('wb')
        self.error = (self.base/'dns-observation.log').open('w')
        command = ['sudo', '-n', shutil.which('timeout'), '--signal=INT', '--kill-after=5', '35',
                   shutil.which('tcpdump'), '-i', 'any', '-p', '-nn', '-s', '256', '-U', '-w', '-',
                   ' and '.join(clauses)]
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.output, stderr=self.error)
        deadline = time.monotonic()+8
        while 'listening on' not in (self.base/'dns-observation.log').read_text():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('capture did not become ready; see dns-observation.log')
            time.sleep(.1)
        self.send_control('before')

    def send_control(self, label):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(query(self.tag, label), ('1.1.1.1', 53))

    def finish(self):
        if self.process.poll() is not None:
            raise RuntimeError('capture ended before guest observation completed')
        self.send_control('after')
        rc = self.process.wait(timeout=42)
        self.output.close();self.error.close()
        log = (self.base/'dns-observation.log').read_text()
        dropped = re.search(r'(\d+) packets dropped by kernel', log)
        if rc not in (0, 124) or not dropped or int(dropped[1]) != 0:
            raise RuntimeError('capture error or packet drops; observation inconclusive')
        packets = list(packet_records((self.base/'dns-observation.pcap').read_bytes()))
        labels = ['before', 'after', 'a-direct', 'a-builtin', 'b-direct', 'b-builtin']
        counts = {label: sum(wire_name(self.tag, label) in p for p in packets) for label in labels}
        if not counts['before'] or not counts['after']:
            raise RuntimeError('capture positive controls missing; observation inconclusive')
        self.result = {'counts': counts, 'tag': self.tag, 'kernel_drops': 0,
                       'guest_dns_absent_from_host_capture': all(counts[x] == 0 for x in labels[2:])}
        (self.base/'dns-observation.json').write_text(json.dumps(self.result, indent=2))
        return self.result

    def close(self):
        # Never issue a broad privileged kill. timeout bounds the owned root
        # capture even if Python is interrupted or killed. Ordinary cleanup
        # waits for that bound rather than leaking an indefinite capture.
        if self.process and self.process.poll() is None:
            self.process.wait(timeout=42)
        for stream in (self.output, self.error):
            if stream and not stream.closed:
                stream.close()
