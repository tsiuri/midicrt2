"""TDD for TunerAnalyzer: pure pitch-reading math + smoothing/gating state
machine, ported from v1's `~/codex/midicrt/pages/tuner.py`. Unlike every
other v2 analyzer, this one is NOT MIDI-driven -- see analyzers/tuner.py's
module docstring for why (v1's tuner has no MIDI handler at all).

Phase 9 Task 3 (live wiring): `on_pitch_sample()` is still tested directly
below with synthetic pitch-detector readings (the smoothing/gating state
machine is unchanged), but it is no longer the only entry point -- the new
`on_audio_block(block, sr)` method (tested in its own section, "audio-block
wiring" below) is the REAL production path, computing `detect_pitch()`
(this module's own dependency-free numpy YIN, see its docstring for the
aubio-vs-YIN investigation) + a v1-formula RMS/dB reading from a raw PCM
block, then delegating into the same `on_pitch_sample()` gate these older
tests exercise directly. `mark_available`/`mark_unavailable` (own section
below) mirror `analyzers/spectrum.py::SpectrumAnalyzer`'s identically-named
methods -- the "no audio hardware" signal `AudioCapture` drives, distinct
from "audio present, no pitch locked".
"""
import numpy as np
import pytest

from midicrt.analyzers.tuner import (
    MIN_CONF,
    SILENCE_DB,
    TUNER_FMAX_HZ,
    TUNER_FMIN_HZ,
    YIN_THRESHOLD,
    TunerAnalyzer,
    detect_pitch,
    freq_to_note,
    tuning_meter,
)
from midicrt.engine.core import MidiEvent

SR = 44100
BLOCK_N = 1024


def _sine(freq: float, sr: int = SR, n: int = BLOCK_N, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_initial_view_model_shows_no_signal():
    a = TunerAnalyzer()
    vm = a.view_model()
    assert vm == {"note": "", "cents": 0.0, "hz": 0.0, "confidence": 0.0,
                  "db": -120.0, "has_signal": False, "available": False, "device": None}


def test_handle_is_a_true_noop_for_any_midi_event():
    a = TunerAnalyzer()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert a.handle(ev) is False
    assert a.view_model()["has_signal"] is False


# -- pure conversion math -----------------------------------------------------

def test_freq_to_note_a440_is_exact():
    note, cents = freq_to_note(440.0)
    assert note == "A4"
    assert cents == pytest.approx(0.0, abs=1e-9)


def test_freq_to_note_middle_c():
    note, cents = freq_to_note(261.6256)   # C4, equal temperament
    assert note == "C4"
    assert abs(cents) < 1.0


def test_freq_to_note_sharp_reports_positive_cents():
    # A touch sharp of A4.
    note, cents = freq_to_note(440.0 * (2 ** (10 / 1200)))   # +10 cents
    assert note == "A4"
    assert cents == pytest.approx(10.0, abs=0.01)


def test_freq_to_note_flat_reports_negative_cents():
    note, cents = freq_to_note(440.0 * (2 ** (-15 / 1200)))
    assert note == "A4"
    assert cents == pytest.approx(-15.0, abs=0.01)


def test_freq_to_note_zero_or_negative_is_empty():
    assert freq_to_note(0.0) == ("", 0.0)
    assert freq_to_note(-5.0) == ("", 0.0)


def test_freq_to_note_octave_boundary():
    note, _cents = freq_to_note(523.2511)   # C5
    assert note == "C5"


def test_tuning_meter_centered_when_in_tune():
    m = tuning_meter(0.0, width=21)
    assert len(m) == 21
    center = 21 // 2
    # The needle ('^') sits exactly on the center tick mark when in tune --
    # v1's own `chars[center] = "|"` then `chars[pos] = "^"` order means the
    # needle glyph wins at that position, matching v1 exactly.
    assert m.count("^") == 1
    assert m.index("^") == center


def test_tuning_meter_needle_moves_right_when_sharp():
    m = tuning_meter(25.0, width=21)   # halfway sharp of full scale (+/-50)
    center = 21 // 2
    assert m.index("^") > center


def test_tuning_meter_needle_moves_left_when_flat():
    m = tuning_meter(-25.0, width=21)
    center = 21 // 2
    assert m.index("^") < center


def test_tuning_meter_clamps_beyond_full_scale():
    m = tuning_meter(500.0, width=21)   # way beyond +/-50c
    assert m.index("^") == 20   # clamped to the rightmost column
    m2 = tuning_meter(-500.0, width=21)
    assert m2.index("^") == 0


def test_tuning_meter_enforces_a_minimum_width():
    m = tuning_meter(0.0, width=1)
    assert len(m) == 7   # v1's own `max(7, width)` floor


# -- smoothing/gating state machine -------------------------------------------

def test_confident_loud_signal_locks_a_note():
    a = TunerAnalyzer()
    changed = a.on_pitch_sample(hz=440.0, confidence=0.9, db=-20.0)
    assert changed is True
    vm = a.view_model()
    assert vm["has_signal"] is True
    assert vm["note"] == "A4"
    assert vm["hz"] == pytest.approx(440.0)


def test_low_confidence_reading_is_rejected():
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=440.0, confidence=MIN_CONF - 0.01, db=-20.0)
    vm = a.view_model()
    assert vm["has_signal"] is False
    assert vm["note"] == ""


