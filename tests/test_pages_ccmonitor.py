"""TDD for CCMonitorPage -- the page-level wrapper exposing only the
per-channel half of analyzers.ccmonitor.CCMonitorAnalyzer's view model. See
that module and pages/ccmonitor.py's own docstrings for the full v1
behavioral notes and the "separate analyzer instance per page" design.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.ccmonitor import CCMonitorPage


def cc(ch0, control, value, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="control_change", channel=ch0,
                      data1=control, data2=value, summary=f"control_change ch{ch0+1} cc{control} v{value}")


def test_view_model_shape_and_defaults():
    page = CCMonitorPage()
    vm = page.view_model()
    assert vm["title"] == "CC MONITOR"
    assert len(vm["channels"]) == 16
    assert vm["channels"][0] == {"ch": 1, "recent": []}


def test_handle_delegates_to_the_analyzer():
    page = CCMonitorPage()
    changed = page.handle(cc(2, 74, 90))   # channel 3 (0-based 2)
    assert changed is True
    vm = page.view_model()
    assert vm["channels"][2]["recent"] == [{"cc": 74, "value": 90, "peak": 90}]


def test_unrelated_event_is_not_dirty():
    page = CCMonitorPage()
    ev = MidiEvent(ts=0.0, source="x", type="note_on", channel=0, data1=60, data2=100,
                    summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False
