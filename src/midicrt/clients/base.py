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
from midicrt.engine.keymap import CLIENT_QUIT_ACTION, DEFAULT_KEYMAP


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
    def request(self, cmd: str, *, timeout: float | None = None, **kw) -> dict:
        """Send `cmd` and block for its response.

        `timeout` (seconds) only applies once `start_reader()` has been
        called -- that's the path with a per-request queue that can
        actually be timed out. The pre-reader sync path
        (`_read_until_sync`) blocks on the raw socket with no timeout
        mechanism; it's only ever used for the single-threaded
        connect/hello handshake, which isn't exposed to the concurrent-
        caller race `timeout` exists to guard against (two threads --
        e.g. an fb client's render loop calling `switch_topic()` from
        `on_event` while its input thread calls `client.action()` --
        both issuing requests over the SAME connection via the reader
        thread). Raises `ClientError` on expiry, same as any other lost
        response.
        """
        if self._reader_thread is not None:
            resp = self._request_via_reader(cmd, kw, timeout)
        else:
            req_id = self._alloc_id()
            self._send({"id": req_id, "cmd": cmd, **kw})
            resp = self._read_until_sync(req_id)
        if resp is None:
            raise ClientError("engine connection lost")
        if not resp.get("ok"):
            raise ClientError(resp.get("error", f"{cmd} failed"))
        return resp

    def _register_pending(self) -> tuple[int, queue.Queue]:
        """Allocate a request id AND register its response queue in
        `self._pending` as ONE atomic critical section under
        `_pending_lock`.

        This used to be two separate steps (`_alloc_id()` outside any
        lock, then a *second*, later `with self._pending_lock:` block to
        insert into `self._pending`). Two threads calling `request()`
        concurrently could both read the pre-increment `self._next_id`
        before either wrote back the increment, allocating the SAME id;
        each then inserted its own queue into `self._pending[that id]`,
        the second write clobbering the first -- so the first caller's
        queue never received a `put()` and `q.get()` blocked forever
        (the fb render-loop freeze this fixes: `switch_topic()` from the
        main thread's `on_event` racing `client.action()` from the input
        thread). Merging both steps into one critical section makes a
        duplicate id structurally impossible: only one thread can be
        between "read `_next_id`" and "write `self._pending[id]`" at a
        time.
        """
        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            req_id = self._alloc_id_locked()
            self._pending[req_id] = q
        return req_id, q

    def _request_via_reader(self, cmd: str, kw: dict,
                             timeout: float | None = None) -> dict | None:
        req_id, q = self._register_pending()
        try:
            self._send({"id": req_id, "cmd": cmd, **kw})
            if timeout is None:
                return q.get()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                raise ClientError(f"{cmd!r} timed out after {timeout}s") from None
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def subscribe(self, topics: list[str], max_rate: float,
                  timeout: float | None = None) -> dict:
        return self.request("subscribe", topics=topics, max_rate=max_rate, timeout=timeout)

    def unsubscribe(self, topics: list[str], timeout: float | None = None) -> dict:
        return self.request("unsubscribe", topics=topics, timeout=timeout)

    def action(self, name: str, args: dict | None = None,
               timeout: float | None = None) -> dict:
        return self.request("action", name=name, args=args or {}, timeout=timeout)

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
        """Thread-safe id allocation for standalone callers (e.g. `connect()`'s
        hello, or the pre-reader sync path in `request()`). NOT used by
        `_register_pending()`, which needs the raw `_alloc_id_locked()` so
        allocation and `self._pending` insertion share one lock acquisition
        (see `_register_pending`'s docstring) -- calling this method (which
        acquires the lock itself) from inside a block that already holds
        `_pending_lock` would deadlock (`threading.Lock` isn't reentrant)."""
        with self._pending_lock:
            return self._alloc_id_locked()

    def _alloc_id_locked(self) -> int:
        """Raw id increment. Caller MUST already hold `_pending_lock`."""
        self._next_id += 1
        return self._next_id


# -- shared multi-page client helpers ----------------------------------------
#
# TUI (clients/tui.py) and fb (clients/fb/app.py) each poll a background
# reader thread's queue for a non-blocking "what's new" drain once per
# render tick, and both need to react to a `page_changed` event by
# unsubscribing the old page's topic and subscribing the new one. These
# three helpers are that shared machinery, extracted here instead of kept as
# near-identical private copies in each client (the phase-2 latent item:
# "TUI msg['topic'] vs fb msg.get('topic') divergence + duplicated
# drain-latest loops" -- both now go through `msg.get("topic")` here).


def drain_latest(inbox: queue.Queue, topics, on_event=None) -> dict[str, dict]:
    """Non-blocking drain of every message currently queued on `inbox`.

    `topics` may be a fixed set/container, or a zero-arg callable returning
    the CURRENT set of topics of interest -- pass a callable when `topics`
    can change mid-drain. This matters because `on_event` (see below) can
    itself call `switch_topic()` and mutate whatever `topics` reads from;
    membership is re-evaluated PER MESSAGE (calling `topics()` fresh each
    time it's not a plain container), not once at call-entry, so a
    same-batch snapshot for a topic `on_event` just switched TO is still
    recognised instead of silently failing a stale membership check.

    For each snapshot whose topic is a current member of `topics`, the
    returned dict keeps the newest `data` payload (the page view-model) per
    topic -- latest wins, matching `ProtocolServer._push_loop`'s own
    coalescing so a client never falls behind processing a burst. Every
    other message (events, or a snapshot for a topic not currently in
    `topics`) is passed to `on_event` if one was given, else silently
    dropped (this is the pre-existing behaviour of both clients' old
    private drain loops when `on_event` is omitted).

    Raises `ClientError` on the reader thread's `None` EOF sentinel, same
    as a failed connect/subscribe -- callers treat a lost connection
    uniformly everywhere in this module.
    """
    get_topics = topics if callable(topics) else (lambda: topics)
    snapshots: dict[str, dict] = {}
    try:
        while True:
            msg = inbox.get_nowait()
            if msg is None:
                raise ClientError("engine connection lost")
            if msg.get("kind") == "snapshot" and msg.get("topic") in get_topics():
                snapshots[msg["topic"]] = msg["data"]
            elif on_event is not None:
                on_event(msg)
    except queue.Empty:
        pass
    return snapshots