def test_below_silence_threshold_is_rejected():
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=440.0, confidence=0.9, db=SILENCE_DB - 1.0)
    assert a.view_model()["has_signal"] is False


def test_zero_hz_is_rejected_even_with_good_confidence():
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=0.0, confidence=0.9, db=-10.0)
    assert a.view_model()["has_signal"] is False


def test_signal_loss_resets_smoothed_pitch_no_hold():
    # v1 does not hold the last note through silence -- a failing-gate
    # frame immediately clears to unknown.
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=440.0, confidence=0.9, db=-20.0)
    assert a.view_model()["has_signal"] is True
    a.on_pitch_sample(hz=440.0, confidence=0.0, db=-20.0)
    assert a.view_model()["has_signal"] is False
    assert a.view_model()["note"] == ""


def test_pitch_is_exponentially_smoothed_across_samples():
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=440.0, confidence=0.9, db=-10.0)
    a.on_pitch_sample(hz=450.0, confidence=0.9, db=-10.0)
    vm = a.view_model()
    expected = 0.55 * 440.0 + 0.45 * 450.0
    assert vm["hz"] == pytest.approx(expected, abs=0.01)


def test_confidence_and_level_readout_updates_even_without_signal():
    a = TunerAnalyzer()
    a.on_pitch_sample(hz=0.0, confidence=0.05, db=-70.0)
    vm = a.view_model()
    assert vm["confidence"] == pytest.approx(0.05)
    assert vm["db"] == pytest.approx(-70.0)
    assert vm["has_signal"] is False


def test_on_pitch_sample_always_reports_dirty():
    a = TunerAnalyzer()
    assert a.on_pitch_sample(hz=440.0, confidence=0.9, db=-10.0) is True
    assert a.on_pitch_sample(hz=440.0, confidence=0.9, db=-10.0) is True   # same reading, still True
    assert a.on_pitch_sample(hz=0.0, confidence=0.0, db=-120.0) is True


def test_never_reads_a_clock_or_does_io():
    # No `import time`/`aubio`/audio I/O anywhere in the module -- a
    # structural guard, mirrors the sibling analyzers' own clock-read tests.
    # `numpy` IS imported (the YIN math needs it) -- same precedent as
    # `analyzers/spectrum.py`'s own FFT math -- only `time`/`aubio` are
    # actually forbidden here.
    import midicrt.analyzers.tuner as mod
    assert not hasattr(mod, "time")
    assert not hasattr(mod, "aubio")


