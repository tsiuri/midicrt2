"""Tests for midicrt.clients.web.bridge -- the EngineClient<->asyncio
fan-out bridge, testable without any HTTP/websocket machinery (see
bridge.py's module docstring for the design). Reuses test_server.py's
`make` fixture pattern, same as tests/test_client_base.py.
"""
import asyncio
import queue

from test_server import make

from midicrt.clients import chrome
from midicrt.clients.base import ClientError, EngineClient
from midicrt.clients.web.bridge import (
    BRIDGE_STATUS_CONNECTED,
    BRIDGE_STATUS_RECONNECTING,
    Bridge,
    WebSink,
)
from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent
from midicrt.engine.server import ProtocolServer


async def _start_bridge(srv, subscribe_rate=50.0, **bridge_kwargs):
    client = EngineClient(srv.socket_path)
    bridge = Bridge(client, subscribe_rate=subscribe_rate, **bridge_kwargs)
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(bridge.start, loop)
    return bridge


# Phase 9 Task 4 (bridge reconnect): tiny, jitter-free backoff for tests
# that need the pump thread to actually retry against a real (or
# real-then-restarted) socket -- keeps these tests fast and, per the
# task's own binding constraint ("robust on a loaded Pi 3, no flaky tight
# timing"), still comfortably bounded even under scheduler pressure: a
# handful of 20ms retries converges well inside any reasonable timeout.
_FAST_RECONNECT = {"reconnect_base_delay": 0.02, "reconnect_max_delay": 0.1,
                   "reconnect_jitter": 0.0}


async def _wait_until(predicate, timeout=5.0, interval=0.02):
    """Poll `predicate()` (a zero-arg callable) until it's truthy or
    `timeout` elapses. Used by the reconnect tests below instead of a
    single fixed sleep -- the bridge's own reconnect timing is
    backoff-driven (not a single deterministic delay), so polling is the
    only robust way to wait for it "eventually" without either racing a
    too-short sleep or wasting a needlessly long one."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


def _drain_all_nowait(q):
    """Every currently-deliverable item off a WebSink's `_CoalescingQueue`,
    via repeated `get_nowait()` until `asyncio.QueueEmpty` -- used to
    assert on the whole delivered sequence (e.g. "no None ever appeared")
    rather than just the next single item."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return items


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

    # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
    # revamp): `Engine._set_current_page` now ALSO emits `keymap_changed`
    # right after `page_changed` (its own docstring has the rationale) --
    # `WebSink.offer`'s per-event-name keying (this task's own fix to a
    # latent collision the OLD shared-`_UNKEYED` scheme had, see bridge.py's
    # module docstring) means BOTH now reach every sink, in order, instead
    # of the second silently clobbering the first.
    keymap_ev = await asyncio.wait_for(sink.queue.get(), timeout=2.0)
    assert keymap_ev["kind"] == "event" and keymap_ev["name"] == "keymap_changed"

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


# -- Phase 9 Task 4: bridge reconnect (Option B, user ruling) ---------------
#
# Through Phase 6-8, an engine-side EOF (midicrtd restarting) permanently
# froze the bridge -- see bridge.py's module docstring "Engine-restart
# reconnect" section for the full pre-fix writeup (also deleted from
# docs/phase6-web.md §7 / docs/phase3-parity.md §7 as part of this task,
# since it's no longer true). These tests prove the fix: an engine-side EOF
# clears stale state and starts reconnecting instead of closing sinks; a
# restarted engine gets picked back up automatically, on the SAME sink/
# websocket, with a fresh (not stale-merged) snapshot; backoff is bounded,
# doubling, and jittered; and a genuine `stop()` still closes sinks (the
# ONE case Option B does not change).

