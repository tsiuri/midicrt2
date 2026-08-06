"""Event-log page: ring buffer of recent MIDI events -> text-run view-model."""
from collections import deque


class EventLogPage:
    name = "eventlog"

    def __init__(self, capacity: int = 200):
        self._lines = deque(maxlen=capacity)
        self._count = 0

    def handle(self, ev) -> None:
        style = "accent" if ev.type == "note_on" else "normal"
        self._lines.append({"text": ev.summary, "style": style})
        self._count += 1

    def clear(self) -> None:
        self._lines.clear()
        self._count = 0

    def view_model(self) -> dict:
        return {"title": "EVENT LOG", "count": self._count, "lines": list(self._lines)}
