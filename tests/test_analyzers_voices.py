"""TDD for VoiceMonitorAnalyzer: a pure state machine fed `MidiEvent`s (no
I/O), ported from v1's `~/codex/midicrt/plugins/zvoicemonitor.py` (poly
counts + peak hold) and `~/codex/midicrt/plugins/polydisplay.py` (per-channel
active-note set + transport-triggered clear) -- see analyzers/voices.py's
module docstring for the full behavioral synthesis notes (note-on/off
pairing via overlap counts, velocity-0-as-off, no sustain/CC64, CC120/123
per-channel clear, start/stop clears all channels, peak never decays).

`MidiEvent.channel` is 0-based (mido convention, matches
tests/test_analyzers_transport.py's own events) -- channel 0 below means
"channel 1" in the analyzer's 1-based view_model.
"""
from midicrt.analyzers.voices import (
    EVENT_LOG_LEN,
    FLASH_DURATION_S,
    POLY_LIMIT_CH,
    POLY_LIMIT_GLOBAL,
    VoiceMonitorAnalyzer,
)
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


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


def _channel(vm, ch1):
    return vm["channels"][ch1 - 1]


def test_initial_view_model_is_all_zero():
    a = VoiceMonitorAnalyzer()
    vm = a.view_model()
    assert vm["total"] == 0 and vm["total_peak"] == 0
    assert len(vm["channels"]) == 16
    assert all(c == {"active": 0, "peak": 0, "notes": []} for c in vm["channels"])


def test_note_on_increments_active_and_appears_in_notes():
    a = VoiceMonitorAnalyzer()
    changed = a.handle(note_on(0, 60))
    assert changed is True
    vm = a.view_model()
    assert _channel(vm, 1) == {"active": 1, "peak": 1, "notes": [60]}
    assert vm["total"] == 1 and vm["total_peak"] == 1


def test_note_off_decrements_and_removes_from_notes():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(note_off(0, 60))
    assert changed is True
    vm = a.view_model()
    assert _channel(vm, 1) == {"active": 0, "peak": 1, "notes": []}  # peak-hold
    assert vm["total"] == 0 and vm["total_peak"] == 1


def test_velocity_zero_note_on_is_treated_as_note_off():
    # Both v1 sources: `if msg.velocity == 0: _note_off(...)`.
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(note_on(0, 60, vel=0))
    assert changed is True
    assert _channel(a.view_model(), 1)["active"] == 0


def test_note_off_for_a_note_never_on_is_a_true_noop():
    # Mirrors zvoicemonitor's `if key not in _active: return`.
    a = VoiceMonitorAnalyzer()
    changed = a.handle(note_off(0, 60))
    assert changed is False
    assert a.view_model() == VoiceMonitorAnalyzer().view_model()


def test_overlapping_retrigger_of_same_pitch_needs_two_note_offs():
    # v1's `_active[(ch, note)]` is a COUNT, not a boolean -- two overlapping
    # note-ons on the same pitch are two real voices; the pitch must stay
    # "on" (in `notes`) until BOTH are released.
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 60))
    vm = a.view_model()
    assert _channel(vm, 1)["active"] == 2
    assert _channel(vm, 1)["notes"] == [60]   # one distinct pitch, two voices
    a.handle(note_off(0, 60))
    vm = a.view_model()
    assert _channel(vm, 1)["active"] == 1
    assert _channel(vm, 1)["notes"] == [60]   # still held -- one voice remains
    a.handle(note_off(0, 60))
    vm = a.view_model()
    assert _channel(vm, 1)["active"] == 0
    assert _channel(vm, 1)["notes"] == []


def test_notes_list_is_sorted_ascending():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 67))
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    assert _channel(a.view_model(), 1)["notes"] == [60, 64, 67]


def test_peak_hold_never_decreases_after_notes_release():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    a.handle(note_on(0, 67))
    assert _channel(a.view_model(), 1)["peak"] == 3
    a.handle(note_off(0, 60))
    a.handle(note_off(0, 64))
    a.handle(note_off(0, 67))
    vm = a.view_model()
    assert _channel(vm, 1) == {"active": 0, "peak": 3, "notes": []}
    a.handle(note_on(0, 60))  # a smaller re-peak must not lower the hold
    assert _channel(a.view_model(), 1)["peak"] == 3