def wait_first_snapshot(inbox: queue.Queue, topic, on_event=None) -> dict:
    """Block for the first snapshot matching `topic`.

    `topic` may be a plain string (fixed target), or a zero-arg callable
    returning the CURRENT target -- pass a callable together with
    `on_event` when the target can change mid-wait. This closes the
    fb-startup race: the input thread is already live while the main
    thread blocks here waiting for the first snapshot of the page it
    subscribed to before startup, so a `page_changed` firing in that
    window must retarget the wait instead of being silently dropped while
    the wait keeps blocking on a topic nobody's steering toward anymore
    (the client would then stay on the stale topic forever). `on_event` is
    invoked for every message that isn't a matching snapshot -- including
    the `page_changed` event itself -- with the SAME semantics as
    `drain_latest`'s `on_event`, so callers pass the identical callback
    (typically one that calls `switch_topic()` and mutates the state
    `topic` reads from) to both functions.

    `subscribe()`'s response is inline but the snapshot itself arrives via
    the pusher up to `1/max_rate` later (docs/phase2-notes.md) -- never
    assume it's already queued right after `subscribe()` returns.
    """
    get_topic = topic if callable(topic) else (lambda: topic)
    while True:
        msg = inbox.get()
        if msg is None:
            raise ClientError("engine connection lost")
        if msg.get("kind") == "snapshot" and msg.get("topic") == get_topic():
            return msg["data"]
        if on_event is not None:
            on_event(msg)


def current_page_topic(client: EngineClient) -> tuple[str, str]:
    """Ask the engine (via `describe`) which page is current right now, so
    a freshly-connecting client subscribes to the CURRENT page's topic
    instead of assuming "eventlog" -- an assumption that broke the moment a
    second page could exist. Returns `(page_name, topic)`."""
    page = client.request("describe")["data"]["current_page"]
    return page, f"page.{page}"


def fetch_keymap(client: EngineClient) -> dict[str, str]:
    """Ask the engine (via `describe`) for its CURRENT key->action keymap
    (Phase 4 Task 1, docs/phase4-notes.md) -- called once at connect
    (alongside `current_page_topic`'s own separate `describe()` call) and
    again whenever a `keymap_changed` event arrives (the engine's
    `config.reload` action re-read `keymap.toml`), so both clients always
    dispatch through whatever the engine reports RIGHT NOW rather than a
    table cached once at connect time.

    Defensive against an older server predating this task (wire compat is
    additive-only, same "an older/newer client and server never crash each
    other" spirit as `clients/tui.py`'s/`clients/fb/app.py`'s own
    `_render_unknown` fallbacks) -- a `describe` response with no
    `"keymap"` key at all, or a non-dict value there, falls back to
    `DEFAULT_KEYMAP` rather than raising `KeyError`/misbehaving on bad
    data."""
    data = client.request("describe")["data"]
    keymap = data.get("keymap")
    return dict(keymap) if isinstance(keymap, dict) else dict(DEFAULT_KEYMAP)


def dispatch_key(client: EngineClient, key: str, keymap: dict[str, str]) -> bool:
    """Shared key-dispatch resolution for both clients (Phase 4 Task 1):
    look `key` up in `keymap` and either handle a `client.*` pseudo-action
    locally or send the named action to the engine via `client.action`.

    Returns `True` when the caller should quit (the `CLIENT_QUIT_ACTION`
    pseudo-action, "client.quit") -- the only `client.*` name with any
    real meaning today -- `False` otherwise. A key absent from `keymap` is
    silently ignored (matches the prior hardcoded behavior of both
    clients' old private key tables). Any OTHER `client.*` value (a
    keymap.toml typo, or a future pseudo-action this client build doesn't
    know about yet) is likewise treated as a harmless local no-op --
    NEVER sent to the engine, which would just reject it as an "unknown
    action" (`client.*` names are never registered in the engine's
    `ActionRegistry` at all, see `engine/keymap.py`'s own "Pseudo-actions"
    section) and needlessly cost the caller a round trip/exception for a
    name that was never meant to leave this process. A rejected REAL
    action (e.g. a stale keymap entry naming an action this build's
    roster doesn't have) surfaces via the normal `ClientError` path --
    non-fatal to the caller's render loop, same as before this task."""
    action = keymap.get(key)
    if action is None:
        return False
    if action.startswith("client."):
        return action == CLIENT_QUIT_ACTION
    client.action(action)
    return False


def switch_topic(client: EngineClient, old_topic: str, new_topic: str, max_rate: float) -> None:
    """Resubscribe on a page change: unsubscribe the old page's topic and
    subscribe the new one, so a client never straddles two pages' worth of
    live snapshot traffic. Re-subscribing intentionally re-delivers the new
    topic's current snapshot (`subscribe()` always seeds the connection's
    latest-snapshot slot from `engine.snapshot_now`), so callers don't need
    a separate "prime the view" step after a page switch -- the next
    `wait_first_snapshot`/`drain_latest` call picks it up normally. No-op
    when the topic didn't actually change."""
    if old_topic == new_topic:
        return
    client.unsubscribe([old_topic])
    client.subscribe([new_topic], max_rate=max_rate)
