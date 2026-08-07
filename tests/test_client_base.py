import asyncio
import json
import queue
import time

import pytest
from test_server import Client, make

from midicrt import proto
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    drain_latest,
    switch_topic,
    wait_first_snapshot,
)
from midicrt.engine.core import MidiEvent


class _FakePage:
    """Second-page double, registered directly onto the live roster (no
    production factory needed) so resubscribe-flow tests have a real second
    topic to switch to."""

    def handle(self, ev):
        return True

    def view_model(self):
        return {"marker": "fake"}


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


async def test_subscribe_rejects_non_positive_max_rate(tmp_path):
    # Regression for the fps=0 hang: the server rejects non-positive/
    # non-finite max_rate with ok:false, and subscribe() must surface that
    # as a ClientError rather than returning the error envelope silently
    # (which left callers blocked forever waiting on a snapshot that would
    # never arrive).
    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    with pytest.raises(ClientError):
        await asyncio.to_thread(client.subscribe, ["page.eventlog"], 0.0)
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


def test_drain_latest_filters_to_requested_topics_latest_wins():
    q: queue.Queue = queue.Queue()
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 1, "data": {"n": 1}})
    q.put({"kind": "snapshot", "topic": "page.other", "seq": 1, "data": {"n": "ignored"}})
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 2, "data": {"n": 2}})
    out = drain_latest(q, {"page.eventlog"})
    assert out == {"page.eventlog": {"n": 2}}  # latest wins; other topic dropped


def test_drain_latest_empty_queue_returns_empty_dict():
    assert drain_latest(queue.Queue(), {"page.eventlog"}) == {}


def test_drain_latest_raises_client_error_on_eof_sentinel():
    q: queue.Queue = queue.Queue()
    q.put(None)
    with pytest.raises(ClientError):
        drain_latest(q, {"page.eventlog"})


def test_drain_latest_hands_non_matching_messages_to_on_event():
    q: queue.Queue = queue.Queue()
    seen = []
    q.put({"kind": "event", "name": "page_changed", "data": {"page": "second"}})
    q.put({"kind": "snapshot", "topic": "page.other", "seq": 1, "data": {}})  # not requested
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 1, "data": {"n": 1}})
    out = drain_latest(q, {"page.eventlog"}, on_event=seen.append)
    assert out == {"page.eventlog": {"n": 1}}
    assert seen == [
        {"kind": "event", "name": "page_changed", "data": {"page": "second"}},
        {"kind": "snapshot", "topic": "page.other", "seq": 1, "data": {}},
    ]


def test_drain_latest_drops_non_matching_messages_without_on_event():
    q: queue.Queue = queue.Queue()
    q.put({"kind": "event", "name": "page_changed", "data": {"page": "second"}})
    out = drain_latest(q, {"page.eventlog"})
    assert out == {}  # no on_event given -> silently dropped, as before


def test_switch_topic_is_noop_when_topics_match():
    class _Recorder:
        def __init__(self):
            self.calls = []

        def unsubscribe(self, topics):
            self.calls.append(("unsub", topics))

        def subscribe(self, topics, max_rate):
            self.calls.append(("sub", topics, max_rate))

    rec = _Recorder()
    switch_topic(rec, "page.eventlog", "page.eventlog", 10.0)
    assert rec.calls == []


async def test_current_page_topic_reads_describe(tmp_path):
    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    page, topic = await asyncio.to_thread(current_page_topic, client)
    assert (page, topic) == ("eventlog", "page.eventlog")
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


async def test_wait_first_snapshot_blocks_until_matching_topic(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    await asyncio.to_thread(client.subscribe, ["page.eventlog"], 50.0)
    inbox = client.start_reader()
    for i in range(3):
        await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, f"n{i}"))
    vm = await asyncio.to_thread(wait_first_snapshot, inbox, "page.eventlog")
    assert vm["lines"][-1]["text"] == "n2"
    eng.stop(); await task; await srv.close()
    await asyncio.to_thread(client.close)


async def test_wait_first_snapshot_raises_on_eof(tmp_path):
    q: queue.Queue = queue.Queue()
    q.put(None)
    with pytest.raises(ClientError):
        wait_first_snapshot(q, "page.eventlog")


async def test_switch_topic_unsubscribes_old_and_subscribes_new(tmp_path):
    # The resubscribe flow: a fake second page (no production factory needed)
    # gives us a real second topic to switch to.
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    eng.register_page("second", _FakePage())
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    await asyncio.to_thread(client.subscribe, ["page.eventlog"], 50.0)
    inbox = client.start_reader()

    await asyncio.to_thread(switch_topic, client, "page.eventlog", "page.second", 50.0)

    vm = await asyncio.to_thread(wait_first_snapshot, inbox, "page.second")
    assert vm == {"marker": "fake"}

    conn = next(iter(srv._conns))
    assert conn.topics == {"page.second"}  # old topic dropped, new topic in place

    eng.stop(); await task; await srv.close()
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
