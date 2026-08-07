"""Unix-socket protocol server: request/response + latest-wins snapshot push."""
import asyncio
import contextlib
import json
import logging
import math
import os

from midicrt import proto
from midicrt.engine.actions import ActionError

_LOG = logging.getLogger(__name__)

# Slow-client policy for the immediate (unbuffered) event-send path: unlike
# snapshots, which coalesce to latest-wins in `_push_loop`, events are sent
# the moment they're emitted with no backpressure. A stalled client's kernel
# socket buffer can grow without bound, so we check the write-buffer
# high-water mark before every event send and drop the connection outright
# if it's over budget (simplest correct option: no queueing/retry, just cut
# a client that can't keep up -- it will reconnect and resubscribe).
_MAX_EVENT_WRITE_BUFFER = 256 * 1024  # 256 KiB


class _ClientConn:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.decoder = proto.LineDecoder()
        self.greeted = False
        self.max_rate = 10.0
        self.topics: set[str] = set()
        self.latest: dict[str, dict] = {}   # topic -> newest unsent snapshot
        self.pusher: asyncio.Task | None = None

    def send(self, msg: dict) -> None:
        self.writer.write(proto.encode(msg))


class ProtocolServer:
    def __init__(self, engine, socket_path: str):
        self.engine = engine
        self.socket_path = socket_path
        self._server: asyncio.Server | None = None
        self._conns: set[_ClientConn] = set()
        # Important perf fix (2026-08-07 fix wave, finding 1): a real,
        # incrementally-maintained per-topic subscriber refcount (NOT a
        # live O(n_conns) scan over self._conns -- run()'s dirty-flush loop
        # calls the provider once per dirty topic every tick, so this needs
        # to be a cheap dict lookup, not a scan repeated many times a
        # second). Incremented in `_dispatch`'s "subscribe" branch,
        # decremented in "unsubscribe" and in `_drop` (covers a client
        # disconnect too -- see `_drop`'s own comment for why it's
        # idempotent-safe to decrement here). Wired into the engine via
        # `set_topic_refcount_provider` so `Engine._flush_dirty()` can skip
        # materializing a dirty topic's view_model() when this reports 0 --
        # see that setter's own docstring in engine/core.py for the full
        # root-cause writeup (Img2TxtVizAnalyzer.tick() always dirty at
        # 30Hz, 35-40% idle CPU with zero subscribers).
        self._topic_refcounts: dict[str, int] = {}
        engine.add_listener(self._on_engine_message)
        engine.set_topic_refcount_provider(self._topic_refcount)

    @property
    def clients(self) -> int:
        return len(self._conns)

    # -- per-topic subscriber refcount (finding 1) -------------------------

    def _topic_refcount(self, topic: str) -> int:
        return self._topic_refcounts.get(topic, 0)

    def _incref_topics(self, topics: set[str]) -> None:
        for topic in topics:
            self._topic_refcounts[topic] = self._topic_refcounts.get(topic, 0) + 1

    def _decref_topics(self, topics: set[str]) -> None:
        for topic in topics:
            count = self._topic_refcounts.get(topic, 0) - 1
            if count <= 0:
                self._topic_refcounts.pop(topic, None)
            else:
                self._topic_refcounts[topic] = count

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)

    async def close(self) -> None:
        for conn in list(self._conns):
            await self._drop(conn)
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -- engine -> clients ------------------------------------------------
    def _on_engine_message(self, msg: dict) -> None:
        for conn in list(self._conns):
            if not conn.greeted:
                continue
            if msg.get("kind") == "snapshot":
                if msg.get("topic") in conn.topics:
                    conn.latest[msg["topic"]] = msg          # drop-and-replace
            elif conn.writer.transport.get_write_buffer_size() > _MAX_EVENT_WRITE_BUFFER:
                _LOG.warning("dropping slow client: event write buffer exceeded %d bytes",
                             _MAX_EVENT_WRITE_BUFFER)
                asyncio.create_task(self._drop(conn))
            else:
                conn.send(msg)                               # events: immediate

    async def _push_loop(self, conn: _ClientConn) -> None:
        while True:
            await asyncio.sleep(1.0 / conn.max_rate)
            for topic in sorted(conn.latest):
                conn.send(conn.latest.pop(topic))
            with contextlib.suppress(ConnectionError):
                await conn.writer.drain()

    # -- client -> engine -------------------------------------------------
    async def _handle(self, reader, writer) -> None:
        conn = _ClientConn(reader, writer)
        self._conns.add(conn)
        conn.send(proto.hello())
        try:
            while data := await reader.read(65536):
                try:
                    msgs = conn.decoder.feed(data)
                except json.JSONDecodeError:
                    _LOG.warning("malformed JSON from client, dropping connection")
                    await self._drop(conn)
                    break
                for msg in msgs:
                    await self._dispatch(conn, msg)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._drop(conn)

    async def _drop(self, conn: _ClientConn) -> None:
        # `conn in self._conns` guards the refcount decrement so it only
        # ever runs ONCE per connection even though `_drop` can legitimately
        # be called twice for the same conn (a real pre-existing shape:
        # `close()` iterates every tracked conn calling `_drop`, and closing
        # a conn's writer there can independently wake its own `_handle`
        # coroutine's read loop into ALSO reaching its `finally: await
        # self._drop(conn)`). Without this guard the second call would
        # double-decrement `_topic_refcounts` for topics this conn was
        # subscribed to, under-counting a DIFFERENT conn's still-live
        # subscription to the same topic.
        if conn in self._conns:
            self._conns.discard(conn)
            self._decref_topics(conn.topics)
        if conn.pusher:
            conn.pusher.cancel()
        with contextlib.suppress(Exception):
            conn.writer.close()

    async def _dispatch(self, conn: _ClientConn, msg: dict) -> None:
        id, cmd = msg.get("id", 0), msg.get("cmd")
        if not conn.greeted:
            if cmd != "hello":
                conn.send(proto.error_response(id, "hello required"))
                return
            client_ver = str(msg.get("proto_version", ""))
            try:
                client_major = proto.major(client_ver or "0")
            except ValueError:
                conn.send(proto.error_response(id, f"bad proto_version: {client_ver!r}"))
                await conn.writer.drain()
                await self._drop(conn)
                return
            if client_major != proto.major(proto.PROTO_VERSION):
                conn.send(proto.error_response(id, f"proto major mismatch: {client_ver}"))
                await conn.writer.drain()
                await self._drop(conn)
                return
            conn.greeted = True
            conn.pusher = asyncio.create_task(self._push_loop(conn))
            conn.send(proto.response(id, {"ok": "hello"}))
        elif cmd == "describe":
            conn.send(proto.response(id, {
                "actions": self.engine.actions.describe(),
                # `pages`: unchanged from phase 2 (sorted bare names, display
                # only). `topics` (new, additive) carries roster/cycle order.
                "pages": sorted(self.engine.pages),
                "topics": self.engine.topics,
                "current_page": self.engine.current_page,
                # Phase 4 Task 1 (docs/phase4-notes.md): was a reserved
                # `{}` placeholder through phase 3 -- now the engine's
                # real, live key->action table (`engine/keymap.py`),
                # already validated against THIS build's action registry
                # at construction/`config.reload` time (see
                # `Engine.__init__`'s own comment for why that validation
                # happens engine-side, not here).
                "keymap": self.engine.keymap,
                "engine_version": self.engine.status()["engine_version"],
                "proto_version": proto.PROTO_VERSION,
            }))
        elif cmd == "status":
            conn.send(proto.response(id, self.engine.status() | {"clients": self.clients}))
        elif cmd == "subscribe":
            try:
                rate = float(msg.get("max_rate", 10.0))
            except (ValueError, TypeError):
                conn.send(proto.error_response(id, "max_rate must be > 0"))
                return
            if not math.isfinite(rate) or rate <= 0:
                conn.send(proto.error_response(id, "max_rate must be > 0"))
                return
            conn.max_rate = min(max(rate, 0.1), 60.0)
            requested = set(msg.get("topics", []))
            new_topics = requested - conn.topics
            conn.topics |= requested
            # Refcount incremented BEFORE the initial-snapshot delivery loop
            # below -- not because snapshot_now() itself checks the
            # refcount (it deliberately never does, see Engine.
            # set_topic_refcount_provider's docstring), but so the very
            # NEXT run() tick's _flush_dirty() already sees this
            # subscription if this topic goes dirty again before this
            # method returns.
            self._incref_topics(new_topics)
            for topic in conn.topics:
                snap = self.engine.snapshot_now(topic)
                if snap:
                    conn.latest[topic] = snap
            conn.send(proto.response(id, {"topics": sorted(conn.topics)}))
        elif cmd == "unsubscribe":
            removed = set(msg.get("topics", [])) & conn.topics
            conn.topics -= removed
            self._decref_topics(removed)
            conn.send(proto.response(id, {"topics": sorted(conn.topics)}))
        elif cmd == "action":
            try:
                data = await self.engine.actions.dispatch(
                    msg.get("name", ""), msg.get("args", {}) or {})
                conn.send(proto.response(id, data))
            except ActionError as exc:
                conn.send(proto.error_response(id, str(exc)))
        else:
            conn.send(proto.error_response(id, f"unknown cmd: {cmd}"))
