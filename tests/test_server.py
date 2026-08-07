import asyncio

from midicrt import proto
from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent
from midicrt.engine.server import ProtocolServer


class Client:
    """Minimal test client speaking the wire protocol."""

    def __init__(self):
        self.decoder = proto.LineDecoder()
        self.inbox = []
        self._id = 0

    async def connect(self, path):
        self.reader, self.writer = await asyncio.open_unix_connection(path)

    async def read_msgs(self, timeout=0.5):
        try:
            data = await asyncio.wait_for(self.reader.read(65536), timeout)
        except TimeoutError:
            return self.inbox
        self.inbox.extend(self.decoder.feed(data))
        return self.inbox

    async def request(self, cmd, **kw):
        self._id += 1
        self.writer.write(proto.encode({"id": self._id, "cmd": cmd, **kw}))
        await self.writer.drain()
        while True:
            await self.read_msgs()
            for m in self.inbox:
                if m.get("id") == self._id:
                    return m

    async def hello(self, version=proto.PROTO_VERSION):
        return await self.request("hello", proto_version=version)


async def make(tmp_path, **cfg):
    eng = Engine(Config(**cfg))
    srv = ProtocolServer(eng, str(tmp_path / "ctl.sock"))
    await srv.start()
    task = asyncio.create_task(eng.run())
    return eng, srv, task


async def test_hello_and_describe(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.read_msgs(0.2)
    assert c.inbox[0]["kind"] == "hello"
    r = await c.hello()
    assert r["ok"] is True
    d = await c.request("describe")
    assert "eventlog.clear" in d["data"]["actions"]
    assert d["data"]["pages"] == ["eventlog"]
    assert d["data"]["topics"] == ["page.eventlog", "overlay.status"]
    eng.stop(); await task; await srv.close()


async def test_version_mismatch_rejected(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    r = await c.hello(version="99.0.0")
    assert r["ok"] is False
    eng.stop(); await task; await srv.close()


async def test_subscribe_streams_latest_snapshot(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.eventlog"], max_rate=50.0)
    for i in range(5):
        await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, f"n{i}"))
    await asyncio.sleep(0.3)
    await c.read_msgs(0.2)
    snaps = [m for m in c.inbox if m.get("kind") == "snapshot"]
    assert snaps, "expected at least one snapshot"
    assert snaps[-1]["data"]["lines"][-1]["text"] == "n4"  # latest wins
    eng.stop(); await task; await srv.close()


async def test_subscribe_to_page_and_overlay_delivers_both_snapshots(tmp_path):
    # phase-3 task 3: a client subscribing to a page topic ALONGSIDE
    # overlay.status (the chrome multi-topic subscribe both clients now do)
    # must get snapshots for both, independently -- proves the dispatch
    # table's multi-topic support end-to-end over the real wire protocol,
    # not just drain_latest()'s in-memory contract (tests/test_client_base.py).
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    r = await c.request("subscribe", topics=["page.eventlog", "overlay.status"], max_rate=50.0)
    assert r["ok"] is True
    assert sorted(r["data"]["topics"]) == ["overlay.status", "page.eventlog"]

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "n0"))
    await asyncio.sleep(0.3)
    await c.read_msgs(0.2)
    snaps = {m["topic"] for m in c.inbox if m.get("kind") == "snapshot"}
    assert snaps == {"page.eventlog", "overlay.status"}
    eng.stop(); await task; await srv.close()


async def test_clock_tick_updates_only_overlay_status_over_the_wire(tmp_path):
    # phase-3 task 3, review follow-up: the "eventlog must not show clock
    # spam" contract was previously only verified at the engine-unit level
    # (test_engine_core.py). This proves it end-to-end over a REAL
    # subscribed connection: a clock_tick delivered through a running
    # engine must dirty overlay.status but must NOT cause a second
    # page.eventlog snapshot on the wire.
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.eventlog", "overlay.status"], max_rate=50.0)
    await asyncio.sleep(0.1)
    await c.read_msgs(0.1)
    c.inbox.clear()   # drop the initial subscribe-time seeded snapshots

    await eng.queue.put(MidiEvent(0.0, "t", "start", None, None, None, "start"))
    await asyncio.sleep(0.15)
    await eng.queue.put(MidiEvent(0.5, "t", "clock_tick", None, 24, None, "clock_tick",
                                  clock_batch_start=None))
    await asyncio.sleep(0.3)
    await c.read_msgs(0.2)

    snap_topics = [m["topic"] for m in c.inbox if m.get("kind") == "snapshot"]
    # "start" is the only eventlog-visible event here -- clock_tick must
    # never add a second page.eventlog snapshot.
    assert snap_topics.count("page.eventlog") == 1
    overlay_snaps = [m for m in c.inbox
                     if m.get("kind") == "snapshot" and m["topic"] == "overlay.status"]
    assert overlay_snaps, "expected at least one overlay.status snapshot"
    # ...and overlay.status DID pick up the clock_tick's effect (beat
    # advanced past the post-"start" idle value of 1).
    assert overlay_snaps[-1]["data"]["beat"] == 2
    eng.stop(); await task; await srv.close()


