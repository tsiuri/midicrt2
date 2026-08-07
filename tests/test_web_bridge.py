"""Tests for midicrt.clients.web.bridge -- the EngineClient<->asyncio
fan-out bridge, testable without any HTTP/websocket machinery (see
bridge.py's module docstring for the design). Reuses test_server.py's
`make` fixture pattern, same as tests/test_client_base.py.
"""
import asyncio

from test_server import make

from midicrt.clients.base import EngineClient
from midicrt.clients.web.bridge import Bridge, WebSink
from midicrt.engine.core import MidiEvent


async def _start_bridge(srv, subscribe_rate=50.0):
    client = EngineClient(srv.socket_path)
    bridge = Bridge(client, subscribe_rate=subscribe_rate)
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(bridge.start, loop)
    return bridge


async def _wait_for_topic(sink, topic, timeout=2.0):
    """Both `page.eventlog` and `overlay.status` get seeded at subscribe
    time (phase2-notes.md: "never assume ordering" between topics), and a
    WebSink's maxsize=1 drop-and-replace queue means whichever snapshot
    lands last before a slow consumer reads is the one that survives --
    legitimately racy, not a bridge bug. Tests that care about ONE topic's
    content must loop past unrelated messages instead of assuming the
    first thing in the queue is theirs (mirrors test_web_app.py's own
    `_wait_for_eventlog` helper)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msg = await asyncio.wait_for(sink.queue.get(), timeout=timeout)
        if msg is not None and msg.get("topic") == topic:
            return msg
    raise AssertionError(f"no message for topic {topic!r} within {timeout}s")


async def test_bridge_start_subscribes_current_page_and_overlay(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    assert bridge.state["page"] == "eventlog"
    assert bridge.state["topic"] == "page.eventlog"
    conn = next(iter(srv._conns))
    assert conn.topics == {"page.eventlog", "overlay.status"}
    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_fans_out_snapshot_to_multiple_sinks(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    sink_a, sink_b = WebSink(), WebSink()
    bridge.add_sink(sink_a)
    bridge.add_sink(sink_b)

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "n0"))

    msg_a = await _wait_for_topic(sink_a, "page.eventlog")
    msg_b = await _wait_for_topic(sink_b, "page.eventlog")
    assert msg_a["data"]["lines"][-1]["text"] == "n0"
    assert msg_b["data"]["lines"][-1]["text"] == "n0"

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_late_joining_sink_gets_replayed_latest_snapshot(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    early = WebSink()
    bridge.add_sink(early)
    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "n0"))
    await _wait_for_topic(early, "page.eventlog")

    late = WebSink()
    bridge.add_sink(late)  # joins AFTER the snapshot already landed
    replayed = await _wait_for_topic(late, "page.eventlog")
    assert replayed["data"]["lines"][-1]["text"] == "n0"

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_follows_page_changed_and_resubscribes(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    sink = WebSink()
    bridge.add_sink(sink)

    ctl = EngineClient(srv.socket_path)
    await asyncio.to_thread(ctl.connect)
    r = await asyncio.to_thread(ctl.action, "page.next")
    assert r["ok"] is True and r["data"]["page"] == "voices"
    await asyncio.to_thread(ctl.close)

    # First message through the sink: the page_changed event itself. State
    # flips immediately (optimistic -- see bridge.py's module docstring);
    # the actual unsubscribe/subscribe round trip is deferred to a worker
    # thread and may still be in flight at this point.
    ev = await asyncio.wait_for(sink.queue.get(), timeout=2.0)
    assert ev["kind"] == "event" and ev["name"] == "page_changed"
    assert bridge.state["page"] == "voices"
    assert bridge.state["topic"] == "page.voices"

    # A real snapshot (voices or overlay.status) follows once the deferred
    # resubscribe completes and its seeded latest snapshot reaches the next
    # push tick -- waiting for THIS proves the round trip actually landed
    # server-side (checking conn.topics immediately after the event would
    # race the deferred resubscribe task).
    snap = await asyncio.wait_for(sink.queue.get(), timeout=2.0)
    assert snap["kind"] == "snapshot"
    assert snap["topic"] in {"page.voices", "overlay.status"}

    conn = next(c for c in srv._conns if c.topics == {"page.voices", "overlay.status"})
    assert conn is not None  # old topic dropped, new topic in place, overlay untouched

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_describe_and_action_proxy_to_engine(tmp_path):
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)

    data = await asyncio.to_thread(bridge.describe)
    assert data["pages"] == ["eventlog", "voices"]

    result = await asyncio.to_thread(bridge.action, "page.next")
    assert result["page"] == "voices"

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


def test_websink_offer_drops_and_replaces_when_full():
    sink = WebSink()
    sink.offer({"n": 1})
    sink.offer({"n": 2})  # first item was never consumed
    assert sink.queue.qsize() == 1
    assert sink.queue.get_nowait() == {"n": 2}


def test_websink_offer_after_drain_delivers_normally():
    sink = WebSink()
    sink.offer({"n": 1})
    assert sink.queue.get_nowait() == {"n": 1}
    sink.offer({"n": 2})
    assert sink.queue.get_nowait() == {"n": 2}


def test_websink_none_is_the_eof_sentinel():
    sink = WebSink()
    sink.offer(None)
    assert sink.queue.get_nowait() is None
