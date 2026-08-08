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
Each browser's `WebSink` holds a `maxsize=1` asyncio.Queue: `offer()` is
drop-and-replace (matches `ProtocolServer._push_loop`'s own latest-wins
policy for snapshots), so one slow browser tab can never make the bridge
-- or any OTHER tab -- buffer unboundedly; it just misses intermediate
frames and catches up to the newest one whenever it next reads.
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

DEFAULT_SUBSCRIBE_RATE = 10.0


class WebSink:
    """Per-websocket delivery point. `offer()` is called from the bridge's
    loop-thread callback (`Bridge._fan_out`); the websocket handler task is
    the only consumer (`await sink.queue.get()`). A `None` item is the EOF
    sentinel -- "the engine connection was lost, stop reading and close the
    socket" -- mirroring `EngineClient`'s own `None` sentinel convention
    (see `clients/base.py`'s `_on_eof`).
    """

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    def offer(self, msg) -> None:
        """Drop-and-replace: never blocks, never grows past one queued item."""
        try:
            self.queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.queue.get_nowait()  # discard whatever's queued...
        except asyncio.QueueEmpty:
            pass  # ...raced with the consumer's own get(); nothing to discard
        try:
            self.queue.put_nowait(msg)  # ...and replace it with the newest item
        except asyncio.QueueFull:
            pass  # raced with a concurrent offer(); drop -- next offer() will land


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
                self._latest[topic] = msg
                if topic == chrome.OVERLAY_STATUS_TOPIC:
                    self.state["status_vm"] = msg["data"]
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
        state right away instead of waiting for the next engine change."""
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
