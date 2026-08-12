"""TDD for PianorollState/PianorollPage: v1's flagship two-projection-mode
scrolling note display -- see pages/pianoroll.py's module docstring for the
full port notes (storage substrate, tempo-relative math derivation, VM
mapping, deliberately-not-ported list).

Synthetic events below construct MidiEvents directly at known real
timestamps (mirroring tests/test_analyzers_transport.py's own convention)
rather than going through engine/midi_in.py -- these are the state
machine's own math contract in isolation.
"""
from itertools import pairwise

import pytest

from midicrt.engine.core import MidiEvent
from midicrt.pages.pianoroll import (
    _ROLL_ROW_FADE_S,
    ZOOM_MAX,
    ZOOM_MIN,
    PianorollPage,
    PianorollState,
    _flash_mult,
    _overlap_regions_for_row,
    _parse_channel_spec,
)


def note_on(ch0, note, vel=100, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def note_off(ch0, note, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_off", channel=ch0,
                      data1=note, data2=0, summary=f"note_off ch{ch0 + 1} n{note}")


def cc(ch0, control, value=0, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="control_change", channel=ch0,
                      data1=control, data2=value, summary=f"control_change ch{ch0 + 1} cc{control}")


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def clock_tick(ts, batch_start, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="clock_tick", channel=None,
                      data1=24, data2=None, summary="clock_tick",
                      clock_batch_start=batch_start)


# -- basic note lifecycle -----------------------------------------------------

def test_initial_view_model_is_empty():
    s = PianorollState(now=100.0)
    vm = s.view_model()
    assert vm["title"] == "PIANOROLL"
    assert vm["notes"] == []
    assert vm["range"] == {"lo": 36, "hi": 83}
    # Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9): default
    # flipped from "wallclock" to "tempo" -- v1-parity fix, see pages/
    # pianoroll.py's own __init__ comment for the full file:line evidence.
    assert vm["window"]["mode"] == "tempo"
    assert vm["window"]["zoom"] == 1.0


def test_note_on_off_produces_a_span_in_wallclock_mode():
    s = PianorollState(now=100.0, span_s=8.0)
    changed = s.handle(note_on(0, 60, vel=100, ts=98.0))
    assert changed is True
    changed = s.handle(note_off(0, 60, ts=99.0))
    assert changed is True

    vm = s.view_model()
    assert len(vm["notes"]) == 1
    n = vm["notes"][0]
    assert n["ch"] == 1
    assert n["active"] is False
    assert n["vel"] == pytest.approx(100 / 127)
    # onset 2s ago -> x0 = 1 - 2/8 = 0.75; release 1s ago -> x1 = 1 - 1/8 = 0.875
    assert n["x0"] == pytest.approx(0.75)
    assert n["x1"] == pytest.approx(0.875)


def test_still_held_note_extends_to_now():
    s = PianorollState(now=100.0, span_s=8.0)
    s.handle(note_on(0, 60, ts=96.0))
    vm = s.view_model()
    n = vm["notes"][0]
    assert n["active"] is True
    assert n["x1"] == pytest.approx(1.0)
    assert n["x0"] == pytest.approx(1 - 4 / 8)


def test_note_on_velocity_zero_is_treated_as_note_off():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, vel=100, ts=95.0))
    changed = s.handle(note_on(0, 60, vel=0, ts=96.0))
    assert changed is True
    assert s.view_model()["notes"][0]["active"] is False


def test_stray_note_off_is_a_true_no_op():
    s = PianorollState(now=100.0)
    changed = s.handle(note_off(0, 60, ts=99.0))
    assert changed is False
    assert s.view_model()["notes"] == []


def test_retrigger_closes_the_previous_span_first():
    s = PianorollState(now=100.0, span_s=8.0)
    s.handle(note_on(0, 60, vel=90, ts=95.0))
    changed = s.handle(note_on(0, 60, vel=110, ts=97.0))
    assert changed is True
    vm = s.view_model()
    assert len(vm["notes"]) == 2
    closed = next(n for n in vm["notes"] if not n["active"])
    active = next(n for n in vm["notes"] if n["active"])
    assert closed["x1"] == pytest.approx(active["x0"])   # retrigger point is shared
    assert active["vel"] == pytest.approx(110 / 127)


def test_pitch_outside_range_is_dropped():
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    s.handle(note_on(0, 30, ts=99.0))     # below range
    s.handle(note_on(0, 90, ts=99.0))     # above range
    s.handle(note_on(0, 66, ts=99.0))     # inside range
    vm = s.view_model()
    assert len(vm["notes"]) == 1


def test_channel_filter_drops_hidden_channels():
    s = PianorollState(now=100.0)
    s.set_channels("1,3")
    s.handle(note_on(0, 60, ts=99.0))   # channel 1 -- visible
    s.handle(note_on(1, 62, ts=99.0))   # channel 2 -- hidden
    s.handle(note_on(2, 64, ts=99.0))   # channel 3 -- visible
    vm = s.view_model()
    assert sorted(n["ch"] for n in vm["notes"]) == [1, 3]


def test_note_scrolled_entirely_past_the_window_is_dropped():
    s = PianorollState(now=100.0, span_s=4.0)
    s.handle(note_on(0, 60, ts=90.0))
    s.handle(note_off(0, 60, ts=91.0))   # both ends 9s/10s ago, span is 4s
    assert s.view_model()["notes"] == []


def test_pitch_y_is_zero_at_top_for_the_highest_pitch():
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    s.handle(note_on(0, 72, ts=99.0))
    s.handle(note_on(0, 60, ts=99.0))
    vm = s.view_model()
    by_pitch = {n["y"]: True for n in vm["notes"]}
    ys = sorted(n["y"] for n in vm["notes"])
    assert ys[0] == pytest.approx(0.0)   # highest pitch (72) at the top
    assert ys[1] == pytest.approx(1.0)   # lowest pitch (60) at the bottom
    assert by_pitch  # sanity, avoids unused-var lint on the dict