def test_global_total_and_peak_sum_across_channels():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))    # ch1
    a.handle(note_on(1, 62))    # ch2
    a.handle(note_on(1, 65))    # ch2
    vm = a.view_model()
    assert vm["total"] == 3 and vm["total_peak"] == 3
    a.handle(note_off(1, 62))
    a.handle(note_off(1, 65))
    vm = a.view_model()
    assert vm["total"] == 1 and vm["total_peak"] == 3   # peak-hold at the global level too


def test_channel_indexing_matches_v1_one_based_convention():
    # mido channel 0 -> v1/analyzer channel 1 (index 0 of "channels").
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    vm = a.view_model()
    assert vm["channels"][0]["active"] == 1
    assert all(c["active"] == 0 for c in vm["channels"][1:])


def test_cc120_all_sound_off_clears_the_channel():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    a.handle(note_on(1, 62))   # untouched channel
    changed = a.handle(cc(0, 120, 0))
    assert changed is True
    vm = a.view_model()
    assert _channel(vm, 1) == {"active": 0, "peak": 2, "notes": []}  # peak untouched
    assert _channel(vm, 2)["active"] == 1                             # other channel untouched
    assert vm["total"] == 1


def test_cc123_all_notes_off_clears_the_channel():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(cc(0, 123, 0))
    assert changed is True
    assert _channel(a.view_model(), 1)["active"] == 0


def test_clear_channel_is_a_noop_when_already_empty():
    a = VoiceMonitorAnalyzer()
    changed = a.handle(cc(0, 120, 0))
    assert changed is False


def test_other_control_changes_do_not_clear_notes():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(cc(0, 7, 100))   # volume, not 120/123
    assert changed is False
    assert _channel(a.view_model(), 1)["active"] == 1


def test_sustain_cc64_is_not_special_cased():
    # Neither v1 source (`zvoicemonitor.py`/`polydisplay.py`) references
    # CC64 anywhere -- a held sustain pedal must NOT keep a released note
    # "on" here, matching that (arguably incomplete, but real) v1 behavior.
    a = VoiceMonitorAnalyzer()
    a.handle(cc(0, 64, 127))   # sustain down
    a.handle(note_on(0, 60))
    a.handle(note_off(0, 60))  # released while sustain is "down"
    assert _channel(a.view_model(), 1)["active"] == 0
    assert _channel(a.view_model(), 1)["notes"] == []


def test_start_clears_all_channels_but_not_peak():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(1, 62))
    changed = a.handle(transport("start"))
    assert changed is True
    vm = a.view_model()
    assert vm["total"] == 0
    assert all(c["active"] == 0 and c["notes"] == [] for c in vm["channels"])
    assert _channel(vm, 1)["peak"] == 1 and _channel(vm, 2)["peak"] == 1


def test_stop_clears_all_channels_but_not_peak():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(transport("stop"))
    assert changed is True
    vm = a.view_model()
    assert vm["total"] == 0
    assert _channel(vm, 1)["peak"] == 1


def test_clear_all_is_a_noop_when_nothing_is_active():
    a = VoiceMonitorAnalyzer()
    changed = a.handle(transport("start"))
    assert changed is False
    changed = a.handle(transport("stop"))
    assert changed is False


def test_unrelated_event_types_are_a_pure_noop():
    a = VoiceMonitorAnalyzer()
    changed = a.handle(MidiEvent(ts=0.0, source="clock", type="clock_tick", channel=None,
                                 data1=24, data2=None, summary="clock_tick"))
    assert changed is False
    assert a.view_model() == VoiceMonitorAnalyzer().view_model()


def test_note_on_with_no_channel_is_a_safe_noop():
    # Defensive guard mirroring polydisplay's `if ch_raw is None: return` --
    # translate() always sets a channel for note_on/off/cc in practice, but
    # a malformed/synthetic event must not crash the analyzer.
    a = VoiceMonitorAnalyzer()
    changed = a.handle(MidiEvent(ts=0.0, source="x", type="note_on", channel=None,
                                 data1=60, data2=100, summary="note_on"))
    assert changed is False
