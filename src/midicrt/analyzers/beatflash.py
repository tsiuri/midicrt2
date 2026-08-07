"""BeatFlashAnalyzer: beat-synced flash pulse, ported from v1's
`~/codex/midicrt/plugins/beatflash.py` (READ-ONLY reference on the Pi).

v1's actual behavior (read before touching the peak/decay math)
---------------------------------------------------------------------------
v1's `draw()` runs every UI frame: on a beat boundary (`tick // PPQN !=
_last_tick // PPQN`) it sets `_flash_state = True` and stamps
`_last_flash_time`; the NEXT time `draw()` notices `(now -
_last_flash_time) > 0.1`, it clears `_flash_state` back to False. The
on-screen symbol is a hard binary toggle -- solid reverse-video "██" while
`_flash_state`, two blank spaces otherwise -- sampled at whatever the UI's
frame rate happens to be, with NO bar/downbeat distinction: every beat
flashes identically. `if not running: return` gates the whole function, so
nothing flashes (and nothing decays) while the transport is stopped.

Adaptation: continuous decay + a real bar distinction (disclosed v2
additions)
---------------------------------------------------------------------------
v2 has no per-frame polling loop inside this analyzer -- `tick(now)` is
called once per `tick_hz` period by `engine/core.py`'s `_tick_analyzers`
(see that module's "Analyzer wall-clock tick" section), the same
injected-clock contract `analyzers/stucknotes.py` already established.
Rather than reproduce v1's binary 0/1 flag sampled at an arbitrary frame
rate, this analyzer reports a continuously-decaying `intensity` (`peak *
(1 - elapsed / FLASH_DURATION_S)`, floored at 0) so a client redraw landing
ANYWHERE inside the flash window sees a graduated value instead of only
ever "fully on" or "fully off" depending on luck -- clients quantize that
intensity into their own displayed levels (see `clients/chrome.py`'s
`beatflash_glyph()`).

The task brief also asks for "brief flash on beat, stronger on bar" -- v1
itself has no such distinction (every beat is identical, see above); this
is a disclosed v2 addition. Rather than lengthen the bar flash's DURATION
(which would need a client to somehow show elapsed time -- awkward for a
single glyph), "stronger" is expressed as a higher PEAK (`BAR_PEAK` vs
`BEAT_PEAK`) decaying over the SAME `FLASH_DURATION_S` v1 already uses --
`clients/chrome.py`'s glyph ramp reserves its top character exclusively for
`intensity > BEAT_PEAK`, so only a bar flash can ever reach it.

Bar-boundary detection reuses `analyzers.transport.BEATS_PER_BAR`
---------------------------------------------------------------------------
v1 has no concept of "bar" here at all (see above), so there is nothing to
port for it; this analyzer derives one the same way `analyzers/
transport.py` does for its own BAR/BEAT display -- a beat is a bar's
downbeat exactly when `TransportAnalyzer`'s own `.beat` property would
read 1, i.e. `self._beats % BEATS_PER_BAR == 0` on the SAME
beats-since-start counter convention (reset on "start", frozen on "stop",
unaffected by "continue"). The constant is IMPORTED, not redeclared as a
second magic `4` that could drift from transport.py's own.

Transport gating mirrors v1 (and analyzers/transport.py) exactly
---------------------------------------------------------------------------
- "clock_tick" while NOT running is a true no-op, matching v1's own `if
  not running: return` -- no flash starts, no beat is counted.
- "start" resets the beat counter to 0 and clears any in-progress flash (a
  fresh transport start should not show a flash left over from a previous
  session) -- mirrors `analyzers/transport.py`'s own reset-on-start.
- "stop"/"continue" do not touch the beat counter, matching v1 (which has
  no beat-counter concept of its own to reset either way).

An improvement over a real v1 quirk (disclosed, not a regression)
---------------------------------------------------------------------------
v1's `draw()` early-returns BEFORE the turn-off-after-0.1s check whenever
`not running` -- so a flash that happens to be active the instant the
transport stops freezes on screen forever (until the next "start" clears
it, since v1 has no other way to reset `_flash_state`). This analyzer's
`tick(now)` computes decay purely from elapsed wall-clock time regardless
of `running`, so a flash always finishes fading out on schedule even
through a stop -- a strict improvement, not v1 behavior this task
deliberately preserves.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from midicrt.analyzers.transport import BEATS_PER_BAR

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's/stucknotes.py's own
    # comment -- avoids a circular import with engine.core, which builds
    # _ANALYZER_FACTORIES from modules like this one.
    from midicrt.engine.core import MidiEvent

FLASH_DURATION_S = 0.1     # matches v1's hardcoded 0.1s hold exactly
BEAT_PEAK = 1.0
BAR_PEAK = 1.4              # v2 addition: "stronger on bar" (see module docstring)


class BeatFlashAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `tick(now)
    -> bool` (dirty; wall-progress decay, see module docstring),
    `view_model() -> dict`. No I/O -- `now` is always injected by the
    caller, never read here."""

    def __init__(self) -> None:
        self._beats: int = 0
        self._running: bool = False
        self._flash_start: float | None = None
        self._flash_peak: float = 0.0
        self._is_bar: bool = False
        self._last_intensity: float = 0.0

    # -- event handling -------------------------------------------------------

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type == "start":
            self._beats = 0
            self._running = True
            self._flash_start = None
            self._flash_peak = 0.0
            self._is_bar = False
            dirty = self._last_intensity != 0.0
            self._last_intensity = 0.0
            return dirty
        if ev.type == "stop":
            self._running = False
            return False   # decay keeps running via tick(), see module docstring
        if ev.type == "continue":
            self._running = True
            return False
        if ev.type != "clock_tick":
            return False
        if not self._running:
            return False   # true no-op, mirrors v1's own transport gate
        self._beats += 1
        self._is_bar = (self._beats % BEATS_PER_BAR) == 0
        self._flash_peak = BAR_PEAK if self._is_bar else BEAT_PEAK
        self._flash_start = ev.ts
        self._last_intensity = self._flash_peak
        return True

    # -- wall-progress hook (see module docstring) -----------------------------

    def tick(self, now: float) -> bool:
        if self._flash_start is None:
            return False
        elapsed = now - self._flash_start
        frac = elapsed / FLASH_DURATION_S if FLASH_DURATION_S > 0 else 1.0
        intensity = max(0.0, self._flash_peak * (1.0 - frac))
        if intensity <= 0.0:
            intensity = 0.0
            self._flash_start = None
        changed = intensity != self._last_intensity
        self._last_intensity = intensity
        return changed

    # -- view model -------------------------------------------------------------

    def view_model(self) -> dict:
        return {"intensity": round(self._last_intensity, 3), "is_bar": self._is_bar}
