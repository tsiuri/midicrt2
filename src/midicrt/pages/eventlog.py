"""Event-log page: ring buffer of recent MIDI events -> text-run view-model."""
from collections import deque

# `clock_tick` (engine/midi_in.py's aggregated 24-pulse MIDI clock event,
# phase-3 task 3) is transport timing for the status overlay, not a
# user-meaningful MIDI event -- the eventlog page must not show it (even at
# one per beat, ~1-2/sec, it would drown real note/CC traffic and defeats
# the whole point of aggregating clock in the first place).
_SUPPRESSED_TYPES = {"clock_tick"}


class EventLogPage:
    name = "eventlog"

    def __init__(self, capacity: int = 200):
        self._lines = deque(maxlen=capacity)
        self._count = 0

    def handle(self, ev) -> bool:
        if ev.type in _SUPPRESSED_TYPES:
            return False
        style = "accent" if ev.type == "note_on" else "normal"
        self._lines.append({"text": ev.summary, "style": style})
        self._count += 1
        return True

    def clear(self) -> None:
        self._lines.clear()
        self._count = 0

    def view_model(self) -> dict:
        return {"title": "EVENT LOG", "count": self._count, "lines": list(self._lines)}

    # -- page-declared actions (Phase 4 Task 0, docs/phase4-notes.md) -------
    #
    # `eventlog.clear` used to be registered directly, unconditionally, in
    # Engine.__init__ (`self.actions.register("eventlog.clear",
    # self._clear_eventlog, ...)`) even though it is entirely page-scoped
    # state -- one of the "Engine-owned actions that actually belong to
    # their page" the task-0 brief calls out by name. `Engine.__init__` now
    # discovers this via `page.actions()` (one generic loop over the whole
    # roster, see that method's own docstring) instead of a hand-written
    # registration line; the roster itself is the only guard now (a custom
    # config without "eventlog" simply never gets asked for its actions at
    # all -- a stricter, safer default than the old code's unconditional
    # registration, which would have advertised this action even against a
    # roster missing "eventlog" and failed with an uncaught KeyError at
    # dispatch time instead of a clean "unknown action").

    def actions(self) -> list[tuple[str, object, str, dict[str, str]]]:
        return [("eventlog.clear", self.clear, "Clear the event log", {})]
