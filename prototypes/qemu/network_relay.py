#!/usr/bin/env python3
"""QEMU guestfwd child: relay one connection to a fixed host UDS.
The socket path is assigned by the host launcher, never by the guest.
"""
import os
import select
import socket
import sys

tcp = sys.argv[1] == '--tcp'
with socket.socket(socket.AF_INET if tcp else socket.AF_UNIX) as upstream:
    upstream.settimeout(10)
    upstream.connect((sys.argv[2], int(sys.argv[3])) if tcp else sys.argv[1])
    while True:
        ready, _, _ = select.select([0, upstream], [], [], 300)
        if not ready:
            break
        if 0 in ready:
            data = os.read(0, 65536)
            if not data:
                break
            upstream.sendall(data)
        if upstream in ready:
            data = upstream.recv(65536)
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
