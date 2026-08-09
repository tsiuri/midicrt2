import asyncio
import json
import queue
import threading
import time

import pytest
from test_server import Client, make

from midicrt import proto
from midicrt.clients.base import (
    KEY_HANDLED,
    KEY_HELP_TOGGLE,
    KEY_NOOP,
    KEY_QUIT,
    ClientError,
    EngineClient,
    current_page_topic,
    dispatch_key,
    drain_latest,
    fetch_keymap,
    fetch_keymap_sections,
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


async def test_page_next_action_drives_real_resubscribe_to_voices(tmp_path):
    # The test above calls switch_topic() DIRECTLY -- it never proves the
    # EVENT-DRIVEN path (page.next -> page_changed on the wire -> a
    # client's own on_event callback -> switch_topic -> the new page's
    # snapshot) actually works end to end against a REAL second page. This
    # is the "T1 multi-page machinery's first real exercise" claim's actual
    # proof (phase-3 task 4 review). The `on_event` closure below is a
    # minimal harness reproducing tui.py's run_tui/fb's app.py
    # `_make_page_switcher` callback shape exactly -- same drain/dispatch
    # helpers (`wait_first_snapshot`, `switch_topic`), just without a
    # terminal/framebuffer around them.
    eng, srv, task = await make(tmp_path, tick_hz=100.0)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    page, topic = await asyncio.to_thread(current_page_topic, client)
    assert (page, topic) == ("eventlog", "page.eventlog")
    await asyncio.to_thread(client.subscribe, [topic], 50.0)
    inbox = client.start_reader()

    state = {"page": page, "topic": topic}

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, 50.0)
            state["page"], state["topic"] = new_page, new_topic

    # Prime on the real eventlog snapshot first, exactly like the TUI/fb
    # startup wait -- proves the client is genuinely live on page.eventlog
    # before anything switches.
    vm = await asyncio.to_thread(wait_first_snapshot, inbox, lambda: state["topic"], on_event)
    assert vm == {"title": "EVENT LOG", "count": 0, "lines": []}
    assert state["page"] == "eventlog"

    # Fire the real page.next action from a SEPARATE connection (mirrors a
    # keypress dispatching an action independent of the render loop) --
    # config.py's default roster is ["eventlog", "voices"], so this lands
    # directly on the real "voices" page, not a fake double.
    ctl = EngineClient(srv.socket_path)
    await asyncio.to_thread(ctl.connect)
    r = await asyncio.to_thread(ctl.action, "page.next")
    assert r["ok"] is True and r["data"]["page"] == "voices"
    await asyncio.to_thread(ctl.close)

    # Block for the next matching snapshot -- the FIRST message through the
    # persistent client's inbox is the page_changed EVENT itself, which
    # `on_event` (not a manual switch_topic() call from the test) consumes
    # and reacts to by resubscribing; `wait_first_snapshot`'s callable
    # `topic` argument re-reads `state["topic"]` per message so the
    # NEW topic (page.voices), set by on_event mid-call, is recognised
    # rather than the stale one this call started with.
    vm = await asyncio.to_thread(wait_first_snapshot, inbox, lambda: state["topic"], on_event)

    assert state["page"] == "voices" and state["topic"] == "page.voices"
    # A REAL VoicesPage snapshot -- 16 rows, real v1 instrument names from
    # Config()'s default `instruments`, not a stub/fake shape.
    assert vm["title"] == "VOICES"
    assert len(vm["rows"]) == 16
    assert vm["rows"][0]["name"] == "Kawai XD5"
    assert vm["rows"][15]["name"] == "Akai CD4"
    assert all(r.keys() == {"ch", "name", "active", "peak", "notes"} for r in vm["rows"])

    # Server-side confirmation: the persistent client's connection really
    # dropped page.eventlog and holds only page.voices now.
    persistent_conn = next(c for c in srv._conns if c.topics == {"page.voices"})
    assert persistent_conn is not None

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


# -- review fixes: request-id lock, event-aware startup wait, live topic ----
# -- filter (all dormant under today's single-page config; the moment a    --
# -- second page ships, fb's main thread (switch_topic via on_event) and   --
# -- the input thread (client.action()) issue requests concurrently)      --