# -- detect_pitch: dependency-free numpy YIN (Phase 9 Task 3) ----------------
#
# aubio investigation (see module docstring + task-3-report.md for the full
# evidence): aubio 0.4.9 has NO prebuilt wheel for aarch64/cp313 on PyPI
# (sdist only), and an actual from-source build attempt on this exact Pi
# FAILS outright -- `python/ext/ufuncs.c` uses a ufunc-loop function pointer
# signature (`npy_intp*`) incompatible with the numpy version installed
# here (which wants `const npy_intp*`), a real compile error, not a missing
# system package. A dependency-free numpy YIN (de Cheveigné & Kawahara
# 2002) is used instead, per the task brief's own presumptive preference.
#
# `MIN_CONF` is recalibrated to 0.65 for THIS detector's own confidence
# metric (see module docstring) -- v1's literal 0.30 was tuned for aubio's
# own internal confidence computation, a different metric numerically; a
# 500-seed white-noise sweep at this block size never exceeds confidence
# 0.61 (see module docstring's own empirical table), so 0.65 leaves
# deliberate headroom above the noise ceiling while staying well below
# real-signal confidence (~0.82-1.0 even at 6-20dB SNR, ~1.0 for a clean
# tone).

def test_detect_pitch_440hz_sine_locks_a4_at_zero_cents():
    hz, confidence = detect_pitch(_sine(440.0), SR)
    assert hz == pytest.approx(440.0, abs=0.5)
    assert confidence >= MIN_CONF
    note, cents = freq_to_note(hz)
    assert note == "A4"
    assert cents == pytest.approx(0.0, abs=2.0)


def test_detect_pitch_446hz_sine_is_23_cents_sharp_of_a4():
    # The task brief's own worked example: 446Hz -> A4, +23 cents ballpark.
    hz, confidence = detect_pitch(_sine(446.0), SR)
    assert hz == pytest.approx(446.0, abs=0.5)
    assert confidence >= MIN_CONF
    note, cents = freq_to_note(hz)
    assert note == "A4"
    assert cents == pytest.approx(23.5, abs=2.0)


@pytest.mark.parametrize("name,freq", [
    ("E2", 82.41), ("A2", 110.0), ("D3", 146.83),
    ("G3", 196.0), ("B3", 246.94), ("E4", 329.63),
])
def test_detect_pitch_covers_the_six_open_guitar_strings(name, freq):
    hz, confidence = detect_pitch(_sine(freq), SR)
    assert hz == pytest.approx(freq, rel=0.01)
    assert confidence >= MIN_CONF


def test_detect_pitch_white_noise_is_low_confidence():
    # A fixed seed, not a hunt for a lucky one -- the module docstring's
    # own 500-seed sweep is the real evidence this threshold holds broadly;
    # this test just proves the wiring rejects a representative noisy
    # block, not that a single seed is special.
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(BLOCK_N) * 0.3).astype(np.float32)
    _hz, confidence = detect_pitch(noise, SR)
    assert confidence < MIN_CONF


def test_detect_pitch_silence_is_zero_confidence():
    _hz, confidence = detect_pitch(np.zeros(BLOCK_N, dtype=np.float32), SR)
    assert confidence == pytest.approx(0.0)


def test_detect_pitch_too_short_block_returns_no_pitch():
    hz, confidence = detect_pitch(np.zeros(4, dtype=np.float32), SR)
    assert hz == 0.0
    assert confidence == 0.0


def test_detect_pitch_empty_block_returns_no_pitch():
    hz, confidence = detect_pitch(np.zeros(0, dtype=np.float32), SR)
    assert hz == 0.0
    assert confidence == 0.0


def test_detect_pitch_uses_the_documented_default_range():
    assert TUNER_FMIN_HZ < 82.41   # covers guitar low E2 with margin
    assert TUNER_FMAX_HZ > 1000.0
    assert 0.0 < YIN_THRESHOLD < 1.0


