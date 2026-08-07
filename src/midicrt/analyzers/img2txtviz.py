"""Img2TxtVizAnalyzer: v1's `pages/img2txtviz.py` (552 lines, READ-ONLY
reference on the Pi) ported as a pure, wall-clock-injected state machine --
see docs/phase3-notes.md's v1 source map and
`.superpowers/sdd/2026-08-06-midicrt2-phase3-parity/task-10-brief.md` for the
task this ports.

Investigation finding (read before assuming this loads real image files)
---------------------------------------------------------------------------
Despite its name -- and despite `~/codex/midicrt/imgbank/continue/` sitting
right next to it on disk (4 real images, ~960KB total, confirmed via `du`/
`ls` on the Pi: `GGXBB5GaYAAI8KA.jpg`, `kanyeclose.png`, `looking_great.png`,
`odrum4c3abic1.webp`) -- v1's `pages/img2txtviz.py` **never loads an image
file at all**. Confirmed by reading the complete 552-line source: no
`PIL`/`Image` import, no `open()`/file read anywhere in the module, and
`grep -rn imgbank ~/codex/midicrt/` (whole tree, every file type) returns
ZERO hits inside any `.py` file -- the directory is orphaned/dead data with
no live reference anywhere in the codebase, not a resource this page reads.
The page is actually a real-time, MIDI-(and optionally audio-spectrum-)
REACTIVE procedural ASCII-art generator: a sine-wave field modulated by
note velocity/energy/CC values and quantized through one of four fixed
ASCII-density charsets each frame. "img2txt" names the ASCII IMAGE it
*produces*, not an image it *consumes*.

Consequently: no `imgbank_dir` config key exists (there is nothing on disk
this page reads, so a "point v2's default at v1's imgbank path" fallback
would be pointing at nothing this page ever opens), and there is no
"next image" control -- the task brief's own speculative "(next image
action?)" parenthetical does not apply. v1's REAL controls, read from its
`keypress()` (lines ~178-214 there): `[`/`]` block size, `i` invert, `c`
charset cycle, `a` audio toggle, `g`/`h`/`-`/`+` gamma, `j`/`k` fps cap,
`u` auto-quality. Of these, `i` (invert) and `c` (charset cycle) are true
persistent-STATE toggles that change what the page shows -- ported here as
`toggle_invert()`/`cycle_charset()`, wired as `img2txtviz.invert`/
`img2txtviz.charset` actions in engine/core.py (mirrors the
`pianoroll.zoom`/`.projection`/`.channels` precedent from task 7). The rest
(block size, gamma, fps cap, auto-quality) are v1 REAL-TIME RENDER
PERFORMANCE knobs tuning a variable-resolution ASCII buffer against a
live terminal's frame budget -- concepts that do not exist in v2's
architecture at all (this analyzer emits one FIXED-size grid of already-
final [0,1] values every tick; there is no per-client render loop for it to
tune, and clients quantize/upsample that grid to their own raster
independently, see clients/tui.py's/clients/fb/app.py's own render
functions) -- NOT ported, disclosed here rather than silently dropped.

v1 has NO tests for this page at all (`~/codex/midicrt/tests` grepped for
"img2txt" -- zero hits), and its chaotic wave-mixing output was never a
frozen contract even in v1. This port therefore does not attempt to
reproduce v1's specific ~8-term ad hoc formula byte-for-byte (there is no
"correct" v1 output to match in the first place) -- it reproduces v1's REAL
STRUCTURE and ported it in a place where it belongs (MIDI-driven excitation
state feeding a spatial wave field, gated by invert/charset, decaying
exponentially over injected wall-clock time) through an independently-
specified formula chosen for exact testability instead. See "Disclosed v2
adaptation" below for the exact departures.

Ported verbatim from v1 (same identifiers/values, `_decay()`/`handle()`)
---------------------------------------------------------------------------
- The four ASCII density charsets (`CHARSETS`), darkest-to-brightest.
- `energy`/`spark`/`vel_splash` exponential decay rates: v1's `_decay()`
  multiplies each by `exp(-dt * RATE * RETURN_SPEED_MULT)` with
  `RATE = 1.35/2.2/8.0` and a fixed (never keypress-adjustable, confirmed
  by grep) `RETURN_SPEED_MULT = 3.0` -- baked in below as
  `_ENERGY_DECAY_PER_S`/`_SPARK_DECAY_PER_S`/`_SPLASH_DECAY_PER_S`.
- The velocity-nonlinear note-on excitation: `vel01 ** 1.85` weighting,
  `rate_scale = clamp(dt_note / 0.10, 0.30, 1.0)` note-density damping, and
  the exact `energy`/`spark`/`vel_splash` increment formulas + their
  `[0, 2.6]`/`[0, 2.8]`/`[0, 3.2]` clamps -- v1's `handle()`.
- `_last_program` selecting a charset OFFSET (`(charset_ix + last_program)
  % len(CHARSETS)`, v1's `_render_ascii`'s `charset = _CHARSETS[(_charset_ix
  + _last_program) % len(_CHARSETS)]`) -- this required a small, disclosed
  fix to `engine/midi_in.py::translate()` (see that module's own comment):
  v2's translate() never populated `data1` for `program_change` events
  before this task, so no analyzer could ever recover the real program
  number. Fixed additively (one new elif branch); nothing depended on the
  old None.

Disclosed v2 adaptation (not a literal port -- see "Investigation finding")
---------------------------------------------------------------------------
- Grid resolution: fixed `GRID_COLS=40, GRID_ROWS=20` (2:1) -- independent
  of any client's terminal/pixel size, matching pages/pianoroll.py's own
  "engine emits a fixed normalized shape, renderers quantize/upsample to
  their own raster" convention (docs/phase3-notes.md's "renderers only
  quantize" rule) rather than v1's live-terminal-size / user-adjustable-
  block-size buffer.
- The wave field (`_cell_value` below) is a disclosed, independently-
  specified 3-term spatial function (two orthogonal sine sweeps + a
  Manhattan-distance "ring" sweep -- v1 also uses Manhattan distance for
  its own ring term, `abs(nx-ctr_x)+abs(ny-ctr_y)`) rather than v1's ~8-term
  ad hoc mix of shimmer/pulse/edge-detect/trail-decay terms -- chosen for
  exact hand-verifiable testability (see tests/test_analyzers_img2txtviz.py's
  "known state -> expected cell value" test) while preserving the same
  INPUT set v1 reacts to: note/octave phase, CC1 ("mod wheel", v1's real
  speed modulator) and CC74 ("brightness", v1's real contrast modulator),
  and the energy/spark/vel_splash excitation triplet boosting ring
  amplitude and overall contrast. `handle()`/`tick()` only ever advance via
  injected time (`ev.ts`/`now`), never a real clock read internally --
  matching `analyzers/spectrum.py`'s/`analyzers/stucknotes.py`'s "no I/O"
  convention, which v1's own direct `time.monotonic()` reads inside
  `_decay()`/`_render_ascii()` do not follow.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

GRID_COLS = 40
GRID_ROWS = 20

# -- v1-ported charsets (darkest -> brightest), verbatim from `_CHARSETS` --
CHARSETS = (
    " .:-=+*#%@",
    "  .,:;irsXA253hMHGS#9B&@",
    " .'`^\",:;Il!i~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    " .oO0@",
)

_TWO_PI = 2.0 * math.pi

# -- v1-ported decay rates: RATE * RETURN_SPEED_MULT(3.0), see module
# docstring's "Ported verbatim" section --
_ENERGY_DECAY_PER_S = 1.35 * 3.0
_SPARK_DECAY_PER_S = 2.2 * 3.0
_SPLASH_DECAY_PER_S = 8.0 * 3.0
_DECAY_FLOOR = 1e-4   # below this, snap to exactly 0.0 -- v1's own epsilon

# -- disclosed v2 wave-field constants (see module docstring) --
_SPEED_X = 0.15
_SPEED_Y = 0.11
_RING_FREQ = 4.0


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


class Img2TxtVizAnalyzer:
    """Pure state machine (aside from no locking needed -- unlike
    `analyzers/spectrum.py`, nothing here is written from a background
    thread): `handle(MidiEvent) -> bool`, `tick(now) -> bool`,
    `view_model() -> dict`. See module docstring for the full v1
    investigation and the disclosed wave-field adaptation.
    """

    def __init__(self) -> None:
        self._active_notes: dict[tuple[int, int], int] = {}
        self._last_note = 60          # v1's own default (middle C)
        self._last_vel = 0
        self._last_program = 0
        self._cc: dict[int, int] = {}
        self._energy = 0.0
        self._spark = 0.0
        self._vel_splash = 0.0
        self._last_note_on_ts: float | None = None
        self._last_ts: float | None = None   # shared wall-clock reference for _advance()
        self._phase = 0.0             # accumulated elapsed seconds, drives the wave field
        self._charset_ix = 0
        self._invert = False

    # -- wall-clock advancement (injected only -- see module docstring) -----

    def _advance(self, ts: float) -> None:
        if self._last_ts is None:
            dt = 0.0
        else:
            dt = max(0.0, ts - self._last_ts)
        self._last_ts = ts
        self._energy *= math.exp(-dt * _ENERGY_DECAY_PER_S)
        self._spark *= math.exp(-dt * _SPARK_DECAY_PER_S)
        self._vel_splash *= math.exp(-dt * _SPLASH_DECAY_PER_S)
        if self._energy < _DECAY_FLOOR:
            self._energy = 0.0
        if self._spark < _DECAY_FLOOR:
            self._spark = 0.0
        if self._vel_splash < _DECAY_FLOOR:
            self._vel_splash = 0.0
        self._phase += dt

    # -- MIDI-driven state (v1's handle(), ported verbatim -- see docstring) -

    def handle(self, ev: MidiEvent) -> bool:
        self._advance(ev.ts)
        if ev.type == "note_on":
            ch = ev.channel or 0
            note = ev.data1 or 0
            vel = ev.data2 or 0
            if vel > 0:
                self._active_notes[(ch, note)] = vel
                self._last_note = note
                self._last_vel = vel
                vel01 = _clamp(vel / 127.0, 0.0, 1.0)
                vel_w = vel01 ** 1.85
                dt_note = (ev.ts - self._last_note_on_ts
                           if self._last_note_on_ts is not None else 0.12)
                rate_scale = _clamp(dt_note / 0.10, 0.30, 1.0)
                self._last_note_on_ts = ev.ts
                self._energy = _clamp(
                    self._energy + vel_w * (0.35 + 0.65 * rate_scale), 0.0, 2.6)
                self._spark = _clamp(self._spark + (0.18 + vel_w * 0.95), 0.0, 2.8)
                self._vel_splash = _clamp(self._vel_splash + vel_w * 1.75, 0.0, 3.2)
            else:
                self._active_notes.pop((ch, note), None)
            return True
        if ev.type == "note_off":
            ch = ev.channel or 0
            note = ev.data1 or 0
            self._active_notes.pop((ch, note), None)
            return True
        if ev.type == "control_change":
            ctrl = ev.data1 or 0
            val = ev.data2 or 0
            self._cc[ctrl] = int(_clamp(val, 0, 127))
            return True
        if ev.type == "program_change":
            self._last_program = (ev.data1 or 0) % 128
            return True
        return False   # e.g. clock_tick/start/stop -- decay still advanced above, not dirty

    def tick(self, now: float) -> bool:
        """Injected wall-clock progress (engine's `_tick_pages`, same
        contract as `analyzers/spectrum.py`'s/`pages/pianoroll.py`'s own
        `tick(now)`). Unlike those two (which report `False` once settled
        at rest), this ALWAYS returns `True`: v1's page is a continuous
        animation with no "nothing changed" state by design -- the wave
        field's spatial terms depend on `self._phase`, which strictly
        advances every real tick regardless of MIDI activity, so the grid
        is never truly at rest. A disclosed choice, not an oversight.
        """
        self._advance(now)
        return True

    # -- runtime-adjustable controls (spec §5) -------------------------------

    def cycle_charset(self) -> str:
        self._charset_ix = (self._charset_ix + 1) % len(CHARSETS)
        return self._active_charset()

    def toggle_invert(self) -> bool:
        self._invert = not self._invert
        return self._invert

    def _active_charset(self) -> str:
        return CHARSETS[(self._charset_ix + self._last_program) % len(CHARSETS)]

    # -- wave field (disclosed v2 adaptation -- see module docstring) -------

    def _cell_value(self, nx: float, ny: float, note_phase: float,
                     octave_phase: float, drive: float, brightness_bias: float) -> float:
        wave_x = 0.5 + 0.5 * math.sin(
            _TWO_PI * (nx * 2.0 + self._phase * _SPEED_X + note_phase))
        wave_y = 0.5 + 0.5 * math.sin(
            _TWO_PI * (ny * 2.0 - self._phase * _SPEED_Y + octave_phase))
        d = abs(nx - 0.5) + abs(ny - 0.5)
        ring = 0.5 + 0.5 * math.sin(
            _TWO_PI * (d * _RING_FREQ - self._phase * (0.35 + drive * 0.4)))
        base = 0.35 * wave_x + 0.30 * wave_y + 0.35 * ring
        base += drive * 0.15 * ring
        contrast = 1.0 + min(drive, 2.0) * 0.25
        v = _clamp((base - 0.5) * contrast + 0.5 + brightness_bias, 0.0, 1.0)
        if self._invert:
            v = 1.0 - v
        return v

    def _compute_grid(self) -> list[list[float]]:
        note_phase = (self._last_note % 12) / 12.0
        octave_phase = ((self._last_note // 12) % 8) / 8.0
        # v1's real CC1 ("mod wheel")/CC74 ("brightness") reactivity,
        # folded into one speed/contrast "drive" scalar plus a small
        # brightness bias -- see module docstring's disclosed-adaptation
        # note. `.get(74, 64)` mirrors v1's own CC74 neutral-center default
        # (64, the MIDI mid-value) so an untouched CC74 contributes zero
        # bias, exactly like an untouched mod wheel (CC1 default 0)
        # contributes zero drive.
        cc1 = self._cc.get(1, 0) / 127.0
        cc74 = self._cc.get(74, 64)
        brightness_bias = (cc74 - 64) / 64.0 * 0.1
        drive = _clamp(
            self._energy * 0.25 + self._vel_splash * 0.35 + cc1 * 0.3, 0.0, 2.0)
        grid = []
        for r in range(GRID_ROWS):
            ny = r / max(1, GRID_ROWS - 1)
            row = [
                self._cell_value(c / max(1, GRID_COLS - 1), ny, note_phase,
                                  octave_phase, drive, brightness_bias)
                for c in range(GRID_COLS)
            ]
            grid.append(row)
        return grid

    def view_model(self) -> dict:
        return {
            "grid": self._compute_grid(),
            "charset": self._active_charset(),
            "invert": self._invert,
            "active_notes": len(self._active_notes),
            "last_note": self._last_note,
            "last_vel": self._last_vel,
            "last_program": self._last_program,
            "energy": round(self._energy, 4),
            "vel_splash": round(self._vel_splash, 4),
        }
