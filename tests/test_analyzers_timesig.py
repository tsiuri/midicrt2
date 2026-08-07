"""TDD for TimesigAnalyzer: a pure state machine fed `MidiEvent`s (no I/O,
no clock reads -- all timing from `ev.ts`/`ev.clock_batch_start`). Ported
from v1's `~/codex/midicrt/plugins/ztimesig.py`; see analyzers/timesig.py's
module docstring for the tick-position reconstruction this requires (v2
only has one `clock_tick` event per BEAT, not v1's per-24-ppqn-pulse
counter) and for why `ztimesig_exp.py` (which ALSO runs live in v1,
correcting the task brief's "port the one v1 actually runs" assumption) is
not ported here.
"""
import pytest

from midicrt.analyzers.timesig import PPQN, TimesigAnalyzer, _gauss, _score_candidate
from midicrt.engine.core import MidiEvent


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def clock_tick(ts, batch_start, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="clock_tick", channel=None,
                      data1=24, data2=None, summary="clock_tick",
                      clock_batch_start=batch_start)


def note_on(ts, vel=100, ch0=0, note=60, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


# -- pure math (module-level functions, ported verbatim from v1) ------------

def test_gauss_peaks_at_zero_distance():
    assert _gauss(0, 4.0) == pytest.approx(1.0)
    assert _gauss(100, 4.0) < _gauss(1, 4.0)


def test_gauss_zero_sigma_is_zero():
    assert _gauss(0, 0.0) == 0.0


def test_score_candidate_empty_events_is_zero():
    assert _score_candidate([], 96, 24, 12, 4.0) == 0.0


def test_score_candidate_perfect_downbeat_alignment_scores_near_max():
    # Every onset exactly on a bar downbeat (phase 0 for offset 0): the
    # best achievable score approaches 1.0 + 0.6 + 0.25 (down+beat+sub, all
    # at gauss(0)=1) normalized by weight -- comfortably higher than a
    # candidate whose bar length the onsets do NOT align to.
    events = [(96 * k, 2.0) for k in range(8)]   # every onset at tick 0 mod 96
    aligned = _score_candidate(events, 96, 24, 12, 4.0)
    misaligned = _score_candidate([(t + 11, w) for t, w in events], 96, 24, 12, 4.0)
    assert aligned > misaligned


# -- initial state / gating ---------------------------------------------------

def test_initial_view_model_is_empty():
    a = TimesigAnalyzer()
    assert a.view_model() == {
        "labels": [], "confidence": 0.0, "events": 0,
        "events_window": 0, "events_total": 0, "pending": None,
    }


def test_note_on_before_start_is_ignored():
    a = TimesigAnalyzer()
    changed = a.handle(note_on(ts=0.0, vel=100))
    assert changed is False
    assert a.view_model()["events_total"] == 0


def test_clock_tick_alone_is_not_dirty():
    # v1's ztimesig.py never reacts to clock itself -- only note_on.
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is False


def test_start_resets_and_reports_dirty():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(note_on(ts=0.5, vel=100))
    changed = a.handle(transport("start", ts=10.0))
    assert changed is True
    assert a.view_model()["events_total"] == 0


def test_stop_when_already_stopped_is_not_dirty():
    a = TimesigAnalyzer()
    assert a.handle(transport("stop", ts=0.0)) is False


def test_stop_then_note_on_is_ignored_until_continue():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(transport("stop", ts=0.1))
    changed = a.handle(note_on(ts=0.2, vel=100))
    assert changed is False


def test_continue_resumes_without_resetting_event_history():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(note_on(ts=0.5, vel=100))
    a.handle(transport("stop", ts=0.6))
    before_total = a.view_model()["events_total"]
    changed = a.handle(transport("continue", ts=5.0))
    assert changed is True
    assert a.view_model()["events_total"] == before_total   # unchanged, unlike "start"


def test_continue_when_already_running_is_not_dirty():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    assert a.handle(transport("continue", ts=0.1)) is False


def test_velocity_zero_note_on_is_ignored():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    changed = a.handle(note_on(ts=0.5, vel=0))
    assert changed is False
    assert a.view_model()["events_total"] == 0


def test_handle_never_reads_a_clock_or_does_io():
    # Structural guard mirroring test_analyzers_transport.py's own version:
    # timestamps far from real wall time must not matter.
    a = TimesigAnalyzer()
    base = 10_000_000.0
    a.handle(transport("start", ts=base))
    changed = a.handle(clock_tick(ts=base + 0.5, batch_start=None))
    assert changed is False
    changed = a.handle(note_on(ts=base + 0.5, vel=100))
    assert changed is True
    assert a.view_model()["events_total"] == 1


# -- tick-position reconstruction (see module docstring) ---------------------

def test_note_exactly_on_a_beat_boundary_projects_to_tick_zero_of_that_beat():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))    # beat 1 completes at ts=0.5
    a.handle(clock_tick(ts=1.0, batch_start=0.5))     # beat 2 completes at ts=1.0, 0.5s/beat
    # A note exactly at the ts=1.0 boundary (start of beat 3) -> tick_in_beat 0,
    # beats_elapsed=2 -> global tick 48.
    a.handle(note_on(ts=1.0, vel=100))
    assert a._events[-1][0] == 2 * PPQN


