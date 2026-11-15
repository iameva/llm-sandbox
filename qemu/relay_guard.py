"""Host-only lifetime leases for MCP guestfwd children, scoped to one VM.

The private Unix socket grants no destination selection and carries no tool data.
Closing it cancels every admitted relay, including idle streams to external servers.
"""
from pathlib import Path
import selectors
import socket
import threading


MAX_CONNECTIONS = 64


class RelayGuard:
    def __init__(self, path, limit=MAX_CONNECTIONS):
        if type(limit) is not int or limit < 1:
            raise ValueError('relay connection limit must be positive')
        self.path = Path(path)
        self.limit = limit
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = None
        bound = False
        try:
            # The launcher supplies a fresh path in its private, unexported directory.
            self._listener.bind(str(self.path))
            bound = True
            self.path.chmod(0o600)
            self._listener.listen(limit)
            self._listener.setblocking(False)
            self._thread = threading.Thread(target=self._serve, name='mcp-relay-guard', daemon=True)
            self._thread.start()
        except BaseException:
            self._listener.close()
            if bound:
                self.path.unlink(missing_ok=True)
            raise

    def _serve(self):
        clients = set()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._listener, selectors.EVENT_READ)
                while not self._stop.is_set():
                    for key, _ in selector.select(.1):
                        if key.fileobj is self._listener:
                            try:
                                client, _ = self._listener.accept()
                            except BlockingIOError:
                                continue
                            client.setblocking(False)
                            if len(clients) >= self.limit:
                                client.close()
                                continue
                            try:
                                # Admission precedes any connection to the MCP server.
                                client.sendall(b'1')
                                selector.register(client, selectors.EVENT_READ)
                                clients.add(client)
                            except OSError:
                                client.close()
                        else:
                            # Leases send no data. EOF or any unexpected input releases it.
                            client = key.fileobj
                            selector.unregister(client)
                            clients.remove(client)
                            client.close()
        finally:
            for client in clients:
                client.close()
            self._listener.close()

    def check(self):
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError('MCP relay lifetime guard stopped; stopping this VM')

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
