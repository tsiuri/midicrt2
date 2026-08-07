"""TDD for TunerPage: a thin wrapper around analyzers.tuner.TunerAnalyzer --
see pages/tuner.py's and analyzers/tuner.py's own module docstrings for why
this page shows v1's genuine idle state until a separate, not-yet-built
audio-capture task exists to call `on_pitch_sample()` for real, and for why
it is registered in engine/core.py's `_PAGE_FACTORIES` but NOT in
config.py's default `pages` list.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.tuner import TunerPage


def test_view_model_shape_and_idle_defaults():
    page = TunerPage()
    vm = page.view_model()
    assert vm["title"] == "TUNER"
    assert vm["has_signal"] is False
    assert vm["note"] == ""


def test_handle_is_a_true_noop_for_midi_events():
    page = TunerPage()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False
    assert page.view_model()["has_signal"] is False


def test_each_page_instance_gets_its_own_independent_analyzer():
    page_a = TunerPage()
    page_b = TunerPage()
    page_a._analyzer.on_pitch_sample(hz=440.0, confidence=0.9, db=-10.0)
    assert page_a.view_model()["has_signal"] is True
    assert page_b.view_model()["has_signal"] is False   # untouched
