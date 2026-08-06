"""Engine: owns MIDI event flow, pages, actions; publishes snapshots (spec §2)."""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from midicrt import proto
from midicrt.config import Config
from midicrt.engine.actions import ActionRegistry
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


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.actions = ActionRegistry()
        self.pages = {"eventlog": EventLogPage(capacity=config.eventlog_capacity)}
        self.current_page = "eventlog"
        self.events_total = 0
        self.started_at = time.monotonic()
        self._listeners: list[Callable[[dict], None]] = []
        self._seq: dict[str, int] = {}
        self._dirty: set[str] = set()
        self._running = False
        self.actions.register("eventlog.clear", self._clear_eventlog,
                              description="Clear the event log")

    def _clear_eventlog(self):
        self.pages["eventlog"].clear()
        self._dirty.add("page.eventlog")

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
        for page in self.pages.values():
            page.handle(ev)
        self._dirty.add(f"page.{self.current_page}")

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
