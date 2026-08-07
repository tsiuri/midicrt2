"""TDD for TunerAnalyzer: pure pitch-reading math + smoothing/gating state
machine, ported from v1's `~/codex/midicrt/pages/tuner.py`. Unlike every
other v2 analyzer, this one is NOT MIDI-driven -- see analyzers/tuner.py's
module docstring for why (v1's tuner has no MIDI handler at all, and the
audio-capture pipeline that would feed it real pitch samples is a separate,
not-yet-ported v2 task). Tests exercise the injected-data
`on_pitch_sample()` method with synthetic pitch-detector readings, standing
in for that future audio path.
"""
import pytest

from midicrt.analyzers.tuner import MIN_CONF, SILENCE_DB, TunerAnalyzer, freq_to_note, tuning_meter
from midicrt.engine.core import MidiEvent


def test_initial_view_model_shows_no_signal():
    a = TunerAnalyzer()
    vm = a.view_model()
    assert vm == {"note": "", "cents": 0.0, "hz": 0.0, "confidence": 0.0,
                  "db": -120.0, "has_signal": False}


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
    import midicrt.analyzers.tuner as mod
    assert not hasattr(mod, "time")
    assert not hasattr(mod, "aubio")
