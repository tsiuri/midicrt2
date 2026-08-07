"""TDD for TransportAnalyzer: a pure state machine fed `MidiEvent`s (no I/O,
no clock reads -- see analyzers/transport.py's module docstring for why bpm
is derived from `clock_tick.clock_batch_start`/`.ts`, not a wall-clock read).

Synthetic clock streams below construct `clock_tick` MidiEvents directly at
known BPMs (e.g. 120 BPM -> 0.5s/beat -> a 24-clock batch spans exactly
0.5s) rather than emitting 24 individual "clock" messages through
`midi_in.py` -- that aggregation is `midi_in.py`'s job and is covered by
tests/test_midi_in.py; these tests are the analyzer's own math contract in
isolation.
"""
import pytest

from midicrt.analyzers.transport import TransportAnalyzer
from midicrt.engine.core import MidiEvent


def clock_tick(ts, batch_start, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="clock_tick", channel=None,
                      data1=24, data2=None, summary="clock_tick",
                      clock_batch_start=batch_start)


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def test_initial_view_model_is_all_unknown():
    a = TransportAnalyzer()
    assert a.view_model() == {
        "bpm": None, "bar": 0, "beat": 1, "running": False, "source": None,
    }


def test_clock_tick_before_any_start_is_ignored_like_v1_tempo_map():
    # v1's TempoMap.handle("clock") returns immediately when not running --
    # a free-running clock source with no transport start must not advance
    # bar/beat or fabricate a bpm.
    a = TransportAnalyzer()
    changed = a.handle(clock_tick(ts=1.0, batch_start=0.5))
    assert changed is False
    assert a.view_model() == {
        "bpm": None, "bar": 0, "beat": 1, "running": False, "source": None,
    }


def test_start_sets_running_and_reports_dirty():
    a = TransportAnalyzer()
    changed = a.handle(transport("start", ts=0.0, source="USB MIDI"))
    assert changed is True
    vm = a.view_model()
    assert vm["running"] is True
    assert vm["bar"] == 0 and vm["beat"] == 1 and vm["bpm"] is None
    assert vm["source"] == "USB MIDI"


def test_first_clock_tick_after_start_has_no_prior_boundary_bpm_stays_none():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is True             # beat advanced even though bpm is unknown
    vm = a.view_model()
    assert vm["bpm"] is None
    assert vm["bar"] == 0 and vm["beat"] == 2


def test_bpm_is_exact_for_a_120bpm_synthetic_stream():
    # 120 BPM -> one quarter note every 0.5s -> a 24-clock batch (one beat)
    # spans exactly 0.5s. bpm = 60 / batch_span.
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    changed = a.handle(clock_tick(ts=1.0, batch_start=0.5))
    assert changed is True
    assert a.view_model()["bpm"] == pytest.approx(120.0)


def test_bpm_is_exact_for_a_90bpm_synthetic_stream():
    # 90 BPM -> 60/90 = 0.6667s per beat.
    beat_s = 60.0 / 90.0
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    prev = None
    ts = 0.0
    for _ in range(3):
        ts += beat_s
        a.handle(clock_tick(ts=ts, batch_start=prev))
        prev = ts
    assert a.view_model()["bpm"] == pytest.approx(90.0)


def test_bar_and_beat_wrap_every_four_beats_one_indexed():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    prev = None
    ts = 0.0
    for i in range(1, 10):
        ts += 0.5
        a.handle(clock_tick(ts=ts, batch_start=prev))
        prev = ts
        vm = a.view_model()
        expected_bar, expected_beat = divmod(i, 4)
        assert vm["bar"] == expected_bar
        assert vm["beat"] == expected_beat + 1


def test_stop_clears_running_but_preserves_last_bar_beat_bpm():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(clock_tick(ts=1.0, batch_start=0.5))
    before = a.view_model()
    changed = a.handle(transport("stop", ts=1.1))
    assert changed is True
    after = a.view_model()
    assert after["running"] is False
    assert after["bar"] == before["bar"] and after["beat"] == before["beat"]
    assert after["bpm"] == before["bpm"]


def test_stop_when_already_stopped_is_not_dirty():
    a = TransportAnalyzer()
    changed = a.handle(transport("stop", ts=0.0))
    assert changed is False


def test_clock_after_stop_is_ignored_until_continue_or_start():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(transport("stop", ts=0.6))
    before = a.view_model()
    changed = a.handle(clock_tick(ts=1.0, batch_start=0.5))
    assert changed is False
    assert a.view_model() == before


def test_continue_resumes_running_without_resetting_bar_beat():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(clock_tick(ts=1.0, batch_start=0.5))
    a.handle(transport("stop", ts=1.1))
    changed = a.handle(transport("continue", ts=5.0))
    assert changed is True
    vm = a.view_model()
    assert vm["running"] is True
    assert vm["bar"] == 0 and vm["beat"] == 3   # unchanged from before the stop


def test_continue_when_already_running_is_not_dirty():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(transport("continue", ts=0.1))
    assert changed is False


def test_second_start_resets_bar_beat_and_bpm():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(clock_tick(ts=1.0, batch_start=0.5))
    changed = a.handle(transport("start", ts=10.0))
    assert changed is True
    vm = a.view_model()
    assert vm["running"] is True
    assert vm["bar"] == 0 and vm["beat"] == 1 and vm["bpm"] is None


def test_source_tracks_the_most_recent_transport_relevant_event():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0, source="Port A"))
    assert a.view_model()["source"] == "Port A"
    a.handle(clock_tick(ts=0.5, batch_start=None, source="Port B"))
    assert a.view_model()["source"] == "Port B"


def test_songpos_updates_source_but_no_other_field():
    a = TransportAnalyzer()
    a.handle(transport("start", ts=0.0, source="Port A"))
    before = a.view_model()
    changed = a.handle(transport("songpos", ts=0.2, source="Port C"))
    assert changed is True
    vm = a.view_model()
    assert vm["source"] == "Port C"
    assert vm["bar"] == before["bar"] and vm["beat"] == before["beat"]
    assert vm["running"] == before["running"] and vm["bpm"] == before["bpm"]


def test_unrelated_event_types_are_a_pure_no_op():
    a = TransportAnalyzer()
    note_on = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                        data1=60, data2=100, summary="note_on ch1 n60 v100")
    changed = a.handle(note_on)
    assert changed is False
    assert a.view_model() == {
        "bpm": None, "bar": 0, "beat": 1, "running": False, "source": None,
    }


def test_handle_never_reads_a_clock_or_does_io():
    # Structural guard against regression toward v1's "read wall time in the
    # analyzer" pattern: passing an event whose ev.ts is far from real time
    # must not matter -- the analyzer must only ever compare event
    # timestamps to each other, never to time.time()/time.monotonic().
    a = TransportAnalyzer()
    a.handle(transport("start", ts=10_000_000.0))
    a.handle(clock_tick(ts=10_000_000.5, batch_start=None))
    changed = a.handle(clock_tick(ts=10_000_001.0, batch_start=10_000_000.5))
    assert changed is True
    assert a.view_model()["bpm"] == pytest.approx(120.0)
