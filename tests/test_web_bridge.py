"""Tests for midicrt.clients.web.bridge -- the EngineClient<->asyncio
fan-out bridge, testable without any HTTP/websocket machinery (see
bridge.py's module docstring for the design). Reuses test_server.py's
`make` fixture pattern, same as tests/test_client_base.py.
"""
import asyncio

from test_server import make

from midicrt.clients import chrome
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
    # Pinned to the 2-page roster this assertion actually cares about
    # (merge-reconciliation fix, docs/phase6-notes.md item 5): this test
    # was written when Config's default roster WAS ["eventlog", "voices"]
    # (pre-phase-4); master's default has since grown to 14 pages
    # (phase-3 task 12's gap ports). Pinning `pages=` here makes that
    # dependency explicit instead of accidentally relying on whatever the
    # global default happens to be today.
    eng, srv, task = await make(tmp_path, tick_hz=100.0, pages=["eventlog", "voices"])
    bridge = await _start_bridge(srv)

    data = await asyncio.to_thread(bridge.describe)
    assert data["pages"] == ["eventlog", "voices"]

    result = await asyncio.to_thread(bridge.action, "page.next")
    assert result["page"] == "voices"

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


def test_bridge_attaches_rendered_status_text_to_overlay_status_snapshot():
    """Merge-reconciliation fix (docs/phase6-notes.md item 1, status-bar
    copy drift): the bridge must render the status STRING itself via
    `chrome.status_text()` -- the exact function clients/tui.py's/
    fb/app.py's own status rows call -- and attach it to the fanned-out
    message as `status_text`, so page.html can display it verbatim instead
    of keeping its own copy of the format (the pre-merge copy had already
    drifted: no `rec`/REC_MARKER handling at all). Constructed without
    calling `Bridge.start()` -- `_on_message`'s snapshot path never touches
    `self.loop`, so this is a pure, fast, dependency-free unit test, same
    style as the WebSink tests below.
    """
    bridge = Bridge(EngineClient("/nonexistent"))
    sink = WebSink()
    bridge.add_sink(sink)
    vm = {"bar": 3, "beat": 2, "bpm": 120.0, "running": True, "source": "midi", "rec": True}
    bridge._on_message({"kind": "snapshot", "topic": chrome.OVERLAY_STATUS_TOPIC, "data": vm})

    msg = sink.queue.get_nowait()
    assert msg["status_text"] == chrome.status_text(vm)
    assert msg["status_text"].startswith(chrome.REC_MARKER)  # the rec dot must show


def test_bridge_late_sink_replay_includes_rendered_status_text():
    """The `status_text` enrichment must survive `add_sink()`'s cached-latest
    replay too, not just the live fan-out path -- a browser connecting
    mid-session must see the rec-aware string immediately, not a raw vm it
    has to render itself."""
    bridge = Bridge(EngineClient("/nonexistent"))
    early = WebSink()
    bridge.add_sink(early)
    vm = {"bar": 0, "beat": 1, "bpm": None, "running": False, "source": None, "rec": False}
    bridge._on_message({"kind": "snapshot", "topic": chrome.OVERLAY_STATUS_TOPIC, "data": vm})
    early.queue.get_nowait()  # drain so the late sink's replay is unambiguous

    late = WebSink()
    bridge.add_sink(late)  # joins AFTER the snapshot already landed
    replayed = late.queue.get_nowait()
    assert replayed["status_text"] == chrome.status_text(vm)


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
