#!/usr/bin/env python3
"""Egress allowlist proxy for the LLM sandbox.

Runs on the host. The sandbox reaches the network only through this
process, and only for hostnames on the allowlist.

Design notes:

* No TLS interception. The proxy matches on the hostname in the CONNECT
  line and then tunnels bytes. It never decrypts, so certificate
  pinning keeps working and API traffic stays end-to-end encrypted.

* The proxy resolves hostnames on the client's behalf, so the sandbox
  needs no resolver of its own. Run the sandbox with --dns=none and DNS
  tunnelling is closed.

* Plain HTTP proxying is refused. Everything the sandbox legitimately
  talks to is HTTPS, and refusing cleartext keeps one code path.

* Connections to loopback, link-local and private addresses are refused
  even when the hostname is allowlisted. That stops DNS rebinding, and
  stops the sandbox reaching host services or cloud metadata at
  169.254.169.254.

Two modes:

    log       allow everything, record every attempt. Use this first,
              for a week of normal work, to learn the real allowlist.
    enforce   deny anything not on the allowlist.

Turn a log into an allowlist with --summarize.

The allowlist and the log both default into ~/.config/llm-sandbox, so
the installed proxy and the list it enforces cannot drift apart.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time

CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 300
BUF = 65536

CONFIG_DIR = os.path.expanduser("~/.config/llm-sandbox")
DEFAULT_ALLOW_FILE = os.path.join(CONFIG_DIR, "egress-allowlist.txt")
DEFAULT_LOG = os.path.join(CONFIG_DIR, "egress.log")

# Written by the serve path, read by nothing else in-process.
log_lock = threading.Lock()


def log_event(path, **fields):
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = json.dumps(fields, sort_keys=True)
    with log_lock:
        print(line, file=sys.stderr, flush=True)
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def load_allowlist(path):
    """One hostname per line. A leading dot matches subdomains.

    api.anthropic.com   matches only that name
    .anthropic.com      matches any subdomain, and anthropic.com itself
    """
    hosts = set()
    if not path:
        return hosts
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip().lower()
            if line:
                hosts.add(line)
    return hosts


def host_allowed(host, allowlist):
    host = host.lower().rstrip(".")

    # An IP literal bypasses name-based matching entirely, so it is only
    # ever allowed if listed verbatim.
    try:
        ipaddress.ip_address(host)
        return host in allowlist
    except ValueError:
        pass

    if host in allowlist:
        return True
    for entry in allowlist:
        if entry.startswith(".") and (
            host == entry[1:] or host.endswith(entry)
        ):
            return True
    return False


def resolve_public(host, port):
    """Resolve, rejecting any address that is not publicly routable."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    safe = []
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            continue
        safe.append(info)
    if not safe:
        raise PermissionError(f"{host} resolves only to non-public addresses")
    return safe


def splice(a, b):
    """Pump bytes both ways until either side closes or goes idle."""
    socks = [a, b]
    try:
        while True:
            readable, _, errored = select.select(socks, [], socks, IDLE_TIMEOUT)
            if errored or not readable:
                return
            for src in readable:
                dst = b if src is a else a
                data = src.recv(BUF)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        return