def test_note_at_quarter_beat_projects_to_quarter_tick():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(clock_tick(ts=1.0, batch_start=0.5))     # last beat duration = 0.5s
    a.handle(note_on(ts=1.0 + 0.125, vel=100))         # 1/4 of the way into the next beat
    tick = a._events[-1][0]
    assert tick == 2 * PPQN + round(0.25 * PPQN)


def test_note_before_any_beat_boundary_projects_to_tick_zero():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(note_on(ts=0.3, vel=100))   # no clock_tick observed yet
    assert a._events[-1][0] == 0


def test_collapse_same_tick_merges_chord_notes_into_one_event():
    a = TimesigAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))
    a.handle(note_on(ts=0.5, vel=60))
    a.handle(note_on(ts=0.5, vel=127))   # same reconstructed tick -- a chord
    assert len(a._events) == 1
    assert a.view_model()["events_total"] == 1


# -- end-to-end convergence to a known meter ----------------------------------

def _feed_downbeat_pattern(a, beats_per_bar, n_bars, beat_s=0.11):
    """Feed a click track (one clock_tick per beat) with a strong note
    landing EXACTLY on every `beats_per_bar`-th beat boundary -- the
    sparsest possible pattern that unambiguously identifies a bar length
    (see analyzers/timesig.py's module docstring: an isochronous
    every-beat pulse cannot discriminate bar length at all, since the
    per-beat term scores identically for every quarter-based candidate;
    only genuine downbeat-spaced accents do)."""
    a.handle(transport("start", ts=0.0))
    prev_ts = None
    beat_i = 0
    for _bar in range(n_bars):
        for b in range(beats_per_bar):
            beat_i += 1
            ts = beat_i * beat_s
            a.handle(clock_tick(ts=ts, batch_start=prev_ts))
            prev_ts = ts
            if b == 0:
                a.handle(note_on(ts=ts, vel=127))


def test_converges_to_5_4_on_a_pure_downbeat_every_five_beats():
    a = TimesigAnalyzer()
    _feed_downbeat_pattern(a, beats_per_bar=5, n_bars=18)   # 18 downbeat onsets
    vm = a.view_model()
    assert "5/4" in vm["labels"]
    assert vm["confidence"] >= 0.35
    assert vm["events_total"] == 18


def test_converges_to_3_4_on_a_pure_downbeat_every_three_beats():
    a = TimesigAnalyzer()
    _feed_downbeat_pattern(a, beats_per_bar=3, n_bars=18)
    vm = a.view_model()
    assert "3/4" in vm["labels"]


def test_events_window_and_total_reflect_the_ported_windowing_fields():
    a = TimesigAnalyzer()
    _feed_downbeat_pattern(a, beats_per_bar=4, n_bars=14)
    vm = a.view_model()
    assert vm["events_total"] == 14
    assert vm["events"] <= vm["events_total"]
    assert vm["events_window"] <= vm["events"]
