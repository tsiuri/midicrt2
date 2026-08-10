"""TDD for StuckNotesAnalyzer: a pure state machine fed `MidiEvent`s PLUS an
injected wall-clock `tick(now)` (see analyzers/stucknotes.py's module
docstring for why this analyzer -- alone among v2's analyzers so far --
needs a clock signal from outside: escalation is time-based and can happen
with no new MIDI event at all, unlike every other analyzer which derives
timing purely from `ev.ts` deltas).

Ported from v1's `~/codex/midicrt/plugins/zstucknotes.py`.
"""
from midicrt.analyzers.stucknotes import CRIT_AFTER, HOLD_AFTER, WARN_AFTER, StuckNotesAnalyzer
from midicrt.engine.core import MidiEvent


def note_on(ch0, note, vel=100, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def note_off(ch0, note, vel=64, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_off", channel=ch0,
                      data1=note, data2=vel, summary=f"note_off ch{ch0 + 1} n{note} v{vel}")


def cc(ch0, control, value, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="control_change", channel=ch0,
                      data1=control, data2=value,
                      summary=f"control_change ch{ch0 + 1} cc{control} v{value}")


def test_initial_view_model_is_empty():
    a = StuckNotesAnalyzer()
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_note_on_alone_is_not_yet_stuck():
    a = StuckNotesAnalyzer()
    changed = a.handle(note_on(0, 60, ts=0.0))
    assert changed is True   # active-set changed, even though no alert yet
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_tick_before_warn_after_produces_no_alert():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    dirty = a.tick(WARN_AFTER - 0.1)
    assert dirty is False
    assert a.view_model() == {"alerts": [], "cleared": []}
    assert a.drain_alerts() == []


def test_tick_past_warn_after_escalates_and_emits_one_alert():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    dirty = a.tick(WARN_AFTER + 0.5)
    assert dirty is True
    vm = a.view_model()
    assert vm["alerts"] == [{"ch": 1, "note": 60, "level": "warn", "held_s": WARN_AFTER + 0.5}]
    alerts = a.drain_alerts()
    assert alerts == [{"ch": 1, "note": 60, "level": "warn", "held_s": WARN_AFTER + 0.5}]
    assert a.drain_alerts() == []   # drained -- second call is empty


def test_tick_past_crit_after_escalates_again():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(WARN_AFTER + 0.1)
    a.drain_alerts()
    dirty = a.tick(CRIT_AFTER + 0.5)
    assert dirty is True
    vm = a.view_model()
    assert vm["alerts"][0]["level"] == "crit"
    alerts = a.drain_alerts()
    assert alerts == [{"ch": 1, "note": 60, "level": "crit", "held_s": round(CRIT_AFTER + 0.5, 1)}]


def test_held_s_stays_live_across_ticks_while_alerting_without_new_alert_event():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(WARN_AFTER + 1.0)
    a.drain_alerts()
    dirty = a.tick(WARN_AFTER + 2.0)   # still "warn" -- level unchanged
    assert dirty is True               # but the overlay still refreshes (live readout)
    assert a.view_model()["alerts"][0]["held_s"] == round(WARN_AFTER + 2.0, 1)
    assert a.drain_alerts() == []      # no NEW escalation -- no engine alert event


def test_retrigger_resets_the_age_clock():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(note_on(0, 60, ts=1.5))   # retrigger before WARN_AFTER elapses
    dirty = a.tick(1.5 + WARN_AFTER - 0.1)
    assert dirty is False
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_retrigger_increments_overlap_count_needing_two_offs_to_clear():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(note_on(0, 60, ts=0.0))   # overlapping retrigger, e.g. an arpeggiator
    a.handle(note_off(0, 60, ts=0.1))
    dirty = a.tick(WARN_AFTER + 1.0)
    assert dirty is True   # still one voice held -- still escalates
    assert a.view_model()["alerts"] != []
    a.handle(note_off(0, 60, ts=0.2))
    dirty = a.tick(WARN_AFTER + 1.1)
    # No longer actively alerting -- this test's own concern (overlap-count
    # clearing) ends here. It now ALSO starts the stuck-linger window (see
    # the dedicated hold-after tests below), so a full-dict equality
    # assertion would be wrong here -- assert only the part this test is
    # actually about ("alerts" is empty), and leave "cleared" to the
    # dedicated linger tests.
    assert a.view_model()["alerts"] == []


def test_note_off_clears_alert_immediately():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)
    assert a.view_model()["alerts"] != []
    changed = a.handle(note_off(0, 60, ts=CRIT_AFTER + 1.1))
    assert changed is True
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_note_off_for_a_note_never_on_is_a_true_noop():
    a = StuckNotesAnalyzer()
    changed = a.handle(note_off(0, 60, ts=0.0))
    assert changed is False


