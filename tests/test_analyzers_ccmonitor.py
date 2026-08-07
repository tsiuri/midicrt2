"""TDD for CCMonitorAnalyzer -- see analyzers/ccmonitor.py's own module
docstring for the full v1 (`pages/ccmonitor.py` + `pages/ccgraph.py`)
behavioral synthesis: a per-channel recent-CC window (ccmonitor.py) plus a
global insertion-ordered last/peak tracker with freshness (ccgraph.py).
"""
from midicrt.analyzers.ccmonitor import (
    FRESH_AFTER_S,
    MAX_TRACKED,
    RECENT_PER_CHANNEL,
    CCMonitorAnalyzer,
)
from midicrt.engine.core import MidiEvent


def cc(ch0, control, value, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="control_change", channel=ch0,
                      data1=control, data2=value, summary=f"control_change ch{ch0+1} cc{control} v{value}")


def note_on(ch0=0, note=60, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=100, summary=f"note_on ch{ch0+1} n{note} v100")


def test_view_model_shape_and_defaults():
    a = CCMonitorAnalyzer()
    vm = a.view_model()
    assert len(vm["per_channel"]) == 16
    assert vm["per_channel"][0] == {"ch": 1, "recent": []}
    assert vm["tracked"] == []


def test_non_cc_events_are_a_true_no_op():
    a = CCMonitorAnalyzer()
    assert a.handle(note_on()) is False
    assert a.view_model()["tracked"] == []


def test_control_change_updates_both_per_channel_and_tracked():
    a = CCMonitorAnalyzer()
    changed = a.handle(cc(0, 74, 100, ts=1.0))
    assert changed is True
    vm = a.view_model()
    assert vm["per_channel"][0]["recent"] == [{"cc": 74, "value": 100, "peak": 100}]
    assert vm["tracked"] == [
        {"ch": 1, "cc": 74, "value": 100, "peak": 100, "age_s": 0.0, "fresh": True}]


def test_per_channel_recent_window_is_not_deduplicated_by_controller():
    # v1 ccmonitor.py appends every CC verbatim, even repeats of the same
    # controller number -- not a per-CC-number dedup.
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 74, 10))
    a.handle(cc(0, 74, 20))
    a.handle(cc(0, 74, 30))
    recent = a.view_model()["per_channel"][0]["recent"]
    assert [r["value"] for r in recent] == [10, 20, 30]


def test_per_channel_recent_window_caps_at_six_and_drops_oldest():
    a = CCMonitorAnalyzer()
    for v in range(RECENT_PER_CHANNEL + 3):
        a.handle(cc(0, 1, v))
    recent = a.view_model()["per_channel"][0]["recent"]
    assert len(recent) == RECENT_PER_CHANNEL
    assert [r["value"] for r in recent] == list(range(3, RECENT_PER_CHANNEL + 3))


def test_peak_never_decreases():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 74, 100))
    a.handle(cc(0, 74, 40))
    vm = a.view_model()
    entry = vm["tracked"][0]
    assert entry["value"] == 40   # latest
    assert entry["peak"] == 100   # high-water mark


def test_tracked_caps_at_sixteen_entries_fifo_eviction_of_oldest_new_key():
    a = CCMonitorAnalyzer()
    for i in range(MAX_TRACKED + 2):
        a.handle(cc(0, i, 1))   # each a distinct (ch, cc) key
    tracked = a.view_model()["tracked"]
    assert len(tracked) == MAX_TRACKED
    ccs = [t["cc"] for t in tracked]
    assert ccs == list(range(2, MAX_TRACKED + 2))   # oldest 2 evicted, order preserved


def test_updating_an_already_tracked_key_does_not_move_its_position():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 1, 1))
    a.handle(cc(0, 2, 1))
    a.handle(cc(0, 3, 1))
    a.handle(cc(0, 1, 99))   # re-fire the FIRST key -- must stay first, not jump to the end
    ccs = [t["cc"] for t in a.view_model()["tracked"]]
    assert ccs == [1, 2, 3]
    assert a.view_model()["tracked"][0]["value"] == 99


def test_tick_before_ever_called_defaults_to_fresh_with_zero_age():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 1, 50, ts=1000.0))
    entry = a.view_model()["tracked"][0]
    assert entry["age_s"] == 0.0
    assert entry["fresh"] is True


def test_tick_computes_age_and_fresh_from_injected_now():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 1, 50, ts=1000.0))
    a.tick(1000.5)
    entry = a.view_model()["tracked"][0]
    assert entry["age_s"] == 0.5
    assert entry["fresh"] is True

    a.tick(1000.0 + FRESH_AFTER_S + 1.0)
    entry = a.view_model()["tracked"][0]
    assert entry["fresh"] is False
    assert entry["age_s"] == FRESH_AFTER_S + 1.0


def test_tick_is_dirty_only_on_a_freshness_transition():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 1, 50, ts=1000.0))
    assert a.tick(1000.1) is False    # still fresh -- no bucket change
    assert a.tick(1000.0 + FRESH_AFTER_S + 1.0) is True   # crosses into stale
    assert a.tick(1000.0 + FRESH_AFTER_S + 2.0) is False  # already stale -- no change


def test_a_fresh_new_cc_on_an_already_stale_entry_re_marks_it_fresh():
    a = CCMonitorAnalyzer()
    a.handle(cc(0, 1, 50, ts=1000.0))
    a.tick(1000.0 + FRESH_AFTER_S + 1.0)
    assert a.view_model()["tracked"][0]["fresh"] is False
    a.handle(cc(0, 1, 51, ts=1000.0 + FRESH_AFTER_S + 1.0))
    assert a.view_model()["tracked"][0]["fresh"] is True
