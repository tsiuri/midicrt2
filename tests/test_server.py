import asyncio
import json
import os
import time

from midicrt import proto
from midicrt.config import Config
from midicrt.engine import keymap as keymap_mod
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


async def make(tmp_path, keymap_path=None, config_path=None, **cfg):
    # Disabled by default (task-9 review fix): this helper backs the wire-
    # protocol/server plumbing tests here AND in test_client_base.py, NONE
    # of which are testing pagecycle/screensaver -- but several push
    # synthetic MidiEvents with a placeholder `ts=0` straight onto
    # `eng.queue` (bypassing engine/midi_in.py's real `time.time()`
    # stamping), which stamps `Engine._last_activity_ts` to 0 too. Against
    # a REAL `eng.run()` loop (these tests use one), the very next
    # `_tick_behaviors(time.time())` sees an "idle" gap of the entire Unix
    # epoch -- comfortably past even the shipped defaults -- and the
    # screensaver behavior activates mid-test
    # (`test_client_base.py::test_request_correlates_amid_interleaved_
    # snapshots` caught this: `status()["page"]` came back "screensaver",
    # not "eventlog"). Phase 8 Task 5: pagecycle no longer shares this
    # epoch-zero risk at all -- it bootstraps its own `_last_switch` off
    # whatever `now` its first real `_tick_behaviors` call sees, completely
    # independent of `_last_activity_ts` (see behaviors/pagecycle.py's own
    # docstring) -- still defaulted off here for test isolation (a stock
    # `pagecycle_pages` roster hop mid-test would be just as confusing to
    # an unrelated protocol-plumbing test as screensaver's own epoch-zero
    # bug was). A test that DOES want to exercise the behaviors through a
    # real server can still pass `pagecycle_enabled=True`/
    # `screensaver_enabled=True` explicitly via **cfg.
    cfg.setdefault("pagecycle_enabled", False)
    cfg.setdefault("screensaver_enabled", False)
    eng = Engine(Config(**cfg), keymap_path=keymap_path, config_path=config_path)
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
    # Phase-3 tasks 4/5/7/8/9/10: "voices"/"harmony"/"pianoroll"/"spectrum"/
    # "screensaver"/"img2txtviz"/"config" are live by default (config.py's
    # Config.pages). "pages" is `sorted()` (display-only, alphabetical --
    # see server.py's own comment); "topics" carries the real roster/cycle
    # order.
    assert d["data"]["pages"] == [
        "ccdashboard", "ccmonitor", "chordkey", "config", "eventlog", "harmony",
        "help", "img2txtviz", "pianoroll", "progchanges", "screensaver", "sendnotes",
        "spectrum", "tuner", "voices",
    ]
    # Phase-3 task 6 added "alerts"/"timesig" overlays; task 9 added
    # "beatflash"/"loopprogress" -- see test_engine_core.py::
    # test_topics_include_overlay_after_page_topics. Phase-3 task 12 (gap
    # ports) added "help" right after "config" -- see config.py's own
    # comment for the full default-roster ordering. Phase 8 Task 4 added
    # "marquee" (the header page-title scrolling marquee). Phase 9 Task 2
    # added "polylimit" (poly-limit chrome flash), registered post-hoc last.
    # Phase 9 Task 5 added "sysex" (_SysexStatusOverlay), registered even
    # later still (right after self._midi_out is built) -- lands last.
    assert d["data"]["topics"] == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges",
        "page.ccmonitor", "page.ccdashboard", "page.chordkey", "page.sendnotes", "page.tuner",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress", "overlay.marquee", "overlay.polylimit",
        "overlay.sysex",
    ]
    # Phase 4 Task 1 (docs/phase4-notes.md): "keymap" used to be a reserved
    # `{}` placeholder -- now the engine's real (default, no keymap.toml on
    # this tmp_path) key->action table, byte-identical to `eng.keymap`.
    assert d["data"]["keymap"] == eng.keymap
    assert d["data"]["keymap"] == keymap_mod.DEFAULT_KEYMAP
    # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
    # revamp): the two additive section fields, byte-identical to the
    # engine's own attributes (see Engine._recompute_keymap's docstring).
    assert d["data"]["keymap_global"] == eng.keymap_global
    assert d["data"]["keymap_page"] == eng.keymap_page
    assert d["data"]["keymap_hints_enabled"] is True
    # Phase 10 Task A (docs/demo-feedback-2026-08-12.md item 4): the
    # `show_fps` config flag, surfaced the SAME way `keymap_hints_enabled`
    # is -- see config.py's own field docstring for why this defaults
    # False (an opt-in NEW diagnostic, unlike keymap_hints_enabled's
    # default-on v1-restore).
    assert d["data"]["show_fps"] is False
    eng.stop(); await task; await srv.close()


