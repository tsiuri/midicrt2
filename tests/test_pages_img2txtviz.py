"""TDD for Img2TxtVizPage: a thin wrapper around
analyzers.img2txtviz.Img2TxtVizAnalyzer -- see that module's own docstring
for the full v1 investigation and disclosed wave-field adaptation.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.img2txtviz import Img2TxtVizPage


def test_view_model_shape_and_title():
    page = Img2TxtVizPage()
    vm = page.view_model()
    assert vm["title"] == "IMG2TXT"
    assert "grid" in vm
    assert "charset" in vm
    assert vm["invert"] is False


def test_handle_delegates_to_the_analyzer():
    page = Img2TxtVizPage()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(ev) is True
    assert page.view_model()["active_notes"] == 1


def test_tick_delegates_to_the_analyzer_and_always_reports_dirty():
    page = Img2TxtVizPage()
    assert page.tick(1.0) is True


def test_cycle_charset_and_toggle_invert_delegate_to_the_analyzer():
    page = Img2TxtVizPage()
    before = page.view_model()["charset"]
    after = page.cycle_charset()
    assert after != before
    assert page.view_model()["charset"] == after
    assert page.toggle_invert() is True
    assert page.view_model()["invert"] is True


def test_each_page_instance_gets_its_own_independent_analyzer():
    page_a = Img2TxtVizPage()
    page_b = Img2TxtVizPage()
    page_a.toggle_invert()
    assert page_a.view_model()["invert"] is True
    assert page_b.view_model()["invert"] is False   # untouched