def test_velocity_zero_note_on_is_treated_as_note_off():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    changed = a.handle(note_on(0, 60, vel=0, ts=0.1))
    assert changed is True
    a.tick(WARN_AFTER + 1.0)
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_sustain_suppresses_escalation_while_held():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(cc(0, 64, 127, ts=0.0))   # sustain down
    dirty = a.tick(CRIT_AFTER + 1.0)
    assert dirty is False
    assert a.view_model() == {"alerts": [], "cleared": []}
    assert a.drain_alerts() == []


def test_sustain_release_re_arms_escalation_from_original_last_on():
    # v1 does not reset last_on when sustain releases -- the note has
    # genuinely been held the whole time, it was just hidden from the
    # alert list while sustain suppressed it.
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(cc(0, 64, 127, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)
    a.handle(cc(0, 64, 0, ts=CRIT_AFTER + 2.0))   # sustain up
    dirty = a.tick(CRIT_AFTER + 2.1)
    assert dirty is True
    assert a.view_model()["alerts"][0]["level"] == "crit"


def test_cc64_below_64_is_sustain_up_not_down():
    a = StuckNotesAnalyzer()
    changed = a.handle(cc(0, 64, 10, ts=0.0))
    assert changed is False   # already up by default -- no-op


def test_cc120_all_sound_off_clears_channel():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(note_on(0, 64, ts=0.0))
    changed = a.handle(cc(0, 120, 0, ts=0.1))
    assert changed is True
    a.tick(CRIT_AFTER + 1.0)
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_cc123_all_notes_off_clears_channel():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    changed = a.handle(cc(0, 123, 0, ts=0.1))
    assert changed is True
    a.tick(CRIT_AFTER + 1.0)
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_cc_clear_is_a_noop_on_an_already_clear_channel():
    a = StuckNotesAnalyzer()
    changed = a.handle(cc(0, 120, 0, ts=0.0))
    assert changed is False


def test_cc121_reset_all_controllers_clears_sustain():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(cc(0, 64, 127, ts=0.0))   # sustain down
    changed = a.handle(cc(0, 121, 0, ts=0.1))
    assert changed is True
    dirty = a.tick(CRIT_AFTER + 1.0)   # sustain now off -- escalation proceeds
    assert dirty is True
    assert a.view_model()["alerts"] != []


def test_cc_other_than_sustain_and_notes_off_is_a_noop():
    a = StuckNotesAnalyzer()
    changed = a.handle(cc(0, 7, 100, ts=0.0))   # volume
    assert changed is False


def test_channel_none_is_a_defensive_noop():
    a = StuckNotesAnalyzer()
    ev = MidiEvent(ts=0.0, source="x", type="note_on", channel=None,
                   data1=60, data2=100, summary="note_on")
    assert a.handle(ev) is False


def test_unrelated_event_types_are_a_pure_noop():
    a = StuckNotesAnalyzer()
    ev = MidiEvent(ts=0.0, source="x", type="clock_tick", channel=None,
                   data1=24, data2=None, summary="clock_tick")
    assert a.handle(ev) is False
    assert a.view_model() == {"alerts": [], "cleared": []}


def test_multiple_stuck_notes_sorted_worst_first():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(note_on(1, 64, ts=3.0))
    dirty = a.tick(12.0)
    assert dirty is True
    vm = a.view_model()
    assert [al["note"] for al in vm["alerts"]] == [60, 64]   # older (held longer) first
    assert vm["alerts"][0]["level"] == "crit"
    assert vm["alerts"][1]["level"] == "warn"


def test_tick_never_reads_a_clock_or_does_io():
    # Structural guard, mirrors test_analyzers_transport.py's own
    # `test_handle_never_reads_a_clock_or_does_io`: an analyzer must only
    # ever compare timestamps it was GIVEN (event ts or injected `now`),
    # never read time.time()/monotonic() itself. Using timestamps far from
    # real wall-clock time must not matter.
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=10_000_000.0))
    dirty = a.tick(10_000_000.0 + WARN_AFTER + 1.0)
    assert dirty is True
    assert a.view_model()["alerts"][0]["level"] == "warn"