async def test_describe_reports_show_fps_true_when_configured(tmp_path):
    eng, srv, task = await make(tmp_path, show_fps=True)
    c = Client()
    await c.connect(srv.socket_path)
    await c.read_msgs(0.2)
    await c.hello()
    d = await c.request("describe")
    assert d["data"]["show_fps"] is True
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
    # subscribed client's socket. Roster is eventlog, voices (default since
    # phase-3 task 4), then the dynamically-registered "second" -- page.next
    # from eventlog lands on "voices" first.
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
    assert evs[-1]["data"]["page"] == "voices"
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


# -- subscriber-aware snapshot materialization, server half (Important,
# 2026-08-07 fix wave, finding 1) --------------------------------------
#
# test_engine_core.py covers the engine-side gate (_flush_dirty +
# set_topic_refcount_provider) in isolation. These tests prove the OTHER
# half end-to-end over the real wire protocol: ProtocolServer actually
# increments/decrements the refcount on subscribe/unsubscribe/disconnect,
# and a page's view_model() genuinely stops (and resumes) being called as
# a real client subscribes/unsubscribes/disconnects.

class _CountingPage:
    """Counts view_model() calls -- observes materialization directly,
    mirrors test_engine_core.py's own _SpyPage."""

    def __init__(self):
        self.view_model_calls = 0

    def handle(self, ev) -> bool:
        return True

    def view_model(self) -> dict:
        self.view_model_calls += 1
        return {"n": self.view_model_calls}


async def test_topic_refcount_increments_on_subscribe_and_decrements_on_unsubscribe(tmp_path):
    eng, srv, task = await make(tmp_path)
    assert srv._topic_refcount("page.eventlog") == 0
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.eventlog"], max_rate=50.0)
    assert srv._topic_refcount("page.eventlog") == 1
    await c.request("unsubscribe", topics=["page.eventlog"])
    assert srv._topic_refcount("page.eventlog") == 0
    eng.stop(); await task; await srv.close()


async def test_topic_refcount_decrements_on_client_disconnect(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.eventlog"], max_rate=50.0)
    assert srv._topic_refcount("page.eventlog") == 1
    c.writer.close()
    await asyncio.sleep(0.1)
    assert srv._topic_refcount("page.eventlog") == 0
    eng.stop(); await task; await srv.close()


async def test_topic_refcount_is_shared_correctly_across_two_subscribers(tmp_path):
    eng, srv, task = await make(tmp_path)
    c1, c2 = Client(), Client()
    await c1.connect(srv.socket_path); await c1.hello()
    await c2.connect(srv.socket_path); await c2.hello()
    await c1.request("subscribe", topics=["page.eventlog"], max_rate=50.0)
    await c2.request("subscribe", topics=["page.eventlog"], max_rate=50.0)
    assert srv._topic_refcount("page.eventlog") == 2
    await c1.request("unsubscribe", topics=["page.eventlog"])
    assert srv._topic_refcount("page.eventlog") == 1   # c2 still subscribed
    c2.writer.close()
    await asyncio.sleep(0.1)
    assert srv._topic_refcount("page.eventlog") == 0
    eng.stop(); await task; await srv.close()


async def test_page_with_no_subscriber_is_never_materialized_end_to_end(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=50.0)
    spy = _CountingPage()
    eng.register_page("spy", spy)
    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "x"))
    await asyncio.sleep(0.3)
    assert spy.view_model_calls == 0   # nobody ever subscribed to page.spy
    eng.stop(); await task; await srv.close()


async def test_subscribing_starts_materialization_and_unsubscribing_stops_it(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=50.0)
    spy = _CountingPage()
    eng.register_page("spy", spy)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.spy"], max_rate=50.0)
    calls_after_subscribe = spy.view_model_calls
    assert calls_after_subscribe >= 1   # initial snapshot delivered immediately

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "x"))
    await asyncio.sleep(0.3)
    assert spy.view_model_calls > calls_after_subscribe   # materialized while subscribed

    await c.request("unsubscribe", topics=["page.spy"])
    calls_at_unsub = spy.view_model_calls
    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 61, 1, "y"))
    await asyncio.sleep(0.3)
    assert spy.view_model_calls == calls_at_unsub   # stopped after unsubscribe

    eng.stop(); await task; await srv.close()


