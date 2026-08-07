"""TunerAnalyzer: pitch-reference display, ported from v1's
`~/codex/midicrt/pages/tuner.py` (READ-ONLY reference on the Pi).

What v1's tuner actually is (read before assuming this analyzer is like
its siblings)
---------------------------------------------------------------------------
v1's tuner is NOT a MIDI-driven plugin at all -- grepping v1 for `tuner`
turns up exactly one real feature: `pages/tuner.py`, an audio pitch-
detection page (PAGE_ID 10, "Tuner") built on `aubio.pitch()` fed by
`pages/audiospectrum.py`'s live PipeWire capture (`audio.
get_last_audio_block()`). It has NO `handle(msg)` function anywhere in the
file -- v1's plugin/page loader simply never wires MIDI to it. Every other
v1 source ported in this phase (`zstucknotes.py`, `ztimesig.py`, `zharmony.
py`, ...) is a MIDI-event state machine; the tuner is the one genuine
exception, not an oversight in how it's ported here.

Why this can't be wired to anything live in v2 yet (disclosed blocker)
---------------------------------------------------------------------------
`pages/audiospectrum.py` (v1's PipeWire audio-capture path) is its own
separate, not-yet-ported v2 task per docs/phase3-notes.md's v1 source map
("Spectrum: pages/audiospectrum.py (733) -- discover its audio-input path
(PipeWire runs on the Pi)"), out of this task-6 brief's scope (stuck-notes/
timesig/tuner analyzers). Without it, there is no live pitch-detector
output anywhere in v2 to feed this analyzer. What IS ported here is v1's
post-detection MATH -- the pure frequency->note-name/cents conversion
(`_freq_to_note`), the tuning-meter ASCII gauge (`_meter`), and the
smoothing/gating state machine `draw()` runs on each new audio block
(EMA-smoothed pitch, confidence/silence gating) -- reshaped into an
injected-data method, `on_pitch_sample(hz, confidence, db)`, that a future
audio-capture module would call once per detected pitch reading, exactly
the same "engine does the I/O, analyzer stays pure and receives injected
values" split `analyzers/stucknotes.py`'s `tick(now)` hook uses for wall-
clock injection. Until that future task lands, nothing in the running
daemon calls `on_pitch_sample` -- `pages/tuner.py` (this task's v2 page
wrapper) will show v1's own genuine idle state ("Listening..." with
`has_signal: False`), which is the CORRECT idle rendering, not a stub --
v1's page shows the exact same thing whenever no pitched signal is present
above `SILENCE_DB`/`MIN_CONF`.

`handle(ev)` is a true no-op for the same reason: v1's tuner has no MIDI
handler at all, so there is no MIDI behavior to port. Kept only so this
class satisfies the engine's Page/Analyzer duck shape.

Ported behavior (v1 defaults; v2 has no config.toml section for these
yet, same precedent as every other ported analyzer's thresholds)
---------------------------------------------------------------------------
- A4 = 440 Hz reference, 12-TET nearest-note rounding, signed cents
  deviation from that nearest note -- `freq_to_note()`, verbatim from
  v1's `_freq_to_note`.
- A reading is accepted only when `hz > 0`, `confidence >= MIN_CONF`
  (0.30) AND `db >= SILENCE_DB` (-55.0 dBFS) -- matching v1's gate in
  `draw()` exactly. Otherwise the smoothed pitch resets to unknown
  (`_smoothed_hz = None`, no note shown) -- v1 does the same on any frame
  that fails the gate, it does not hold the last note through silence.
- Accepted readings are exponentially smoothed (`SMOOTHING = 0.55`,
  v1's `_smoothed_hz = SMOOTHING*prev + (1-SMOOTHING)*hz`) before being
  converted to a note name/cents -- smoothing the PITCH, not the
  note label, matching v1.
- `tuning_meter()` is v1's `_meter`: a `width`-character ASCII gauge,
  `|` fixed at center (in tune), `^` at the needle position (cents
  scaled to a fixed +/-50 cent full-scale, clamped to the gauge width).

Not ported (disclosed)
---------------------------------------------------------------------------
- The actual aubio `pitch()` detector object/audio capture -- that is the
  I/O this analyzer deliberately does not own (see above).
- v1's `PITCH_METHOD`/`TOLERANCE` aubio-detector-construction knobs -- they
  configure the (not-yet-ported) detector itself, not this module's pure
  post-processing math.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

SILENCE_DB = -55.0
MIN_CONF = 0.30
SMOOTHING = 0.55


def freq_to_note(freq: float) -> tuple[str, float]:
    """Pure port of v1's `_freq_to_note`: nearest 12-TET note name (A440
    reference) + signed cents deviation from it. `freq <= 0` -> `("", 0.0)`,
    matching v1 exactly (its caller already gates on this)."""
    if freq <= 0.0:
        return "", 0.0
    midi = 69 + 12 * math.log2(freq / 440.0)
    nearest = round(midi)
    note = _NOTE_NAMES[nearest % 12]
    octave = nearest // 12 - 1
    note_name = f"{note}{octave}"
    nearest_freq = 440.0 * (2 ** ((nearest - 69) / 12))
    cents = 1200.0 * math.log2(freq / nearest_freq) if nearest_freq > 0 else 0.0
    return note_name, cents


def tuning_meter(cents: float, width: int = 21) -> str:
    """Pure port of v1's `_meter`: an ASCII needle gauge -- '|' fixed at
    center (in tune), '^' at the needle position (cents scaled to a fixed
    +/-50 cent full scale, clamped to the gauge width)."""
    width = max(7, int(width))
    center = width // 2
    pos = center + round((cents / 50.0) * center)
    pos = max(0, min(width - 1, pos))
    chars = ["-"] * width
    chars[center] = "|"
    chars[pos] = "^"
    return "".join(chars)


class TunerAnalyzer:
    """Pure state machine, but NOT a MIDI one -- see module docstring.
    `handle(MidiEvent) -> bool` is always a no-op; the real input is
    `on_pitch_sample(hz, confidence, db) -> bool`, an injected-data method
    a (future, not-yet-built) audio-capture module would call once per
    pitch-detector reading. No I/O here either way -- the caller already
    did any real detection/capture."""

    def __init__(self) -> None:
        self._smoothed_hz: float | None = None
        self._note = ""
        self._cents = 0.0
        self._hz = 0.0
        self._confidence = 0.0
        self._db = -120.0
        self._has_signal = False

    def handle(self, ev: MidiEvent) -> bool:
        return False   # v1's tuner has no MIDI handler at all -- see module docstring

    def on_pitch_sample(self, hz: float, confidence: float, db: float) -> bool:
        self._hz, self._confidence, self._db = hz, confidence, db
        if hz > 0.0 and confidence >= MIN_CONF and db >= SILENCE_DB:
            self._smoothed_hz = (
                hz if self._smoothed_hz is None
                else (SMOOTHING * self._smoothed_hz) + ((1.0 - SMOOTHING) * hz)
            )
            self._note, self._cents = freq_to_note(self._smoothed_hz)
            self._has_signal = True
        else:
            self._smoothed_hz = None
            self._note, self._cents = "", 0.0
            self._has_signal = False
        # Always dirty: v1's draw() redraws Conf/Level every audio block
        # regardless of whether a note is currently locked, the same "live
        # readout" convention analyzers/stucknotes.py's tick() documents.
        return True

    def view_model(self) -> dict:
        return {
            "note": self._note,
            "cents": round(self._cents, 1),
            "hz": round(self._smoothed_hz, 2) if self._smoothed_hz else 0.0,
            "confidence": round(self._confidence, 2),
            "db": round(self._db, 1),
            "has_signal": self._has_signal,
        }