def test_register_pending_is_one_atomic_critical_section(monkeypatch):
    """Deterministic structural proof (not timing-dependent luck): force
    thread A to pause *inside* the alloc+register critical section (while
    holding `_pending_lock`) and assert thread B's concurrent call to the
    same method is blocked entirely -- not just its id-allocation step --
    until A releases. This is the actual shape of the fb race: the main
    thread's switch_topic()->request() and the input thread's
    client.action()->request() must never be able to interleave their id
    allocation with their `_pending` registration."""
    client = EngineClient("/nonexistent")
    entered = threading.Event()
    release = threading.Event()

    real_alloc = client._alloc_id_locked

    def slow_alloc_locked():
        entered.set()
        assert release.wait(timeout=2), "test setup: release was never signalled"
        return real_alloc()

    monkeypatch.setattr(client, "_alloc_id_locked", slow_alloc_locked)

    result = {}

    def thread_a():
        result["a"] = client._register_pending()

    ta = threading.Thread(target=thread_a)
    ta.start()
    assert entered.wait(timeout=1), "thread A should have entered the critical section"

    b_done = threading.Event()

    def thread_b():
        result["b"] = client._register_pending()
        b_done.set()

    tb = threading.Thread(target=thread_b)
    tb.start()
    # Thread B must be unable to even START its own critical section while A
    # is inside `_pending_lock` -- if alloc and pending-insert were two
    # separate lock acquisitions, B could sneak its own alloc in here.
    assert not b_done.wait(timeout=0.2), "thread B must block while A holds _pending_lock"

    release.set()
    ta.join(timeout=2)
    tb.join(timeout=2)
    assert b_done.is_set(), "thread B should complete once A releases the lock"

    id_a, q_a = result["a"]
    id_b, q_b = result["b"]
    assert id_a != id_b
    assert client._pending[id_a] is q_a
    assert client._pending[id_b] is q_b


def test_register_pending_stress_no_duplicate_ids_under_contention():
    """Secondary, less deterministic net: many threads hammering
    `_register_pending()` concurrently must never produce a duplicate id --
    a duplicate would silently clobber `self._pending[req_id]` and strand
    whichever caller's queue got overwritten."""
    client = EngineClient("/nonexistent")
    ids: list[int] = []
    ids_lock = threading.Lock()

    def worker():
        for _ in range(200):
            req_id, _ = client._register_pending()
            with ids_lock:
                ids.append(req_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(ids) == 8 * 200
    assert len(set(ids)) == len(ids), "duplicate request id allocated under contention"


async def test_request_timeout_raises_client_error_on_expiry(tmp_path):
    # The other half of the fb-freeze bug: even with unique ids, a request
    # that never gets an answer (server hangs, connection half-open, etc.)
    # must not block the caller (the render loop) forever.
    sock_path = str(tmp_path / "blackhole.sock")

    async def handle(reader, writer):
        writer.write(proto.encode(proto.hello()))
        await writer.drain()
        line = await reader.readline()
        msg = json.loads(line)
        writer.write(proto.encode(proto.response(msg["id"], {"ok": "hello"})))
        await writer.drain()
        await reader.readline()  # swallow the next request, never respond
        await asyncio.sleep(5)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=sock_path)
    async with server:
        client = EngineClient(sock_path)
        await asyncio.to_thread(client.connect)
        client.start_reader()
        with pytest.raises(ClientError, match="timed out"):
            await asyncio.to_thread(client.request, "status", timeout=0.2)
        await asyncio.to_thread(client.close)


async def test_request_default_timeout_none_is_unchanged_blocking_behavior(tmp_path):
    # Regression: omitting `timeout` must behave exactly as before (block
    # until the real response arrives), proven against a real engine/server.
    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    client.start_reader()
    resp = await asyncio.to_thread(client.request, "status")
    assert resp["ok"] is True
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