# -- CC123 / transport state machine (v1 pianoroll_state.py parity) ----------

def test_cc123_clears_only_that_channel():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=99.0))   # channel 1
    s.handle(note_on(1, 62, ts=99.0))   # channel 2
    changed = s.handle(cc(0, 123, ts=99.5))
    assert changed is True
    vm = s.view_model()
    assert len(vm["notes"]) == 2   # ch1 closed span + ch2 still active
    ch1 = next(n for n in vm["notes"] if n["ch"] == 1)
    ch2 = next(n for n in vm["notes"] if n["ch"] == 2)
    assert ch1["active"] is False
    assert ch2["active"] is True


def test_cc120_is_ignored_a_v1_quirk_preserved_faithfully():
    # v1's pianoroll_state.py ONLY reacts to CC123, unlike voices.py/
    # stucknotes.py's {120, 123} -- see module docstring's "Ported v1
    # controls" section.
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=99.0))
    changed = s.handle(cc(0, 120, ts=99.5))
    assert changed is False
    assert s.view_model()["notes"][0]["active"] is True


def test_cc123_on_an_empty_channel_is_a_true_no_op():
    s = PianorollState(now=100.0)
    assert s.handle(cc(0, 123, ts=99.0)) is False


def test_start_resets_active_and_history_and_bpm():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=95.0))
    s.handle(note_off(0, 60, ts=96.0))
    s.handle(note_on(0, 64, ts=97.0))   # still active
    changed = s.handle(transport("start", ts=99.0))
    assert changed is True
    assert s.view_model()["notes"] == []


def test_stop_closes_active_notes_but_keeps_closed_history():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=95.0))
    s.handle(note_off(0, 60, ts=96.0))    # already-closed history
    s.handle(note_on(0, 64, ts=97.0))     # still active
    changed = s.handle(transport("stop", ts=99.0))
    assert changed is True
    vm = s.view_model()
    assert len(vm["notes"]) == 2
    assert all(not n["active"] for n in vm["notes"])


def test_stop_with_nothing_active_is_not_dirty():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=90.0))
    s.handle(note_off(0, 60, ts=91.0))
    changed = s.handle(transport("stop", ts=99.0))
    assert changed is False


def test_continue_is_always_a_true_no_op():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=99.0))
    changed = s.handle(transport("continue", ts=99.5))
    assert changed is False
    assert s.view_model()["notes"][0]["active"] is True   # untouched


# -- clock_tick / current-bpm derivation --------------------------------------

def test_clock_tick_before_any_start_is_ignored():
    s = PianorollState(now=100.0)
    s.set_projection("tempo")
    changed = s.handle(clock_tick(ts=1.0, batch_start=0.5))
    assert changed is False


def test_clock_tick_first_batch_after_start_has_no_prior_boundary():
    s = PianorollState(now=100.0)
    s.set_projection("tempo")
    s.handle(transport("start", ts=0.0))
    changed = s.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is False   # no bpm derivable yet -- true no-op


def test_clock_tick_derives_bpm_matching_transport_analyzer_formula():
    s = PianorollState(now=100.0)
    s.set_projection("tempo")
    s.handle(transport("start", ts=0.0))
    # 0.5s per beat -> 120 bpm, exactly analyzers/transport.py's own formula.
    changed = s.handle(clock_tick(ts=0.5, batch_start=0.0))
    assert changed is True
    assert s._current_bpm() == pytest.approx(120.0)


def test_clock_tick_only_dirties_in_tempo_mode():
    # wallclock mode's geometry is bpm-independent -- see module docstring's
    # disclosed "cosmetic span_beats lag" trade-off. Explicit set_projection
    # (Phase 10 Task A flipped the DEFAULT to "tempo" -- see pages/
    # pianoroll.py's own __init__ comment) since this test's whole point is
    # wallclock's own dirty-gating, not whatever the default happens to be.
    s = PianorollState(now=100.0)
    s.set_projection("wallclock")
    s.handle(transport("start", ts=0.0))
    changed = s.handle(clock_tick(ts=0.5, batch_start=0.0))
    assert changed is False
    assert s._current_bpm() == pytest.approx(120.0)   # still derived internally


def test_idle_bpm_fallback_before_any_clock_tick():
    s = PianorollState(now=100.0, idle_bpm=90.0)
    assert s._current_bpm() == pytest.approx(90.0)


# -- tempo-relative squish/stretch (ported from v1's
# tests/test_memory_tempo_projection.py acceptance criteria) -----------------

def _tempo_state_with_note_3s_ago(bpm: float) -> PianorollState:
    s = PianorollState(now=100.0, span_beats=32.0)
    s.set_projection("tempo")
    s.handle(transport("start", ts=0.0))
    period = 60.0 / bpm
    s.handle(clock_tick(ts=period, batch_start=0.0))   # establishes current_bpm
    s.handle(note_on(0, 60, ts=97.0))    # onset 3 real seconds before "now"=100.0
    s.handle(note_off(0, 60, ts=98.0))   # release 2 real seconds before "now"
    return s


def test_tempo_mode_higher_current_bpm_pushes_history_further_from_now():
    # v1 acceptance criterion: "increasing current BPM should visibly
    # stretch earlier notes" -- a higher current bpm must push a FIXED
    # real-time-ago event further from x=1.0 ("now").
    slow = _tempo_state_with_note_3s_ago(bpm=60.0)
    fast = _tempo_state_with_note_3s_ago(bpm=240.0)
    x0_slow = slow.view_model()["notes"][0]["x0"]
    x0_fast = fast.view_model()["notes"][0]["x0"]
    assert x0_fast < x0_slow


