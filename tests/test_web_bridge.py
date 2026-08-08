"""Tests for midicrt.clients.web.bridge -- the EngineClient<->asyncio
fan-out bridge, testable without any HTTP/websocket machinery (see
bridge.py's module docstring for the design). Reuses test_server.py's
`make` fixture pattern, same as tests/test_client_base.py.
"""
import asyncio
import queue

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


async def _wait_until_cached(bridge, topics, timeout=2.0):
    """Poll `bridge._latest` (white-box, same discipline as `test_bridge_
    start_subscribes_current_page_and_overlay`'s own `conn.topics` peek)
    until every topic in `topics` has a cached snapshot -- used instead of
    draining a warm-up WebSink, because draining risks discarding the
    OTHER topic's snapshot if it happens to arrive first (the exact "never
    assume ordering" trap `_wait_for_topic`'s own docstring warns about)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if all(t in bridge._latest for t in topics):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"topics {topics} never cached within {timeout}s")


async def _wait_for_topic(sink, topic, timeout=2.0):
    """Both `page.eventlog` and `overlay.status` get seeded at subscribe
    time (phase2-notes.md: "never assume ordering" between topics).
    Stale docstring note (Task 2 review, Minor): before the T1-review
    carried-bug fix, WebSink held a single maxsize=1 drop-and-replace
    queue, so whichever snapshot landed LAST before a slow consumer read
    was the only one that survived -- that's no longer true. WebSink now
    coalesces PER TOPIC (`_CoalescingQueue`, bridge.py) -- two different
    topics can no longer clobber each other -- but `sink.queue.get()`
    still delivers whichever topic's slot became ready FIRST, which isn't
    guaranteed to be the one a given test cares about. Tests that care
    about ONE topic's content must still loop past unrelated messages
    (their payloads are just not interesting here, not lost) instead of
    assuming the first thing off the queue is theirs (mirrors
    test_web_app.py's own `_wait_for_eventlog` helper)."""
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


async def test_bridge_late_sink_replay_delivers_all_cached_topics(tmp_path):
    """T1-review carried bug, fixed in bridge.py (see WebSink's/
    `_CoalescingQueue`'s own docstrings): a LATE-joining sink's `add_sink()`
    replay used to land every cached topic on ONE shared maxsize=1 slot --
    only the last topic offered in that loop survived, so a reloading
    browser saw either its page render OR its status bar, never both,
    until the next unrelated engine update happened to refill whichever
    one got clobbered. Both `page.eventlog` and `overlay.status` are
    seeded at subscribe time (phase2-notes.md: "never assume ordering"
    between them) -- this test doesn't care which one lands in
    `bridge._latest` first, only that BOTH survive `add_sink()`'s replay
    independently."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    await _wait_until_cached(bridge, ["page.eventlog", "overlay.status"])

    late = WebSink()
    bridge.add_sink(late)  # joins AFTER both topics are already cached

    seen = {}
    for _ in range(2):
        msg = late.queue.get_nowait()  # both slots are ready synchronously --
        seen[msg["topic"]] = msg       # add_sink()'s replay loop is not async
    assert set(seen) == {"page.eventlog", "overlay.status"}

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_fans_out_alert_event(tmp_path, monkeypatch):
    """New-surface test (phase6-notes item 4): `alert` events (a capture
    write-failure is otherwise invisible to a connected browser, per the
    brief) must reach a WebSink through the bridge's normal fan-out --
    proven here with a REAL, near-zero-threshold StuckNotesAnalyzer alert
    (test_engine_core.py's own
    `test_run_loop_calls_tick_analyzers_and_publishes_stucknotes_alert_
    end_to_end` pattern), driven through the REAL wire (ProtocolServer /
    EngineClient / Bridge), not `eng.add_listener()`."""
    import midicrt.analyzers.stucknotes as stucknotes_mod
    monkeypatch.setattr(stucknotes_mod, "WARN_AFTER", 0.05)

    eng, srv, task = await make(tmp_path, tick_hz=200.0)
    bridge = await _start_bridge(srv)
    sink = WebSink()
    bridge.add_sink(sink)

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 100, "held"))
    await asyncio.sleep(0.2)  # let the held note cross WARN_AFTER

    deadline = asyncio.get_event_loop().time() + 2.0
    alert_msg = None
    while asyncio.get_event_loop().time() < deadline and alert_msg is None:
        msg = await asyncio.wait_for(sink.queue.get(), timeout=2.0)
        if msg is not None and msg.get("kind") == "event" and msg.get("name") == "alert":
            alert_msg = msg
    assert alert_msg is not None, "no alert event reached the sink"
    assert alert_msg["data"]["note"] == 60

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_overlay_status_snapshot_carries_rec_flag_after_capture_start(tmp_path):
    """New-surface test (phase6-notes item 6): a REAL `capture.start`
    action (not a synthetic vm dict, unlike `test_bridge_attaches_
    rendered_status_text_to_overlay_status_snapshot` above) must flip the
    `rec` flag an `overlay.status` snapshot carries across the wire -- the
    field page.html's REC indicator (visual class) and REC toggle button
    both key off of."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    sink = WebSink()
    bridge.add_sink(sink)

    result = await asyncio.to_thread(bridge.action, "capture.start")
    assert result["recording"] is True

    deadline = asyncio.get_event_loop().time() + 3.0
    rec_msg = None
    while asyncio.get_event_loop().time() < deadline and rec_msg is None:
        msg = await asyncio.wait_for(sink.queue.get(), timeout=3.0)
        if (msg is not None and msg.get("kind") == "snapshot"
                and msg.get("topic") == "overlay.status" and msg["data"].get("rec") is True):
            rec_msg = msg
    assert rec_msg is not None, "no rec=True overlay.status snapshot reached the sink"
    assert rec_msg["status_text"].startswith(chrome.REC_MARKER)

    await asyncio.to_thread(bridge.action, "capture.stop")
    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task; await srv.close()


async def test_bridge_fans_out_capture_stopped_event_with_session_payload(tmp_path):
    """New-surface test (phase6-notes item 6): `capture_stopped`'s payload
    (`id`/`counts`) is what page.html's last_session panel needs to show
    something real after a REC toggle-off, not just an empty placeholder."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv)
    sink = WebSink()
    bridge.add_sink(sink)

    start = await asyncio.to_thread(bridge.action, "capture.start")
    session_id = start["session_id"]
    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 100, "n0"))
    await asyncio.sleep(0.1)
    await asyncio.to_thread(bridge.action, "capture.stop")

    deadline = asyncio.get_event_loop().time() + 3.0
    stopped_msg = None
    while asyncio.get_event_loop().time() < deadline and stopped_msg is None:
        msg = await asyncio.wait_for(sink.queue.get(), timeout=3.0)
        if msg is not None and msg.get("kind") == "event" and msg.get("name") == "capture_stopped":
            stopped_msg = msg
    assert stopped_msg is not None, "no capture_stopped event reached the sink"
    assert stopped_msg["data"]["id"] == session_id
    assert stopped_msg["data"]["counts"].get("note_on") == 1

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


def test_websink_offer_different_snapshot_topics_do_not_collide():
    """T1-review carried bug, unit-level proof (see
    test_bridge_late_sink_replay_delivers_all_cached_topics for the
    full-stack version): two DIFFERENT snapshot topics offered before
    either is drained must BOTH survive -- the bug this fixes was
    literally the opposite (only the last-offered topic survived)."""
    sink = WebSink()
    sink.offer({"kind": "snapshot", "topic": "page.eventlog", "data": {"n": 1}})
    sink.offer({"kind": "snapshot", "topic": "overlay.status", "data": {"n": 2}})
    assert sink.queue.qsize() == 2

    seen = {}
    for _ in range(2):
        msg = sink.queue.get_nowait()
        seen[msg["topic"]] = msg
    assert seen["page.eventlog"]["data"] == {"n": 1}
    assert seen["overlay.status"]["data"] == {"n": 2}


def test_websink_offer_same_snapshot_topic_still_drops_and_replaces():
    """The fix scopes drop-and-replace to per-topic -- it does not remove
    the policy: the SAME topic offered twice before being drained must
    still coalesce to just the newest one (the queue can't grow per-topic
    either)."""
    sink = WebSink()
    sink.offer({"kind": "snapshot", "topic": "page.eventlog", "data": {"n": 1}})
    sink.offer({"kind": "snapshot", "topic": "page.eventlog", "data": {"n": 2}})
    assert sink.queue.qsize() == 1
    assert sink.queue.get_nowait()["data"] == {"n": 2}


def test_websink_offer_alert_burst_delivers_both_in_order():
    """Review finding (Task 2 review, Important, reproduced): two `alert`
    events offered back-to-back, before either is drained, used to share
    the single `_UNKEYED` bucket -- only the SECOND survived.
    analyzers/stucknotes.py's own module docstring documents exactly this
    burst pattern ("Alert-storm potential": a held note's sustain toggle
    re-firing the SAME warn/crit alert repeatedly with no debounce, all
    emitted synchronously within one `_tick_analyzers` call before any
    consumer coroutine gets a chance to drain the sink). Alerts now get
    their own sequence-keyed slots -- both must survive, in offer order."""
    sink = WebSink()
    alert_a = {"kind": "event", "name": "alert", "data": {"message": "a"}}
    alert_b = {"kind": "event", "name": "alert", "data": {"message": "b"}}
    sink.offer(alert_a)
    sink.offer(alert_b)
    assert sink.queue.qsize() == 2

    first = sink.queue.get_nowait()
    second = sink.queue.get_nowait()
    assert first["data"]["message"] == "a"
    assert second["data"]["message"] == "b"


def test_websink_offer_alert_then_eof_delivers_both_alert_first():
    """Review finding (Task 2 review, Important, reproduced): the EOF
    sentinel used to share the same `_UNKEYED` bucket as every other
    non-snapshot message, so an alert immediately followed by EOF (e.g.
    the engine connection dropping right after a capture write-failure
    alert -- a realistic pairing, since `_capture_write_failed` can
    plausibly precede a connection loss) lost the alert entirely -- only
    EOF survived. EOF now has its own dedicated key, never coalesced with
    an alert (or anything else) in either direction."""
    sink = WebSink()
    alert = {"kind": "event", "name": "alert", "data": {"message": "a"}}
    sink.offer(alert)
    sink.offer(None)
    assert sink.queue.qsize() == 2

    first = sink.queue.get_nowait()
    second = sink.queue.get_nowait()
    assert first["data"]["message"] == "a"
    assert second is None


def test_websink_offer_alert_burst_beyond_cap_drops_oldest_and_counts():
    """Bounded delivery (review's "sane cap ... drop-oldest and increment
    a `dropped_alerts` counter delivered with the next one"): a burst
    past the cap must not grow the sink's memory unboundedly -- the
    OLDEST pending alert is dropped, and a cumulative, monotonically
    increasing `dropped_alerts` count is attached to every alert offered
    from that point on (not reset to zero once attached -- so the count
    survives even if a LATER burst evicts the very alert it was first
    attached to; see WebSink._offer_alert's own docstring)."""
    sink = WebSink()
    for i in range(20):  # well past the 16-slot cap
        sink.offer({"kind": "event", "name": "alert", "data": {"message": f"a{i}"}})
    assert sink.queue.qsize() == 16  # capped, never grew past it

    delivered = [sink.queue.get_nowait() for _ in range(16)]
    messages = [m["data"]["message"] for m in delivered]
    # a0..a3 (the oldest 4) were evicted to make room for a16..a19
    assert messages == [f"a{i}" for i in range(4, 20)]
    # a4..a15 were already pending before the FIRST drop -- never annotated
    assert all("dropped_alerts" not in m for m in delivered[:12])
    # a16..a19 (offered AFTER drops started) carry the running total as of
    # when EACH was offered
    assert [m["dropped_alerts"] for m in delivered[12:]] == [1, 2, 3, 4]


def test_pump_loop_does_not_call_threadsafe_on_a_closed_loop():
    """Ledgered minor (Task 1 review): `_pump_loop` used to call
    `loop.call_soon_threadsafe` unconditionally -- if the owning event
    loop had already closed (a teardown race: `bridge.stop()`'s
    connection close vs. the loop's own shutdown), that raises
    `RuntimeError: Event loop is closed` from this daemon thread
    (surfaced as an occasional `PytestUnhandledThreadExceptionWarning`
    during test teardown, never an actual test failure -- see
    task-1-report.md's self-review). Reproduced directly here without a
    real timing-dependent race: a pre-closed loop plus a pending message
    must not raise."""
    bridge = Bridge(EngineClient("/nonexistent"))
    loop = asyncio.new_event_loop()
    loop.close()
    bridge.loop = loop
    inbox: queue.Queue = queue.Queue()
    inbox.put({"kind": "event", "name": "noop", "data": {}})
    inbox.put(None)
    bridge._inbox = inbox

    bridge._pump_loop()  # must return quietly, not raise