def test_drain_alerts_only_reports_new_escalations_not_every_tick():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(0.5)   # below WARN_AFTER
    assert a.drain_alerts() == []
    a.tick(WARN_AFTER + 0.1)
    assert len(a.drain_alerts()) == 1
    a.tick(WARN_AFTER + 0.2)   # still "warn" -- no new escalation
    assert a.drain_alerts() == []
    a.tick(CRIT_AFTER + 0.1)   # escalates to "crit" -- one new alert
    assert len(a.drain_alerts()) == 1


# -- stuck-linger (Phase 9 Task 2, config.stuck_hold_after) -------------------
#
# v1 evidence (`~/codex/midicrt/plugins/zstucknotes.py`): `HOLD_AFTER =
# 15.0` (line 20); `draw()`'s own "if not stuck" branch (lines 226-248)
# freezes the LAST computed `stuck` snapshot into `_last_stuck_snapshot`
# the moment it goes empty (`_had_stuck` True -> False transition) and
# keeps showing a "STUCK CLEARED: ..." message built from that frozen
# snapshot for `HOLD_AFTER` seconds, then goes blank. This linger
# bookkeeping lives ENTIRELY in `draw()` (the v1 analogue of `tick()`
# here), never in the note-on/note-off handlers -- so, matching v1
# exactly, a note-off clearing the LAST active alert makes `view_model()
# ["alerts"]` empty IMMEDIATELY (handle()-time), but `view_model()
# ["cleared"]` only populates on the NEXT `tick()` call (see
# test_cleared_field_is_empty_until_the_next_tick_after_the_clearing_
# note_off below) -- an at-most-one-tick-period gap this analyzer
# deliberately reproduces rather than "fixes", since `engine/core.py`'s
# `_tick_analyzers` already runs every `tick_hz` period (~33ms at the
# default 30Hz) regardless, exactly mirroring v1's own "only updates at
# the next draw() poll" structure.

def test_hold_after_constructor_default_matches_v1s_hold_after_constant():
    assert HOLD_AFTER == 15.0
    a = StuckNotesAnalyzer()
    assert a._hold_after == HOLD_AFTER


def test_cleared_field_is_empty_until_the_next_tick_after_the_clearing_note_off():
    a = StuckNotesAnalyzer()
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)   # now alerting
    a.handle(note_off(0, 60, ts=CRIT_AFTER + 1.1))   # clears immediately (handle()-time)
    assert a.view_model() == {"alerts": [], "cleared": []}   # linger not armed yet