async def test_disconnecting_the_last_subscriber_stops_materialization(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=50.0)
    spy = _CountingPage()
    eng.register_page("spy", spy)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("subscribe", topics=["page.spy"], max_rate=50.0)
    c.writer.close()
    await asyncio.sleep(0.1)
    calls_at_disconnect = spy.view_model_calls

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "x"))
    await asyncio.sleep(0.3)
    assert spy.view_model_calls == calls_at_disconnect

    eng.stop(); await task; await srv.close()


# -- config.reload round-trip over the wire (Phase 4 Task 1) -----------------

async def test_config_reload_round_trip_over_the_wire(tmp_path):
    """The full path a real operator/client exercises: `describe` shows
    the default keymap, a keymap.toml is dropped onto disk, `action
    config.reload` is dispatched over the socket, and BOTH `describe`'s
    fresh response AND a `keymap_changed` event reflect the change --
    without a daemon restart."""
    keymap_path = tmp_path / "keymap.toml"
    eng, srv, task = await make(tmp_path, keymap_path=str(keymap_path))
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    d = await c.request("describe")
    assert d["data"]["keymap"] == keymap_mod.DEFAULT_KEYMAP

    keymap_path.write_text('[keys]\nn = "page.prev"\n')
    r = await c.request("action", name="config.reload", args={})
    assert r["ok"] is True
    assert r["data"]["keymap"]["n"] == "page.prev"
    await c.read_msgs(0.2)   # drain any straggling event bytes not yet in c.inbox

    d2 = await c.request("describe")
    assert d2["data"]["keymap"]["n"] == "page.prev"
    assert d2["data"]["keymap"]["q"] == "client.quit"   # untouched default, merge semantics

    events = [m for m in c.inbox if m.get("kind") == "event" and m.get("name") == "keymap_changed"]
    assert events, "config.reload must broadcast keymap_changed over the wire"
    assert events[-1]["data"]["keymap"]["n"] == "page.prev"

    eng.stop(); await task; await srv.close()


async def test_config_reload_with_malformed_keymap_toml_keeps_connection_alive(tmp_path):
    """Bindings review, live-reproduced Critical finding: an unguarded
    `load_keymap` inside `_config_reload` used to raise straight out of
    the action handler -- an uncaught exception escaping `ActionRegistry.
    dispatch` here is NOT caught by `ProtocolServer._dispatch`'s own
    `except ActionError` narrowing, so it propagated further and tore
    down the requesting connection. Reproduces that exact shape over a
    REAL socket: the response must still carry `ok: true` with a warning
    (not an error response, and definitely not a dropped connection), the
    keymap must be unchanged, and the SAME connection must still work
    for a subsequent request afterward."""
    keymap_path = tmp_path / "keymap.toml"
    keymap_path.write_text('[keys]\nv = "eventlog.clear"\n')
    eng, srv, task = await make(tmp_path, keymap_path=str(keymap_path))
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    d = await c.request("describe")
    good_keymap = d["data"]["keymap"]
    assert good_keymap["v"] == "eventlog.clear"

    keymap_path.write_text("this is not valid toml {{{ [[[ ===\n")
    r = await c.request("action", name="config.reload", args={})
    assert r["ok"] is True   # not an error response -- malformed file is disclosed, not fatal
    assert r["data"]["keymap"] == good_keymap   # unchanged -- last-good kept
    assert any("keymap.toml" in w.lower() for w in r["data"]["warnings"])

    # Connection alive: a further request over the SAME connection still works.
    d2 = await c.request("describe")
    assert d2["ok"] is True
    assert d2["data"]["keymap"] == good_keymap

    eng.stop(); await task; await srv.close()


