"""TDD for LoopProgressAnalyzer: a pure state machine fed `MidiEvent`s (no
I/O, no clock reads -- see analyzers/loopprogress.py's module docstring for
the v1 comparison and the disclosed "scheduler/sysex status text" omission).
"""
import pytest

from midicrt.analyzers.loopprogress import TOTAL_BEATS, LoopProgressAnalyzer
from midicrt.engine.core import MidiEvent


def clock_tick(ts, batch_start=None, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="clock_tick", channel=None,
                      data1=24, data2=None, summary="clock_tick",
                      clock_batch_start=batch_start)


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def test_initial_view_model_is_zero_and_stopped():
    a = LoopProgressAnalyzer()
    assert a.view_model() == {"fraction": 0.0, "running": False}


def test_total_beats_is_eight_bars_of_four():
    assert TOTAL_BEATS == 32   # v1's TOTAL_BARS=8 * BEATS_PER_BAR=4


def test_clock_tick_before_any_start_is_ignored():
    a = LoopProgressAnalyzer()
    changed = a.handle(clock_tick(ts=1.0))
    assert changed is False
    assert a.view_model() == {"fraction": 0.0, "running": False}


def test_start_marks_running_and_resets_fraction():
    a = LoopProgressAnalyzer()
    changed = a.handle(transport("start", ts=0.0))
    assert changed is True
    assert a.view_model() == {"fraction": 0.0, "running": True}


def test_fraction_advances_one_thirty_second_per_beat():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is True
    assert a.view_model()["fraction"] == pytest.approx(1.0 / TOTAL_BEATS)


def test_fraction_wraps_after_a_full_eight_bar_loop():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    ts = 0.0
    prev = None
    for _ in range(TOTAL_BEATS):
        ts += 0.5
        a.handle(clock_tick(ts=ts, batch_start=prev))
        prev = ts
    assert a.view_model()["fraction"] == pytest.approx(0.0)   # wrapped exactly

    ts += 0.5
    a.handle(clock_tick(ts=ts, batch_start=prev))
    assert a.view_model()["fraction"] == pytest.approx(1.0 / TOTAL_BEATS)


def test_stop_freezes_fraction_and_reports_not_running():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    before = a.view_model()["fraction"]
    changed = a.handle(transport("stop", ts=0.6))
    assert changed is True
    vm = a.view_model()
    assert vm["running"] is False
    assert vm["fraction"] == pytest.approx(before)


def test_stop_when_already_stopped_is_not_dirty():
    a = LoopProgressAnalyzer()
    changed = a.handle(transport("stop", ts=0.0))
    assert changed is False


def test_clock_after_stop_is_ignored_until_continue_or_start():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(transport("stop", ts=0.6))
    before = a.view_model()
    changed = a.handle(clock_tick(ts=1.0, batch_start=0.5))
    assert changed is False
    assert a.view_model() == before


def test_continue_resumes_without_resetting_fraction():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(transport("stop", ts=0.6))
    before = a.view_model()["fraction"]
    changed = a.handle(transport("continue", ts=5.0))
    assert changed is True
    vm = a.view_model()
    assert vm["running"] is True
    assert vm["fraction"] == pytest.approx(before)


def test_continue_when_already_running_is_not_dirty():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(transport("continue", ts=0.1))
    assert changed is False


def test_second_start_resets_fraction_to_zero():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    changed = a.handle(transport("start", ts=10.0))
    assert changed is True
    assert a.view_model() == {"fraction": 0.0, "running": True}


def test_unrelated_event_types_are_a_pure_no_op():
    a = LoopProgressAnalyzer()
    note_on = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                        data1=60, data2=100, summary="note_on ch1 n60 v100")
    changed = a.handle(note_on)
    assert changed is False
    assert a.view_model() == {"fraction": 0.0, "running": False}


def test_handle_never_reads_a_clock_or_does_io():
    a = LoopProgressAnalyzer()
    a.handle(transport("start", ts=10_000_000.0))
    changed = a.handle(clock_tick(ts=10_000_000.5, batch_start=None))
    assert changed is True
    assert a.view_model()["fraction"] == pytest.approx(1.0 / TOTAL_BEATS)