# -- octave-error resistance (fix round, review finding 3) -------------------
#
# `detect_pitch`'s "first dip below threshold, scanning tau small-to-large"
# strategy (i.e. HIGH frequency toward LOW frequency) is what YIN's own
# design uses to avoid octave errors -- a real musical tone (fundamental +
# harmonics) is periodic at its fundamental's period T0 (harmonics are
# integer multiples of f0, so the WHOLE waveform repeats every T0), but
# generally NOT at T0/2 (the 2nd harmonic's own shorter period) UNLESS the
# fundamental's own contribution is negligible -- so the fundamental's tau
# is normally the SMALLEST lag that shows a clean CMNDF dip, correctly
# winning the small-to-large scan even though a harmonic's shorter lag is
# examined first. This section pins that behavior with real multi-
# harmonic synthetic content so a future change to the dip-selection logic
# can't silently regress it.
#
# Empirically-found boundary (disclosed, not fixed): this resistance holds
# up to roughly a 3:1 harmonic:fundamental amplitude ratio (verified below
# across four fundamentals and five random phase offsets) but breaks down
# at more extreme ratios -- a fundamental amplitude sweep against a fixed
# 0.5-amplitude 2nd harmonic (not shipped as a test, verified via a
# throwaway prototype) flips from correctly reporting the fundamental to
# reporting the 2nd harmonic somewhere between a 0.15 and 0.10 fundamental
# amplitude (roughly 3.3:1 to 5:1). Once the fundamental's own periodicity
# signature becomes THAT weak relative to the harmonic, the harmonic's own
# shorter-lag dip becomes the cleaner one and correctly wins per the same
# algorithm -- a known limitation of the YIN family generally at extreme
# harmonic dominance, not something this task's fix round changes.

def _harmonic_series(f0: float, amps: list[float], sr: int = SR, n: int = BLOCK_N) -> np.ndarray:
    t = np.arange(n) / sr
    x = np.zeros(n)
    for k, amp in enumerate(amps, start=1):
        x += amp * np.sin(2 * np.pi * f0 * k * t)
    return x.astype(np.float32)


def test_detect_pitch_fundamental_plus_realistic_harmonic_series_no_octave_error():
    # A realistic, dominant-fundamental instrument-like spectrum (each
    # harmonic weaker than the last) -- the ordinary, non-adversarial case.
    x = _harmonic_series(220.0, [0.5, 0.3, 0.15, 0.08])
    hz, confidence = detect_pitch(x, SR)
    assert hz == pytest.approx(220.0, abs=1.0)
    assert confidence >= MIN_CONF


@pytest.mark.parametrize("f0", [110.0, 146.83, 220.0, 330.0])
def test_detect_pitch_weak_fundamental_strong_second_harmonic_still_picks_fundamental(f0):
    # The adversarial case the brief's own fix-round review named
    # explicitly: a WEAK fundamental (0.2) against a STRONGER 2nd harmonic
    # (0.6, a 3:1 ratio) -- naive autocorrelation-style pitch detection is
    # prone to reporting the harmonic here (an "octave error", one octave
    # too high); this asserts the fundamental still wins, not `f0*2`.
    x = _harmonic_series(f0, [0.2, 0.6])
    hz, confidence = detect_pitch(x, SR)
    assert hz == pytest.approx(f0, abs=3.0)
    assert abs(hz - f0 * 2) > 3.0   # sanity: genuinely not the harmonic, not a loose tolerance overlap
    assert confidence >= MIN_CONF