def test_tempo_mode_lower_current_bpm_squishes_history_toward_now():
    # v1 acceptance criterion: "decreasing current BPM should visibly
    # squish earlier notes" -- restated as the same fact from the other
    # direction, with an explicit width comparison (mirrors v1's own
    # `_project_span_width_ticks`-based test more literally: the covered
    # beat-distance between onset and release shrinks as bpm drops).
    def _width(bpm: float) -> float:
        s = _tempo_state_with_note_3s_ago(bpm=bpm)
        n = s.view_model()["notes"][0]
        return n["x1"] - n["x0"]

    width_slow_bpm = _width(30.0)
    width_fast_bpm = _width(300.0)
    assert width_slow_bpm < width_fast_bpm   # squished vs stretched


def test_wallclock_mode_is_unaffected_by_current_bpm():
    # Explicit set_projection: Phase 10 Task A flipped the DEFAULT to
    # "tempo" (pages/pianoroll.py's own __init__ comment) -- this test's
    # name/point is wallclock's own bpm-independence, so it must pin the
    # mode explicitly rather than lean on whatever the default is.
    def _x0(bpm: float) -> float:
        s = PianorollState(now=100.0, span_s=8.0)
        s.set_projection("wallclock")
        s.handle(transport("start", ts=0.0))
        s.handle(clock_tick(ts=60.0 / bpm, batch_start=0.0))
        s.handle(note_on(0, 60, ts=97.0))
        return s.view_model()["notes"][0]["x0"]

    assert _x0(60.0) == pytest.approx(_x0(240.0))


# -- zoom ----------------------------------------------------------------

def test_zoom_by_narrows_the_effective_wallclock_span():
    s = PianorollState(now=100.0, span_s=8.0)
    base_span = s._effective_span_s()
    s.zoom_by(1.0)   # zoom 1.0 -> 2.0
    assert s._effective_span_s() == pytest.approx(base_span / 2)


def test_zoom_by_clamps_to_min_and_max():
    s = PianorollState(now=100.0)
    assert s.zoom_by(-100.0) == pytest.approx(0.25)
    assert s.zoom_by(100.0) == pytest.approx(4.0)


def test_zoom_reflected_in_window_vm():
    s = PianorollState(now=100.0)
    s.zoom_by(1.0)
    assert s.view_model()["window"]["zoom"] == pytest.approx(2.0)


# -- absolute zoom (review finding, Important): a continuous MIDI binding
# needs an ABSOLUTE setter, not `zoom_by`'s cumulative delta -- see
# engine/bindings.py's module docstring ("Trigger vs continuous") for the
# live-reproduced saturation bug this fixes. `set_zoom_level` sets the zoom
# to EXACTLY the given value (clamped), regardless of the current zoom or
# how many prior calls there were -- unlike `zoom_by`, which always adds to
# whatever the zoom already is.

def test_set_zoom_level_sets_the_exact_absolute_value():
    s = PianorollState(now=100.0)
    assert s.set_zoom_level(2.5) == pytest.approx(2.5)
    assert s.view_model()["window"]["zoom"] == pytest.approx(2.5)


def test_set_zoom_level_is_not_cumulative_across_repeated_calls():
    s = PianorollState(now=100.0)
    s.set_zoom_level(2.0)
    s.set_zoom_level(2.0)   # same level again -- must NOT stack like zoom_by would
    assert s.view_model()["window"]["zoom"] == pytest.approx(2.0)
    s.set_zoom_level(1.0)   # a lower level after a higher one -- must jump straight there
    assert s.view_model()["window"]["zoom"] == pytest.approx(1.0)


def test_set_zoom_level_clamps_to_min_and_max():
    s = PianorollState(now=100.0)
    assert s.set_zoom_level(-5.0) == pytest.approx(ZOOM_MIN)
    assert s.set_zoom_level(999.0) == pytest.approx(ZOOM_MAX)


# -- pitch-window pan (Phase 10 Task A, docs/demo-feedback-2026-08-12.md
# item 11: v1's UP/DOWN arrow pitch-window pan, previously a disclosed gap) --

def test_pan_by_shifts_the_pitch_window_up_preserving_its_size():
    s = PianorollState(now=100.0)   # default range 36..83, size 47
    result = s.pan_by(1)
    assert result == {"lo": 37, "hi": 84}
    assert s.view_model()["range"] == {"lo": 37, "hi": 84}


def test_pan_by_shifts_the_pitch_window_down_preserving_its_size():
    s = PianorollState(now=100.0)
    result = s.pan_by(-1)
    assert result == {"lo": 35, "hi": 82}


def test_pan_by_repeated_calls_accumulate():
    s = PianorollState(now=100.0)
    s.pan_by(1)
    s.pan_by(1)
    assert s.pan_by(1) == {"lo": 39, "hi": 86}


def test_pan_by_clamps_at_the_top_127():
    # v1: `max_top = 127 - (pitch_high - pitch_low); pitch_low = min(max_top,
    # pitch_low + 1)` -- pitch_high must never exceed 127.
    s = PianorollState(now=100.0, pitch_lo=120, pitch_hi=127)   # size 7, already at the top
    assert s.pan_by(1) == {"lo": 120, "hi": 127}   # no further movement possible
    assert s.pan_by(50) == {"lo": 120, "hi": 127}   # a huge delta clamps the same way


def test_pan_by_clamps_at_the_bottom_0():
    # v1: `pitch_low = max(0, pitch_low - 1)` -- pitch_low must never go
    # below 0.
    s = PianorollState(now=100.0, pitch_lo=0, pitch_hi=7)   # size 7, already at the bottom
    assert s.pan_by(-1) == {"lo": 0, "hi": 7}
    assert s.pan_by(-50) == {"lo": 0, "hi": 7}