class Handler(socketserver.StreamRequestHandler):
    timeout = CONNECT_TIMEOUT

    def deny(self, code, reason, **fields):
        log_event(self.server.log_path, decision="deny", reason=reason, **fields)
        try:
            self.wfile.write(
                f"HTTP/1.1 {code} {reason}\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
            )
        except OSError:
            pass

    def handle(self):
        client = self.request.getpeername()[0]

        try:
            request_line = self.rfile.readline(8192).decode("latin-1").strip()
        except (OSError, UnicodeDecodeError):
            return
        if not request_line:
            return

        parts = request_line.split()
        if len(parts) != 3:
            self.deny(400, "malformed request", client=client)
            return

        method, target, _ = parts
        if method.upper() != "CONNECT":
            self.deny(
                405, "only CONNECT accepted", client=client,
                method=method, target=target,
            )
            return

        host, _, port_s = target.rpartition(":")
        if not host:
            self.deny(400, "malformed target", client=client, target=target)
            return
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            self.deny(400, "malformed port", client=client, target=target)
            return

        # Drain the remaining request headers.
        while True:
            try:
                line = self.rfile.readline(8192)
            except OSError:
                return
            if line in (b"\r\n", b"\n", b""):
                break

        allowed = host_allowed(host, self.server.allowlist)
        if port not in self.server.allowed_ports:
            self.deny(403, "port not allowed", client=client,
                      host=host, port=port)
            return

        if not allowed:
            if self.server.mode == "enforce":
                self.deny(403, "host not on allowlist", client=client,
                          host=host, port=port)
                return
            log_event(self.server.log_path, decision="allow-unlisted",
                      client=client, host=host, port=port)
        else:
            log_event(self.server.log_path, decision="allow",
                      client=client, host=host, port=port)

        try:
            infos = resolve_public(host, port)
        except (socket.gaierror, PermissionError) as exc:
            self.deny(502, "resolve failed", client=client,
                      host=host, port=port, detail=str(exc))
            return

        upstream = None
        for info in infos:
            try:
                upstream = socket.socket(info[0], info[1])
                upstream.settimeout(CONNECT_TIMEOUT)
                upstream.connect(info[4])
                break
            except OSError:
                if upstream:
                    upstream.close()
                upstream = None
        if upstream is None:
            self.deny(502, "upstream connect failed", client=client,
                      host=host, port=port)
            return

        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
        except OSError:
            upstream.close()
            return

        self.request.settimeout(None)
        upstream.settimeout(None)
        try:
            splice(self.request, upstream)
        finally:
            upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def summarize(log_path):
    """Print hosts seen in a log, as a file that is safe to use as-is.

    Hosts that were reached are printed live. Hosts that were denied are
    printed commented out, so piping this straight into an allowlist
    cannot silently admit something the proxy previously refused.
    """
    reached = {}
    refused = {}
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            host = event.get("host")
            if not host:
                continue
            bucket = reached if event.get("decision", "").startswith("allow") else refused
            bucket[host] = bucket.get(host, 0) + 1

    def emit(mapping, prefix):
        for host, count in sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{prefix}{host:<44} # {count} connection(s)")

    print("# Hosts the sandbox reached. Review every line before use.")
    emit(reached, "")
    if refused:
        print("\n# Denied by the proxy. Uncomment only if you mean to allow it.")
        emit(refused, "# ")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("log", "enforce"), default="log",
                    help="log: allow all and record. enforce: deny by default.")
    ap.add_argument("--allow-file",
                    help="one hostname per line; '.x.com' matches subdomains "
                         f"(default: {DEFAULT_ALLOW_FILE})")
    ap.add_argument("--listen", default="127.0.0.1:8080", help="host:port to bind")
    ap.add_argument("--log", default=DEFAULT_LOG, help="append decisions here")
    ap.add_argument("--ports", default="443",
                    help="comma-separated destination ports to permit")
    ap.add_argument("--summarize", action="store_true",
                    help="print hosts seen in the log and exit")
    args = ap.parse_args()

    if args.summarize:
        summarize(args.log)
        return

    # A missing default file is not an error: log mode is what you run
    # before any allowlist exists. A missing file you asked for by name
    # is an error, because silently enforcing nothing is the one outcome
    # you would never want from a typo.
    allow_file = args.allow_file
    if allow_file is None:
        allow_file = DEFAULT_ALLOW_FILE if os.path.exists(DEFAULT_ALLOW_FILE) else None
    elif not os.path.exists(allow_file):
        sys.exit(f"no such allow file: {allow_file}")

    allowlist = load_allowlist(allow_file)
    if args.mode == "enforce" and not allowlist:
        sys.exit(f"enforce mode needs a non-empty allow file; "
                 f"{allow_file or DEFAULT_ALLOW_FILE} is missing or has no hosts")

    bind_host, _, bind_port = args.listen.rpartition(":")
    os.makedirs(os.path.dirname(args.log), exist_ok=True)

    server = ProxyServer((bind_host, int(bind_port)), Handler)
    server.mode = args.mode
    server.allowlist = allowlist
    server.log_path = args.log
    server.allowed_ports = {int(p) for p in args.ports.split(",") if p.strip()}

    log_event(args.log, decision="startup", mode=args.mode,
              listen=args.listen, allow_file=allow_file,
              allowlist_size=len(allowlist),
              ports=sorted(server.allowed_ports))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
