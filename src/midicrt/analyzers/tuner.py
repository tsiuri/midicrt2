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

`handle(ev)` is a true no-op for the same reason: v1's tuner has no MIDI
handler at all, so there is no MIDI behavior to port. Kept only so this
class satisfies the engine's Page/Analyzer duck shape.

Phase 9 Task 3 (live wiring) -- the aubio-vs-YIN investigation
---------------------------------------------------------------------------
v1's own detector is `aubio.pitch()`; this task's brief asks to try aubio
first and fall back to a dependency-free numpy YIN only if it "isn't
trivially installable." It isn't, confirmed two ways on THIS exact Pi
(aarch64, Python 3.13, this venv's numpy) before writing a line of the
fallback:

1. `pip download --no-deps aubio` pulls ONLY a source sdist
   (`aubio-0.4.9.tar.gz`) -- no prebuilt wheel exists for aarch64/cp313 on
   PyPI at all (the last aubio release is from 2019).
2. A real `pip install aubio` into a throwaway venv (never touching this
   project's own `~/midicrt2-venv`) has every system build dependency
   already present (`gcc`, `python3-dev`, `libfftw3-dev`, `libsndfile1-dev`
   -- no NEW system package needed, so this wasn't a "heavyweight install"
   question) but the C EXTENSION BUILD ITSELF FAILS:
   `python/ext/ufuncs.c:48: error: initialization of 'void (*)(char **,
   const npy_intp *, ...)' from incompatible pointer type 'void (*)(char
   **, npy_intp *, ...)'` -- aubio 0.4.9's ufunc glue code was written
   against an older numpy C API (`npy_intp*`, non-const) than the numpy
   version installed here (`const npy_intp*`), a genuine upstream
   aubio/numpy version-skew compile error, not a missing-package problem.
   (A stale claim in `docs/phase3-parity.md`'s ID-10 row -- "Task 8
   confirmed aubio installs cleanly on this exact Pi" -- does not match
   this direct, current re-test; no corroborating task-8 brief/report
   survives to explain the discrepancy, so it reads as either a
   since-drifted numpy version or an unverified claim. This task's own
   finding supersedes it, disclosed rather than silently overwritten.)

Per the brief's own presumptive preference, this module implements a
dependency-free numpy YIN pitch detector (`detect_pitch()` below) instead
-- the de Cheveigné & Kawahara (2002) algorithm, computed via FFT-based
autocorrelation (O(N log N), not the textbook O(N*tau_max) double loop) so
a Pi 3 can run it every ~23ms hop without meaningfully denting the engine's
CPU budget (measured cost: see task-3-report.md).

Design choices NOT dictated by v1 (v1's aubio `PITCH_METHOD`/`TOLERANCE`
detector-construction knobs are explicitly not ported -- see "Not ported"
below, unchanged from before this task): `TUNER_FMIN_HZ`/`TUNER_FMAX_HZ`/
`YIN_THRESHOLD` are this module's own, chosen and empirically validated
(synthetic sine sweeps + a 500-seed white-noise sweep, both run on this
exact Pi -- see task-3-report.md for the full numbers) rather than
guessed:

- `TUNER_FMIN_HZ = 65.0` / `TUNER_FMAX_HZ = 1500.0`: at `BLOCKSIZE=1024`
  (`analyzers/spectrum.py`'s own audio-block size, reused here -- see
  `on_audio_block()` below), a YIN difference-function lag `tau` needs a
  correlation window of `N - tau` samples to stay statistically stable;
  letting `tau` run all the way out to `N` (a fmin near the Nyquist floor)
  produces spuriously HIGH confidence on pure noise near that edge (a real
  finding from this task's own sweep, not a hypothetical). Capping
  `tau_max` at `sr/65` (~=678 of the block's 1024 samples, ~66%) keeps
  every one of the 6 open guitar strings (E2 82.41Hz .. E4 329.63Hz)
  detected to within noise-floor precision while avoiding that edge
  instability; a bass low E1 (41.2Hz) or B0 (30.9Hz) falls below this
  floor and won't lock -- a disclosed range limit, not a bug.
- `YIN_THRESHOLD = 0.15`: the original YIN paper's own "absolute
  threshold" default (the first dip of the cumulative mean normalized
  difference function below this value wins, walking to its local
  minimum; falls back to the global minimum in-range if nothing crosses
  it) -- standard, not v1-specific (v1's aubio `TOLERANCE=0.8` configures
  aubio's own internal YIN variant differently and is not portable to this
  from-scratch implementation's threshold semantics 1:1, per the
  already-established "detector-construction knobs are not ported"
  precedent).
- `MIN_CONF = 0.65` (changed from this module's previous placeholder value
  of `0.30`, which was v1's own aubio-tuned default copied over before any
  real detector existed to gate): a 500-seed white-noise sweep at this
  exact block size/threshold NEVER produced a confidence above 0.61 (the
  YIN confidence metric here is `1 - CMNDF(best_tau)`, numerically
  DIFFERENT from aubio's own internal confidence even though both are
  called "confidence" and range [0, 1] -- so v1's literal 0.30 gate value
  does not carry over meaningfully, the same "config value doesn't port,
  the math it feeds does" situation this module's existing `SILENCE_DB`/
  `SMOOTHING` constants were never in question for). 0.65 leaves headroom
  above that measured noise ceiling while staying comfortably below
  real-tone confidence (~0.82-1.0 even down to 6-20dB SNR against added
  noise, ~1.0 for a clean tone) -- see task-3-report.md for the full
  sweep table. This is a disclosed DEVIATION from v1's literal config
  value, not a port of it; `SILENCE_DB=-55.0` and `SMOOTHING=0.55` (both
  still genuine 1:1 v1 ports) are unaffected.

Audio-capture architecture: independent capture, not a shared tap
---------------------------------------------------------------------------
v1's OWN architecture (`pages/audiospectrum.py`'s `register_raw_tap`/
`get_last_audio_block()`) is a single shared PortAudio thread multiple
pages tap into -- the tuner page never opens its own stream in v1. This
task deliberately does NOT reproduce that sharing: `pages/tuner.py`
constructs its own private `analyzers.spectrum.AudioCapture` instance
(the exact same class `pages/spectrum.py` already uses, duck-typed --
`TunerAnalyzer` implements the same `on_audio_block`/`mark_available`/
`mark_unavailable` shape `SpectrumAnalyzer` does, so `AudioCapture` needs
zero changes), independent of whatever `pages/spectrum.py` is doing.
Disclosed tradeoff, not an oversight: sharing one thread across two pages
would require either engine-level plumbing so one page's constructor can
reach into another's already-built `AudioCapture` (real coupling risk to
`analyzers/spectrum.py`/`daemon.py`, both outside this task's own files,
and to the ALREADY-PASSING spectrum test suite) or a new engine-level
audio-hub owner neither page currently has. The independent-capture design
keeps the blast radius to this task's own files (`analyzers/tuner.py`,
`pages/tuner.py`, plus the roster/daemon wiring lines needed to start/stop
it) at the cost of a second native PortAudio stream running concurrently
with spectrum's -- measured on this Pi 3 (906MB RAM, already tight) and
disclosed in task-3-report.md rather than assumed free.

