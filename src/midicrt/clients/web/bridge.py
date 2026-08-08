"""EngineClient <-> asyncio fan-out bridge for midicrt-web.

One `EngineClient` connection is shared by every websocket-connected
browser: a background "pump" thread drains the client's reader-thread
inbox (`EngineClient.start_reader()`) and hands each message to the
asyncio event loop via `loop.call_soon_threadsafe` -- the standard
thread->loop bridge (`call_soon_threadsafe` is the only thread-safe way to
schedule work on a loop from another thread). Once on the loop thread,
`_on_message` fans the message out to every registered `WebSink` (one per
open websocket) and tracks engine state (current page/topic, current
status vm) the same way `clients/tui.py`'s `run_tui`/`clients/fb/app.py`'s
`_run_device` do -- `current_page_topic`/`switch_topic`/the multi-topic
subscribe convention are reused verbatim from `clients/base.py`, not
reimplemented (this module is the "thin adapter outside a terminal loop"
the phase-6 brief asks for).

Page-change resubscribe: deferred, not inline
-----------------------------------------------
`switch_topic()` is a BLOCKING call (it round-trips two requests over the
engine socket). `_on_message` runs synchronously on the loop thread (per
`call_soon_threadsafe`'s contract) -- calling a blocking function directly
there would freeze the ENTIRE event loop for the duration of that round
trip, which is worse than it sounds: in the test suite, the engine+server
fixture runs on this SAME loop (see test_web_bridge.py's `make` reuse), so
blocking the loop also blocks the server coroutines that would otherwise
answer the very request we're blocked on -- a real self-deadlock, not just
a latency blip (caught by the first draft of this module hanging every
page-changed test). `_handle_page_changed` instead flips `self.state`
immediately (optimistic -- matches tui.py's own immediate state flip after
`switch_topic()`, just decoupled from the network round trip) and defers
the actual unsubscribe/subscribe pair to a worker thread via
`asyncio.to_thread` inside a fire-and-forget task, so the loop thread is
always free to keep servicing other websockets/HTTP handlers (and, in
tests, the fixture server itself) while that round trip is in flight.

Multi-browser fan-out
----------------------
Each browser's `WebSink` holds a small set of per-KEY delivery slots
(`_CoalescingQueue`, below): `offer()` is drop-and-replace WITHIN a key
(matches `ProtocolServer._push_loop`'s own latest-wins policy for
snapshots), so one slow browser tab can never make the bridge -- or any
OTHER tab -- buffer unboundedly PER KEY; it just misses intermediate
frames for that key and catches up to the newest one whenever it next
reads. Snapshot messages key on their own `topic` (bounded by the page
roster + `overlay.status`, at most ~15 distinct keys ever); everything
else (events, the EOF sentinel) shares one legacy key, unchanged from the
original single-slot design.

Task 1's review flagged a real bug in the ORIGINAL implementation (a
single, undifferentiated `maxsize=1 asyncio.Queue` for every message
regardless of topic): `add_sink()` replays every cached snapshot in
`self._latest` by calling `offer()` once per topic in a tight loop --
against a single shared slot, only the LAST offer of that loop survived
(drop-and-replace has no concept of "topic" to preserve), so a browser
reconnecting mid-session would see only ONE of {its current page,
`overlay.status`} until the next unrelated engine update happened to
refill the other. `_CoalescingQueue` fixes this by giving each snapshot
topic its own slot -- `add_sink()`'s replay loop needs no change at all
(see its own docstring): offering N cached topics now lands N independent
deliveries instead of one clobbering the rest.
"""
import asyncio
import queue
import threading

from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    switch_topic,
)

# Web spec (docs/phase6-notes.md item 6): 5/s, not the 10/s this branch
# shipped with pre-merge -- a browser tab is a slower consumer than a
# terminal render loop, and WebSink's own drop-and-replace queue already
# makes a higher rate here pure waste (the queue can hold exactly one
# pending frame regardless).
DEFAULT_SUBSCRIBE_RATE = 5.0


# Shared coalescing key for every message that ISN'T a per-topic snapshot
# (events, the EOF sentinel, and anything else offer() doesn't recognize) --
# a single object identity, matching the ORIGINAL single-slot WebSink's
# behavior exactly for these message kinds (see module docstring: only
# snapshots get split into per-topic slots; nothing here regresses the
# "one slow tab can't buffer unboundedly" guarantee since events/EOF are
# rare, discrete occurrences, not a continuous per-tick stream like
# snapshots are).
_UNKEYED = object()