def test_detect_pitch_weak_fundamental_strong_harmonic_resists_octave_error_across_phase():
    # Real signals aren't phase-locked between fundamental and harmonic --
    # proves the resistance above isn't a phase-alignment artifact.
    rng = np.random.default_rng(0)
    for _ in range(5):
        phase = rng.uniform(0, 2 * np.pi)
        t = np.arange(BLOCK_N) / SR
        x = (0.2 * np.sin(2 * np.pi * 220.0 * t)
             + 0.6 * np.sin(2 * np.pi * 440.0 * t + phase)).astype(np.float32)
        hz, confidence = detect_pitch(x, SR)
        assert hz == pytest.approx(220.0, abs=3.0)
        assert confidence >= MIN_CONF


# -- on_audio_block: the real production wiring (Phase 9 Task 3) -------------
#
# `on_audio_block(block, sr)` is what `AudioCapture`'s callback thread
# actually calls (mirrors `analyzers/spectrum.py::SpectrumAnalyzer.
# on_audio_block`'s own shape/name so the same `AudioCapture` class, built
# for spectrum, works here unmodified via duck typing -- see pages/tuner.py
# for the wiring). Computes dB via v1's own formula (`pages/tuner.py`'s
# `draw()`: `rms = sqrt(mean(x*x))`, `db = 20*log10(rms+1e-12)`), pitch via
# `detect_pitch()` above, then delegates into the SAME `on_pitch_sample`
# gate the older tests above already cover directly -- no new gating logic,
# just a new caller.

def test_on_audio_block_locks_a_note_from_a_loud_clean_sine():
    a = TunerAnalyzer()
    changed = a.on_audio_block(_sine(440.0, amp=0.5), SR)
    assert changed is True
    vm = a.view_model()
    assert vm["has_signal"] is True
    assert vm["note"] == "A4"
    assert vm["hz"] == pytest.approx(440.0, abs=1.0)
    assert vm["db"] > SILENCE_DB   # a 0.5-amplitude sine is well above -55dB


def test_on_audio_block_silent_block_shows_no_signal():
    a = TunerAnalyzer()
    a.on_audio_block(np.zeros(BLOCK_N, dtype=np.float32), SR)
    vm = a.view_model()
    assert vm["has_signal"] is False
    assert vm["note"] == ""
    assert vm["db"] < SILENCE_DB


def test_on_audio_block_db_matches_v1s_rms_formula():
    import math
    a = TunerAnalyzer()
    block = _sine(440.0, amp=0.5)
    a.on_audio_block(block, SR)
    rms = math.sqrt(float(np.mean(block.astype(np.float64) ** 2)))
    expected_db = 20.0 * math.log10(rms + 1e-12)
    assert a.view_model()["db"] == pytest.approx(expected_db, abs=0.1)


def test_on_audio_block_always_reports_dirty():
    a = TunerAnalyzer()
    assert a.on_audio_block(_sine(440.0), SR) is True
    assert a.on_audio_block(np.zeros(BLOCK_N, dtype=np.float32), SR) is True


def test_on_audio_block_quiet_signal_below_silence_db_shows_no_signal():
    # A real tone, but too quiet to pass v1's SILENCE_DB gate -- proves the
    # dB computed from the real block (not just an injected constant) is
    # what actually gates, not just confidence.
    a = TunerAnalyzer()
    a.on_audio_block(_sine(440.0, amp=0.0005), SR)
    vm = a.view_model()
    assert vm["db"] < SILENCE_DB
    assert vm["has_signal"] is False


# -- mark_available / mark_unavailable (Phase 9 Task 3, graceful audio- -----
# -- absent degrade, mirrors analyzers/spectrum.py::SpectrumAnalyzer) -------

def test_mark_available_sets_device_and_flag():
    a = TunerAnalyzer()
    a.mark_available("USB Audio Device")
    vm = a.view_model()
    assert vm["available"] is True
    assert vm["device"] == "USB Audio Device"


def test_mark_unavailable_clears_device_and_flag():
    a = TunerAnalyzer()
    a.mark_available("USB Audio Device")
    a.mark_unavailable()
    vm = a.view_model()
    assert vm["available"] is False
    assert vm["device"] is None
