"""TDD for BeatFlashAnalyzer: a pure state machine fed `MidiEvent`s + an
injected wall-clock `tick(now)` (no I/O, no clock reads inside `handle` --
see analyzers/beatflash.py's module docstring for the full v1 comparison
and the disclosed v2 additions: continuous decay instead of a binary flag,
and a real bar/beat distinction v1 never had).
"""
import pytest

from midicrt.analyzers.beatflash import (
    BAR_PEAK,
    BEAT_PEAK,
    FLASH_DURATION_S,
    BeatFlashAnalyzer,
)
from midicrt.engine.core import MidiEvent


def clock_tick(ts, batch_start=None, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="clock_tick", channel=None,
                      data1=24, data2=None, summary="clock_tick",
                      clock_batch_start=batch_start)


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def test_initial_view_model_is_dark():
    a = BeatFlashAnalyzer()
    assert a.view_model() == {"intensity": 0.0, "is_bar": False}


def test_clock_tick_before_any_start_is_ignored():
    a = BeatFlashAnalyzer()
    changed = a.handle(clock_tick(ts=1.0))
    assert changed is False
    assert a.view_model() == {"intensity": 0.0, "is_bar": False}


def test_first_beat_after_start_flashes_at_beat_peak_not_bar():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is True
    vm = a.view_model()
    assert vm["intensity"] == pytest.approx(BEAT_PEAK)
    assert vm["is_bar"] is False


def test_fourth_beat_is_a_bar_flash_at_the_higher_peak():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    ts = 0.0
    prev = None
    for _ in range(4):
        ts += 0.5
        a.handle(clock_tick(ts=ts, batch_start=prev))
        prev = ts
    vm = a.view_model()
    assert vm["is_bar"] is True
    assert vm["intensity"] == pytest.approx(BAR_PEAK)
    assert BAR_PEAK > BEAT_PEAK   # "stronger on bar" per the task brief


def test_tick_decays_intensity_linearly_toward_zero():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.0, batch_start=None))
    assert a.view_model()["intensity"] == pytest.approx(BEAT_PEAK)

    half = FLASH_DURATION_S / 2.0
    changed = a.tick(half)
    assert changed is True
    assert a.view_model()["intensity"] == pytest.approx(BEAT_PEAK * 0.5, abs=1e-6)


def test_tick_reaches_exactly_zero_at_the_full_duration_and_stays_dirty_once():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.0, batch_start=None))

    changed = a.tick(FLASH_DURATION_S)
    assert changed is True
    assert a.view_model()["intensity"] == 0.0

    # Fully decayed -- a later tick with no new beat must not re-report dirty.
    changed_again = a.tick(FLASH_DURATION_S + 1.0)
    assert changed_again is False


def test_tick_with_no_flash_ever_started_is_not_dirty():
    a = BeatFlashAnalyzer()
    assert a.tick(100.0) is False


def test_stop_does_not_freeze_an_in_progress_flash_unlike_v1():
    # v1's own draw() early-returns before its turn-off check when the
    # transport isn't running, so a flash active at the moment of a stop
    # visually freezes forever -- disclosed as a v1 quirk, not reproduced
    # here (see module docstring): decay is purely wall-clock driven and
    # keeps fading out through a stop.
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.0, batch_start=None))
    a.handle(transport("stop", ts=0.01))
    changed = a.tick(FLASH_DURATION_S)
    assert changed is True
    assert a.view_model()["intensity"] == 0.0


def test_second_start_clears_beat_counter_and_any_active_flash():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.0, batch_start=None))
    assert a.view_model()["intensity"] > 0.0

    changed = a.handle(transport("start", ts=10.0))
    assert changed is True
    assert a.view_model() == {"intensity": 0.0, "is_bar": False}
    # The beat counter restarts too -- the next tick after a fresh start is
    # beat 1 of bar 0 again, not a bar flash.
    changed = a.handle(clock_tick(ts=10.5, batch_start=None))
    assert changed is True
    assert a.view_model()["is_bar"] is False


def test_unrelated_event_types_are_a_pure_no_op():
    a = BeatFlashAnalyzer()
    note_on = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                        data1=60, data2=100, summary="note_on ch1 n60 v100")
    changed = a.handle(note_on)
    assert changed is False
    assert a.view_model() == {"intensity": 0.0, "is_bar": False}


def test_handle_never_reads_a_clock_or_does_io():
    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=10_000_000.0))
    changed = a.handle(clock_tick(ts=10_000_000.5, batch_start=None))
    assert changed is True
    assert a.view_model()["intensity"] == pytest.approx(BEAT_PEAK)