async def test_bridge_engine_eof_does_not_close_existing_sinks(tmp_path):
    """Core Option B claim, isolated from the reconnect-success path
    (test_bridge_reconnects_after_engine_restart... below covers that):
    when the engine connection drops, an already-open sink must NOT
    receive the `None` EOF sentinel (that's what makes app.py::_handle_ws
    close a browser's websocket) -- it stays registered and open, seeing
    only a `bridge_status: reconnecting` event, while the bridge retries
    in the background. No second server is ever started here -- the pump
    thread just keeps retrying against nothing, which is fine to tear down
    via `stop()` (proven separately below)."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv, **_FAST_RECONNECT)
    sink = WebSink()
    bridge.add_sink(sink)
    await _wait_until_cached(bridge, ["page.eventlog", "overlay.status"])
    _drain_all_nowait(sink.queue)  # discard the warm-up replay, not under test

    await srv.close()  # engine-side EOF: closes the bridge's connection

    await _wait_until(lambda: bridge.status == BRIDGE_STATUS_RECONNECTING)
    # Give a few retry cycles a chance to run (there is nothing listening,
    # so every one fails and loops) -- still no sink should ever see None.
    await asyncio.sleep(0.1)
    delivered = _drain_all_nowait(sink.queue)
    assert None not in delivered
    assert any(m.get("name") == "bridge_status" and m["data"]["status"] == "reconnecting"
              for m in delivered)
    assert bridge.engine_hello == {}
    assert bridge._latest == {}

    await asyncio.to_thread(bridge.stop)
    eng.stop(); await task


async def test_bridge_reconnects_after_engine_restart_and_resumes_on_same_sink(tmp_path):
    """The full Option B flow, end to end: a sink registered BEFORE the
    outage stays registered THROUGH it and receives fresh, correct data
    AFTER the engine comes back -- proving both "browsers keep their
    websockets" and "the bridge actually reconnects", not just one half.
    The restarted engine is a genuinely NEW `Engine`/`ProtocolServer` pair
    (not the same one resumed) bound to the SAME socket path, mirroring a
    real `midicrtd` restart -- and is moved to a DIFFERENT current page
    than the pre-outage one was on, so this also proves the re-subscribe
    re-derives the current page FRESH from the reconnected engine rather
    than assuming the pre-outage topic still applies (see bridge.py's
    module docstring for why that matters), and that the synthesized
    `page_changed` event fires because the page genuinely changed."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv, **_FAST_RECONNECT)
    sink = WebSink()
    bridge.add_sink(sink)
    await _wait_until_cached(bridge, ["page.eventlog", "overlay.status"])
    _drain_all_nowait(sink.queue)

    # Move the pre-outage engine to "voices" -- the restarted engine below
    # boots fresh and defaults back to "eventlog" (Config's own default
    # current page), so this is what proves re-subscribe re-derives the
    # CURRENT page rather than reusing the stale "voices" topic.
    ctl = EngineClient(srv.socket_path)
    await asyncio.to_thread(ctl.connect)
    await asyncio.to_thread(ctl.action, "page.next")
    await asyncio.to_thread(ctl.close)
    await _wait_until(lambda: bridge.state["page"] == "voices")
    _drain_all_nowait(sink.queue)  # discard the page_changed/keymap_changed/snapshot noise

    # Simulate `midicrtd` restarting: tear down the old engine+server
    # entirely, then bring up a BRAND NEW one at the exact same socket path.
    await srv.close()
    eng.stop()
    await task
    await _wait_until(lambda: bridge.status == BRIDGE_STATUS_RECONNECTING)

    eng2, srv2, task2 = await make(tmp_path, tick_hz=100.0)
    await _wait_until(lambda: bridge.status == BRIDGE_STATUS_CONNECTED, timeout=10.0)

    assert bridge.state["page"] == "eventlog"  # re-derived fresh, not stuck on "voices"
    assert bridge.state["topic"] == "page.eventlog"

    delivered = _drain_all_nowait(sink.queue)
    assert None not in delivered  # the sink was never closed across the outage
    kinds = [(m.get("kind"), m.get("name") or m.get("topic")) for m in delivered]
    assert ("event", "bridge_status") in kinds
    assert ("event", "page_changed") in kinds  # voices -> eventlog IS a real change
    page_changed = next(m for m in delivered if m.get("name") == "page_changed")
    assert page_changed["data"]["page"] == "eventlog"

    # Prove data actually flows again, not just that state fields updated:
    # a brand-new MIDI event on the NEW engine must reach the SAME sink.
    await eng2.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 100, "after-restart"))
    fresh = await _wait_for_topic(sink, "page.eventlog", timeout=5.0)
    assert fresh["data"]["lines"][-1]["text"] == "after-restart"

    await asyncio.to_thread(bridge.stop)
    eng2.stop(); await task2; await srv2.close()