def test_pan_by_zero_delta_is_a_noop():
    s = PianorollState(now=100.0)
    assert s.pan_by(0) == {"lo": 36, "hi": 83}


# -- projection mode control ----------------------------------------------

def test_set_projection_accepts_known_modes():
    s = PianorollState(now=100.0)
    assert s.set_projection("tempo") == "tempo"
    assert s.view_model()["window"]["mode"] == "tempo"
    assert s.set_projection("WALLCLOCK") == "wallclock"   # case-insensitive


def test_set_projection_rejects_unknown_mode():
    s = PianorollState(now=100.0)
    with pytest.raises(ValueError):
        s.set_projection("bogus")


def test_window_always_reports_both_span_fields():
    s = PianorollState(now=100.0)
    w = s.view_model()["window"]
    assert "span_s" in w and "span_beats" in w
    s.set_projection("tempo")
    w = s.view_model()["window"]
    assert "span_s" in w and "span_beats" in w


# -- client-side extrapolation params (Phase 10 Task A, items 3+9) ---------

def test_window_origin_ts_is_the_states_own_now():
    s = PianorollState(now=123.5)
    assert s.view_model()["window"]["origin_ts"] == 123.5
    s.tick(200.0)
    assert s.view_model()["window"]["origin_ts"] == 200.0


def test_window_velocity_is_negative_reciprocal_of_span_s_in_wallclock_mode():
    s = PianorollState(now=100.0, span_s=8.0)
    s.set_projection("wallclock")
    w = s.view_model()["window"]
    assert w["velocity"] == pytest.approx(-1.0 / 8.0)


def test_window_velocity_matches_wallclock_formula_in_tempo_mode_too():
    # The whole point of a single shared "velocity" field (module
    # docstring's "Client-side extrapolation" section): `-1.0/span_s` is
    # algebraically identical in EITHER mode once span_s is correctly
    # mode-aware -- no client-side mode branch needed. Cross-checked here
    # against tempo mode's own from-first-principles formula
    # `-(bpm/60)/span_beats` at a non-trivial bpm.
    s = PianorollState(now=100.0, span_beats=16.0)
    s.set_projection("tempo")
    s.handle(transport("start", ts=0.0))
    s.handle(clock_tick(ts=0.25, batch_start=0.0))   # bpm = 60/0.25 = 240
    w = s.view_model()["window"]
    assert w["velocity"] == pytest.approx(-1.0 / w["span_s"])
    assert w["velocity"] == pytest.approx(-(240.0 / 60.0) / 16.0)


def test_window_velocity_is_zero_when_span_s_is_zero():
    # Degenerate zoom/bpm edge case -- _x() itself already guards `span <=
    # 0` by returning 1.0 rather than dividing by zero; velocity must be
    # equally safe, never NaN/inf.
    s = PianorollState(now=100.0, span_s=0.0)
    s.set_projection("wallclock")
    assert s.view_model()["window"]["velocity"] == 0.0


def test_window_running_mirrors_the_grids_own_running_flag():
    s = PianorollState(now=100.0)
    vm = s.view_model()
    assert vm["window"]["running"] is False
    assert vm["grid"]["running"] is False
    s.handle(transport("start", ts=100.0))
    vm = s.view_model()
    assert vm["window"]["running"] is True
    assert vm["grid"]["running"] is True


# -- channel spec parsing / set_channels -----------------------------------

def test_parse_channel_spec_empty_means_all():
    assert _parse_channel_spec("") == set(range(1, 17))


def test_parse_channel_spec_list_and_ranges():
    assert _parse_channel_spec("1,3,5-8") == {1, 3, 5, 6, 7, 8}


def test_parse_channel_spec_out_of_range_tokens_are_dropped():
    assert _parse_channel_spec("1,99,20-25") == {1}


def test_parse_channel_spec_raises_on_malformed_input():
    # Deliberate divergence from v1's silent fallback-to-all -- see module
    # docstring's "Ported v1 controls" section.
    with pytest.raises(ValueError):
        _parse_channel_spec("x,y,z")


def test_set_channels_returns_sorted_list():
    s = PianorollState(now=100.0)
    assert s.set_channels("3,1,2") == [1, 2, 3]


def test_set_channels_raises_on_malformed_spec():
    s = PianorollState(now=100.0)
    with pytest.raises(ValueError):
        s.set_channels("nope")


# -- tick(now): wall-clock progress + pruning ------------------------------

def test_tick_returns_false_when_nothing_has_ever_played():
    s = PianorollState(now=100.0)
    assert s.tick(101.0) is False


def test_tick_returns_true_while_a_note_is_active():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=99.0))
    assert s.tick(101.0) is True


def test_tick_returns_true_while_closed_history_is_within_retention():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=90.0))
    s.handle(note_off(0, 60, ts=91.0))
    assert s.tick(95.0) is True


def test_tick_prunes_closed_spans_older_than_retention():
    s = PianorollState(now=100.0)
    s.handle(note_on(0, 60, ts=0.0))
    s.handle(note_off(0, 60, ts=1.0))
    assert s.tick(1.0 + 121.0) is False   # HISTORY_RETENTION_S == 120.0
    assert s.view_model()["notes"] == []


def test_tick_updates_now_used_by_view_model():
    s = PianorollState(now=100.0, span_s=8.0)
    s.handle(note_on(0, 60, ts=100.0))
    s.tick(104.0)
    n = s.view_model()["notes"][0]
    assert n["x0"] == pytest.approx(1 - 4 / 8)


