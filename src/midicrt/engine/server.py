"""Unix-socket protocol server: request/response + latest-wins snapshot push."""
import asyncio
import contextlib
import logging
import math
import os

from midicrt import proto
from midicrt.engine.actions import ActionError

_LOG = logging.getLogger(__name__)


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
        engine.add_listener(self._on_engine_message)

    @property
    def clients(self) -> int:
        return len(self._conns)

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
                if msg["topic"] in conn.topics:
                    conn.latest[msg["topic"]] = msg          # drop-and-replace
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
                for msg in conn.decoder.feed(data):
                    await self._dispatch(conn, msg)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._drop(conn)

    async def _drop(self, conn: _ClientConn) -> None:
        self._conns.discard(conn)
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
            if proto.major(client_ver or "0") != proto.major(proto.PROTO_VERSION):
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
                "pages": sorted(self.engine.pages),
                "current_page": self.engine.current_page,
                "keymap": {},
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
            conn.max_rate = rate
            conn.topics |= set(msg.get("topics", []))
            for topic in conn.topics:
                snap = self.engine.snapshot_now(topic)
                if snap:
                    conn.latest[topic] = snap
            conn.send(proto.response(id, {"topics": sorted(conn.topics)}))
        elif cmd == "unsubscribe":
            conn.topics -= set(msg.get("topics", []))
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
