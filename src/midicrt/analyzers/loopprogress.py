"""LoopProgressAnalyzer: an 8-bar cyclic position marker, ported from v1's
`~/codex/midicrt/plugins/loopprogress.py` (READ-ONLY reference on the Pi).

v1's actual behavior (read before touching the math)
---------------------------------------------------------------------------
v1's `draw(state)` reads `state["tick"]` (a raw, 24-ppqn, monotonically
increasing-since-start clock counter) and `state["running"]`, computes
`TICKS_PER_BAR = 24*4 = 96`, `TOTAL_TICKS = TOTAL_BARS(8) * 96 = 768`, then
`frac = (tick % TOTAL_TICKS) / TOTAL_TICKS` and `pos = int(frac *
BAR_WIDTH(8))`. It draws an 8-cell bracketed bar `[        ]` with a single
`*` at `pos` -- but ONLY while `running` (the `bar_chars[pos] = "*"`
assignment sits inside `if running:`; while stopped the bar renders fully
blank, even though `frac`/`pos` themselves are still computed from
whatever `tick` currently holds).

Granularity adaptation (same problem `analyzers/transport.py` already
solved for bpm)
---------------------------------------------------------------------------
v2's `engine/midi_in.py` does not expose raw 24-ppqn ticks to analyzers at
all -- clock pulses arrive pre-aggregated as one `clock_tick` MidiEvent per
beat (see that module's docstring). This analyzer counts BEATS instead of
raw ticks: `TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR` (8*4 = 32, the exact
same real-world span as v1's 768 raw ticks -- 768/24 = 32 quarter notes),
and `fraction = (beats_since_start % TOTAL_BEATS) / TOTAL_BEATS` replaces
v1's `frac`. `BEATS_PER_BAR` is IMPORTED from `analyzers.transport`, not
redeclared, matching `analyzers/beatflash.py`'s own precedent for the same
constant.

VM shape: `fraction` instead of a pre-rendered bar string
---------------------------------------------------------------------------
v1 renders the `[***    ]`-style bar itself, inline in `draw()`. This
analyzer instead reports a plain `fraction` (0.0-1.0) plus `running`, and
leaves the actual bracket/asterisk text-building to `clients/chrome.py`'s
`loopprogress_bar()` -- the same "engine ships numbers, chrome.py builds
the ONE shared string both clients render identically" split every other
overlay in this codebase already uses (see `clients/chrome.py`'s own
module docstring).

Transport gating mirrors `analyzers/transport.py`/`analyzers/beatflash.py`
exactly
---------------------------------------------------------------------------
- "clock_tick" while NOT running is a true no-op -- no beat is counted,
  `fraction` does not move. v1's own `tick` counter is likewise only ever
  advanced by real clock pulses, which a stopped transport does not
  receive in practice; gating here keeps this analyzer consistent with its
  siblings rather than inventing a third convention.
- "start" resets the beat counter to 0 (`fraction` -> 0.0), matching v1's
  own `tick_counter` reset on a fresh transport start.
- "stop" freezes `fraction` at its last value and reports `running: False`
  -- a renderer combining the two (see `clients/chrome.py`'s
  `loopprogress_bar()`) reproduces v1's "blank bar while stopped" visual
  exactly, without needing this analyzer to silently discard the frozen
  position itself (a future "resume exactly where it left off" reading of
  the frozen value stays available on `fraction`, matching how `analyzers/
  transport.py` keeps its own bar/beat/bpm intact across a stop).
- "continue" resumes without resetting the beat counter, matching v1
  (which has no continue-specific reset of its own either).

Not ported (disclosed, not silently dropped)
---------------------------------------------------------------------------
v1's `draw()` ALSO renders a left-of-bar status string built from
`midicrt._scheduler_health_status` and `midicrt.sysex_status` (a recent-
SysEx-message summary, shown for `SYSEX_DISPLAY_SECS=5.0` after the last
one). Neither concept exists anywhere in v2 yet -- there is no scheduler
health metric and no SysEx event type surfaced by `engine/midi_in.py` -- so
there is no v2 data source to port this from; it is dropped here as
tangential diagnostic clutter with no current v2 analog, the same class of
disclosed omission as `analyzers/loopprogress.py`'s sibling modules make
for their own not-ported v1 fields (e.g. `analyzers/stucknotes.py`'s
PANIC_ON_CRIT).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from midicrt.analyzers.transport import BEATS_PER_BAR

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

TOTAL_BARS = 8   # matches v1's TOTAL_BARS exactly
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR   # 32 -- same real-world span as v1's 768 raw ticks


class LoopProgressAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `view_model()
    -> dict`. No I/O, no clock reads -- all timing derives from counting
    `clock_tick` events, never a live clock."""

    def __init__(self) -> None:
        self._beats: int = 0
        self._running: bool = False

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type == "start":
            self._beats = 0
            self._running = True
            return True
        if ev.type == "stop":
            was_running = self._running
            self._running = False
            return was_running
        if ev.type == "continue":
            was_running = self._running
            self._running = True
            return not was_running
        if ev.type != "clock_tick":
            return False
        if not self._running:
            return False   # true no-op, mirrors sibling analyzers' transport gate
        self._beats += 1
        return True

    @property
    def fraction(self) -> float:
        return (self._beats % TOTAL_BEATS) / TOTAL_BEATS

    def view_model(self) -> dict:
        return {"fraction": self.fraction, "running": self._running}
