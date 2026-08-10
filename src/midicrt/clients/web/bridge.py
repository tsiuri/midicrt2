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
roster + `overlay.status`, at most ~15 distinct keys ever); `alert`
events get their own bounded, sequence-keyed slots (see `WebSink.
_offer_alert`, below); the EOF sentinel gets one permanently dedicated
key of its own; every OTHER named event (`page_changed`, `keymap_
changed`, `learn_*`, `capture_*`, ...) keys on its own `("event", name)`
slot too (Phase 8 Task 6 -- see that task's own paragraph further down
for the collision this replaced); only a message that is none of the
above still falls back to one remaining legacy `_UNKEYED` slot.

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

Task 2's review then reproduced the SAME collision class one level up:
every non-snapshot message (including `alert` and the EOF sentinel)
still shared one `_UNKEYED` slot, so two alerts offered back-to-back (or
an alert immediately followed by EOF) silently dropped everything but
the last. This matters specifically for `alert` because
`analyzers/stucknotes.py`'s own "Alert-storm potential" section documents
a real, undebounced burst mechanism (a held note's sustain toggle
re-firing the same warn/crit alert repeatedly, all emitted synchronously
within one `_tick_analyzers` call, before any consumer coroutine gets a
chance to drain the sink) -- and phase6-notes item 4 requires alerts to
surface, full stop. Fixed by giving `alert` events their own bounded,
sequence-keyed slots (`WebSink._offer_alert`) and the EOF sentinel a
permanently separate key (`_EOF_KEY`) that can never be coalesced with an
alert or anything else in either direction.

Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap revamp)
found the SAME collision class a third time: every OTHER named event
(`page_changed`, `learn_*`, `capture_*`, ...) originally shared one
remaining `_UNKEYED` slot, "a deliberate, disclosed scope cut" at the
time because none of them had a documented burst-generating mechanism.
That held only as long as no single discrete action ever emitted TWO
DIFFERENT event names back-to-back -- `Engine._set_current_page` now
does exactly that (`page_changed` immediately followed by `keymap_
changed`, see that method's own docstring), which silently dropped the
`page_changed` half under the old shared-slot scheme (live-reproduced:
`test_bridge_follows_page_changed_and_resubscribes`). `WebSink.offer()`
now keys every event by its `name` (`("event", name)`) instead of
lumping them all into `_UNKEYED` -- generalizing the same "give a
distinct, meaningfully-different message its own slot" precedent
already applied to `alert`/EOF, this time to ALL named events at once
rather than patching just the one pairing that happened to expose it
(`learn_armed` immediately followed by `learn_bound`/`learn_cancelled`
had the identical latent exposure). Repeated SAME-named events still
correctly coalesce to the latest; only different names stop colliding.
`_UNKEYED` remains as the defensive fallback for a message that is
neither a snapshot, an alert, nor a named event.

Engine-restart reconnect (Phase 9 Task 4, user ruling: "option B")
--------------------------------------------------------------------------
Through Phase 6-8, an EOF on the engine socket (`midicrtd` restarting)
permanently killed this bridge's usefulness: `_pump_loop` read the `None`
sentinel once, offered it to every sink (which made `app.py::_handle_ws`
close that browser's websocket), and returned -- nothing ever reconnected
`self.client`. A browser reconnecting afterward got a completely
normal-looking `hello_message()` built from `self.engine_hello` (captured
once at the ORIGINAL `start()` and never cleared) plus a replay of
whatever stale snapshots were still sitting in `self._latest` -- an
honest-looking lie, then permanent silence (`docs/phase6-web.md` §7,
pre-fix, documented this in full; that writeup is deleted as part of this
fix, not just superseded).

**Option B (user ruling, binding): browsers KEEP their websockets across
an engine outage.** `_pump_loop` no longer offers the EOF sentinel to
sinks on an ordinary engine disconnect -- only a genuine `stop()` does
that now (`_on_shutdown`, the bridge itself going away, e.g. `midicrt-web`
process exit -- see `stop()`'s own docstring). An engine-side EOF instead
runs `_reconnect_loop()` **on the same pump thread** (blocking there is
fine -- it is a dedicated daemon thread, never the event loop): clear the
stale `engine_hello`/`_latest` immediately (`_on_disconnected`, scheduled
onto the loop via the usual `call_soon_threadsafe` bridge) so any NEW
websocket connecting mid-outage gets an honest empty hello instead of a
misleadingly normal one, then retry `connect()` + subscribe + `start_
reader()` against a **brand-new** `EngineClient` (reusing the dead one is
a trap: `start_reader()` is a no-op once `_reader_thread` is non-None, and
the old reader thread can never be restarted) with bounded, jittered,
doubling backoff (`DEFAULT_RECONNECT_BASE_DELAY`/`_MAX_DELAY`, deliberately
mirroring `page.html`'s OWN browser-side reconnect backoff shape --
500ms first retry, doubling, capped at 10s -- so the two reconnect loops
this one feature now has, browser<->midicrt-web and bridge<->midicrtd,
read as one consistent posture, not two arbitrarily different ones), kept
running **indefinitely** (the daemon may be down for minutes -- a
`systemctl restart midicrtd` or a real crash-and-supervisor-restart cycle,
not a sub-second blip) until it succeeds or `stop()` sets `self.
_stopping` (checked before every attempt AND used to short-circuit an
in-progress backoff sleep via `threading.Event.wait`'s own timeout-or-set
return value, so a bridge shutdown mid-outage doesn't leave this thread
sleeping for up to 10 more seconds first).

Once reconnected, `_on_reconnected` (loop thread) re-derives the CURRENT
page fresh via `current_page_topic()` against the NEW connection rather
than assuming the pre-outage topic still matches -- `midicrtd` restarting
is a fresh process; nothing guarantees its default current page is the
same one browsers were looking at before, and subscribing to a topic that
is no longer the engine's live "current" page would just freeze that
topic's snapshot forever (exactly the tui/fb clients' own reasoning for
always tracking current-page-via-`switch_topic`, never a client-remembered
page name). A `page_changed`-shaped event is synthesized here too, but
ONLY if the page actually differs, so `page.html`'s nav-highlight state
(`currentPage`, driven by `hello`/`page_changed` frames, not by snapshot
content) stays honest across a restart that lands on a different default
page -- the fresh snapshot itself would render the right BODY regardless
(every `PAGE_RENDERERS` entry fully replaces `#page-body`'s innerHTML off
the snapshot's OWN `topic`, never merges into stale DOM -- verified by
reading `page.html`'s renderers, not assumed; this is the "drop-and-
replace protocol" the brief asked to double check), but the top nav
button would otherwise still highlight the pre-outage page.

A `bridge_status` event (`{"kind": "event", "name": "bridge_status",
"data": {"status": "reconnecting" | "connected"}}`) is fanned out on both
transitions -- wired into the ONE indicator `page.html` already had
(`#conn`, previously driven only by the browser's own websocket open/
close) rather than inventing a second one; see that file's `handleMessage`
for the two-line addition this required. `hello_message()` also grew a
`"status"` field so a websocket that connects (or reconnects) while the
bridge itself is mid-outage sees this honestly from its very first frame,
not just from a live event it might have connected too late to catch.

Review fix: "retry forever" made structurally true, not just usually true
--------------------------------------------------------------------------
The first landed version only caught `ClientError` around each connect
attempt in `_reconnect_loop` -- but `clients/base.py`'s read paths used to
let a malformed/corrupt line from the peer raise a bare, uncaught
`json.JSONDecodeError` (protocol-version skew, a stale-socket race, a
genuinely corrupt peer during the reconnect handshake specifically), which
killed the pump thread and ended reconnection forever: the exact
frozen-forever bug this whole feature exists to fix, reborn through a
narrower trigger than the original "midicrtd just isn't up yet" case.
Fixed two ways, both required: (1) `clients/base.py` now converts
`JSONDecodeError` into the SAME EOF/`ClientError` contract every other
disconnect already uses (see that module's own docstring -- this also
brings the client side up to the exact policy `engine/server.py`'s
`ProtocolServer._handle` already applies to a malformed line from a
CLIENT, dropping that connection rather than crashing); (2)
`_reconnect_loop` also catches a bare `Exception` around each attempt now
(logged at WARNING with a traceback, then retried) as belt-and-suspenders
on top of fix (1) -- structurally guaranteeing no exception class, known
or future, can ever escape this loop, rather than relying on having
enumerated every failure mode a connect attempt could raise. Each attempt
also explicitly `.close()`s the `EngineClient` it just tried (whether it
failed or is being replaced by a successful one) instead of relying on
garbage collection to eventually release the socket fd.
"""
import asyncio
import logging
import queue
import random
import threading
from collections import deque

from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    switch_topic,
)

_LOG = logging.getLogger(__name__)

# Web spec (docs/phase6-notes.md item 6): 5/s, not the 10/s this branch
# shipped with pre-merge -- a browser tab is a slower consumer than a
# terminal render loop, and WebSink's own drop-and-replace queue already
# makes a higher rate here pure waste (the queue can hold exactly one
# pending frame regardless).
DEFAULT_SUBSCRIBE_RATE = 5.0

# Reconnect backoff (module docstring's "Engine-restart reconnect" section):
# deliberately the SAME shape page.html's own browser-side reconnect uses
# (500ms first retry, doubling, capped at 10s) -- one consistent reconnect
# posture across both legs of this feature (bridge<->midicrtd here,
# browser<->midicrt-web there), not two arbitrarily different ones.
DEFAULT_RECONNECT_BASE_DELAY = 0.5   # seconds -- first retry, before jitter
DEFAULT_RECONNECT_MAX_DELAY = 10.0   # seconds -- backoff ceiling, before jitter
DEFAULT_RECONNECT_JITTER = 0.2       # +/-20% spread applied to each computed delay

# `bridge_status` event values fanned out to sinks (and mirrored in
# `hello_message()`'s own "status" field) -- see module docstring.
BRIDGE_STATUS_CONNECTED = "connected"
BRIDGE_STATUS_RECONNECTING = "reconnecting"


# Shared coalescing key for every message that isn't a per-topic snapshot,
# a per-sequence alert slot, or the EOF sentinel -- i.e. `page_changed`/
# `learn_*`/`capture_*` events and anything else offer() doesn't recognize.
# A single object identity, matching the ORIGINAL single-slot WebSink's
# behavior exactly for these message kinds (see module docstring for why
# generalizing further wasn't needed: none of them has a documented
# burst-generating mechanism the way `alert` does).
_UNKEYED = object()

# EOF (the `None` sentinel) gets its own PERMANENT key, distinct from
# `_UNKEYED` and from every alert key -- Task 2's review reproduced an
# alert immediately followed by EOF losing the alert (both shared
# `_UNKEYED` before this fix); EOF must always be delivered, and must
# never clobber -- or be clobbered by -- anything else.
_EOF_KEY = object()

# Bound on outstanding (undelivered) alert slots per sink (review's "a
# sane cap ... say 16"). Past this, `WebSink._offer_alert` drops the
# OLDEST pending alert to make room -- bounded, not unbounded, growth
# even under analyzers/stucknotes.py's documented undebounced alert-storm
# case, while still surfacing every alert that fits.
_ALERT_QUEUE_CAP = 16


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

    `evict(key)` removes a pending key WITHOUT delivering it -- used by
    `WebSink`'s bounded alert delivery to drop the oldest outstanding
    alert slot when its cap is exceeded. The evicted key's entry can still
    be sitting in `_ready` (a plain `asyncio.Queue` has no O(1) random
    removal) -- `get()`/`get_nowait()` skip any key no longer present in
    `_pending` rather than assuming every dequeued key is still live.

    `get()`/`get_nowait()`/`qsize()` mirror `asyncio.Queue`'s own API
    (every existing call site -- app.py's websocket handler, every test
    here -- keeps working unchanged against `sink.queue`), except `qsize()`
    now reports `len(self._pending)` (the true count of deliverable items)
    rather than `self._ready.qsize()`, which would overcount once evicted
    keys can leave stale entries behind -- equivalent in the non-eviction
    case, correct in both. `put_nowait` is deliberately NOT exposed:
    `offer()` (on `WebSink`) is the only writer, so key selection can
    never be bypassed.
    """

    def __init__(self):
        self._pending: dict[object, object] = {}
        self._ready: asyncio.Queue = asyncio.Queue()

    def __contains__(self, key: object) -> bool:
        return key in self._pending

    def offer(self, key: object, msg) -> None:
        is_new_key = key not in self._pending
        self._pending[key] = msg  # drop-and-replace within this key
        if is_new_key:
            self._ready.put_nowait(key)  # never blocks: unbounded key queue

    def evict(self, key: object) -> None:
        """Drop `key`'s pending payload without delivering it. A no-op if
        it's already been delivered or was never pending."""
        self._pending.pop(key, None)

    async def get(self):
        while True:
            key = await self._ready.get()
            if key in self._pending:
                return self._pending.pop(key)
            # else: evicted before its turn came up (see evict()) -- skip it

    def get_nowait(self):
        while True:
            key = self._ready.get_nowait()  # raises asyncio.QueueEmpty when truly empty
            if key in self._pending:
                return self._pending.pop(key)

    def qsize(self) -> int:
        return len(self._pending)


class WebSink:
    """Per-websocket delivery point. `offer()` is called from the bridge's
    loop-thread callback (`Bridge._fan_out`, and from `Bridge.add_sink()`'s
    cached-snapshot replay); the websocket handler task is the only
    consumer (`await sink.queue.get()`). A `None` item is the EOF sentinel
    -- "the engine connection was lost, stop reading and close the socket"
    -- mirroring `EngineClient`'s own `None` sentinel convention (see
    `clients/base.py`'s `_on_eof`).

    Backed by `_CoalescingQueue` (see module docstring for the bugs this
    fixes): a snapshot message coalesces on its own `topic`; an `alert`
    event gets its own bounded, sequence-keyed slot (`_offer_alert`); EOF
    gets one permanently dedicated key; every OTHER named event
    (`page_changed`, `keymap_changed`, `learn_*`, `capture_*`, ...) now
    coalesces on its own `("event", name)` key (Phase 8 Task 6 fix --
    see module docstring's own writeup of the `page_changed`+`keymap_
    changed` collision this replaces) instead of sharing one legacy
    `_UNKEYED` slot; `_UNKEYED` itself survives only as the defensive
    fallback for a message that is none of the above.
    """

    def __init__(self):
        self.queue = _CoalescingQueue()
        self._alert_seq = 0
        self._pending_alert_keys: deque = deque()
        self._dropped_alerts = 0

    def offer(self, msg) -> None:
        """Route to the right coalescing key -- never blocks. See the
        class docstring / module docstring for what each kind gets."""
        if msg is None:
            self.queue.offer(_EOF_KEY, msg)
            return
        if isinstance(msg, dict) and msg.get("kind") == "snapshot" and "topic" in msg:
            self.queue.offer(("snapshot", msg["topic"]), msg)
            return
        if isinstance(msg, dict) and msg.get("kind") == "event" and msg.get("name") == "alert":
            self._offer_alert(msg)
            return
        if isinstance(msg, dict) and msg.get("kind") == "event" and msg.get("name"):
            # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
            # revamp) discovered the SAME collision class Tasks 1/2 already
            # fixed for snapshots/alerts, one level further: `Engine.
            # _set_current_page` now emits `page_changed` immediately
            # followed by `keymap_changed` from the ONE SAME discrete page
            # transition (see that method's own docstring for why) --
            # exactly the back-to-back-before-any-consumer-drains pattern
            # that clobbers a shared `_UNKEYED` slot (drop-and-replace has
            # no concept of "these are two DIFFERENT event names", so the
            # second offer silently ate the first). Keying by event NAME
            # instead generalizes the existing "give a genuinely distinct,
            # meaningfully-different message its own slot" precedent
            # (alert/EOF already got this) to every named event kind at
            # once, not just this one pairing -- `page_changed`+
            # `keymap_changed` was simply the first pairing that actually
            # exercised the latent bug; `learn_armed` immediately followed
            # by `learn_bound`/`learn_cancelled` had the identical exposure
            # and is fixed by the same change. Repeated same-named events
            # (e.g. two `page_changed` before a slow sink drains) still
            # correctly coalesce to the latest -- only DIFFERENT names stop
            # colliding.
            self.queue.offer(("event", msg["name"]), msg)
            return
        self.queue.offer(_UNKEYED, msg)

    def _offer_alert(self, msg: dict) -> None:
        """Each alert gets a BRAND NEW, never-reused key (an incrementing
        sequence number) -- unlike snapshots, alerts must never coalesce
        with each other; every one is a distinct, meaningful occurrence
        (phase6-notes item 4: alerts MUST surface). Bounded by
        `_ALERT_QUEUE_CAP`: once that many are outstanding, the OLDEST is
        evicted to make room, and a cumulative `dropped_alerts` count is
        attached to every alert offered from that point forward -- NOT
        reset back to zero once attached, so the count is never itself
        lost even if a later burst evicts the very alert message it was
        first stamped onto (the next surviving alert still carries an
        equal-or-greater total). Only the alert actually being delivered
        here is annotated -- a shallow copy, never mutating the caller's
        `msg` in place, because `Bridge._fan_out` passes the SAME dict
        instance to every registered sink; mutating it here would leak
        THIS sink's own drop count into every other sink's copy."""
        while self._pending_alert_keys and self._pending_alert_keys[0] not in self.queue:
            self._pending_alert_keys.popleft()  # already delivered elsewhere -- prune stale head
        if len(self._pending_alert_keys) >= _ALERT_QUEUE_CAP:
            oldest = self._pending_alert_keys.popleft()
            self.queue.evict(oldest)
            self._dropped_alerts += 1
        self._alert_seq += 1
        key = ("alert", self._alert_seq)
        self._pending_alert_keys.append(key)
        if self._dropped_alerts:
            msg = {**msg, "dropped_alerts": self._dropped_alerts}
        self.queue.offer(key, msg)


class Bridge:
    """One EngineClient connection, fanned out to N WebSinks. Survives an
    engine restart -- see the module docstring's "Engine-restart reconnect"
    section for the full design (Option B: sinks/websockets stay open
    across the outage; only the `EngineClient` leg reconnects)."""

    def __init__(self, client: EngineClient, subscribe_rate: float = DEFAULT_SUBSCRIBE_RATE,
                 reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY,
                 reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY,
                 reconnect_jitter: float = DEFAULT_RECONNECT_JITTER):
        self.client = client
        # Captured once here (not re-read off `self.client` later) so every
        # reconnect attempt can build a BRAND NEW `EngineClient` against the
        # same path -- reusing a dead client is a trap, see the module
        # docstring's "Engine-restart reconnect" section.
        self._socket_path = client.socket_path
        self.subscribe_rate = subscribe_rate
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._reconnect_jitter = reconnect_jitter
        self.loop: asyncio.AbstractEventLoop | None = None
        self.engine_hello: dict = {}
        self.state = {"page": None, "topic": None, "status_vm": dict(chrome.DEFAULT_STATUS_VM)}
        # "connected" | "reconnecting" -- see BRIDGE_STATUS_* / hello_
        # message()'s own "status" field / the module docstring.
        self.status: str = BRIDGE_STATUS_CONNECTED
        self._latest: dict[str, dict] = {}
        self._sinks: set[WebSink] = set()
        self._inbox: queue.Queue | None = None
        self._pump_thread: threading.Thread | None = None
        # Set by stop() -- checked by _reconnect_loop() before every retry
        # attempt AND used to short-circuit an in-progress backoff sleep
        # (Event.wait's own timeout-or-set-early return value), so a real
        # shutdown mid-outage doesn't leave the pump thread sleeping for up
        # to _reconnect_max_delay more seconds first.
        self._stopping = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def _connect_client(self, client: EngineClient) -> tuple[dict, str, str, queue.Queue]:
        """Blocking: connect, hello, ask which page is CURRENT (not an
        assumed "eventlog" -- see `current_page_topic`'s docstring),
        subscribe to it plus `overlay.status`, then start THAT client's
        reader thread. Shared by `start()` (the first connect) and
        `_reconnect_loop()` (every retry after an engine-side EOF) so the
        two paths can never drift apart -- every step here blocks on
        socket I/O, so both callers must run this off the event loop
        thread. Returns `(hello, page, topic, inbox)`; raises `ClientError`
        on any failure (connect refused, hello rejected, or the connection
        dying again mid-handshake) -- callers decide what "failed to
        (re)connect" means for their own context."""
        hello = client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic, chrome.OVERLAY_STATUS_TOPIC], max_rate=self.subscribe_rate)
        inbox = client.start_reader()
        return hello, page, topic, inbox

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking: `_connect_client()` against this bridge's original
        `self.client`, THEN start this bridge's own pump thread. Same
        ordering as `run_tui`/`fb.app.run`: subscribe() happens on the
        pre-reader sync path deliberately (see `EngineClient.request`'s
        docstring). Must be called off the event loop thread (e.g. via
        `asyncio.to_thread`) since every step here blocks on socket I/O;
        `loop` is the loop `_on_message`/`_on_disconnected`/`_on_
        reconnected`/`_on_shutdown` get scheduled onto, captured explicitly
        because `asyncio.get_running_loop()` would raise off the loop
        thread.
        """
        self.loop = loop
        self.engine_hello, page, topic, self._inbox = self._connect_client(self.client)
        self.state["page"], self.state["topic"] = page, topic
        self.status = BRIDGE_STATUS_CONNECTED
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self) -> None:
        """Blocking: tells the pump thread to give up (checked before every
        reconnect attempt and used to cut short an in-progress backoff
        sleep) and closes the currently-active engine connection. Call via
        `asyncio.to_thread` from async code (e.g. an aiohttp `on_cleanup`
        hook) for the same reason `start()` does.

        Unlike an ordinary engine-side EOF (Option B: reconnect, sinks stay
        open), a deliberate `stop()` means THIS bridge is going away for
        good -- schedules `_on_shutdown` (offers every sink the `None` EOF
        sentinel, same as the pre-reconnect behavior) so already-open
        websockets close cleanly instead of just going dark when the
        process exits."""
        self._stopping.set()
        self.client.close()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._on_shutdown)

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
                # Engine-side EOF -- try to reconnect (Option B) rather than
                # treating this pump thread's life as over. `_reconnect_loop`
                # itself checks `_stopping` first thing and returns False
                # immediately for a genuine `stop()`-driven EOF, so this
                # branch degrades to the old "read one EOF and exit" shape
                # exactly when the bridge is actually shutting down.
                if not self._reconnect_loop():
                    return
                continue  # resume reading from the NEW self._inbox
            self.loop.call_soon_threadsafe(self._on_message, msg)

    def _reconnect_loop(self) -> bool:
        """Blocking (runs ON the pump thread -- see module docstring for
        why that's fine here): retry connecting to the engine with bounded,
        jittered, doubling backoff until it succeeds or `stop()` is called.
        Returns True once reconnected (`self.client`/`self._inbox` updated
        in place, ready for `_pump_loop` to resume reading), False if
        `stop()` won the race (the pump thread should exit, matching the
        pre-reconnect EOF-then-exit shape).

        Structurally retry-forever (review fix): `_connect_client()` can
        fail two ways -- an ordinary `ClientError` (connection refused, a
        rejected hello, or -- since `clients/base.py`'s own fix -- a
        malformed/corrupt line from the peer, now folded into the same
        EOF/`ClientError` contract every other disconnect uses), or, in
        principle, something genuinely unanticipated. Both are caught here
        so NO exception raised while attempting to (re)connect can ever
        kill this loop -- the bare `except Exception` is deliberate
        belt-and-suspenders on top of the `ClientError`-specific fix, not
        a substitute for it (an unexpected exception class is logged at
        WARNING with a traceback, since silently swallowing an ACTUALLY
        novel bug class would be its own hazard -- this loop must never
        crash, but a crash-worthy bug elsewhere should still be visible
        somewhere)."""
        if self._stopping.is_set() or self.loop.is_closed():
            return False
        self.loop.call_soon_threadsafe(self._on_disconnected)
        delay = self._reconnect_base_delay
        while True:
            if self._stopping.is_set():
                return False
            # A brand-new EngineClient every attempt -- reusing the dead
            # one is a trap, see the module docstring. Constructed OUTSIDE
            # the try/except below (bare attribute assignment, cannot
            # itself raise ClientError or anything else worth guarding)
            # so both failure branches can unconditionally close it.
            client = EngineClient(self._socket_path)
            try:
                hello, page, topic, inbox = self._connect_client(client)
            except ClientError:
                client.close()  # release the partially-opened socket, no GC-timing reliance
            except Exception:  # noqa: BLE001 -- deliberate belt-and-suspenders, see docstring
                # above: this loop's contract is retry-forever, so no exception class may
                # ever escape it -- logged at WARNING (not silently swallowed) precisely
                # because a blind catch here trades "might mask a real bug" for "must never
                # freeze the bridge again," a trade worth making explicitly, not implicitly.
                client.close()
                _LOG.warning(
                    "Bridge reconnect attempt raised an unexpected exception (not "
                    "ClientError) -- treating it as a failed attempt and retrying, per "
                    "this loop's retry-forever contract", exc_info=True)
            else:
                self.client.close()  # the client this attempt is replacing -- explicit, no GC-timing reliance
                self.client = client
                self._inbox = inbox
                if not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self._on_reconnected, hello, page, topic)
                return True
            if self._stopping.wait(self._jittered_delay(delay)):
                return False  # stop() fired during the backoff sleep
            delay = min(delay * 2.0, self._reconnect_max_delay)

    def _jittered_delay(self, delay: float) -> float:
        """+/- `self._reconnect_jitter` fraction of `delay`, never negative
        -- avoids a fleet of bridges (or, more realistically here, repeated
        retry attempts) all waking on the exact same cadence. A jitter
        fraction of 0 (or less) disables jitter entirely, for tests that
        want deterministic delays."""
        if self._reconnect_jitter <= 0:
            return delay
        spread = delay * self._reconnect_jitter
        return max(0.0, delay + random.uniform(-spread, spread))

    # -- loop-thread handlers (everything below here runs ON the loop) ------
    def _on_disconnected(self) -> None:
        """Engine-side EOF detected -- clear stale state so a NEW websocket
        connecting mid-outage gets an honest empty hello (not a normal-
        looking one built from a captured-at-`start()`-time `engine_hello`
        that's now a lie), and tell already-open sinks we're reconnecting.
        Deliberately does NOT offer sinks the `None` EOF sentinel -- Option
        B (user ruling) keeps existing browser websockets open across the
        outage; they see a data gap, then fresh snapshots resume on the
        SAME socket once the engine comes back (see `_on_reconnected`)."""
        self.status = BRIDGE_STATUS_RECONNECTING
        self.engine_hello = {}
        self._latest = {}
        self._fan_out({"kind": "event", "name": "bridge_status",
                       "data": {"status": BRIDGE_STATUS_RECONNECTING}})

    def _on_reconnected(self, hello: dict, page: str, topic: str) -> None:
        """Engine connection restored. Re-derives current page/topic FRESH
        (via `_reconnect_loop`'s own `_connect_client` call) rather than
        assuming the pre-outage topic still matches -- see the module
        docstring for why. Emits `page_changed` only if the page actually
        changed, so `page.html`'s nav highlight stays honest without
        spuriously re-flagging a page that didn't move."""
        self.engine_hello = hello
        old_page = self.state["page"]
        self.state["page"], self.state["topic"] = page, topic
        self.status = BRIDGE_STATUS_CONNECTED
        self._fan_out({"kind": "event", "name": "bridge_status",
                       "data": {"status": BRIDGE_STATUS_CONNECTED}})
        if page != old_page:
            self._fan_out({"kind": "event", "name": "page_changed", "data": {"page": page}})

    def _on_shutdown(self) -> None:
        """This bridge is going away for good (a real `stop()`, not a
        transient reconnect) -- unlike `_on_disconnected` (Option B: sinks
        stay open across an engine-restart gap), tell every open sink to
        close, same EOF sentinel `app.py::_handle_ws` already knows how to
        act on (this is the pre-reconnect behavior, preserved for this one
        case: the process itself is exiting, so there is no "fresh
        snapshot" to eventually resume with)."""
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
            # Phase 9 Task 4: so a websocket that connects (or is already
            # open) during a mid-outage reconnect sees this honestly from
            # its very first frame -- see module docstring.
            "status": self.status,
        }

    # -- request/response proxies (blocking; call via asyncio.to_thread) -----
    def describe(self) -> dict:
        return self.client.request("describe")["data"]

    def action(self, name: str, args: dict | None = None) -> dict:
        return self.client.action(name, args or {})["data"]