async def test_bind_learn_arm_capture_persist_event_round_trip_over_the_wire(tmp_path):
    """The headline Phase 4 Task 3 proof (docs/phase4-notes.md): the WHOLE
    DAW-style learn cycle -- arm via a real `action bind.learn` request,
    capture a real MidiEvent through a real running `Engine.run()` loop,
    persist to a real bindings.toml, and see BOTH `learn_armed` and
    `learn_bound` arrive over a REAL subscribed socket connection. Engine-
    unit-level coverage of the same mechanics (arm validation, disqualified
    events, re-arm, timeout, capture-consumption) lives in
    test_engine_core.py, mirroring test_bindings.py vs that file's own
    split; this is the one test that proves none of it silently depends on
    being called in-process rather than through the real wire protocol."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    r = await c.request("action", name="bind.learn",
                        args={"action": "page.next", "mode": "trigger", "args": {}})
    assert r["ok"] is True
    assert r["data"]["armed"] is True

    await eng.queue.put(MidiEvent(0.0, "Midi Through:0", "note_on", 2, 60, 100, "n"))
    await asyncio.sleep(0.2)
    await c.read_msgs(0.2)

    armed_events = [m for m in c.inbox
                    if m.get("kind") == "event" and m.get("name") == "learn_armed"]
    assert armed_events, "expected learn_armed on the wire"
    assert armed_events[-1]["data"]["action"] == "page.next"

    bound_events = [m for m in c.inbox
                    if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert bound_events, "expected learn_bound on the wire"
    binding_data = bound_events[-1]["data"]["binding"]
    assert binding_data["action"] == "page.next"
    assert binding_data["match"]["type"] == "note_on"
    assert binding_data["match"]["number"] == 60
    assert binding_data["match"]["channel"] == 2
    assert binding_data["valid"] is True

    # Really persisted -- a fresh load from disk shows the same binding.
    from midicrt.engine.bindings import BindingsFile
    reloaded = BindingsFile.load(eng._bindings_path)
    assert len(reloaded.bindings) == 1
    assert reloaded.bindings[0].id == binding_data["id"]

    eng.stop(); await task; await srv.close()


async def test_bind_learn_timeout_over_the_wire_via_backdated_arm(tmp_path):
    """`_tick_learn`'s 30s auto-cancel, proven against the real `run()`
    loop's own tick (not a directly-injected `now` like test_engine_core.
    py's unit test) -- backdating `_learn_armed.armed_at` lets a fast
    `tick_hz` cross `LEARN_TIMEOUT_S` in well under a second of real wall
    time instead of an actual 30s sleep."""
    from midicrt.engine.bindings import LEARN_TIMEOUT_S

    eng, srv, task = await make(tmp_path, tick_hz=200.0)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    r = await c.request("action", name="bind.learn",
                        args={"action": "page.next", "mode": "trigger", "args": {}})
    assert r["ok"] is True
    eng._learn_armed.armed_at -= LEARN_TIMEOUT_S + 1.0   # backdate past the timeout

    await asyncio.sleep(0.1)
    await c.read_msgs(0.2)
    cancelled = [m for m in c.inbox
                if m.get("kind") == "event" and m.get("name") == "learn_cancelled"]
    assert cancelled, "expected learn_cancelled on the wire"
    assert cancelled[-1]["data"]["reason"] == "timeout"
    assert eng._learn_armed is None

    eng.stop(); await task; await srv.close()


async def test_config_reload_with_wrong_shaped_keys_keeps_connection_alive(tmp_path):
    """Re-review, live-reproduced: `keys = "oops"` (and `keys = 5` /
    `keys = ["a"]`) is syntactically VALID TOML that still crashed
    `load_keymap` with an uncaught `AttributeError` -- invisible to the
    previous fix's `(OSError, ValueError, TOMLDecodeError)` catch tuple.
    Same wire-level contract as the malformed-TOML test above: `ok: true`
    with a warning, keymap unchanged, connection alive for a subsequent
    request."""
    keymap_path = tmp_path / "keymap.toml"
    keymap_path.write_text('[keys]\nv = "eventlog.clear"\n')
    eng, srv, task = await make(tmp_path, keymap_path=str(keymap_path))
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    d = await c.request("describe")
    good_keymap = d["data"]["keymap"]

    keymap_path.write_text('keys = "oops"\n')
    r = await c.request("action", name="config.reload", args={})
    assert r["ok"] is True
    assert r["data"]["keymap"] == good_keymap
    assert any("keys" in w.lower() for w in r["data"]["warnings"])

    d2 = await c.request("describe")
    assert d2["ok"] is True
    assert d2["data"]["keymap"] == good_keymap

    eng.stop(); await task; await srv.close()


# -- event-sourced capture (Phase 5 Task 1, docs/phase5-notes.md) -----------
#
# `CaptureSink`'s own contract is test_capture.py's job; provenance origins
# for bindings/behaviors/sysex are exercised directly on a bare `Engine` in
# test_engine_core.py. This is the "lifecycle over the wire" evidence the
# task brief asks for -- a real client, over a real unix socket, driving
# capture.start/.status/.stop and observing capture_started/capture_stopped
# -- plus the "client" origin's own wire-level proof (the other three
# origins never reach `ActionRegistry.dispatch` via a wire client at all).

def _read_session_lines(eng, session_id):
    with open(eng._capture.session_path(session_id), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _read_capture_index(eng):
    with open(f"{eng._capture.dir}/index.json", encoding="utf-8") as f:
        return json.load(f)


async def test_capture_lifecycle_over_the_wire(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    idle = await c.request("action", name="capture.status", args={})
    assert idle["data"]["recording"] is False

    start = await c.request("action", name="capture.start", args={})
    assert start["ok"] is True
    session_id = start["data"]["session_id"]
    assert start["data"]["recording"] is True

    live = await c.request("action", name="capture.status", args={})
    assert live["data"]["recording"] is True
    assert live["data"]["session_id"] == session_id

    await c.read_msgs(0.2)
    started = [m for m in c.inbox if m.get("kind") == "event" and m.get("name") == "capture_started"]
    assert started and started[-1]["data"]["id"] == session_id

    eng.queue.put_nowait(MidiEvent(ts=time.time(), source="t", type="note_on", channel=0,
                                   data1=60, data2=100, summary="note_on ch1 n60 v100"))
    await asyncio.sleep(0.1)

    stop = await c.request("action", name="capture.stop", args={})
    assert stop["ok"] is True
    assert stop["data"]["session_id"] == session_id
    assert stop["data"]["counts"].get("note_on") == 1

    await c.read_msgs(0.2)
    stopped = [m for m in c.inbox if m.get("kind") == "event" and m.get("name") == "capture_stopped"]
    assert stopped and stopped[-1]["data"]["id"] == session_id
    assert stopped[-1]["data"]["counts"].get("note_on") == 1

    idle_again = await c.request("action", name="capture.status", args={})
    assert idle_again["data"]["recording"] is False

    lines = _read_session_lines(eng, session_id)
    assert lines[0]["kind"] == "header"
    assert lines[0]["session_id"] == session_id
    assert any(line["kind"] == "event" and line["type"] == "note_on" for line in lines)

    eng.stop(); await task; await srv.close()


async def test_capture_records_client_origin_action_mark_over_the_wire(tmp_path):
    eng, srv, task = await make(tmp_path)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()
    await c.request("action", name="capture.start", args={})
    await c.request("action", name="page.next", args={})
    stop = await c.request("action", name="capture.stop", args={})
    session_id = stop["data"]["session_id"]

    lines = _read_session_lines(eng, session_id)
    marks = [line for line in lines if line["kind"] == "action" and line["name"] == "page.next"]
    assert marks and marks[0]["origin"] == "client"

    eng.stop(); await task; await srv.close()


async def test_capture_pin_and_retention_over_the_wire(tmp_path):
    eng, srv, task = await make(tmp_path, capture_retention=1)
    c = Client()
    await c.connect(srv.socket_path)
    await c.hello()

    first = await c.request("action", name="capture.start", args={})
    first_id = first["data"]["session_id"]
    await c.request("action", name="capture.stop", args={})
    pin = await c.request("action", name="capture.pin", args={"id": first_id})
    assert pin["data"] == {"pinned": True, "id": first_id}

    for _ in range(2):
        await c.request("action", name="capture.start", args={})
        await c.request("action", name="capture.stop", args={})

    # retention=1: the pinned first session must survive regardless; the
    # unpinned ones settle at exactly 1 resident (see engine/capture.py's
    # own `_sweep_retention` docstring for why the target is `retention -
    # 1` before each new session is created).
    assert os.path.exists(eng._capture.session_path(first_id))
    rows = _read_capture_index(eng)
    unpinned = [r for r in rows if not r["pinned"]]
    assert len(unpinned) == 1
    assert any(r["id"] == first_id and r["pinned"] for r in rows)

    eng.stop(); await task; await srv.close()

    eng.stop(); await task; await srv.close()