# -- structural guard: no internal clock reads -------------------------------

def test_handle_and_tick_never_read_a_real_clock():
    # Passing timestamps far from real wall time must not matter -- the
    # state machine must only ever compare its OWN stored timestamps,
    # never time.time()/time.monotonic(). Mirrors tests/
    # test_analyzers_transport.py's identical structural guard.
    s = PianorollState(now=10_000_000.0, span_s=8.0)
    s.handle(note_on(0, 60, ts=9_999_998.0))
    s.tick(10_000_000.0)
    n = s.view_model()["notes"][0]
    assert n["x0"] == pytest.approx(1 - 2 / 8)


# -- PianorollPage: thin delegation (mirrors test_pages_harmony.py's style) --

def test_page_view_model_shape_and_defaults():
    page = PianorollPage()
    vm = page.view_model()
    assert vm["title"] == "PIANOROLL"
    assert vm["notes"] == []


def test_page_handle_delegates_to_the_state():
    page = PianorollPage()
    changed = page.handle(note_on(0, 60, ts=0.0))
    assert changed is True
    assert page.view_model()["notes"][0]["ch"] == 1


def test_page_tick_delegates_to_the_state():
    page = PianorollPage()
    page.handle(note_on(0, 60, ts=0.0))
    assert page.tick(1.0) is True


def test_page_unrelated_event_is_not_dirty():
    page = PianorollPage()
    changed = page.handle(clock_tick(ts=0.5, batch_start=None))
    assert changed is False


def test_page_action_glue_methods_delegate():
    page = PianorollPage()
    assert page.zoom_by(1.0) == pytest.approx(2.0)
    assert page.set_zoom_level(3.0) == pytest.approx(3.0)
    assert page.set_projection("tempo") == "tempo"
    assert page.set_channels("1,2") == [1, 2]


def test_each_page_instance_gets_its_own_independent_state():
    page_a = PianorollPage()
    page_b = PianorollPage()
    page_a.handle(note_on(0, 60, ts=0.0))
    assert page_a.view_model()["notes"]
    assert page_b.view_model()["notes"] == []   # untouched


# -- Phase 8 Task 3: the "paper" grid (docs/visual-audit.md §9c) -----------
#
# `grid.beat_xs`/`grid.bar_xs` are computed through the EXACT SAME `_x()`/
# `_dist()`/`_current_bpm()` machinery the notes above already exercise --
# see pages/pianoroll.py's module docstring "Paper grid" section for the
# `_beat_zero_ts` anchor + current-bpm-derived period design and why it
# deliberately reuses the "re-project ALL history via current bpm" choice
# `_dist()` already made for notes, rather than v1's own per-historical-
# tempo-segment tick integration.

def test_grid_pitch_guide_ys_one_entry_per_semitone_with_correct_c_flag():
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    guides = s.view_model()["grid"]["pitch_guide_ys"]
    assert len(guides) == 13   # 60..72 inclusive
    assert [g["pitch"] for g in guides] == list(range(72, 59, -1))   # hi->lo, top-down
    c_pitches = {g["pitch"] for g in guides if g["is_c"]}
    assert c_pitches == {60, 72}   # C4 and C5 only
    assert {g["name"] for g in guides if g["is_c"]} == {"C4", "C5"}


def test_grid_pitch_guide_y_matches_note_y_exactly():
    # Float-identity, not approx -- both are computed by the literal same
    # `_y(pitch)` expression on the same (pitch, hi, lo) ints, so a note's
    # "y" and its row's guide "y" must compare bit-equal, letting a
    # renderer match them by set membership with no epsilon fudge.
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    s.handle(note_on(0, 66, ts=99.0))
    note_y = s.view_model()["notes"][0]["y"]
    guide_ys = {g["y"] for g in s.view_model()["grid"]["pitch_guide_ys"]}
    assert note_y in guide_ys


def test_grid_running_flag_reflects_transport_state():
    s = PianorollState(now=100.0)
    assert s.view_model()["grid"]["running"] is False
    s.handle(transport("start", ts=100.0))
    assert s.view_model()["grid"]["running"] is True
    s.handle(transport("stop", ts=101.0))
    assert s.view_model()["grid"]["running"] is False


def test_grid_beat_lines_evenly_spaced_at_known_bpm_wallclock_mode():
    # bpm=120 (period 0.5s), span_s=8.0, zoom=1.0, beat_zero_ts == construction
    # "now" -- so the nearest-to-now boundary sits exactly at x=1.0 and every
    # subsequent one steps back by period/span = 0.5/8 = 0.0625 in x, down to
    # and including the window edge (dist == span is inclusive).
    s = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)
    grid = s.view_model()["grid"]
    xs = sorted(grid["beat_xs"] + grid["bar_xs"])
    assert len(xs) == 17   # m = 0..-16 inclusive
    assert xs[0] == pytest.approx(0.0)
    assert xs[-1] == pytest.approx(1.0)
    diffs = [b - a for a, b in pairwise(xs)]
    assert all(d == pytest.approx(0.0625) for d in diffs)


def test_grid_bar_lines_are_every_fourth_beat():
    s = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)
    grid = s.view_model()["grid"]
    assert len(grid["bar_xs"]) == 5     # m = 0, -4, -8, -12, -16
    assert len(grid["beat_xs"]) == 12   # the remaining 12 of the 17 total
    # bar_xs and beat_xs never share a position
    assert not (set(grid["bar_xs"]) & set(grid["beat_xs"]))


def test_grid_scales_with_zoom():
    s = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)
    base_count = len(s.view_model()["grid"]["beat_xs"] + s.view_model()["grid"]["bar_xs"])
    s.zoom_by(1.0)   # zoom 1.0 -> 2.0, halves the effective span
    zoomed_count = len(s.view_model()["grid"]["beat_xs"] + s.view_model()["grid"]["bar_xs"])
    assert base_count == 17
    assert zoomed_count == 9   # span halves to 4.0s -> m = 0..-8 inclusive


