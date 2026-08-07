"""StuckNotesAnalyzer: long-held/orphaned-note detection, ported from v1's
`~/codex/midicrt/plugins/zstucknotes.py` (READ-ONLY reference on the Pi).

v1's layout (read before touching this file)
---------------------------------------------------------------------------
zstucknotes.py is a v1 "plugin" with no `PAGE_ID` at all: its own `draw()`
paints a warning line directly onto the bottom of the screen regardless of
which page is currently selected (`midicrt.draw_line(midicrt.SCREEN_ROWS -
Y_POS_OFFSET, text)`) -- the same unconditional-every-page treatment as
`plugins/timeclock.py`, the v1 source `analyzers/transport.py` already
ported into v2's `overlay.status` chrome (see that module's docstring).
This analyzer follows the same precedent: it is registered as a second
`overlay.<name>` analyzer (`overlay.alerts`), rendered by both clients as
an always-present (usually blank) chrome row, not a page -- see
`clients/chrome.py`'s `alerts_text()`.

(v1 ALSO has a separate, secondary page, `pages/stuckheat.py`, PAGE_ID=12
"Stuck Heatmap", showing lifetime pitch-class/note histograms sourced from
`zstucknotes.get_stuck_stats()`. That is a historical-stats feature, not
part of the live-detection VM contract this task specifies
(`{"ch", "note", "held_s"}` per the task-6 brief) -- not ported here,
disclosed as a possible future page, not lost logic.)

Time-based escalation needs a wall-clock tick, not just events
---------------------------------------------------------------------------
Every other v2 analyzer to date derives all timing from `MidiEvent.ts`
alone (see analyzers/transport.py's/harmony.py's "no clock reads" rule,
enforced by tests/test_analyzers_transport.py's
`test_handle_never_reads_a_clock_or_does_io`). That works because those
analyzers only ever need "how much event-time passed BETWEEN two received
events" -- but v1's stuck-note escalation (WARN_AFTER=2s/CRIT_AFTER=10s of
a note staying on) can cross a threshold with NO new MIDI event at all
(the classic case: a note gets stuck and nothing else happens for ten
seconds). v1's own `draw()` polls `time.time()` every UI frame specifically
to catch this. A pure event-driven `handle()` alone cannot: the analyzer
needs to be told "time has passed" from OUTSIDE (the engine, which is
allowed to touch the wall clock) -- hence the added, OPTIONAL `tick(now:
float) -> bool` method, called by `engine/core.py`'s run loop once per
`tick_hz` period (see that module's `_tick_analyzers`, added by this task).
This keeps the "analyzers never read the clock themselves" rule intact:
`now` is always INJECTED here, never read internally -- there is no
`import time` anywhere in this file.

`drain_alerts()`: the second half of the injected-clock story
---------------------------------------------------------------------------
The task brief also wants an engine `alert` EVENT (T1's `emit_event` path)
fired on each NEW escalation (a note crossing none->warn or warn->crit),
distinct from the ordinary dirty-snapshot path (`overlay.alerts`'s
view_model, polled/pushed on every change including a live-updating
`held_s`). Since analyzers must stay "no I/O" (phase3-notes.md), this
analyzer cannot call `engine.emit_event()` itself -- instead `tick()`
appends each new escalation to an internal queue, and the engine drains it
via `drain_alerts() -> list[dict]` (called right after `tick()`,
independent of the dirty/view_model path) and turns each drained dict into
its own `emit_event("alert", ...)` call. This mirrors the `tick()` hook's
own "engine does I/O, analyzer stays pure" split.

Ported thresholds/semantics (v1 defaults; v2 has no config.toml section
for these yet -- same "hardcode v1's defaults" precedent as
analyzers/harmony.py's RECENT_NOTE_COUNT etc.)
---------------------------------------------------------------------------
- WARN_AFTER=2.0s / CRIT_AFTER=10.0s of continuous hold -> "warn"/"crit".
- A note-ON retrigger while already sounding (an overlapping note-on for
  the same (channel, pitch), no intervening note-off) resets the age
  clock to 0 and increments an overlap COUNT, matching v1's `_note_on`
  (`entry["count"] += 1; entry["last_on"] = now`) -- symmetric with
  `analyzers/voices.py`'s own overlap-count semantics for the same v1
  quirk (an arpeggiator, or a synth that legally overlaps two voices on
  one pitch).
- A stray note-off for a (channel, pitch) never seen is a true no-op,
  mirroring v1's `if not entry: return`.
- CC64 (sustain) SUSPENDS escalation while held per-channel
  (SUSPEND_WHEN_SUSTAIN=True, v1's default): a note already "warn"/"crit"
  is force-reported "none" (dropped from the alert list) the instant
  sustain goes down, and v1's own loop `continue`s immediately in that
  branch WITHOUT even computing the age that frame -- ported verbatim
  (see `tick()`'s control flow, which mirrors v1's `draw()` loop
  statement-for-statement).
- CC120 (All Sound Off) / CC123 (All Notes Off) clears every tracked note
  on that channel, matching v1's `_clear_channel`.
- CC121 (Reset All Controllers) clears sustain, matching v1's
  `elif msg.control == 121: _sustain[ch] = False`.

Not ported (disclosed, not silently dropped)
---------------------------------------------------------------------------
- **PANIC_ON_CRIT** (v1 auto-sends an All-Notes-Off CC back out a MIDI
  port when a note goes critical) -- an ACTUATION side effect (real MIDI
  output), not analyzer state; phase3-notes.md's "Analyzers = pure state
  machines... no I/O" rules this out categorically, the same class of
  omission as `analyzers/voices.py`'s dropped poly-limit warnings.
- **HOLD_AFTER** (v1 keeps showing "STUCK CLEARED: ..." text for 15s
  after the last stuck note releases) -- a text-retention/history nicety
  for the RENDERED message, not the detector's own state. The task's VM
  contract is a plain "list of currently stuck notes"
  (`{"ch", "note", "held_s"}`) with no "recently cleared" field to carry
  that history into, so a renderer sees the alert list go empty
  immediately on release rather than lingering, unlike v1.
- **v1's octave-letter note naming** (`_fmt_note`, e.g. "C4(060)", which
  v1's own comment says is an intentionally off-by-2-octave convention
  "to match polydisplay's octave shift") -- not reproduced; this
  analyzer's VM reports raw MIDI note numbers only, matching
  `analyzers/voices.py`'s own `notes` field convention. A renderer
  wanting note names can already reuse `analyzers.theory.NOTE_NAMES`
  (pitch-class only, no octave) the way `pages/harmony.py` does.

Live `held_s` readout (disclosed design choice)
---------------------------------------------------------------------------
`tick()` marks the overlay dirty on EVERY call while at least one note is
actively alerting, not just on a level transition, so `held_s` climbs
smoothly in a live view the same way v1's `draw()` (called every UI frame)
visibly does. This is more chatty than other v2 overlays' dirty-marking
(which fires only on real level/membership changes), but the existing
`ProtocolServer._push_loop` "latest wins" coalescing per topic already
absorbs the extra churn without flooding a client -- disclosed here so a
future reader doesn't mistake it for an oversight.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's own comment -- avoids a
    # circular import with engine.core, which builds _ANALYZER_FACTORIES
    # from modules like this one.
    from midicrt.engine.core import MidiEvent

WARN_AFTER = 2.0
CRIT_AFTER = 10.0
SUSPEND_WHEN_SUSTAIN = True

N_CHANNELS = 16
_ALL_NOTES_OFF_CONTROLS = {120, 123}


class StuckNotesAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `tick(now)
    -> bool` (dirty; wall-progress hook, see module docstring),
    `view_model() -> dict`, `drain_alerts() -> list[dict]` (new-escalation
    queue for the engine's `alert` event path). No I/O -- `tick`'s `now`
    is always injected by the caller, never read here.
    """

    def __init__(self) -> None:
        # (channel 1-based, note) -> {"count": int, "last_on": float}
        self._active: dict[tuple[int, int], dict] = {}
        self._levels: dict[tuple[int, int], str] = {}
        self._ages: dict[tuple[int, int], float] = {}
        self._sustain: dict[int, bool] = dict.fromkeys(range(1, N_CHANNELS + 1), False)
        self._pending_alerts: list[dict] = []

    # -- event handling -----------------------------------------------------

    def handle(self, ev: MidiEvent) -> bool:
        if ev.channel is None:
            return False
        ch = ev.channel + 1
        if ev.type == "note_on":
            if ev.data2 == 0:
                return self._note_off(ch, ev.data1)
            return self._note_on(ch, ev.data1, ev.ts)
        if ev.type == "note_off":
            return self._note_off(ch, ev.data1)
        if ev.type == "control_change":
            if ev.data1 == 64:
                return self._set_sustain(ch, ev.data2 >= 64)
            if ev.data1 in _ALL_NOTES_OFF_CONTROLS:
                return self._clear_channel(ch)
            if ev.data1 == 121:
                return self._set_sustain(ch, False)
        return False

    def _note_on(self, ch: int, note: int, ts: float) -> bool:
        key = (ch, note)
        entry = self._active.get(key)
        if entry:
            entry["count"] += 1
            entry["last_on"] = ts   # retrigger resets the age clock, matches v1
        else:
            self._active[key] = {"count": 1, "last_on": ts}
        return True

    def _note_off(self, ch: int, note: int) -> bool:
        key = (ch, note)
        entry = self._active.get(key)
        if not entry:
            return False   # stray/already-off note-off -- true no-op, mirrors v1
        entry["count"] -= 1
        if entry["count"] <= 0:
            del self._active[key]
            self._levels.pop(key, None)
            self._ages.pop(key, None)
        return True

    def _set_sustain(self, ch: int, down: bool) -> bool:
        if self._sustain[ch] == down:
            return False
        self._sustain[ch] = down
        return True

    def _clear_channel(self, ch: int) -> bool:
        keys = [k for k in self._active if k[0] == ch]
        if not keys:
            return False   # already clear -- true no-op
        for key in keys:
            del self._active[key]
            self._levels.pop(key, None)
            self._ages.pop(key, None)
        return True

    # -- wall-progress hook (see module docstring) ---------------------------

    def tick(self, now: float) -> bool:
        dirty = False
        for key, entry in list(self._active.items()):
            ch, _note = key
            if SUSPEND_WHEN_SUSTAIN and self._sustain.get(ch, False):
                # v1's draw() loop: force "none" and `continue` immediately,
                # without even computing age this frame -- ported verbatim.
                prev = self._levels.pop(key, "none")
                self._ages.pop(key, None)
                if prev != "none":
                    dirty = True
                continue
            age = now - entry["last_on"]
            level = "crit" if age >= CRIT_AFTER else ("warn" if age >= WARN_AFTER else "none")
            self._ages[key] = age
            prev = self._levels.get(key, "none")
            if level == prev:
                if level != "none":
                    dirty = True   # keep held_s live while alerting, see docstring
                continue
            dirty = True
            if level == "none":
                self._levels.pop(key, None)
            else:
                self._levels[key] = level
                # A "new detection" per the task brief: every transition
                # INTO warn or crit (none->warn, warn->crit) gets its own
                # engine `alert` event -- mirrors v1's own `_log(...)` call,
                # which fires on EVERY level change to "warn"/"crit", not
                # just the first (v1's separate `_stuck_counts` bookkeeping
                # is the only thing gated to "first time only" -- that is a
                # lifetime-histogram feature this task doesn't port, see
                # module docstring).
                self._pending_alerts.append({
                    "ch": ch, "note": key[1], "level": level,
                    "held_s": round(age, 1),
                })
        return dirty

    def drain_alerts(self) -> list[dict]:
        drained, self._pending_alerts = self._pending_alerts, []
        return drained

    # -- view model -----------------------------------------------------------

    def view_model(self) -> dict:
        alerts = [
            {"ch": ch, "note": note, "level": level, "held_s": round(self._ages.get((ch, note), 0.0), 1)}
            for (ch, note), level in self._levels.items()
        ]
        alerts.sort(key=lambda a: a["held_s"], reverse=True)   # worst (oldest) first, matches v1
        return {"alerts": alerts}
