"""TDD for Img2TxtVizAnalyzer -- see src/midicrt/analyzers/img2txtviz.py's
module docstring for the full v1 investigation (no image-bank loading in
v1 despite the name) and the disclosed wave-field adaptation this ports
instead.
"""
import math

import pytest

from midicrt.analyzers.img2txtviz import CHARSETS, GRID_COLS, GRID_ROWS, Img2TxtVizAnalyzer
from midicrt.engine.core import MidiEvent


def ev(**kw):
    base = {"ts": 0.0, "source": "test", "type": "note_on", "channel": 0,
            "data1": 60, "data2": 100, "summary": "x"}
    base.update(kw)
    return MidiEvent(**base)


# -- fresh-state shape --------------------------------------------------------


def test_fresh_view_model_shape_and_idle_defaults():
    a = Img2TxtVizAnalyzer()
    vm = a.view_model()
    assert vm["active_notes"] == 0
    assert vm["last_note"] == 60
    assert vm["last_vel"] == 0
    assert vm["last_program"] == 0
    assert vm["energy"] == 0.0
    assert vm["vel_splash"] == 0.0
    assert vm["invert"] is False
    assert vm["charset"] == CHARSETS[0]
    grid = vm["grid"]
    assert len(grid) == GRID_ROWS
    assert all(len(row) == GRID_COLS for row in grid)
    assert all(0.0 <= v <= 1.0 for row in grid for v in row)


def test_grid_dimensions_are_the_documented_fixed_constants():
    assert GRID_COLS == 40
    assert GRID_ROWS == 20


# -- known-state exact math (the "known tiny image -> expected cells" analog,
# adapted per the module docstring's disclosed adaptation: there is no image
# to convert, so this is "known analyzer state -> expected grid cell") ------


def test_known_idle_state_produces_exact_top_left_cell_value():
    # Hand-derived at rest (phase=0, energy=spark=splash=0, cc empty,
    # last_note=60 -> note_phase=0/12=0.0, octave_phase=(60//12)%8/8=5/8=0.625):
    # nx=ny=0 (top-left corner):
    #   wave_x = 0.5 + 0.5*sin(2pi*0) = 0.5
    #   wave_y = 0.5 + 0.5*sin(2pi*0.625) = 0.5 + 0.5*sin(pi + pi/4)
    #          = 0.5 - 0.5*sin(pi/4) = 0.5 - 0.35355339 = 0.14644661
    #   d = |0-0.5| + |0-0.5| = 1.0; ring = 0.5 + 0.5*sin(2pi*4.0) = 0.5 (sin(8pi)=0)
    #   base = 0.35*0.5 + 0.30*0.14644661 + 0.35*0.5 = 0.39393398
    #   drive=0 -> contrast=1, brightness_bias=0 -> v = base
    a = Img2TxtVizAnalyzer()
    grid = a.view_model()["grid"]
    expected = 0.35 * 0.5 + 0.30 * (0.5 - 0.5 * math.sin(math.pi / 4)) + 0.35 * 0.5
    assert grid[0][0] == pytest.approx(expected, abs=1e-9)
    assert grid[0][0] == pytest.approx(0.393933982822018, abs=1e-9)


def test_grid_is_pure_and_deterministic_without_further_ticks():
    a = Img2TxtVizAnalyzer()
    first = a.view_model()["grid"]
    second = a.view_model()["grid"]
    assert first == second


# -- wall-clock-injected animation (tick) ------------------------------------


def test_tick_always_reports_dirty():
    # Disclosed choice (module docstring): unlike spectrum/pianoroll, this
    # page never settles at rest -- the wave field's phase strictly advances
    # every real tick, so tick() always returns True.
    a = Img2TxtVizAnalyzer()
    assert a.tick(1.0) is True
    assert a.tick(1.0) is True   # dt=0 this time -- still True, by design


def test_tick_advances_phase_and_changes_the_grid():
    a = Img2TxtVizAnalyzer()
    a.tick(1.0)   # first call ever establishes the time reference at dt=0 (no jump)
    before = a.view_model()["grid"]
    a.tick(6.0)   # a REAL 5.0s of injected progress from here
    after = a.view_model()["grid"]
    assert before != after