Ported behavior (v1 defaults; v2 has no config.toml section for these
yet, same precedent as every other ported analyzer's thresholds)
---------------------------------------------------------------------------
- A4 = 440 Hz reference, 12-TET nearest-note rounding, signed cents
  deviation from that nearest note -- `freq_to_note()`, verbatim from
  v1's `_freq_to_note`.
- A reading is accepted only when `hz > 0`, `confidence >= MIN_CONF` AND
  `db >= SILENCE_DB` (-55.0 dBFS) -- matching v1's gate SHAPE in `draw()`
  exactly (see MIN_CONF's own value note above for why the NUMBER
  differs). Otherwise the smoothed pitch resets to unknown (`_smoothed_hz
  = None`, no note shown) -- v1 does the same on any frame that fails the
  gate, it does not hold the last note through silence.
- Accepted readings are exponentially smoothed (`SMOOTHING = 0.55`,
  v1's `_smoothed_hz = SMOOTHING*prev + (1-SMOOTHING)*hz`) before being
  converted to a note name/cents -- smoothing the PITCH, not the
  note label, matching v1.
- `tuning_meter()` is v1's `_meter`: a `width`-character ASCII gauge,
  `|` fixed at center (in tune), `^` at the needle position (cents
  scaled to a fixed +/-50 cent full-scale, clamped to the gauge width).
- v1's dB reading (`pages/tuner.py::draw()`): `rms = sqrt(mean(x*x))`,
  `db = 20*log10(rms + 1e-12)` -- ported verbatim into `on_audio_block()`
  below (the exact same formula `pages/tuner.py`'s v1 source computes per
  block before ever calling into aubio).
- `mark_available(device_desc)`/`mark_unavailable()` mirror
  `analyzers/spectrum.py::SpectrumAnalyzer`'s identically-named methods --
  a v2 ADDITION (v1's tuner never needed this since it always shared
  spectrum's already-open stream); drives the page's "no audio input"
  degrade-gracefully state, distinct from "audio present, no pitch
  locked" (`has_signal=False` with `available=True`).

Not ported (disclosed)
---------------------------------------------------------------------------
- The actual aubio `pitch()` detector object -- replaced by `detect_pitch()`
  below, see the investigation section above.
- v1's `PITCH_METHOD`/`TOLERANCE` aubio-detector-construction knobs -- they
  configured aubio's own internal detector, which this module no longer
  uses at all.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

SILENCE_DB = -55.0
MIN_CONF = 0.65
SMOOTHING = 0.55

# -- detect_pitch: dependency-free numpy YIN tuning (see module docstring
# for the empirical validation behind each of these three constants) -------
TUNER_FMIN_HZ = 65.0
TUNER_FMAX_HZ = 1500.0
YIN_THRESHOLD = 0.15


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


def detect_pitch(block: np.ndarray, sr: int, fmin_hz: float = TUNER_FMIN_HZ,
                  fmax_hz: float = TUNER_FMAX_HZ,
                  yin_threshold: float = YIN_THRESHOLD) -> tuple[float, float]:
    """Dependency-free numpy YIN pitch detector (de Cheveigne & Kawahara,
    2002) -- see module docstring's aubio-vs-YIN investigation for why this
    exists instead of a `aubio.pitch()` call. Pure function: no state, no
    I/O, directly testable against synthetic PCM.

    Computes the YIN difference function `d(tau) = sum_j (x[j]-x[j+tau])^2`
    for every lag via FFT-based (zero-padded, linear, not circular)
    autocorrelation -- O(N log N) total, not the textbook O(N*tau_max)
    nested loop -- then the cumulative mean normalized difference function
    (CMNDF), picks the first lag whose CMNDF dips below `yin_threshold`
    (walking forward to its local minimum) or the global CMNDF minimum in
    range if nothing crosses, refines it with parabolic interpolation for
    sub-sample accuracy, and returns `(hz, confidence)` where
    `confidence = 1 - CMNDF(chosen_tau)` (higher = more periodic/certain).

    Returns `(0.0, 0.0)` for a block too short to search any lag (fewer
    than 8 samples, or `fmin_hz`/`fmax_hz` leave no valid tau range) --
    the caller's own gate (`hz > 0`) already treats this as "no pitch."
    """
    x = np.asarray(block, dtype=np.float64)
    n = x.size
    if n < 8:
        return 0.0, 0.0
    tau_min = max(1, int(sr / fmax_hz))
    tau_max = min(n - 2, int(sr / fmin_hz))
    if tau_max <= tau_min + 1:
        return 0.0, 0.0

    # Zero-padded (linear, not circular) autocorrelation via FFT: pad to
    # >= n + tau_max so xpad[j+tau] never wraps around for any j/tau this
    # function ever looks at -- the standard trick that makes
    # irfft(fft(x)*conj(fft(x))) equal the LINEAR autocorrelation sum
    # rather than the circular one.
    nfft = 1
    while nfft < n + tau_max:
        nfft *= 2
    fx = np.fft.rfft(x, nfft)
    acf = np.fft.irfft(fx * np.conj(fx), nfft)[: tau_max + 1]

    # d(tau) = sum_{j=0}^{n-tau-1} x[j]^2 + sum_{j=0}^{n-tau-1} x[j+tau]^2
    #          - 2*acf(tau), both energy sums via a prefix-sum of squares
    # (avoids an O(tau_max) Python loop entirely).
    cumsum_sq = np.concatenate(([0.0], np.cumsum(x * x)))
    total_sq = cumsum_sq[n]
    taus = np.arange(tau_max + 1)
    energy_head = cumsum_sq[n - taus]
    energy_tail = total_sq - cumsum_sq[taus]
    d = energy_head + energy_tail - 2.0 * acf
    d = np.clip(d, 0.0, None)   # numerical noise guard (d is a sum of squares, never negative)

    cmndf = np.ones(tau_max + 1, dtype=np.float64)
    running = np.cumsum(d[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        cmndf[1:] = np.where(running > 1e-12, d[1:] * taus[1:] / running, 1.0)

    window = cmndf[tau_min:tau_max + 1]
    below = np.flatnonzero(window < yin_threshold)
    if below.size:
        tau_est = tau_min + int(below[0])
        while tau_est + 1 <= tau_max and cmndf[tau_est + 1] < cmndf[tau_est]:
            tau_est += 1
    else:
        tau_est = tau_min + int(np.argmin(window))

    if 0 < tau_est < tau_max:
        s0, s1, s2 = cmndf[tau_est - 1], cmndf[tau_est], cmndf[tau_est + 1]
        denom = s0 - 2.0 * s1 + s2
        shift = 0.5 * (s0 - s2) / denom if abs(denom) > 1e-12 else 0.0
        shift = max(-1.0, min(1.0, shift))
    else:
        shift = 0.0

    tau_refined = tau_est + shift
    if tau_refined <= 0:
        return 0.0, 0.0
    hz = float(sr) / float(tau_refined)
    confidence = max(0.0, min(1.0, 1.0 - float(cmndf[tau_est])))
    return hz, confidence


class TunerAnalyzer:
    """Pure-math state machine, but NOT a MIDI one -- see module docstring.
    `handle(MidiEvent) -> bool` is always a no-op. Two ways in for pitch
    data: `on_audio_block(block, sr)` -- the real production path,
    called by `pages/tuner.py`'s own `AudioCapture` instance -- computes
    `detect_pitch()` + v1's dB formula from a raw PCM block and delegates
    into `on_pitch_sample()`; the latter stays directly callable too
    (every `on_pitch_sample` test predates this task and still exercises
    the smoothing/gating math in isolation from the detector). No I/O in
    either method -- `on_audio_block`'s only "impurity" is numpy math on
    an array the caller already captured."""

    def __init__(self) -> None:
        self._smoothed_hz: float | None = None
        self._note = ""
        self._cents = 0.0
        self._hz = 0.0
        self._confidence = 0.0
        self._db = -120.0
        self._has_signal = False
        self._available = False
        self._device: str | None = None

    def handle(self, ev: MidiEvent) -> bool:
        return False   # v1's tuner has no MIDI handler at all -- see module docstring

    def on_audio_block(self, block: np.ndarray, sr: int) -> bool:
        """Injected-data method `AudioCapture`'s callback thread calls once
        per captured PCM block (mirrors `analyzers/spectrum.py::
        SpectrumAnalyzer.on_audio_block`'s own name/shape -- see module
        docstring's "Audio-capture architecture" section for why the SAME
        `AudioCapture` class works here duck-typed, unmodified). dB via
        v1's own `pages/tuner.py::draw()` formula; hz/confidence via
        `detect_pitch()` above; both then run through the EXISTING
        `on_pitch_sample` gate/smoothing, unchanged."""
        x = np.asarray(block, dtype=np.float64)
        rms = math.sqrt(float(np.mean(x * x))) if x.size else 0.0
        db = 20.0 * math.log10(rms + 1e-12)
        hz, confidence = detect_pitch(block, sr)
        return self.on_pitch_sample(hz, confidence, db)

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

    def mark_available(self, device_desc: str) -> None:
        """Called by `AudioCapture` once its input stream is genuinely
        open -- see `analyzers/spectrum.py::SpectrumAnalyzer.
        mark_available`'s identical docstring; this mirrors it exactly."""
        self._available = True
        self._device = device_desc

    def mark_unavailable(self) -> None:
        """Called by `AudioCapture` before it was ever able to open a
        stream, or after one closes/fails -- drives the page's "no audio
        input" placeholder, distinct from "audio present, no pitch
        locked" (`has_signal=False` with `available` still True)."""
        self._available = False
        self._device = None

    def view_model(self) -> dict:
        return {
            "note": self._note,
            "cents": round(self._cents, 1),
            "hz": round(self._smoothed_hz, 2) if self._smoothed_hz else 0.0,
            "confidence": round(self._confidence, 2),
            "db": round(self._db, 1),
            "has_signal": self._has_signal,
            "available": self._available,
            "device": self._device,
        }
