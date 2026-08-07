"""Program Changes page (page name "progchanges"): a rolling log of
program-change events, ported from v1's `~/codex/midicrt/pages/proglog.py`
(PAGE_ID 7, "Program Changes", 132 lines, READ-ONLY reference).

v1 comparison
---------------------------------------------------------------------------
v1's `handle(msg)` appends one formatted line per `program_change` message
to a `deque(maxlen=300)` (`MAX_LOG`): `f"[{ts}]  Ch{msg.channel + 1:02d} ->
Program {msg.program:03d}"` (`ts = time.strftime("%H:%M:%S")`, wall-clock at
RECEIPT time). `draw()` then does real scrolling (`scroll_up`/`scroll_down`/
`page_up`/`page_down`/`scroll_home`/`scroll_end`, an `scroll_offset` int) --
v1's own keybindings section documents these as Up/Down/PgUp/PgDn/Home/End
on PAGE 6 (Event Log), not page 7 itself, and grepping `midicrt.py`'s
keypress dispatch confirms `scroll_offset`/the scroll functions are never
actually wired to any key for page 7 in the shipped build -- proglog.py's
own scroll API is dead code in practice (`scroll_offset` stays 0 forever),
so `draw()` always shows the tail of the log. This page ports that OBSERVED
behavior (a scrolling TAIL, exactly `pages/eventlog.py`'s own shape) rather
than the unreachable scroll-offset machinery.

Why this reuses `pages/eventlog.py`'s exact `{title, count, lines}` VM shape
---------------------------------------------------------------------------
v1's proglog.py IS, structurally, a second event log -- a rolling buffer of
formatted text lines, tailed to whatever fits the screen -- just filtered to
one MIDI event type with its own line format instead of every event type
with `midi_in.py::translate()`'s generic `summary`. Reusing eventlog's own
`{"title", "count", "lines": [{"text", "style"}]}` contract (rather than
inventing a new one) lets both fb/tui renderers reuse the SAME `_tail()`
slicing convention `render_frame`/`render_lines` already use for eventlog,
keeping "what's visible" consistent across pages the same way it already is
across clients. Every line uses `"style": "normal"` -- v1 has no per-line
color distinction on this page (unlike eventlog's note_on "accent" lines).

Program numbers already flow through the pipeline (task 10 finding)
---------------------------------------------------------------------------
`engine/midi_in.py::translate()` only started populating `data1` with the
program number for `program_change` events in phase-3 task 10 (needed then
for `analyzers/img2txtviz.py`'s charset-offset feature) -- before that fix,
`ev.data1` was always `None` for this event type and no analyzer/page could
have recovered the program number at all. This page is the first to
consume that field for its own primary purpose.

Timestamp formatting is a pure function of `ev.ts`, not a live clock read
---------------------------------------------------------------------------
v1 formats the RECEIPT-time wall clock (`time.strftime("%H:%M:%S")`, called
fresh inside `handle()`). `MidiEvent.ts` is stamped by `engine/midi_in.py`
from `time.time()` at the moment the raw MIDI message arrived -- the SAME
wall-clock domain v1's own `time.strftime()` call would have read at
essentially the same instant. Formatting `time.localtime(ev.ts)` here is
therefore a pure, deterministic function of the EVENT's own already-injected
timestamp (not a fresh clock read inside this module), matching every other
v2 module's "no internal clock reads" discipline while still reproducing
v1's real observed wall-clock display.

Defensive `channel`/`data1` guards (not a v1 behavior -- see other pages)
---------------------------------------------------------------------------
v1 never guards against a malformed `program_change` (mido always supplies
both `msg.channel` and `msg.program` for this message type in practice).
This page still guards `ev.channel is None`/`ev.data1 is None` as a
defensive no-op, matching the same malformed-event convention every other
v2 analyzer/page here already follows (e.g. `analyzers/voices.py`'s own
`channel is None` guard) -- not a v1-observed code path, disclosed here so
a future reader does not mistake it for a ported behavior.
"""
import time
from collections import deque

MAX_LOG = 300   # matches v1's proglog.py MAX_LOG


class ProgChangesPage:
    name = "progchanges"

    def __init__(self, capacity: int = MAX_LOG):
        self._lines = deque(maxlen=capacity)
        self._count = 0

    def handle(self, ev) -> bool:
        if ev.type != "program_change":
            return False
        if ev.channel is None or ev.data1 is None:
            return False   # defensive only -- see module docstring
        ts_text = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        text = f"[{ts_text}]  Ch{ev.channel + 1:02d} → Program {ev.data1:03d}"
        self._lines.append({"text": text, "style": "normal"})
        self._count += 1
        return True

    def view_model(self) -> dict:
        return {"title": "PROGRAM CHANGES", "count": self._count, "lines": list(self._lines)}