def test_grid_tempo_mode_spacing_differs_from_wallclock_at_same_bpm():
    # Proves the grid genuinely re-projects through the ACTIVE mode's own
    # `_dist()`, not just a fixed real-time spacing -- mirrors the note-level
    # squish/stretch tests above. bpm=240 (period 0.25s): wallclock's fixed
    # 8s window covers 32 periods either side of "now" (33 lines); tempo
    # mode's fixed 16-beat window covers only 16 periods (17 lines) at the
    # SAME bpm/period, because tempo's distance unit is beats, not seconds.
    def _count(mode: str) -> int:
        s = PianorollState(now=100.0, span_s=8.0, span_beats=16.0, idle_bpm=240.0)
        s.set_projection(mode)
        grid = s.view_model()["grid"]
        return len(grid["beat_xs"] + grid["bar_xs"])

    assert _count("wallclock") == 33
    assert _count("tempo") == 17


def test_grid_keeps_scrolling_while_stopped_at_idle_bpm():
    # The core "paper" ruling (docs/visual-audit.md §9c/`pianoroll_state.py`
    # `on_tick`): the grid must not freeze while the transport is stopped --
    # it keeps advancing at idle_scroll_bpm from wall-clock time alone.
    # Never started at all here (mirrors v1's own "idle before any run"
    # case, where `last_run_bpm` already defaults to `idle_scroll_bpm`).
    s = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)   # period 0.5s
    first = s.view_model()["grid"]
    assert min(abs(x - 1.0) for x in first["beat_xs"] + first["bar_xs"]) == pytest.approx(0.0)
    assert s.tick(105.25) is False   # nothing has ever played -- not dirty
    later = s.view_model()["grid"]
    # 5.25s later is 10.5 periods -- NOT a whole number of beats, so the
    # boundary nearest "now" is no longer exactly at x=1.0: proof the grid's
    # phase has genuinely advanced with wall-clock time, not just been
    # recomputed identically.
    nearest_gap = min(1.0 - x for x in later["beat_xs"] + later["bar_xs"])
    assert nearest_gap == pytest.approx(0.25 / 8.0)
    assert later["running"] is False


def test_grid_uses_last_run_bpm_after_stop_not_idle_bpm():
    # v1's `last_run_bpm` persists across a stop -- the grid must keep
    # scrolling at the tempo the transport actually ran at, not fall back
    # to idle_scroll_bpm just because the transport is currently stopped.
    # Explicit set_projection("wallclock"): this test's own math (grid
    # spacing = period/span_s, i.e. BPM-DEPENDENT) is specifically
    # wallclock's grid formula -- "tempo" mode (Phase 10 Task A's new
    # DEFAULT, see pages/pianoroll.py's own __init__ comment) places grid
    # marks a CONSTANT 1/span_beats apart regardless of bpm by
    # construction (that constancy IS item 9's whole point -- "division
    # width stable"), so this bpm-persistence assertion needs wallclock
    # mode to mean anything.
    s = PianorollState(now=0.0, span_s=8.0, idle_bpm=120.0)
    s.set_projection("wallclock")
    s.handle(transport("start", ts=0.0))
    s.handle(clock_tick(ts=0.25, batch_start=0.0))   # bpm = 60/0.25 = 240
    assert s._current_bpm() == pytest.approx(240.0)
    s.handle(transport("stop", ts=0.5))
    assert s._current_bpm() == pytest.approx(240.0)   # NOT idle_bpm=120
    s.tick(1.0)
    grid = s.view_model()["grid"]
    xs = sorted(grid["beat_xs"] + grid["bar_xs"])
    diffs = [b - a for a, b in pairwise(xs)]
    # period/span = 0.25/8 = 0.03125 -- proves 240bpm spacing, not 120bpm's
    # 0.0625, despite the transport being stopped at the time of this read.
    assert all(d == pytest.approx(0.03125) for d in diffs)


def test_grid_beat_zero_anchor_resets_on_transport_start():
    # Advancing wall-clock time alone (no "start") leaves the OLD anchor in
    # place, so the nearest-to-now boundary drifts off x=1.0 the moment
    # elapsed time isn't a whole number of periods. A "start" event at that
    # SAME moment instead re-anchors phase exactly there (v1's tick counter
    # resets to 0 on "start"), snapping the nearest boundary back to exactly
    # x=1.0 -- proof the anchor genuinely moved, not just stayed pinned to
    # construction time while "now" moved past it.
    def _nearest_x(state) -> float:
        grid = state.view_model()["grid"]
        return max(grid["beat_xs"] + grid["bar_xs"])

    drifted = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)
    drifted.tick(100.25)
    assert _nearest_x(drifted) == pytest.approx(0.96875)   # 1 - 0.25/8

    restarted = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0)
    restarted.handle(transport("start", ts=100.25))
    assert _nearest_x(restarted) == pytest.approx(1.0)


def test_grid_bpm_non_positive_produces_empty_grid_lines_not_a_crash():
    s = PianorollState(now=100.0, idle_bpm=0.0)
    grid = s.view_model()["grid"]
    assert grid["beat_xs"] == []
    assert grid["bar_xs"] == []
    assert grid["pitch_guide_ys"]   # pitch guides are independent of bpm


