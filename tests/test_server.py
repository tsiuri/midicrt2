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
