#!/usr/bin/env python3
"""Relay fixed guestfwd endpoints; optional guest loopback MCP listeners."""
import argparse
from contextlib import contextmanager, ExitStack
import errno
import os
import select
import socket
import socketserver
import stat
import sys
import threading
import time


MCP_ADDRESS = '10.0.2.101'


def shutdown_output(fd, close_pipe=False):
    """Half-close sockets; close an output pipe only with the caller's permission."""
    if stat.S_ISSOCK(os.fstat(fd).st_mode):
        output = socket.socket(fileno=fd)
        try:
            output.shutdown(socket.SHUT_WR)
        finally:
            output.detach()
    elif close_pipe:
        os.close(fd)


def relay(upstream, input_fd=0, output_fd=1, lifetime=None, close_output_pipe=False):
    """Drain each direction independently; only the owner can cancel idle streams."""
    upstream.setblocking(False)
    os.set_blocking(input_fd, False)
    os.set_blocking(output_fd, False)
    upstream_fd = upstream.fileno()
    lifetime_fd = lifetime.fileno() if lifetime is not None else None
    to_proxy, to_guest = bytearray(), bytearray()
    input_open = upstream_open = True
    upstream_shutdown = output_shutdown = False
    limit = 256*1024
    while True:
        if not input_open and not to_proxy and not upstream_shutdown:
            upstream.shutdown(socket.SHUT_WR)
            upstream_shutdown = True
        if not upstream_open and not to_guest and not output_shutdown:
            shutdown_output(output_fd, close_output_pipe)
            output_shutdown = True
        if upstream_shutdown and output_shutdown:
            return
        events = {}
        def watch(fd, event):
            events[fd] = events.get(fd, 0) | event
        if input_open and len(to_proxy) < limit:
            watch(input_fd, select.POLLIN)
        if upstream_open and len(to_guest) < limit:
            watch(upstream_fd, select.POLLIN)
        if to_proxy:
            watch(upstream_fd, select.POLLOUT)
        if to_guest:
            watch(output_fd, select.POLLOUT)
        if lifetime_fd is not None:
            watch(lifetime_fd, select.POLLIN)
        # poll handles high descriptor numbers and combines socket read/write events.
        poller = select.poll()
        for fd, event in events.items():
            poller.register(fd, event)
        ready = dict(poller.poll())
        if lifetime_fd is not None and lifetime_fd in ready:
            return  # EOF (or unexpected data) means the owning run has ended.
        if any(event & select.POLLNVAL for event in ready.values()):
            raise OSError('relay descriptor closed')
        readable = {fd for fd, event in ready.items()
                    if event & (select.POLLIN | select.POLLHUP | select.POLLERR)}
        writable = {fd for fd, event in ready.items()
                    if event & (select.POLLOUT | select.POLLHUP | select.POLLERR)}
        if input_open and len(to_proxy) < limit and input_fd in readable:
            try:
                data = os.read(input_fd, min(65536, limit-len(to_proxy)))
                input_open = bool(data)
                to_proxy.extend(data)
            except BlockingIOError:
                pass
        if upstream_open and len(to_guest) < limit and upstream_fd in readable:
            try:
                data = upstream.recv(min(65536, limit-len(to_guest)))
                upstream_open = bool(data)
                to_guest.extend(data)
            except BlockingIOError:
                pass
        if to_proxy and upstream_fd in writable:
            try:
                del to_proxy[:upstream.send(to_proxy)]
            except BlockingIOError:
                pass
        if to_guest and output_fd in writable:
            try:
                del to_guest[:os.write(output_fd, to_guest)]
            except BlockingIOError:
                pass


def connect_upstream(upstream, destination, lifetime=None, timeout=10):
    """Bound connection setup while letting owner shutdown interrupt it immediately."""
    upstream.setblocking(False)
    error = upstream.connect_ex(destination)
    if error in (0, errno.EISCONN):
        return True
    if error not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
        raise OSError(error, os.strerror(error))
    poller = select.poll()
    poller.register(upstream, select.POLLOUT)
    if lifetime is not None:
        poller.register(lifetime, select.POLLIN)
    deadline = time.monotonic()+timeout
    while True:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('upstream connection timed out')
        ready = dict(poller.poll(max(1, int(remaining*1000))))
        if lifetime is not None and lifetime.fileno() in ready:
            return False
        if upstream.fileno() in ready:
            error = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, os.strerror(error))
            return True


class ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket() as upstream:
                if connect_upstream(upstream, self.server.destination, self.server.lifetime):
                    relay(upstream, self.request.fileno(), self.request.fileno(), self.server.lifetime)
        except OSError:
            # A disconnected client or unavailable server closes only this stream.
            pass


class LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    # server_close joins handlers after the lifetime socket cancels their relays.
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(64)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


@contextmanager
def loopback_forward(port, destination):
    """Keep localhost headers/NO_PROXY, and close active streams on context exit."""
    lifetime, owner = socket.socketpair()
    with lifetime, owner:
        with LoopbackServer(('127.0.0.1', port), ForwardHandler) as server:
            server.destination = destination
            server.lifetime = lifetime
            worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.1), daemon=True)
            worker.start()
            try:
                yield server.server_address
            finally:
                owner.close()
                server.shutdown()
                worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guard', help='private host lifetime/admission socket (MCP only)')
    parser.add_argument('--tcp', nargs=2, metavar=('HOST', 'PORT'))
    parser.add_argument('unix_socket', nargs='?')
    args = parser.parse_args()
    if bool(args.tcp) == bool(args.unix_socket):
        parser.error('choose --tcp HOST PORT or a Unix socket')
    if args.guard and not args.tcp:
        parser.error('--guard requires --tcp')
    try:
        with ExitStack() as stack:
            lifetime = None
            if args.guard:
                lifetime = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                lifetime.settimeout(10)
                lifetime.connect(args.guard)
                if lifetime.recv(1) != b'1':
                    raise OSError('MCP relay limit reached or owning VM stopped')
            upstream = stack.enter_context(socket.socket(socket.AF_INET if args.tcp else socket.AF_UNIX))
            if args.tcp:
                if not connect_upstream(upstream, (args.tcp[0], int(args.tcp[1])), lifetime):
                    return 0
            else:
                # Preserve the legacy Unix proxy path. Unlike TCP EINPROGRESS,
                # Unix EAGAIN need not represent a queued asynchronous connection.
                upstream.settimeout(10)
                upstream.connect(args.unix_socket)
            # This process owns stdout; for pipe-based callers, EOF must also
            # reach them before they finish sending in the opposite direction.
            relay(upstream, lifetime=lifetime, close_output_pipe=True)
        return 0
    except (OSError, ValueError) as exc:
        target = ':'.join(args.tcp) if args.tcp else args.unix_socket
        print(f'Network relay to {target} stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
