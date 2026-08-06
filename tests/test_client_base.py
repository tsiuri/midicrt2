import asyncio
import json
import queue
import time

import pytest
from test_server import Client, make

from midicrt import proto
from midicrt.clients.base import ClientError, EngineClient
from midicrt.engine.core import MidiEvent


async def test_connect_hello_happy_path(tmp_path):
    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    hello = await asyncio.to_thread(client.connect)
    assert hello["kind"] == "hello"
    assert hello["proto_version"] == proto.PROTO_VERSION
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


async def test_connect_rejects_bad_proto_version(tmp_path, monkeypatch):
    # A real Engine/ProtocolServer shares this process's `midicrt.proto` module
    # with the client under test, so monkeypatching PROTO_VERSION would shift
    # *both* sides' comparison and never produce a mismatch. Use a small fake
    # server instead, pinned to major version 1 independent of the mutable
    # global, so patching the client's outgoing version is the only thing that
    # changes.
    sock_path = str(tmp_path / "strict.sock")

    async def handle(reader, writer):
        writer.write(proto.encode(proto.hello()))
        await writer.drain()
        line = await reader.readline()
        msg = json.loads(line)
        client_ver = str(msg.get("proto_version", ""))
        if proto.major(client_ver) != 1:
            writer.write(proto.encode(
                proto.error_response(msg["id"], f"proto major mismatch: {client_ver}")))
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=sock_path)
    async with server:
        monkeypatch.setattr(proto, "PROTO_VERSION", "99.0.0")
        client = EngineClient(sock_path)
        with pytest.raises(ClientError):
            await asyncio.to_thread(client.connect)
        await asyncio.to_thread(client.close)


async def test_request_correlates_amid_interleaved_snapshots(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)

    sub = await asyncio.to_thread(client.subscribe, ["page.eventlog"], 50.0)
    assert sub["ok"] is True

    for i in range(5):
        await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, f"n{i}"))
    await asyncio.sleep(0.3)  # let the pusher deliver at least one snapshot

    status = await asyncio.to_thread(client.request, "status")
    assert status["ok"] is True
    assert status["data"]["page"] == "eventlog"

    inbox = client.start_reader()
    snaps = []
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not snaps:
        try:
            msg = inbox.get(timeout=0.2)
        except queue.Empty:
            continue
        if msg is None:
            break
        if msg.get("kind") == "snapshot":
            snaps.append(msg)
    assert snaps, "expected a buffered snapshot to land in the reader queue"

    eng.stop(); await task; await srv.close()
    await asyncio.to_thread(client.close)


async def test_request_eof_mid_request_raises_client_error(tmp_path):
    sock_path = str(tmp_path / "flaky.sock")

    async def handle(reader, writer):
        writer.write(proto.encode(proto.hello()))
        await writer.drain()
        line = await reader.readline()
        msg = json.loads(line)
        writer.write(proto.encode(proto.response(msg["id"], {"ok": "hello"})))
        await writer.drain()
        await reader.readline()  # read the next request, then vanish without answering
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=sock_path)
    async with server:
        client = EngineClient(sock_path)
        await asyncio.to_thread(client.connect)
        with pytest.raises(ClientError):
            await asyncio.to_thread(client.request, "status")
        await asyncio.to_thread(client.close)


async def test_malformed_json_drops_only_that_client(tmp_path):
    eng, srv, task = await make(tmp_path)

    bad = Client()
    await bad.connect(srv.socket_path)
    await bad.hello()
    bad.writer.write(b"not-json-at-all\n")
    await bad.writer.drain()
    data = await asyncio.wait_for(bad.reader.read(65536), timeout=5.0)
    assert data == b""  # server dropped this client

    good = EngineClient(srv.socket_path)
    hello = await asyncio.to_thread(good.connect)
    assert hello["kind"] == "hello"
    status = await asyncio.to_thread(good.request, "status")
    assert status["ok"] is True
    await asyncio.to_thread(good.close)

    eng.stop(); await task; await srv.close()
