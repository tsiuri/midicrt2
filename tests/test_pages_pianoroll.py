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
    ZOOM_MAX,
    ZOOM_MIN,
    PianorollPage,
    PianorollState,
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
    assert vm["window"]["mode"] == "wallclock"
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
    # disclosed "cosmetic span_beats lag" trade-off.
    s = PianorollState(now=100.0)   # default mode is "wallclock"
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
    def _x0(bpm: float) -> float:
        s = PianorollState(now=100.0, span_s=8.0)
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
    s = PianorollState(now=0.0, span_s=8.0, idle_bpm=120.0)
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