# -- review fix: pitch_guide_ys memoization -----------------------------
#
# `pitch_guide_ys` is invariant data (one {y, is_c, pitch, name} dict per
# semitone in [pitch_lo, pitch_hi]) that view_model() was rebuilding --
# reformatting every note name string via f-string + NOTE_NAMES lookup --
# on EVERY call, even though nothing in this class mutates pitch_lo/pitch_hi
# after construction today (pitch-range panning is disclosed future work,
# module docstring's "Not ported" section). Cached keyed on the
# (pitch_lo, pitch_hi) pair so a future range-change feature invalidates
# it automatically rather than needing its own cache-busting code.

def test_grid_pitch_guide_ys_is_memoized_across_calls():
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    first = s.view_model()["grid"]["pitch_guide_ys"]
    second = s.view_model()["grid"]["pitch_guide_ys"]
    assert first is second   # same object -- proves the second call did not rebuild
    assert first == second   # (content sanity, in case identity check above is ever relaxed)


def test_grid_pitch_guide_ys_cache_invalidates_on_range_change():
    s = PianorollState(now=100.0, pitch_lo=60, pitch_hi=72)
    first = s.view_model()["grid"]["pitch_guide_ys"]
    # No public setter exists yet for pitch_lo/pitch_hi (pitch-window
    # panning is disclosed future work) -- mutate the private fields
    # directly to simulate that future feature and prove the cache keys
    # off the CURRENT range rather than latching onto the first one seen.
    s._pitch_lo = 61
    s._pitch_hi = 73
    second = s.view_model()["grid"]["pitch_guide_ys"]
    assert second is not first
    assert [g["pitch"] for g in second] == list(range(73, 60, -1))
    # Reverting to the original range must not still be considered
    # "already cached" against the wrong (now stale) object.
    s._pitch_lo, s._pitch_hi = 60, 72
    third = s.view_model()["grid"]["pitch_guide_ys"]
    assert third is not second
    assert third == first


# -- active-row tint + 1s fade-out (Phase 8 Task 4, docs/visual-audit.md §9c) -
#
# v1's `highlight_pitches`: "any span with visible note data in the current
# window," which is EXACTLY the filter `notes[]` is already built from --
# see pages/pianoroll.py's own "Active-row tint + 1s fade-out" docstring
# section.

def test_row_tint_intensity_is_full_while_note_is_visible():
    s = PianorollState(now=0.0, span_s=8.0)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.tick(0.5)
    tint = s.view_model()["row_tint"]
    assert len(tint) == 1
    assert tint[0]["y"] == pytest.approx(s._y(60))
    assert tint[0]["intensity"] == pytest.approx(1.0)


def test_row_tint_absent_for_a_pitch_never_touched():
    s = PianorollState(now=0.0, span_s=8.0)
    s.tick(1.0)
    assert s.view_model()["row_tint"] == []


def test_row_tint_fades_linearly_after_note_scrolls_off_the_window():
    # span_s=8.0 -- a note onset at ts=0.0 released at ts=0.1 scrolls fully
    # off-window once dist(release_ts) > span, i.e. once now > 8.1. At that
    # instant `_row_fade_until` was last refreshed to (the tick just BEFORE
    # it left the window) + _ROLL_ROW_FADE_S -- drive it precisely via two
    # ticks straddling that boundary.
    s = PianorollState(now=0.0, span_s=8.0)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_off(0, 60, ts=0.1))
    s.tick(8.0)   # still (barely) visible: dist(0.1) = 7.9 <= 8.0
    fade_until = s._row_fade_until[60]
    assert fade_until == pytest.approx(8.0 + _ROLL_ROW_FADE_S)
    s.tick(8.0 + _ROLL_ROW_FADE_S / 2)   # halfway through the fade window
    tint = {t["y"]: t["intensity"] for t in s.view_model()["row_tint"]}
    assert tint[s._y(60)] == pytest.approx(0.5, abs=0.01)
    s.tick(8.0 + _ROLL_ROW_FADE_S + 1.0)   # well past the fade window
    assert s._y(60) not in {t["y"] for t in s.view_model()["row_tint"]}
    assert 60 not in s._row_fade_until   # expired entry is actually dropped


def test_row_tint_refreshes_continuously_while_note_stays_visible():
    # A note that never leaves the window (span_s=8.0, note only 1s old)
    # must NOT start fading just because 1 real second has passed since
    # its onset -- _refresh_row_fade() re-arms fade_until every tick while
    # still visible.
    s = PianorollState(now=0.0, span_s=8.0)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.tick(0.5)
    s.tick(1.0)   # 1.0s after onset -- would be MID-FADE if not re-armed
    tint = {t["y"]: t["intensity"] for t in s.view_model()["row_tint"]}
    assert tint[s._y(60)] == pytest.approx(1.0)


# -- overlap flash (Phase 8 Task 4, docs/visual-audit.md §9c) ----------------
#
# Pure phase-math tests against `_flash_mult`/`_overlap_regions_for_row`
# first (full control over `now`), then a PianorollState-level integration
# test proving the real class actually wires two overlapping notes into
# `overlap_flash` grouped by `y`.

def test_flash_mult_matches_v1_count_based_speed_table():
    assert _flash_mult(1) == pytest.approx(0.70)   # unreachable in practice, still correct
    assert _flash_mult(2) == pytest.approx(0.90)
    assert _flash_mult(3) == pytest.approx(1.30)
    assert _flash_mult(4) == pytest.approx(1.70)
    assert _flash_mult(10) == pytest.approx(1.70)   # 4+ all share the same tier


def test_overlap_regions_empty_for_fewer_than_two_notes():
    assert _overlap_regions_for_row([], now=0.0) == []
    assert _overlap_regions_for_row([{"x0": 0.0, "x1": 1.0, "ch": 1, "vel": 1.0}], now=0.0) == []