async def test_subscribe_rejects_non_positive_max_rate(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    r = await c.request("subscribe", topics=["page.eventlog"], max_rate=0)
    assert r["ok"] is False
    r = await c.request("subscribe", topics=["page.eventlog"], max_rate=20.0)
    assert r["ok"] is True
    assert r["data"]["topics"] == ["page.eventlog"]
    eng.stop(); await task; await srv.close()


async def test_malformed_proto_version_rejected_and_closes(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    c.writer.write(proto.encode({"id": 1, "cmd": "hello", "proto_version": "abc"}))
    await c.writer.drain()

    # Note: deliberately not using Client.request()/read_msgs() here -- that helper
    # re-creates an asyncio.wait_for()/Task on every poll iteration, which pathologically
    # stalls (rather than cleanly timing out) when no response ever arrives. A single
    # outer wait_for around one continuous read loop avoids that.
    async def drain_until_eof():
        while True:
            data = await c.reader.read(65536)
            c.inbox.extend(c.decoder.feed(data))
            if data == b"":
                return

    await asyncio.wait_for(drain_until_eof(), timeout=5.0)
    responses = [m for m in c.inbox if m.get("id") == 1]
    assert responses and responses[0]["ok"] is False
    # connection is dropped by the server -> further reads keep returning EOF
    data = await asyncio.wait_for(c.reader.read(65536), timeout=1.0)
    assert data == b""
    eng.stop(); await task; await srv.close()


async def test_subscribe_clamps_max_rate(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    r = await c.request("subscribe", topics=["page.eventlog"], max_rate=1000000.0)
    assert r["ok"] is True
    matching = [conn for conn in srv._conns if conn.max_rate == 60.0]
    assert matching, "expected the subscribing connection's max_rate clamped to 60.0"
    eng.stop(); await task; await srv.close()


async def test_action_roundtrip_and_errors(tmp_path):
    eng, srv, task = await make(tmp_path)
    eng.pages["eventlog"].handle(MidiEvent(0, "t", "note_on", 0, 60, 1, "x"))
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    r = await c.request("action", name="eventlog.clear", args={})
    assert r["ok"] is True
    assert eng.pages["eventlog"].view_model()["count"] == 0
    r = await c.request("action", name="bogus", args={})
    assert r["ok"] is False
    r = await c.request("nonsense")
    assert r["ok"] is False
    eng.stop(); await task; await srv.close()


class _FakePage:
    """Second-page double so the roster is exercised without a production
    factory for it (mirrors test_engine_core.py's helper)."""

    def handle(self, ev):
        return True

    def view_model(self):
        return {}


async def test_page_next_action_emits_page_changed_event(tmp_path):
    # The first real event producer (phase3-notes.md item 3): dispatching an
    # action that changes the page must land a `page_changed` event on a
    # subscribed client's socket.
    eng, srv, task = await make(tmp_path)
    eng.register_page("second", _FakePage())
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.eventlog"], max_rate=50.0)

    r = await c.request("action", name="page.next", args={})
    assert r["ok"] is True

    await c.read_msgs(0.3)
    evs = [m for m in c.inbox if m.get("kind") == "event" and m.get("name") == "page_changed"]
    assert evs, "expected a page_changed event on the wire"
    assert evs[-1]["data"]["page"] == "second"
    eng.stop(); await task; await srv.close()


class _FakeTransport:
    def __init__(self, size):
        self._size = size

    def get_write_buffer_size(self):
        return self._size


class _FakeWriter:
    def __init__(self, transport):
        self.transport = transport
        self.closed = False

    def write(self, data):
        pass

    def close(self):
        self.closed = True


async def test_slow_client_dropped_on_event_write_buffer_high_water(tmp_path):
    # Events (unlike snapshots) are sent immediately with no latest-wins
    # coalescing, so a stalled client can pile up an unbounded write buffer.
    # A fake conn with a transport reporting an oversized buffer stands in
    # for "deliberately-stalled" per the task brief.
    from midicrt.engine.server import _ClientConn

    eng, srv, task = await make(tmp_path)
    fake_writer = _FakeWriter(_FakeTransport(10 * 1024 * 1024))
    conn = _ClientConn(reader=None, writer=fake_writer)
    conn.greeted = True
    srv._conns.add(conn)

    eng.emit_event("page_changed", {"page": "eventlog"})
    await asyncio.sleep(0.05)  # let the scheduled _drop() task run

    assert conn not in srv._conns
    assert fake_writer.closed
    eng.stop(); await task; await srv.close()