def test_cleared_snapshot_populates_on_the_next_tick_after_clearing():
    a = StuckNotesAnalyzer(hold_after=5.0)
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)
    a.handle(note_off(0, 60, ts=CRIT_AFTER + 1.1))
    dirty = a.tick(CRIT_AFTER + 1.2)   # next tick -- linger arms here
    assert dirty is True
    vm = a.view_model()
    assert vm["alerts"] == []
    assert vm["cleared"] == [
        {"ch": 1, "note": 60, "level": "crit", "held_s": round(CRIT_AFTER + 1.0, 1)}
    ]


def test_cleared_snapshot_expires_after_hold_after_seconds():
    a = StuckNotesAnalyzer(hold_after=5.0)
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)
    a.handle(note_off(0, 60, ts=CRIT_AFTER + 1.1))
    a.tick(CRIT_AFTER + 1.2)   # linger armed
    assert a.view_model()["cleared"] != []
    dirty = a.tick(CRIT_AFTER + 1.2 + 5.0 - 0.1)   # just under 5s later -- still lingering
    assert dirty is False
    assert a.view_model()["cleared"] != []
    dirty = a.tick(CRIT_AFTER + 1.2 + 5.0 + 0.1)   # just past 5s -- expires
    assert dirty is True
    assert a.view_model()["cleared"] == []


def test_cleared_snapshot_uses_the_configured_hold_after_not_the_v1_default():
    a = StuckNotesAnalyzer(hold_after=0.5)   # far shorter than v1's 15s default
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(WARN_AFTER + 1.0)
    a.handle(note_off(0, 60, ts=WARN_AFTER + 1.1))
    a.tick(WARN_AFTER + 1.2)   # linger armed
    assert a.view_model()["cleared"] != []
    a.tick(WARN_AFTER + 1.2 + 0.6)   # past the short 0.5s window
    assert a.view_model()["cleared"] == []


def test_a_fresh_alert_during_the_linger_window_takes_priority_over_cleared():
    a = StuckNotesAnalyzer(hold_after=5.0)
    a.handle(note_on(0, 60, ts=0.0))
    a.tick(CRIT_AFTER + 1.0)
    a.handle(note_off(0, 60, ts=CRIT_AFTER + 1.1))
    a.tick(CRIT_AFTER + 1.2)   # linger armed for note 60
    assert a.view_model()["cleared"] != []
    a.handle(note_on(1, 64, ts=CRIT_AFTER + 1.3))   # a NEW note goes stuck
    dirty = a.tick(CRIT_AFTER + 1.3 + WARN_AFTER + 0.1)
    assert dirty is True
    vm = a.view_model()
    assert vm["alerts"] != []
    assert vm["cleared"] == []   # abandoned once a real alert is active again


def test_cleared_list_reflects_multiple_notes_worst_first_like_alerts_does():
    a = StuckNotesAnalyzer(hold_after=5.0)
    a.handle(note_on(0, 60, ts=0.0))
    a.handle(note_on(1, 64, ts=3.0))
    a.tick(12.0)   # both alerting, note 60 (crit) older than note 64 (warn)
    a.handle(note_off(0, 60, ts=12.1))
    a.handle(note_off(1, 64, ts=12.1))
    dirty = a.tick(12.2)
    assert dirty is True
    cleared = a.view_model()["cleared"]
    assert [c["note"] for c in cleared] == [60, 64]   # same worst-held-first order as alerts


def test_tick_still_never_reads_a_clock_or_does_io_with_hold_after_active():
    # Extends the existing structural guard (test_tick_never_reads_a_clock_
    # or_does_io above) to the new linger bookkeeping specifically: using
    # timestamps far from real wall-clock time must not matter here either.
    a = StuckNotesAnalyzer(hold_after=5.0)
    a.handle(note_on(0, 60, ts=10_000_000.0))
    a.tick(10_000_000.0 + CRIT_AFTER + 1.0)
    a.handle(note_off(0, 60, ts=10_000_000.0 + CRIT_AFTER + 1.1))
    dirty = a.tick(10_000_000.0 + CRIT_AFTER + 1.2)
    assert dirty is True
    assert a.view_model()["cleared"] != []
