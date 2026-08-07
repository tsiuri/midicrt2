"""Engine: owns MIDI event flow, pages, actions; publishes snapshots (spec §2).

Page roster
-----------
`Engine.pages` is an ordered dict (`name -> page instance`) built from
`config.pages` (default `["eventlog"]`), filtered against `_PAGE_FACTORIES`
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
topics" (currently `page.<name>` for each roster entry) -- `describe`
reports it verbatim so it can never drift from what `snapshot_now` can
actually resolve. Future phases append non-page topics (overlays) here as
those land.
"""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from midicrt import proto
from midicrt.config import Config
from midicrt.engine.actions import ActionError, ActionRegistry
from midicrt.pages.eventlog import EventLogPage


@dataclass
class MidiEvent:
    ts: float
    source: str
    type: str
    channel: int | None
    data1: int | None
    data2: int | None
    summary: str


class Page(Protocol):
    def handle(self, ev: MidiEvent) -> bool: ...
    def view_model(self) -> dict: ...


PageFactory = Callable[[Config], Page]

# Known production pages, keyed by the name used in config.pages / topics.
# Add an entry here as each phase-3 parity page lands.
_PAGE_FACTORIES: dict[str, PageFactory] = {
    "eventlog": lambda config: EventLogPage(capacity=config.eventlog_capacity),
}


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.actions = ActionRegistry()
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
        """All subscribable topics, roster order."""
        return [f"page.{name}" for name in self.pages]

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _broadcast(self, msg: dict) -> None:
        for cb in list(self._listeners):
            cb(msg)

    def emit_event(self, name: str, data: dict) -> None:
        self._broadcast(proto.event(name, data))

    def snapshot_now(self, topic: str) -> dict | None:
        page = self.pages.get(topic.removeprefix("page."))
        if page is None:
            return None
        seq = self._seq.get(topic, 0) + 1
        self._seq[topic] = seq
        return proto.snapshot(topic, seq, page.view_model())

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