class _CoalescingQueue:
    """Delivery queue backing `WebSink` -- N independent maxsize-1 "slots",
    one per coalescing key, instead of a single global one. `offer(key,
    msg)` replaces whatever's pending for THAT key (drop-and-replace, same
    policy the original single-slot queue had, now scoped per key); a
    brand-new key gets its own slot and its own position in the delivery
    FIFO. Re-offering an already-pending key does NOT move its position --
    only its payload updates -- so delivery order is "first time each key
    became pending since it was last drained", the natural generalization
    of the old single-key queue's FIFO-of-one.

    `get()`/`get_nowait()`/`qsize()` mirror `asyncio.Queue`'s own API
    exactly (every existing call site -- app.py's websocket handler,
    every test here -- keeps working unchanged against `sink.queue`).
    `put_nowait` is deliberately NOT exposed: `offer()` (on `WebSink`) is
    the only writer, so key selection can never be bypassed.
    """

    def __init__(self):
        self._pending: dict[object, object] = {}
        self._ready: asyncio.Queue = asyncio.Queue()

    def offer(self, key: object, msg) -> None:
        is_new_key = key not in self._pending
        self._pending[key] = msg  # drop-and-replace within this key
        if is_new_key:
            self._ready.put_nowait(key)  # never blocks: unbounded key queue

    async def get(self):
        key = await self._ready.get()
        return self._pending.pop(key)

    def get_nowait(self):
        key = self._ready.get_nowait()  # raises asyncio.QueueEmpty, same as before
        return self._pending.pop(key)

    def qsize(self) -> int:
        return self._ready.qsize()


class WebSink:
    """Per-websocket delivery point. `offer()` is called from the bridge's
    loop-thread callback (`Bridge._fan_out`, and from `Bridge.add_sink()`'s
    cached-snapshot replay); the websocket handler task is the only
    consumer (`await sink.queue.get()`). A `None` item is the EOF sentinel
    -- "the engine connection was lost, stop reading and close the socket"
    -- mirroring `EngineClient`'s own `None` sentinel convention (see
    `clients/base.py`'s `_on_eof`).

    Backed by `_CoalescingQueue` (see module docstring for the multi-topic
    replay bug this fixes): a snapshot message coalesces on its own
    `topic`; everything else (events, `None`) shares one legacy slot,
    identical to the original single-`asyncio.Queue(maxsize=1)` design.
    """

    def __init__(self):
        self.queue = _CoalescingQueue()

    def offer(self, msg) -> None:
        """Drop-and-replace within the message's coalescing key -- never
        blocks. A snapshot's key is its own `topic` (so two different
        topics can never clobber each other, see module docstring); every
        other message shares `_UNKEYED`, unchanged from before this fix."""
        key = _UNKEYED
        if isinstance(msg, dict) and msg.get("kind") == "snapshot" and "topic" in msg:
            key = ("snapshot", msg["topic"])
        self.queue.offer(key, msg)