def test_first_tick_call_establishes_the_time_reference_without_a_jump():
    # The very first tick/handle call has no prior `_last_ts` -- dt must be
    # treated as 0.0 (no huge phantom-elapsed-time jump), mirroring
    # analyzers/spectrum.py's SpectrumAnalyzer.tick()'s identical contract.
    a = Img2TxtVizAnalyzer()
    idle_grid = a.view_model()["grid"]
    a.tick(1_000_000.0)   # first call ever, at an arbitrary large timestamp
    # No decay-driven jump should have applied to energy/splash (both were
    # already 0), but the phase must have advanced by 0 (not 1e6 seconds) --
    # confirmed indirectly: a SECOND tick a moment later should move the
    # grid by a small, continuous amount, not by another huge jump.
    a.tick(1_000_000.05)
    small_step_grid = a.view_model()["grid"]
    assert idle_grid != small_step_grid   # some motion happened
    # A small dt (0.05s) at these wave speeds cannot flip every single cell
    # from one extreme to the other -- sanity bound, not an exact value.
    deltas = [abs(a1 - b1) for r1, r2 in zip(idle_grid, small_step_grid) for a1, b1 in zip(r1, r2)]
    assert max(deltas) < 0.2


# -- invert / charset controls (spec §5 runtime-adjustable controls) --------


def test_toggle_invert_flips_the_returned_flag():
    a = Img2TxtVizAnalyzer()
    assert a.toggle_invert() is True
    assert a.view_model()["invert"] is True
    assert a.toggle_invert() is False
    assert a.view_model()["invert"] is False


def test_toggle_invert_produces_the_exact_elementwise_complement():
    a = Img2TxtVizAnalyzer()
    before = a.view_model()["grid"]
    a.toggle_invert()
    after = a.view_model()["grid"]
    for row_before, row_after in zip(before, after):
        for v_before, v_after in zip(row_before, row_after):
            assert v_after == pytest.approx(1.0 - v_before, abs=1e-9)


def test_cycle_charset_advances_and_wraps_through_all_four_charsets():
    a = Img2TxtVizAnalyzer()
    assert a.view_model()["charset"] == CHARSETS[0]
    assert a.cycle_charset() == CHARSETS[1]
    assert a.cycle_charset() == CHARSETS[2]
    assert a.cycle_charset() == CHARSETS[3]
    assert a.cycle_charset() == CHARSETS[0]   # wraps


def test_program_change_offsets_the_active_charset_without_cycling():
    # v1's `_render_ascii`: charset = _CHARSETS[(_charset_ix + _last_program)
    # % len(_CHARSETS)] -- ported verbatim (module docstring).
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="program_change", data1=2, data2=None))
    assert a.view_model()["last_program"] == 2
    assert a.view_model()["charset"] == CHARSETS[2]
    a.cycle_charset()   # charset_ix now 1; offset by program 2 -> index 3
    assert a.view_model()["charset"] == CHARSETS[3]


def test_program_change_wraps_modulo_128():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="program_change", data1=130, data2=None))
    assert a.view_model()["last_program"] == 130 % 128


# -- MIDI-driven excitation state (ported verbatim from v1, see docstring) --


def test_note_on_tracks_last_note_and_velocity_and_active_set():
    a = Img2TxtVizAnalyzer()
    assert a.handle(ev(type="note_on", channel=2, data1=64, data2=90, ts=0.0)) is True
    vm = a.view_model()
    assert vm["last_note"] == 64
    assert vm["last_vel"] == 90
    assert vm["active_notes"] == 1


def test_note_on_zero_velocity_is_treated_as_note_off():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="note_on", channel=0, data1=60, data2=100, ts=0.0))
    assert a.view_model()["active_notes"] == 1
    a.handle(ev(type="note_on", channel=0, data1=60, data2=0, ts=0.01))
    assert a.view_model()["active_notes"] == 0


def test_note_off_removes_the_active_note():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="note_on", channel=0, data1=60, data2=100, ts=0.0))
    a.handle(ev(type="note_off", channel=0, data1=60, data2=0, ts=0.01))
    assert a.view_model()["active_notes"] == 0


