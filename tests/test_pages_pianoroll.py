"""TDD for PianorollState/PianorollPage: v1's flagship two-projection-mode
scrolling note display -- see pages/pianoroll.py's module docstring for the
full port notes (storage substrate, tempo-relative math derivation, VM
mapping, deliberately-not-ported list).

Synthetic events below construct MidiEvents directly at known real
timestamps (mirroring tests/test_analyzers_transport.py's own convention)
rather than going through engine/midi_in.py -- these are the state
machine's own math contract in isolation.
"""
import pytest

from midicrt.engine.core import MidiEvent
from midicrt.pages.pianoroll import (
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
    assert page.set_projection("tempo") == "tempo"
    assert page.set_channels("1,2") == [1, 2]


def test_each_page_instance_gets_its_own_independent_state():
    page_a = PianorollPage()
    page_b = PianorollPage()
    page_a.handle(note_on(0, 60, ts=0.0))
    assert page_a.view_model()["notes"]
    assert page_b.view_model()["notes"] == []   # untouched