# -- poly-limit log (Phase 9 Task 2, config.poly_limit_global/poly_limit_ch) -
#
# v1 evidence (`~/codex/midicrt/plugins/zvoicemonitor.py`): `POLY_LIMIT_GLOBAL
# = 16` (line 11), `POLY_LIMIT_CH = 8` (line 12), `_events = deque(maxlen=
# EVENT_LOG_LEN)` (line 56, `EVENT_LOG_LEN = 8`, line 14) recording an
# "instant" tag event via `_events.appendleft(...)` (line 81) the moment a
# note-on's resulting total/channel count first exceeds its limit
# (`hit_global`/`hit_ch`, lines 78-81). v1's "sustain" tag (a SECOND,
# beat-duration-gated re-notification via `_update_over`/`OVER_LIMIT_BEATS`,
# lines 107-122) is NOT ported here -- it needs a MIDI-clock tick/beat
# counter this analyzer has no access to (a disclosed simplification,
# matching this task's own scope: only `poly_limit_global`/`poly_limit_ch`
# are named config knobs). v1's `per_channel_limits` 16-entry override list
# is also not ported (config.py's own docstring), so `ch_limit` in every
# event here is always the SAME scalar `poly_limit_ch`.

def test_poly_limit_defaults_match_v1s_shipped_constants():
    assert POLY_LIMIT_GLOBAL == 16
    assert POLY_LIMIT_CH == 8
    a = VoiceMonitorAnalyzer()
    assert a._limit_global == 16
    assert a._limit_ch == 8


def test_events_list_is_empty_initially():
    a = VoiceMonitorAnalyzer()
    assert a.view_model()["events"] == []


def test_a_note_on_under_both_limits_logs_no_event():
    a = VoiceMonitorAnalyzer(poly_limit_global=16, poly_limit_ch=8)
    a.handle(note_on(0, 60, ts=1.0))
    assert a.view_model()["events"] == []


def test_exceeding_the_per_channel_limit_logs_an_instant_event():
    a = VoiceMonitorAnalyzer(poly_limit_global=16, poly_limit_ch=2)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 62, ts=1.0))
    changed = a.handle(note_on(0, 64, ts=1.5))   # 3rd voice on ch1 -- exceeds limit 2
    assert changed is True
    events = a.view_model()["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["ch"] == 1 and ev["note"] == 64
    assert ev["total"] == 3 and ev["ch_total"] == 3 and ev["ch_limit"] == 2
    assert ev["hit_global"] is False and ev["hit_ch"] is True


def test_exceeding_the_global_limit_logs_an_instant_event():
    a = VoiceMonitorAnalyzer(poly_limit_global=2, poly_limit_ch=16)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(1, 62, ts=1.0))
    changed = a.handle(note_on(2, 64, ts=1.5))   # 3rd voice overall -- exceeds global limit 2
    assert changed is True
    ev = a.view_model()["events"][0]
    assert ev["hit_global"] is True and ev["hit_ch"] is False
    assert ev["total"] == 3


def test_a_limit_of_zero_or_less_never_triggers_that_limit():
    # Mirrors v1's own `if POLY_LIMIT_GLOBAL > 0`/`if ch_limit and ch_limit
    # > 0` guards -- a non-positive limit means "no limit", not "always over".
    a = VoiceMonitorAnalyzer(poly_limit_global=0, poly_limit_ch=0)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 62, ts=1.0))
    assert a.view_model()["events"] == []


def test_events_deque_evicts_oldest_beyond_event_log_len():
    # poly_limit_ch=1: the FIRST note-on on a channel never exceeds (1 is
    # not > 1); the SECOND (an overlapping retrigger of the SAME pitch,
    # v1's own "count, not boolean" voice semantics) does -- exactly one
    # qualifying event per iteration.
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    for i in range(EVENT_LOG_LEN + 3):
        t = float(i)
        a.handle(note_on(0, 60, ts=t))
        a.handle(note_on(0, 60, ts=t + 0.01))   # exceeds -- logs one event
        a.handle(note_off(0, 60, ts=t + 0.02))
        a.handle(note_off(0, 60, ts=t + 0.02))
    events = a.view_model()["events"]
    assert len(events) == EVENT_LOG_LEN


def test_events_are_newest_first_appendleft_matches_v1():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))   # exceeds -- event #1 (note 60)
    a.handle(note_off(0, 60, ts=1.1))
    a.handle(note_off(0, 60, ts=1.1))
    a.handle(note_on(0, 61, ts=2.0))
    a.handle(note_on(0, 61, ts=2.0))   # exceeds -- event #2 (note 61)
    events = a.view_model()["events"]
    assert events[0]["note"] == 61   # most recent event first
    assert events[1]["note"] == 60


def test_age_s_defaults_to_zero_before_any_tick():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    assert a.view_model()["events"][0]["age_s"] == 0.0


