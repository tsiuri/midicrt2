"""Engine: owns MIDI event flow, pages, actions; publishes snapshots (spec §2).

Page roster
-----------
`Engine.pages` is an ordered dict (`name -> page instance`) built from
`config.pages` (default `["eventlog", "voices"]` as of phase-3 task 4),
filtered against `_PAGE_FACTORIES`
-- the module-level registry of known page constructors. Dict insertion
order is preserved and IS the cycle order used by the `page.next`/
`page.prev` actions. Later phase-3 tasks add real pages by adding an entry
to `_PAGE_FACTORIES`; tests (or, later, dynamically-arriving overlays) that
need a page with no production factory yet can call `Engine.register_page()`
to append one directly to the live roster.

Dirty tracking
--------------
Each page's `handle(ev)` returns `True` when the event changed that page's
state (falsy means "no-op for this page", e.g. a future page that filters
by channel). `Engine._handle` marks `page.<name>` dirty for every page that
reports True, not just the current page -- this was the phase-2 latent bug:
`_handle` used to mark only `page.<current_page>` dirty even though every
page consumed every event, so a client subscribed to a non-current page's
topic would never see it update. Chosen over an injected `mark_dirty()`
callback because it keeps pages pure functions of (state, event) -> bool,
with no back-reference to the engine.

Event topics
------------
`Engine.topics` is the single source of truth for "all subscribable
topics" -- `page.<name>` for each page roster entry AND `overlay.<name>`
for each analyzer (phase-3 task 3 is the first of the latter) -- `describe`
reports it verbatim so it can never drift from what `snapshot_now` can
actually resolve.

Analyzers (phase-3 task 3)
--------------------------
`Engine.analyzers` mirrors `Engine.pages`: an ordered dict (`name ->
analyzer instance`) built from `_ANALYZER_FACTORIES`, published under
`overlay.<name>` topics instead of `page.<name>`. Unlike pages, analyzers
are not config-gated (no `config.overlays` list exists yet) -- every
registered analyzer is always live, since there is currently exactly one
(`"status"` -> `TransportAnalyzer`) and it is meant to be visible
regardless of which page is current (a status bar, not a page). Analyzers
are wired BEFORE pages in `__init__`/`_handle` on the (currently latent,
future-proofing) assumption that a page's own view_model could one day
read an analyzer's derived state within the same event tick; today the two
sets are independent and order has no observable effect.
`Engine.register_analyzer()` mirrors `register_page()` for the same
test/dynamically-arriving-overlay reasons.
"""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from midicrt import proto
from midicrt.analyzers.transport import TransportAnalyzer
from midicrt.config import Config
from midicrt.engine.actions import ActionError, ActionRegistry
from midicrt.pages.eventlog import EventLogPage
from midicrt.pages.voices import VoicesPage


@dataclass
class MidiEvent:
    ts: float
    source: str
    type: str
    channel: int | None
    data1: int | None
    data2: int | None
    summary: str
    # `type == "clock_tick"` only: timestamp of the PREVIOUS 24-raw-clock
    # aggregation boundary (see engine/midi_in.py's module docstring for why
    # clock is batched instead of queued per-pulse). `ts - clock_batch_start`
    # spans exactly 24 clock pulses (one quarter note), letting
    # analyzers/transport.py derive bpm without any smoothing of its own.
    # None for every other event type, and for the first batch after a
    # start/stop/continue reset (no prior boundary exists yet).
    clock_batch_start: float | None = None


class Page(Protocol):
    def handle(self, ev: MidiEvent) -> bool: ...
    def view_model(self) -> dict: ...


class Analyzer(Protocol):
    """Same shape as `Page` (see analyzers/__init__.py) -- kept as a
    separate Protocol rather than reusing `Page` so the two roster kinds
    stay independently evolvable even though today they're structurally
    identical."""

    def handle(self, ev: MidiEvent) -> bool: ...
    def view_model(self) -> dict: ...


PageFactory = Callable[[Config], Page]
AnalyzerFactory = Callable[[Config], Analyzer]

# Known production pages, keyed by the name used in config.pages / topics.
# Add an entry here as each phase-3 parity page lands.
_PAGE_FACTORIES: dict[str, PageFactory] = {
    "eventlog": lambda config: EventLogPage(capacity=config.eventlog_capacity),
    # Phase-3 task 4: v1's main screen, the first second page -- see
    # pages/voices.py + analyzers/voices.py. `config.pages` now defaults to
    # ["eventlog", "voices"] (config.py) so it's live without a config.toml.
    "voices": lambda config: VoicesPage(instruments=config.instruments),
}