class Bridge:
    """One EngineClient connection, fanned out to N WebSinks."""

    def __init__(self, client: EngineClient, subscribe_rate: float = DEFAULT_SUBSCRIBE_RATE):
        self.client = client
        self.subscribe_rate = subscribe_rate
        self.loop: asyncio.AbstractEventLoop | None = None
        self.engine_hello: dict = {}
        self.state = {"page": None, "topic": None, "status_vm": dict(chrome.DEFAULT_STATUS_VM)}
        self._latest: dict[str, dict] = {}
        self._sinks: set[WebSink] = set()
        self._inbox: queue.Queue | None = None
        self._pump_thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking: connect, hello, ask which page is CURRENT (not an
        assumed "eventlog" -- see `current_page_topic`'s docstring),
        subscribe to it plus `overlay.status`, THEN start the reader thread
        and this bridge's own pump thread. Same ordering as
        `run_tui`/`fb.app.run`: subscribe() happens on the pre-reader sync
        path deliberately (see `EngineClient.request`'s docstring). Must be
        called off the event loop thread (e.g. via `asyncio.to_thread`)
        since every step here blocks on socket I/O; `loop` is the loop
        `_on_message`/`_on_eof` get scheduled onto, captured explicitly
        because `asyncio.get_running_loop()` would raise off the loop thread.
        """
        self.loop = loop
        self.engine_hello = self.client.connect()
        page, topic = current_page_topic(self.client)
        self.client.subscribe([topic, chrome.OVERLAY_STATUS_TOPIC], max_rate=self.subscribe_rate)
        self.state["page"], self.state["topic"] = page, topic
        self._inbox = self.client.start_reader()
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self) -> None:
        """Blocking: closes the single shared engine connection. Call via
        `asyncio.to_thread` from async code (e.g. an aiohttp `on_cleanup`
        hook) for the same reason `start()` does."""
        self.client.close()

    def _pump_loop(self) -> None:
        while True:
            msg = self._inbox.get()
            # Ledgered minor (Task 1 review): teardown race guard -- if
            # `bridge.stop()`'s connection close raced the owning event
            # loop's own shutdown, the loop may already be closed by the
            # time this thread wakes on the reader's EOF sentinel; calling
            # `call_soon_threadsafe` on a closed loop raises `RuntimeError:
            # Event loop is closed` from this daemon thread (observed as a
            # `PytestUnhandledThreadExceptionWarning` during test teardown,
            # never a real test failure -- see task-1-report.md's
            # self-review). This narrows but does not eliminate the race
            # (the loop can still close between this check and the call
            # below); it's a cheap mitigation for the common case, not a
            # full fix.
            if self.loop.is_closed():
                return
            if msg is None:
                self.loop.call_soon_threadsafe(self._on_eof)
                return
            self.loop.call_soon_threadsafe(self._on_message, msg)

    # -- loop-thread handlers (everything below here runs ON the loop) ------
    def _on_eof(self) -> None:
        for sink in list(self._sinks):
            sink.offer(None)

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "snapshot":
            topic = msg.get("topic")
            if topic == self.state["topic"] or topic == chrome.OVERLAY_STATUS_TOPIC:
                if topic == chrome.OVERLAY_STATUS_TOPIC:
                    self.state["status_vm"] = msg["data"]
                    # Merge-reconciliation fix (docs/phase6-notes.md item 1):
                    # render the status STRING here, server-side, with the
                    # exact same `chrome.status_text()` function clients/
                    # tui.py's/fb/app.py's own status rows call, and ship it
                    # alongside the raw vm as `status_text`. page.html
                    # displays this verbatim instead of keeping its own JS
                    # copy of the format -- the pre-merge copy had silently
                    # drifted (no `rec`/REC_MARKER handling at all, see
                    # chrome.py's own REC_MARKER). One rendering path, never
                    # two to keep in sync again.
                    msg["status_text"] = chrome.status_text(msg["data"])
                self._latest[topic] = msg
                self._fan_out(msg)
            # else: a stale snapshot for a topic we just switched away from
            # (switch_topic's re-subscribe timing) -- drop it, same filter
            # discipline as tui.py/fb's own drain_latest.
        elif kind == "event":
            if msg.get("name") == "page_changed":
                self._handle_page_changed(msg["data"]["page"])
            self._fan_out(msg)

    def _handle_page_changed(self, new_page: str) -> None:
        new_topic = f"page.{new_page}"
        old_topic = self.state["topic"]
        # Flip state and drop the old topic's cache entry NOW (see module
        # docstring) -- the actual unsubscribe/subscribe round trip is
        # deferred below so this loop-thread callback never blocks.
        self._latest.pop(old_topic, None)
        self.state["page"], self.state["topic"] = new_page, new_topic
        self.loop.create_task(self._resubscribe(old_topic, new_topic))

    async def _resubscribe(self, old_topic: str, new_topic: str) -> None:
        try:
            await asyncio.to_thread(
                switch_topic, self.client, old_topic, new_topic, self.subscribe_rate)
        except ClientError:
            pass  # connection loss surfaces via the pump thread's own EOF path

    def _fan_out(self, msg: dict) -> None:
        for sink in list(self._sinks):
            sink.offer(msg)

    # -- sinks ----------------------------------------------------------------
    def add_sink(self, sink: WebSink) -> None:
        """Register a new websocket's sink and immediately replay whatever
        latest snapshots this bridge already has cached (current page topic
        + overlay.status), so a browser that connects mid-session sees
        state right away instead of waiting for the next engine change.

        T1-review carried bug, fixed here (see bridge.py's module
        docstring + `_CoalescingQueue`): this loop calls `sink.offer()`
        once per cached topic, and used to land on a single shared
        maxsize=1 slot -- only the LAST offer in the loop survived, so a
        reconnecting browser saw either its page or `overlay.status`,
        never both, until the next unrelated engine update refilled
        whichever one got clobbered. `WebSink.offer()` now keys snapshots
        by topic, so this loop needs no change: each cached topic lands in
        its own slot."""
        self._sinks.add(sink)
        for msg in self._latest.values():
            sink.offer(msg)

    def remove_sink(self, sink: WebSink) -> None:
        self._sinks.discard(sink)

    def hello_message(self) -> dict:
        return {
            "kind": "hello",
            "proto_version": self.engine_hello.get("proto_version"),
            "engine_version": self.engine_hello.get("engine_version"),
            "page": self.state["page"],
            "topic": self.state["topic"],
        }

    # -- request/response proxies (blocking; call via asyncio.to_thread) -----
    def describe(self) -> dict:
        return self.client.request("describe")["data"]

    def action(self, name: str, args: dict | None = None) -> dict:
        return self.client.action(name, args or {})["data"]