def test_age_s_reflects_the_injected_tick_clock():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    a.tick(3.5)
    assert a.view_model()["events"][0]["age_s"] == 2.5


def test_clear_channel_does_not_erase_the_event_log():
    # v1's `_clear_channel` only zeroes `_active`/`_active_ch` -- `_events`
    # is a HISTORY, untouched by any clear path, same "peak never resets"
    # precedent this module's own docstring already established for `_peak`.
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(cc(0, 123, 0, ts=1.1))
    assert a.view_model()["events"] != []


# -- chrome flash (Phase 9 Task 2, disclosed v2-native addition -- v1's
# zvoicemonitor.py has no visual/chrome home of its own at all, see module
# docstring) ------------------------------------------------------------

def test_flash_view_model_is_not_flashing_initially():
    a = VoiceMonitorAnalyzer()
    assert a.flash_view_model() == {"flashing": False}


def test_flash_view_model_is_not_flashing_before_any_tick_even_after_an_exceed():
    # handle() alone starts the flash window but never reports it "active"
    # on its own -- flashing is a tick()-observed, wall-clock-bounded state
    # (mirrors analyzers/beatflash.py's own injected-clock convention), not
    # something handle()'s return value communicates.
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    assert a.flash_view_model() == {"flashing": False}


def test_tick_reports_flashing_true_right_after_an_exceed():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    dirty = a.tick(1.1)   # well within FLASH_DURATION_S
    assert dirty is True
    assert a.flash_view_model() == {"flashing": True}


def test_tick_reports_flashing_false_once_flash_duration_elapses():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    a.tick(1.1)
    dirty = a.tick(1.0 + FLASH_DURATION_S + 0.1)
    assert dirty is True   # ON -> OFF transition
    assert a.flash_view_model() == {"flashing": False}


def test_tick_does_not_redundantly_report_dirty_mid_flash_or_after_it_ends():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    assert a.tick(1.05) is True    # OFF -> ON
    assert a.tick(1.10) is False   # still ON -- no transition
    assert a.tick(1.0 + FLASH_DURATION_S + 0.1) is True    # ON -> OFF
    assert a.tick(1.0 + FLASH_DURATION_S + 0.2) is False   # still OFF -- no transition


def test_a_second_exceed_during_an_active_flash_extends_the_window():
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 60, ts=1.0))
    a.tick(1.1)
    assert a.flash_view_model()["flashing"] is True
    a.handle(note_on(1, 64, ts=1.3))
    a.handle(note_on(1, 64, ts=1.3))   # a second, later exceed (different channel)
    a.tick(1.0 + FLASH_DURATION_S + 0.1)   # past the FIRST window, still within the SECOND
    assert a.flash_view_model()["flashing"] is True


def test_tick_never_reads_a_clock_or_does_io():
    # Structural guard, mirrors test_analyzers_stucknotes.py's own -- an
    # analyzer must only ever compare timestamps it was GIVEN.
    a = VoiceMonitorAnalyzer(poly_limit_global=100, poly_limit_ch=1)
    a.handle(note_on(0, 60, ts=10_000_000.0))
    a.handle(note_on(0, 60, ts=10_000_000.0))
    dirty = a.tick(10_000_000.0 + 0.05)
    assert dirty is True
    assert a.flash_view_model() == {"flashing": True}


# -- shared-instance dedup guard (needed for the chrome-flash overlay wiring,
# engine/core.py -- see analyzers/harmony.py's own identical precedent for
# why sharing one instance across two roster entries needs this) -----------

def test_handle_is_idempotent_for_the_same_event_object_twice():
    a = VoiceMonitorAnalyzer()
    ev = note_on(0, 60, ts=1.0)
    first = a.handle(ev)
    second = a.handle(ev)   # SAME object reference -- simulates shared-instance double-dispatch
    assert first is True
    assert second is True   # cached dirty result, not a re-processed False
    vm = a.view_model()
    assert vm["channels"][0]["active"] == 1   # only counted ONCE, not twice
    assert vm["total"] == 1


def test_handle_processes_a_genuinely_new_event_object_normally():
    a = VoiceMonitorAnalyzer()
    a.handle(note_on(0, 60, ts=1.0))
    a.handle(note_on(0, 62, ts=1.0))   # a DIFFERENT event object -- must process normally
    assert a.view_model()["total"] == 2
