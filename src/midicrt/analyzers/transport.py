"""TransportAnalyzer: BAR/BEAT/BPM/running/clock-source, ported from v1's
`~/codex/midicrt/plugins/timeclock.py` (display) and the transport engine
it reads from, `~/codex/midicrt/engine/state/tempo_map.py` (the actual BPM
math -- `plugins/beat_counter.py`, the other file the task brief names, is
a dead no-op stub in v1, kept only so plugin load order doesn't shift; it
contributes nothing to read).

Clock granularity (read this before touching the bpm math)
------------------------------------------------------------
v1's `TempoMap` recomputes bpm on EVERY raw MIDI clock pulse (24 ppqn) from
a rolling window of the last `interval_window` (default 24) inter-pulse
gaps: `bpm = 60 / (24 * avg_interval)`. v2 cannot do that -- `midi_in.py`
deliberately does not queue individual clock pulses (50-100 msgs/sec would
flood the engine queue and spam every page's `handle()`, this analyzer's
included); instead it aggregates 24 pulses into one `clock_tick` MidiEvent
per beat (see midi_in.py's module docstring for the full rationale).

This analyzer adapts v1's math to that coarser granularity rather than
pretending it still has per-pulse data. Each `clock_tick` event carries
`clock_batch_start`: the timestamp of the PREVIOUS 24-pulse boundary (i.e.
the prior `clock_tick`'s own `ts`). The span `ts - clock_batch_start` is
EXACTLY 24 raw clock intervals -- one quarter note, unbiased -- so:

    bpm = 60.0 / (ts - clock_batch_start)

is an exact instantaneous tempo estimate, not an approximation, PROVIDED
`clock_batch_start` is available (it is None for the first batch after a
start/continue/reset, since no prior boundary exists yet -- bpm reports
None for that one beat rather than guessing). This is deliberately NOT a
rolling average like v1's: at one sample per beat instead of one per
pulse, a multi-sample smoothing window would need several beats to
respond, which reads as laggy on a CRT status line; v1's own window
(interval_window=24 pulses = 1 beat) is, not coincidentally, the same time
span as one of our batches, so an unsmoothed per-batch estimate is the
closest honest match to v1's default responsiveness rather than a
regression.

BAR/BEAT arithmetic
--------------------
v1 shows BAR/BEAT/TICK (`plugins/timeclock.py`); the overlay VM this
analyzer publishes drops TICK -- with only beat-boundary events available,
there IS no sub-beat tick to show, and the task-3 brief's VM contract
(`{"bpm", "bar", "beat", "running", "source"}`) confirms TICK is out of
scope here. BAR/BEAT keep v1's convention: BAR is 0-indexed ("BAR 0000" at
the first bar), BEAT is 1-indexed within a hardcoded 4/4 bar ("BEAT 01"..
"BEAT 04") -- time-signature detection (v1's `plugins/ztimesig.py`) is a
separate future port per docs/phase3-notes.md's v1 source map, not part of
this task, so BEATS_PER_BAR is a constant here, not detected.

Transport gating, mirroring v1's `TempoMap.handle()` exactly
---------------------------------------------------------------
- "start": bar/beat/bpm reset (beats-elapsed -> 0, bpm -> None), running
  -> True. Always reported dirty (a restart is a real content change even
  if `running` was already True).
- "continue": running -> True, bar/beat/bpm UNCHANGED (v1 doesn't reset on
  continue, only on start). No-op (not dirty) if already running.
- "stop": running -> False, bar/beat/bpm UNCHANGED (v1 keeps showing the
  last transport position while stopped). No-op if already stopped.
- "clock_tick" while NOT running: ignored entirely, like v1's
  `if kind == "clock": if not self.running: return` -- a free-running
  clock source with no transport start must not advance bar/beat or
  fabricate a bpm.
- "songpos": updates `source` (it's a clock-master message like the
  others) but no other field -- v1's TempoMap doesn't decode songpos into
  a bar/beat position either (it falls through to the meter-candidate
  no-op branch), so there is no v1 behavior to port for it.
- anything else (note_on, control_change, ...): pure no-op, not dirty.

`source` tracking is gated PER BRANCH, not updated unconditionally up
front: it only changes as a side effect of an event that has some real
effect (a "stop" while already stopped, or a "clock_tick" while not
running, is a TRUE no-op -- nothing changes, including `source`, exactly
mirroring v1's `return` with no side effects at all), except "songpos",
whose only possible effect IS the source update (it carries no other
transport data), so a differing source there alone makes it dirty.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: `engine.core` imports THIS module (to build
    # `_ANALYZER_FACTORIES`), so a runtime import here would be circular.
    # Analyzers only need MidiEvent's attributes (duck-typed), never
    # construct or isinstance-check it, so deferring to type-checking time
    # (`from __future__ import annotations` above keeps the `ev: MidiEvent`
    # annotation itself unevaluated at runtime) costs nothing.
    from midicrt.engine.core import MidiEvent

BEATS_PER_BAR = 4

_SOURCE_TRACKED_TYPES = {"clock_tick", "start", "stop", "continue", "songpos"}


class TransportAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `view_model()
    -> dict`. No I/O, no clock reads -- see module docstring."""

    def __init__(self) -> None:
        self._bpm: float | None = None
        self._beats: int = 0          # clock_tick count since the last "start"
        self._running: bool = False
        self._source: str | None = None

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type not in _SOURCE_TRACKED_TYPES:
            return False
        if ev.type == "clock_tick":
            return self._advance_beat(ev)
        if ev.type == "start":
            self._beats = 0
            self._bpm = None
            self._running = True
            self._source = ev.source
            return True
        if ev.type == "continue":
            was_running = self._running
            self._running = True
            if was_running:
                return False
            self._source = ev.source
            return True
        if ev.type == "stop":
            was_running = self._running
            self._running = False
            if not was_running:
                return False
            self._source = ev.source
            return True
        # "songpos": no transport data to apply (see module docstring) --
        # dirty only if it actually tells us something new about the source.
        if ev.source == self._source:
            return False
        self._source = ev.source
        return True

    def _advance_beat(self, ev: MidiEvent) -> bool:
        if not self._running:
            return False   # true no-op, mirrors v1's TempoMap "not running -> return"
        self._beats += 1
        self._source = ev.source
        start_ts = ev.clock_batch_start
        self._bpm = 60.0 / (ev.ts - start_ts) if start_ts is not None else None
        return True

    @property
    def bar(self) -> int:
        return self._beats // BEATS_PER_BAR

    @property
    def beat(self) -> int:
        return (self._beats % BEATS_PER_BAR) + 1

    def view_model(self) -> dict:
        return {
            "bpm": self._bpm,
            "bar": self.bar,
            "beat": self.beat,
            "running": self._running,
            "source": self._source,
        }