# Known production analyzers, keyed by the name used in the `overlay.<name>`
# topic. Unlike pages, not config-gated -- see the module docstring.
_ANALYZER_FACTORIES: dict[str, AnalyzerFactory] = {
    "status": lambda config: TransportAnalyzer(),
}


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.actions = ActionRegistry()
        self.analyzers: dict[str, Analyzer] = {
            name: factory(config) for name, factory in _ANALYZER_FACTORIES.items()
        }
        self.pages: dict[str, Page] = {
            name: _PAGE_FACTORIES[name](config)
            for name in config.pages
            if name in _PAGE_FACTORIES
        }
        self.current_page = next(iter(self.pages), "eventlog")
        self.events_total = 0
        self.started_at = time.monotonic()
        self._listeners: list[Callable[[dict], None]] = []
        self._seq: dict[str, int] = {}
        self._dirty: set[str] = set()
        self._running = False
        self.actions.register("eventlog.clear", self._clear_eventlog,
                              description="Clear the event log")
        self.actions.register("page.next", self._page_next,
                              description="Advance to the next page in the roster")
        self.actions.register("page.prev", self._page_prev,
                              description="Go back to the previous page in the roster")
        self.actions.register("page.goto", self._page_goto,
                              description="Jump to a named page", args={"name": "str"})

    def _clear_eventlog(self):
        self.pages["eventlog"].clear()
        self._dirty.add("page.eventlog")

    def register_page(self, name: str, page: Page) -> None:
        """Append `page` to the live roster under `name`. Production pages
        register via `_PAGE_FACTORIES` + `config.pages`; this hook exists
        for pages with no factory yet (tests today; dynamically-arriving
        overlays later)."""
        self.pages[name] = page

    def register_analyzer(self, name: str, analyzer: Analyzer) -> None:
        """Append `analyzer` to the live roster under `name`, publishing it
        under `overlay.<name>`. Mirrors `register_page()` for the same
        no-production-factory-yet reasons (tests today)."""
        self.analyzers[name] = analyzer

    def _page_order(self) -> list[str]:
        return list(self.pages)

    def _set_current_page(self, name: str) -> dict:
        self.current_page = name
        self.emit_event("page_changed", {"page": name})
        return {"page": name}

    def _page_next(self) -> dict:
        order = self._page_order()
        idx = order.index(self.current_page)
        return self._set_current_page(order[(idx + 1) % len(order)])

    def _page_prev(self) -> dict:
        order = self._page_order()
        idx = order.index(self.current_page)
        return self._set_current_page(order[(idx - 1) % len(order)])

    def _page_goto(self, name: str) -> dict:
        if name not in self.pages:
            raise ActionError(f"unknown page: {name}")
        return self._set_current_page(name)

    @property
    def topics(self) -> list[str]:
        """All subscribable topics, roster order: pages first, then overlays."""
        return [f"page.{name}" for name in self.pages] + \
            [f"overlay.{name}" for name in self.analyzers]

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _broadcast(self, msg: dict) -> None:
        for cb in list(self._listeners):
            cb(msg)

    def emit_event(self, name: str, data: dict) -> None:
        self._broadcast(proto.event(name, data))

    def snapshot_now(self, topic: str) -> dict | None:
        if topic.startswith("page."):
            obj = self.pages.get(topic.removeprefix("page."))
        elif topic.startswith("overlay."):
            obj = self.analyzers.get(topic.removeprefix("overlay."))
        else:
            obj = None
        if obj is None:
            return None
        seq = self._seq.get(topic, 0) + 1
        self._seq[topic] = seq
        return proto.snapshot(topic, seq, obj.view_model())

    def status(self) -> dict:
        return {
            "page": self.current_page,
            "events_total": self.events_total,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "engine_version": __import__("midicrt").__version__,
            "proto_version": proto.PROTO_VERSION,
        }

    def _handle(self, ev: MidiEvent) -> None:
        self.events_total += 1
        for name, analyzer in self.analyzers.items():
            if analyzer.handle(ev):
                self._dirty.add(f"overlay.{name}")
        for name, page in self.pages.items():
            if page.handle(ev):
                self._dirty.add(f"page.{name}")

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        tick = 1.0 / max(self.config.tick_hz, 1.0)
        while self._running:
            try:
                ev = await asyncio.wait_for(self.queue.get(), timeout=tick)
                self._handle(ev)
                while not self.queue.empty():          # coalesce a burst
                    self._handle(self.queue.get_nowait())
            except TimeoutError:
                pass
            for topic in sorted(self._dirty):
                snap = self.snapshot_now(topic)
                if snap:
                    self._broadcast(snap)
            self._dirty.clear()