def test_wait_first_snapshot_retargets_on_page_changed_during_wait():
    """Regression: a page_changed arriving before the first snapshot for the
    ORIGINAL topic must retarget the wait via on_event, not get silently
    dropped while wait_first_snapshot keeps blocking on a topic nobody's
    steering toward anymore (the fb-startup race: the input thread is
    already live while the main thread blocks here waiting for the first
    snapshot of the page it subscribed to before startup)."""
    q: queue.Queue = queue.Queue()
    state = {"topic": "page.eventlog"}

    def on_event(msg):
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            state["topic"] = f"page.{msg['data']['page']}"

    q.put({"kind": "event", "name": "page_changed", "data": {"page": "second"}})
    # A stale snapshot for the ORIGINAL topic, arriving after the switch
    # (the known transient-artifact case from task-1's report) -- must NOT
    # satisfy the wait now that the target has moved on.
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 9, "data": {"stale": True}})
    q.put({"kind": "snapshot", "topic": "page.second", "seq": 1, "data": {"marker": "fake"}})

    vm = wait_first_snapshot(q, lambda: state["topic"], on_event)
    assert vm == {"marker": "fake"}


def test_wait_first_snapshot_still_accepts_a_plain_string_topic():
    # Backward-compat: a fixed string target (no on_event) is still valid --
    # every existing call site that never needs mid-wait retargeting.
    q: queue.Queue = queue.Queue()
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 1, "data": {"n": 1}})
    assert wait_first_snapshot(q, "page.eventlog") == {"n": 1}


def test_drain_latest_self_heals_within_same_batch_on_page_change():
    """Regression: `on_event` mutates `state["topic"]` mid-drain via
    switch_topic(), and a same-batch snapshot for the NEW topic must not be
    dropped by a topics-membership check frozen once at call-entry."""
    q: queue.Queue = queue.Queue()
    state = {"topic": "page.eventlog"}

    def on_event(msg):
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            state["topic"] = f"page.{msg['data']['page']}"

    q.put({"kind": "event", "name": "page_changed", "data": {"page": "second"}})
    q.put({"kind": "snapshot", "topic": "page.second", "seq": 1, "data": {"marker": "fake"}})

    out = drain_latest(q, lambda: {state["topic"]}, on_event=on_event)
    assert out == {"page.second": {"marker": "fake"}}


def test_drain_latest_still_accepts_a_plain_set_of_topics():
    # Backward-compat: a fixed set (no retargeting) behaves exactly as
    # before -- every existing call site that subscribes to one static topic.
    q: queue.Queue = queue.Queue()
    q.put({"kind": "snapshot", "topic": "page.eventlog", "seq": 1, "data": {"n": 1}})
    assert drain_latest(q, {"page.eventlog"}) == {"page.eventlog": {"n": 1}}


# -- dispatch_key / fetch_keymap (Phase 4 Task 1, docs/phase4-notes.md) -----
#
# Shared key-resolution machinery both clients now build their key dispatch
# from -- see clients/base.py's own docstrings. Tested here at the unit
# level with a tiny recorder double (same convention as
# `test_switch_topic_is_noop_when_topics_match`'s `_Recorder`), no real
# terminal/evdev/socket needed for `dispatch_key` itself.

class _ActionRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def action(self, name, args=None):
        self.calls.append((name, args or {}))


def test_dispatch_key_client_quit_returns_quit_without_calling_action():
    client = _ActionRecorder()
    assert dispatch_key(client, "q", {"q": "client.quit"}) == KEY_QUIT
    assert client.calls == []


def test_dispatch_key_sends_named_action_for_a_real_key():
    client = _ActionRecorder()
    assert dispatch_key(client, "c", {"c": "eventlog.clear"}) == KEY_HANDLED
    assert client.calls == [("eventlog.clear", {})]


def test_dispatch_key_unmapped_key_is_a_silent_noop():
    client = _ActionRecorder()
    assert dispatch_key(client, "z", {"q": "client.quit"}) == KEY_NOOP
    assert client.calls == []


def test_dispatch_key_unrecognized_client_pseudo_action_is_a_silent_noop():
    # A keymap.toml typo or a future pseudo-action this client build
    # doesn't know about yet -- never reaches the engine either way.
    client = _ActionRecorder()
    assert dispatch_key(client, "x", {"x": "client.bogus"}) == KEY_NOOP
    assert client.calls == []


