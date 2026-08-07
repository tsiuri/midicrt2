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
from midicrt.analyzers.voices import VoiceMonitorAnalyzer
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