async def test_bridge_stop_during_reconnect_backoff_exits_promptly(tmp_path):
    """`stop()` mid-outage must cut a pending backoff sleep short (via
    `threading.Event.wait`'s own timeout-or-set-early return) rather than
    leaving the pump thread sleeping for up to the full backoff window --
    proven with a deliberately LONG base delay (well beyond any reasonable
    test timeout if this didn't work) and a tight wall-clock bound on how
    long `stop()`'s effects take to land."""
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    bridge = await _start_bridge(srv, reconnect_base_delay=5.0, reconnect_max_delay=5.0,
                                 reconnect_jitter=0.0)
    sink = WebSink()
    bridge.add_sink(sink)
    await _wait_until_cached(bridge, ["page.eventlog", "overlay.status"])

    await srv.close()
    await _wait_until(lambda: bridge.status == BRIDGE_STATUS_RECONNECTING)

    started = asyncio.get_event_loop().time()
    await asyncio.to_thread(bridge.stop)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 2.0, f"stop() took {elapsed}s -- backoff sleep was not cut short"

    # A genuine stop() (unlike the transient EOF above) DOES close sinks --
    # the one case Option B intentionally leaves unchanged (see
    # Bridge._on_shutdown's own docstring). `_on_shutdown` is scheduled via
    # call_soon_threadsafe, so poll (accumulating every drain, not just the
    # latest) rather than assuming it has already run by the time stop()
    # returns.
    delivered = []

    async def _poll_for_close():
        while None not in delivered:
            delivered.extend(_drain_all_nowait(sink.queue))
            if None not in delivered:
                await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll_for_close(), timeout=2.0)
    assert None in delivered

    eng.stop(); await task


def test_bridge_stop_offers_eof_sentinel_to_open_sinks():
    """Unit-level pin (no real sockets) of the one behavior Option B
    deliberately preserves: an actual `stop()` -- the bridge itself going
    away, not a transient engine restart -- still tells every open sink to
    close, exactly like the pre-reconnect EOF-to-sinks behavior. Built the
    same dependency-free way as `test_pump_loop_does_not_call_threadsafe_
    on_a_closed_loop` above: no `start()`, no real socket, just the
    loop-thread handler invoked directly (`_on_shutdown` never touches
    `self.loop` itself -- only its caller, `stop()`, does -- so no event
    loop object is needed here at all)."""
    bridge = Bridge(EngineClient("/nonexistent"))
    sink = WebSink()
    bridge.add_sink(sink)

    bridge._on_shutdown()

    assert sink.queue.get_nowait() is None


def test_bridge_on_disconnected_clears_stale_state_and_flags_reconnecting():
    """Unit-level pin of `_on_disconnected` in isolation: stale
    `engine_hello`/`_latest` must be cleared (so a NEW websocket connecting
    mid-outage gets an honest hello, per bridge.py's module docstring) and
    `status` must flip -- without needing a real socket or the pump
    thread at all (`_on_disconnected` never touches `self.loop` either --
    only `_reconnect_loop`, its caller, does)."""
    bridge = Bridge(EngineClient("/nonexistent"))
    bridge.engine_hello = {"proto_version": "1.0", "engine_version": "2.0.0"}
    bridge._latest = {"page.eventlog": {"kind": "snapshot", "topic": "page.eventlog"}}
    bridge.state["page"], bridge.state["topic"] = "eventlog", "page.eventlog"
    sink = WebSink()
    bridge.add_sink(sink)
    sink.queue.get_nowait()  # discard add_sink()'s cached-latest replay -- not under test

    bridge._on_disconnected()

    assert bridge.status == BRIDGE_STATUS_RECONNECTING
    assert bridge.engine_hello == {}
    assert bridge._latest == {}
    msg = sink.queue.get_nowait()
    assert msg == {"kind": "event", "name": "bridge_status", "data": {"status": "reconnecting"}}


def test_hello_message_carries_bridge_status():
    """`hello_message()` grew a `"status"` field (Phase 9 Task 4) so a
    websocket connecting -- or reconnecting -- mid-outage sees the honest
    current status from its very first frame, not just from a live event
    it might have connected too late to catch."""
    bridge = Bridge(EngineClient("/nonexistent"))
    bridge.state["page"], bridge.state["topic"] = "eventlog", "page.eventlog"
    assert bridge.hello_message()["status"] == "connected"

    bridge.status = "reconnecting"
    assert bridge.hello_message()["status"] == "reconnecting"


def test_jittered_delay_stays_within_the_configured_spread():
    bridge = Bridge(EngineClient("/nonexistent"), reconnect_jitter=0.25)
    for _ in range(200):
        d = bridge._jittered_delay(4.0)
        assert 3.0 <= d <= 5.0


def test_jittered_delay_disabled_returns_the_exact_delay():
    bridge = Bridge(EngineClient("/nonexistent"), reconnect_jitter=0.0)
    assert bridge._jittered_delay(4.0) == 4.0


def test_jittered_delay_never_goes_negative_for_a_tiny_delay_with_large_jitter():
    bridge = Bridge(EngineClient("/nonexistent"), reconnect_jitter=2.0)  # +/-200%
    for _ in range(200):
        assert bridge._jittered_delay(0.01) >= 0.0