def test_control_change_stores_the_clamped_value():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="control_change", data1=1, data2=127, ts=0.0))
    assert a._cc[1] == 127


def test_handle_returns_false_for_a_non_excitation_event_type():
    a = Img2TxtVizAnalyzer()
    assert a.handle(ev(type="clock_tick", data1=24, data2=None, ts=0.0)) is False


def test_handle_still_advances_the_wave_field_even_on_a_non_dirty_event():
    # v1's own `_decay()` is called unconditionally at the top of
    # `handle(msg)` regardless of message type -- ported: a clock_tick still
    # advances `_phase` even though it returns False (not "dirty" for the
    # note/cc/program fields specifically).
    a = Img2TxtVizAnalyzer()
    a.tick(1.0)   # establish the time reference first (see the tick test above)
    before = a.view_model()["grid"]
    a.handle(ev(type="clock_tick", data1=24, data2=None, ts=6.0))
    after = a.view_model()["grid"]
    assert before != after


def test_note_on_increases_energy_spark_and_vel_splash():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="note_on", channel=0, data1=60, data2=127, ts=0.0))
    vm = a.view_model()
    assert vm["energy"] > 0.0
    assert vm["vel_splash"] > 0.0


def test_repeated_high_velocity_notes_saturate_at_v1s_clamp_ceilings():
    # Tiny inter-note spacing (1ms) keeps the exponential decay between hits
    # negligible (exp(-0.001 * 4.05) ~= 0.996) so 50 max-velocity hits
    # accumulate past the clamp ceiling rather than reaching a lower decay
    # equilibrium -- see the DEDICATED decay test below for the case where
    # the spacing is wide enough for decay to matter.
    a = Img2TxtVizAnalyzer()
    for i in range(50):
        a.handle(ev(type="note_on", channel=0, data1=60 + (i % 4), data2=127, ts=i * 0.001))
    vm = a.view_model()
    assert vm["energy"] == pytest.approx(2.6, abs=1e-9)
    assert vm["vel_splash"] == pytest.approx(3.2, abs=1e-9)


def test_decay_between_widely_spaced_notes_reaches_an_equilibrium_below_the_clamp():
    # Wider spacing (0.2s) lets exponential decay between hits outpace the
    # per-hit increment before saturation -- a real, disclosed consequence
    # of v1's own decay-rate/increment formula (ported verbatim), not a bug:
    # energy settles at a steady-state E where E = E*decay_factor + increment,
    # i.e. E = increment / (1 - decay_factor).
    a = Img2TxtVizAnalyzer()
    for i in range(50):
        a.handle(ev(type="note_on", channel=0, data1=60 + (i % 4), data2=127, ts=i * 0.2))
    decay_factor = math.exp(-0.2 * 1.35 * 3.0)
    increment = 1.0 * (0.35 + 0.65 * 1.0)   # vel_w=1.0 (max velocity), rate_scale clamped to 1.0
    expected_equilibrium = increment / (1.0 - decay_factor)
    assert a.view_model()["energy"] == pytest.approx(expected_equilibrium, rel=1e-3)
    assert a.view_model()["energy"] < 2.6   # well under the clamp ceiling


def test_energy_decays_exponentially_over_injected_time():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="note_on", channel=0, data1=60, data2=127, ts=0.0))
    energy_at_0 = a.view_model()["energy"]
    assert energy_at_0 > 0.0
    a.tick(1.0)   # 1.0s of injected wall-clock progress
    energy_at_1 = a.view_model()["energy"]
    expected = energy_at_0 * math.exp(-1.0 * 1.35 * 3.0)
    assert energy_at_1 == pytest.approx(expected, abs=1e-3)
    assert energy_at_1 < energy_at_0


def test_decay_snaps_to_exactly_zero_below_the_v1_epsilon():
    a = Img2TxtVizAnalyzer()
    a.handle(ev(type="note_on", channel=0, data1=60, data2=1, ts=0.0))  # tiny excitation
    a.tick(1000.0)   # comfortably past every decay half-life
    vm = a.view_model()
    assert vm["energy"] == 0.0
    assert vm["vel_splash"] == 0.0
