"""TDD for CCDashboardPage -- the page-level wrapper exposing only the
global-tracker half of analyzers.ccmonitor.CCMonitorAnalyzer's view model,
including the `tick(now)`-driven freshness this page (unlike
pages/ccmonitor.py) actually needs. See that analyzer module and
pages/ccdashboard.py's own docstrings for the full v1 behavioral notes.
"""
from midicrt.analyzers.ccmonitor import FRESH_AFTER_S
from midicrt.engine.core import MidiEvent
from midicrt.pages.ccdashboard import CCDashboardPage


def cc(ch0, control, value, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="control_change", channel=ch0,
                      data1=control, data2=value, summary=f"control_change ch{ch0+1} cc{control} v{value}")


def test_view_model_shape_and_defaults():
    page = CCDashboardPage()
    vm = page.view_model()
    assert vm["title"] == "CC DASHBOARD"
    assert vm["entries"] == []


def test_handle_delegates_to_the_analyzer():
    page = CCDashboardPage()
    changed = page.handle(cc(0, 74, 100, ts=5.0))
    assert changed is True
    vm = page.view_model()
    assert vm["entries"] == [
        {"ch": 1, "cc": 74, "value": 100, "peak": 100, "age_s": 0.0, "fresh": True}]


def test_tick_delegates_to_the_analyzer_and_updates_freshness():
    page = CCDashboardPage()
    page.handle(cc(0, 74, 100, ts=1000.0))
    dirty = page.tick(1000.0 + FRESH_AFTER_S + 1.0)
    assert dirty is True
    assert page.view_model()["entries"][0]["fresh"] is False


def test_unrelated_event_is_not_dirty():
    page = CCDashboardPage()
    ev = MidiEvent(ts=0.0, source="x", type="note_on", channel=0, data1=60, data2=100,
                    summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False
