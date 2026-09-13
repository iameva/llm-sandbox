#!/usr/bin/env python3
"""Relay one guestfwd connection to a host-selected proxy endpoint."""
import os
import select
import socket
import sys


def relay(upstream, input_fd=0, output_fd=1):
    """Bound both buffers and let the proxy decide when an idle tunnel ends."""
    upstream.setblocking(False)
    os.set_blocking(input_fd, False)
    os.set_blocking(output_fd, False)
    to_proxy, to_guest = bytearray(), bytearray()
    input_open = upstream_open = True
    shutdown = False
    limit = 256*1024
    while upstream_open or to_guest:
        if not input_open and not to_proxy and not shutdown:
            upstream.shutdown(socket.SHUT_WR)
            shutdown = True
        reads = []
        if input_open and len(to_proxy) < limit:
            reads.append(input_fd)
        if upstream_open and len(to_guest) < limit:
            reads.append(upstream)
        writes = ([upstream] if to_proxy and upstream_open else []) + ([output_fd] if to_guest else [])
        readable, writable, _ = select.select(reads, writes, [])
        if input_fd in readable:
            data = os.read(input_fd, min(65536, limit-len(to_proxy)))
            input_open = bool(data)
            to_proxy.extend(data)
        if upstream in readable:
            data = upstream.recv(min(65536, limit-len(to_guest)))
            upstream_open = bool(data)
            to_guest.extend(data)
        if upstream in writable:
            try:
                del to_proxy[:upstream.send(to_proxy)]
            except BlockingIOError:
                pass
        if output_fd in writable:
            try:
                del to_guest[:os.write(output_fd, to_guest)]
            except BlockingIOError:
                pass


def main():
    tcp = sys.argv[1] == '--tcp'
    try:
        with socket.socket(socket.AF_INET if tcp else socket.AF_UNIX) as upstream:
            upstream.settimeout(10)
            upstream.connect((sys.argv[2], int(sys.argv[3])) if tcp else sys.argv[1])
            relay(upstream)
        return 0
    except OSError as exc:
        print(f'Proxy relay stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