def test_reconnect_loop_backoff_doubles_and_caps_with_jitter(monkeypatch):
    """Pure unit test of the backoff SHAPE the module docstring claims
    (0.5s base, doubling, capped at 10s, +/-20% jitter) -- no real
    sockets, no real sleeping. `EngineClient.connect` always raises
    `ClientError`; `bridge._stopping.wait` is stubbed to record the
    requested delay and report "not stopped yet" for the first 5 calls,
    then "stopped" on the 6th -- so this test is deterministic and
    instant regardless of the real base/max delay values, while still
    exercising `_reconnect_loop`'s actual delay-computation logic."""
    bridge = Bridge(EngineClient("/nonexistent"))  # default backoff constants
    loop = asyncio.new_event_loop()
    bridge.loop = loop
    recorded = []

    def fake_wait(timeout):
        recorded.append(timeout)
        if len(recorded) >= 6:
            bridge._stopping.set()
        return bridge._stopping.is_set()

    monkeypatch.setattr(bridge._stopping, "wait", fake_wait)
    monkeypatch.setattr(EngineClient, "connect",
                        lambda self: (_ for _ in ()).throw(ClientError("nope")))

    result = bridge._reconnect_loop()

    assert result is False  # stop() (simulated) won the race
    assert len(recorded) == 6
    expected_base = [0.5, 1.0, 2.0, 4.0, 8.0, 10.0]  # doubling, capped at 10.0
    for got, base in zip(recorded, expected_base):
        assert base * 0.8 <= got <= base * 1.2, (got, base)
    loop.close()


async def test_bridge_page_goto_action_arms_pagecycle_user_pause(tmp_path):
    """Phase 9 Task 4 (tab-click navigation, user ruling: dispatches
    page.goto as origin=client "exactly like any other binding surface").
    A web tab click posts `page.goto` through `Bridge.action()` ->
    `EngineClient.action()` -> the REAL wire -> `ProtocolServer._dispatch`'s
    ONE `origin="client"` dispatch call (engine/server.py) -- the SAME
    origin every other real client (CLI/TUI/fb/keymap-bound key) already
    funnels through, per behaviors/pagecycle.py's own "Origin ruling"
    docstring: there is no separate "web" origin string anywhere in v2.
    test_engine_core.py's `test_pagecycle_client_origin_page_action_
    pauses_rotation` already proves the origin="client" plumbing itself
    pauses rotation, but by calling `eng.actions.dispatch(...,
    origin="client")` DIRECTLY -- a shortcut that bypasses the real
    dispatch path entirely. This test closes that gap: it proves a
    WEB-SURFACE action, driven through the actual bridge a tab click uses,
    lands on that exact same origin and arms the SAME `pagecycle_user_
    pause` window -- an integration test at the engine level, per the
    task's own brief, not just a unit-level origin-string check.

    Deliberately does NOT use test_server.py's `make()` (unlike every
    other test in this file) -- `make()` also spawns `eng.run()` as a
    REAL background task, and `Engine.run()`'s own periodic tick computes
    `now = time.time()` directly (engine/core.py, right before its
    `_tick_behaviors` call), NOT via `self._clock()` -- so a real
    background run loop would silently bypass the fake clock below and
    reintroduce the exact clock-domain mismatch bug test_engine_core.py's
    own `test_pagecycle_honors_a_tiny_user_pause_at_fake_clock_scale`
    docstring calls "Fix round 1" (a real tick sees a real, huge `now`,
    which blows straight past a small fake `pagecycle_user_pause` window
    and makes rotation look permanently stuck -- reproduced live while
    writing this test, the FIRST version used `make()` and failed exactly
    this way). `ProtocolServer`'s own request handling -- including
    "action" dispatch -- runs entirely inside each connection's own read
    loop coroutine, independent of `run()`'s autonomous tick loop, so
    `bridge.action()` below still exercises the real wire without
    `eng.run()` running at all."""
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        pagecycle_enabled=True, screensaver_enabled=False))
    srv = ProtocolServer(eng, str(tmp_path / "ctl.sock"))
    await srv.start()
    fake_now = [0.0]
    eng._clock = lambda: fake_now[0]
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(fake_now[0])  # bootstrap pagecycle's own _last_switch

    bridge = await _start_bridge(srv)
    result = await asyncio.to_thread(bridge.action, "page.goto", {"name": "voices"})
    assert result["page"] == "voices"
    assert eng.current_page == "voices"

    fake_now[0] = 5.0
    await eng._tick_behaviors(fake_now[0])  # interval elapsed, but must stay paused
    assert eng.current_page == "voices"
    fake_now[0] = 29.9
    await eng._tick_behaviors(fake_now[0])
    assert eng.current_page == "voices"
    fake_now[0] = 30.0
    await eng._tick_behaviors(fake_now[0])  # pause window over -- rotation resumes
    assert eng.current_page != "voices"

    await asyncio.to_thread(bridge.stop)
    await srv.close()
