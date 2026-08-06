"""Shared client library: connect/hello/subscribe handshake + id-correlated
request/response over the midicrt engine protocol.

Both `cli.py` (one-shot request/response) and `tui.py` (persistent
subscription + interactive actions) used to hand-roll this handshake; see
docs/phase2-notes.md for the wire-protocol facts this depends on and the
deferred hardening items this extraction absorbs.
"""
import queue
import socket
import threading

from midicrt import proto


class ClientError(Exception):
    """Connect failure, protocol version rejection, or a lost connection."""


class EngineClient:
    """Blocking Unix-socket client for the midicrt engine protocol.

    Call `connect()` once, then `request()` / `subscribe()` / `action()` for
    request/response traffic. Call `start_reader()` to begin a background
    daemon thread that continuously decodes incoming lines; snapshot/event
    messages (anything without an `id`) land on the returned queue, with
    `None` pushed as an EOF sentinel.

    `request()` works whether or not the reader thread has been started:
    before `start_reader()`, it reads the socket inline, buffering any
    async (snapshot/event) messages it passes over into the same queue the
    reader would use; after, it hands off to the reader thread and waits on
    a per-request queue keyed by message id, so streaming and
    request/response traffic can interleave on one connection.
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock: socket.socket | None = None
        self._decoder = proto.LineDecoder()
        self._sync_buf: list[dict] = []
        self._write_lock = threading.Lock()
        self._async_queue: queue.Queue = queue.Queue()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._next_id = 0

    # -- lifecycle ----------------------------------------------------
    def connect(self) -> dict:
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self.socket_path)
        except OSError as exc:
            raise ClientError(f"cannot connect to {self.socket_path}: {exc}") from exc
        hello = self._read_next_sync()
        if hello is None:
            raise ClientError("engine connection lost")
        hello_id = self._alloc_id()
        self._send({"id": hello_id, "cmd": "hello", "proto_version": proto.PROTO_VERSION})
        resp = self._read_until_sync(hello_id)
        if resp is None:
            raise ClientError("engine connection lost")
        if not resp.get("ok"):
            raise ClientError(f"protocol version rejected by engine: {resp.get('error', '')}")
        return hello

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- request/response ---------------------------------------------
    def request(self, cmd: str, **kw) -> dict:
        req_id = self._alloc_id()
        if self._reader_thread is not None:
            resp = self._request_via_reader(req_id, cmd, kw)
        else:
            self._send({"id": req_id, "cmd": cmd, **kw})
            resp = self._read_until_sync(req_id)
        if resp is None:
            raise ClientError("engine connection lost")
        return resp

    def _request_via_reader(self, req_id: int, cmd: str, kw: dict) -> dict | None:
        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[req_id] = q
        try:
            self._send({"id": req_id, "cmd": cmd, **kw})
            return q.get()
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def subscribe(self, topics: list[str], max_rate: float) -> dict:
        return self.request("subscribe", topics=topics, max_rate=max_rate)

    def action(self, name: str, args: dict | None = None) -> dict:
        return self.request("action", name=name, args=args or {})

    # -- streaming ------------------------------------------------------
    def start_reader(self) -> queue.Queue:
        if self._reader_thread is None:
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
        return self._async_queue

    def _reader_loop(self) -> None:
        while True:
            if self._sync_buf:
                msg = self._sync_buf.pop(0)
            else:
                sock = self._sock
                if sock is None:
                    self._on_eof()
                    return
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    self._on_eof()
                    return
                for decoded in self._decoder.feed(data):
                    self._route(decoded)
                continue
            self._route(msg)

    def _route(self, msg: dict) -> None:
        if "id" in msg:
            with self._pending_lock:
                q = self._pending.get(msg["id"])
            if q is not None:
                q.put(msg)
            # else: response to a request nobody is waiting on -- drop it.
        else:
            self._async_queue.put(msg)

    def _on_eof(self) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
        for q in waiters:
            q.put(None)
        self._async_queue.put(None)

    # -- low-level sync io (used before start_reader() is called) -------
    def _send(self, msg: dict) -> None:
        sock = self._sock
        if sock is None:
            raise ClientError("engine connection lost")
        with self._write_lock:
            try:
                sock.sendall(proto.encode(msg))
            except OSError as exc:
                raise ClientError("engine connection lost") from exc

    def _read_next_sync(self) -> dict | None:
        while not self._sync_buf:
            sock = self._sock
            if sock is None:
                return None
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                return None
            self._sync_buf.extend(self._decoder.feed(data))
        return self._sync_buf.pop(0)

    def _read_until_sync(self, target_id: int) -> dict | None:
        while True:
            msg = self._read_next_sync()
            if msg is None:
                return None
            if msg.get("id") == target_id:
                return msg
            self._async_queue.put(msg)  # async message passed over -- buffer it

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id