def test_dispatch_key_drives_a_remapped_page_action():
    # "a remapped key in a fake keymap drives the action" -- the exact
    # scenario a user's keymap.toml exists to enable: "n" no longer means
    # page.next once the fake describe-shaped keymap below says otherwise.
    client = _ActionRecorder()
    fake_keymap = {"n": "page.prev"}
    assert dispatch_key(client, "n", fake_keymap) == KEY_HANDLED
    assert client.calls == [("page.prev", {})]


# -- Phase 8 Task 6: args-table entries + help_toggle -----------------------

def test_dispatch_key_sends_baked_args_for_a_table_entry():
    client = _ActionRecorder()
    keymap = {"1": {"action": "page.jump", "args": {"position": 1}}}
    assert dispatch_key(client, "1", keymap) == KEY_HANDLED
    assert client.calls == [("page.jump", {"position": 1})]


def test_dispatch_key_help_toggle_returns_help_toggle_without_calling_action():
    client = _ActionRecorder()
    assert dispatch_key(client, "?", {"?": "client.help_toggle"}) == KEY_HELP_TOGGLE
    assert client.calls == []


async def test_fetch_keymap_reads_the_real_engine_keymap_over_the_wire(tmp_path):
    from midicrt.engine.keymap import DEFAULT_KEYMAP

    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    keymap = await asyncio.to_thread(fetch_keymap, client)
    # A build with no keymap.toml ships the pure built-in default -- see
    # engine/keymap.py's own module docstring for the full schema-v2
    # shape (page.jump args-tables, client.help_toggle, ...).
    assert keymap == eng.keymap == DEFAULT_KEYMAP
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


def test_fetch_keymap_falls_back_to_default_when_server_predates_the_field():
    # Wire-compat: an older server's `describe` response has no "keymap"
    # key at all -- must not raise, must fall back to DEFAULT_KEYMAP.
    from midicrt.engine.keymap import DEFAULT_KEYMAP

    class _OldServerClient:
        def request(self, cmd):
            return {"data": {"current_page": "eventlog"}}   # no "keymap" key

    assert fetch_keymap(_OldServerClient()) == DEFAULT_KEYMAP


async def test_fetch_keymap_sections_reads_global_and_page_sections(tmp_path):
    eng, srv, task = await make(tmp_path)
    client = EngineClient(srv.socket_path)
    await asyncio.to_thread(client.connect)
    bundle = await asyncio.to_thread(fetch_keymap_sections, client)
    assert bundle["effective"] == eng.keymap
    assert bundle["global"] == eng.keymap_global
    assert bundle["page"] == eng.keymap_page
    assert bundle["hints_enabled"] is True
    assert bundle["roster"] == list(eng.pages)
    await asyncio.to_thread(client.close)
    eng.stop(); await task; await srv.close()


def test_fetch_keymap_sections_falls_back_to_empty_dicts_when_server_predates_the_fields():
    from midicrt.engine.keymap import DEFAULT_KEYMAP

    class _OldServerClient:
        def request(self, cmd):
            return {"data": {"current_page": "eventlog"}}   # no keymap_global/keymap_page/topics

    bundle = fetch_keymap_sections(_OldServerClient())
    assert bundle == {"effective": DEFAULT_KEYMAP, "global": {}, "page": {}, "hints_enabled": True,
                      "roster": []}


def test_fetch_keymap_sections_derives_roster_from_topics_not_the_sorted_pages_field():
    # "pages" (describe's own field) is alphabetically sorted, display-only
    # (engine/server.py's own comment) -- the roster must come from
    # "topics" (roster/cycle order) instead, or a page.jump entry would
    # resolve to the WRONG target page name in the overlay.
    class _FakeClient:
        def request(self, cmd):
            return {"data": {
                "pages": ["eventlog", "voices"],   # sorted -- happens to match order here
                "topics": ["page.voices", "page.eventlog", "overlay.status"],
            }}

    bundle = fetch_keymap_sections(_FakeClient())
    assert bundle["roster"] == ["voices", "eventlog"]