def test_overlap_regions_empty_when_notes_do_not_overlap():
    notes = [{"x0": 0.0, "x1": 0.3, "ch": 1, "vel": 1.0},
             {"x0": 0.5, "x1": 0.8, "ch": 2, "vel": 1.0}]
    assert _overlap_regions_for_row(notes, now=0.0) == []


def test_overlap_regions_emits_one_region_for_two_overlapping_notes():
    notes = [{"x0": 0.75, "x1": 1.0, "ch": 1, "vel": 1.0},
             {"x0": 0.875, "x1": 1.0, "ch": 2, "vel": 0.5}]
    # n=2 -> total_phases=3, flash_hz = 16.0*0.90 = 14.4. Pick `now` so
    # int(now*flash_hz) % 3 == 0 -> phase_idx 0 -> the FIRST active note
    # (active_sorted = [0, 1], sorted by ORIGINAL LIST INDEX) shows.
    now = 0.0   # int(0*14.4) % 3 == 0
    regions = _overlap_regions_for_row(notes, now=now)
    assert len(regions) == 1
    assert regions[0]["x0"] == pytest.approx(0.875)
    assert regions[0]["x1"] == pytest.approx(1.0)
    assert regions[0]["ch"] == 1
    assert regions[0]["vel"] == pytest.approx(1.0)


def test_overlap_regions_cycles_through_phases_including_blink_to_bg():
    notes = [{"x0": 0.0, "x1": 1.0, "ch": 1, "vel": 1.0},
             {"x0": 0.0, "x1": 1.0, "ch": 2, "vel": 0.5}]
    flash_hz = 16.0 * _flash_mult(2)   # 14.4, total_phases=3
    period = 1.0 / flash_hz
    # phase 0 -> note ch=1 (active_sorted[0])
    r0 = _overlap_regions_for_row(notes, now=0.0 * period)
    assert r0[0]["ch"] == 1
    # phase 1 -> note ch=2 (active_sorted[1])
    r1 = _overlap_regions_for_row(notes, now=1.0 * period)
    assert r1[0]["ch"] == 2
    # phase 2 (== n) -> blink to BG, ch/vel both None
    r2 = _overlap_regions_for_row(notes, now=2.0 * period)
    assert r2[0]["ch"] is None
    assert r2[0]["vel"] is None
    # phase 3 wraps back to 0
    r3 = _overlap_regions_for_row(notes, now=3.0 * period)
    assert r3[0]["ch"] == 1


def test_overlap_regions_three_way_overlap_uses_four_phases():
    notes = [{"x0": 0.0, "x1": 1.0, "ch": 1, "vel": 1.0},
             {"x0": 0.0, "x1": 1.0, "ch": 2, "vel": 1.0},
             {"x0": 0.0, "x1": 1.0, "ch": 3, "vel": 1.0}]
    flash_hz = 16.0 * _flash_mult(3)   # n=3 -> total_phases=4
    period = 1.0 / flash_hz
    chs = [_overlap_regions_for_row(notes, now=k * period)[0]["ch"] for k in range(4)]
    assert chs == [1, 2, 3, None]


def test_pianoroll_state_wires_two_overlapping_notes_on_same_pitch_into_overlap_flash():
    # ch1 held from ts=0.0, ch2 held from ts=1.0 -- BOTH still active at
    # now=2.0, both on pitch 60 -- their projected [x0,x1] windows overlap
    # ([0.75,1.0] and [0.875,1.0] at span_s=8.0), matching the pure-function
    # scenario above but exercised through the real class end-to-end.
    s = PianorollState(now=0.0, span_s=8.0, pitch_lo=60, pitch_hi=60)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_on(1, 60, vel=80, ts=1.0))
    s.tick(2.0)
    vm = s.view_model()
    notes_by_ch = {n["ch"]: n for n in vm["notes"]}
    assert notes_by_ch[1]["x0"] == pytest.approx(0.75)
    assert notes_by_ch[2]["x0"] == pytest.approx(0.875)
    assert notes_by_ch[1]["x1"] == pytest.approx(1.0)
    assert notes_by_ch[2]["x1"] == pytest.approx(1.0)
    overlap = vm["overlap_flash"]
    assert len(overlap) == 1
    assert overlap[0]["y"] == pytest.approx(s._y(60))
    assert overlap[0]["x0"] == pytest.approx(0.875)
    assert overlap[0]["x1"] == pytest.approx(1.0)
    assert overlap[0]["ch"] in (None, 1, 2)   # exact phase depends on wall-clock `now`


def test_pianoroll_state_overlap_flash_empty_for_non_overlapping_notes():
    s = PianorollState(now=0.0, span_s=8.0, pitch_lo=60, pitch_hi=60)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_off(0, 60, ts=1.0))
    s.handle(note_on(1, 60, vel=80, ts=5.0))
    s.tick(5.5)
    assert s.view_model()["overlap_flash"] == []


def test_pianoroll_state_overlap_flash_empty_for_a_single_note():
    s = PianorollState(now=0.0, span_s=8.0)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.tick(1.0)
    assert s.view_model()["overlap_flash"] == []


def test_pianoroll_state_overlap_flash_groups_by_pitch_row_not_globally():
    # Two overlapping notes on pitch 60 and two SEPARATE overlapping notes
    # on pitch 64 -- must produce independent overlap regions per row, not
    # cross-contaminate.
    s = PianorollState(now=0.0, span_s=8.0, pitch_lo=60, pitch_hi=64)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_on(1, 60, vel=80, ts=1.0))
    s.handle(note_on(2, 64, vel=100, ts=0.0))
    s.handle(note_on(3, 64, vel=80, ts=1.0))
    s.tick(2.0)
    overlap = s.view_model()["overlap_flash"]
    ys = {r["y"] for r in overlap}
    assert ys == {s._y(60), s._y(64)}
    assert len(overlap) == 2
