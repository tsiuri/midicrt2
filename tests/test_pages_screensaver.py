"""ScreensaverPage: a content-free page `behaviors/screensaver.py` switches
to (see pages/screensaver.py's module docstring for the v1 comparison and
the disclosed "chrome stays lit" limitation)."""
from midicrt.engine.core import MidiEvent
from midicrt.pages.screensaver import ScreensaverPage


def test_view_model_carries_no_content():
    page = ScreensaverPage()
    assert page.view_model() == {"title": "SCREENSAVER"}


def test_handle_is_always_a_pure_no_op():
    page = ScreensaverPage()
    note_on = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                        data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(note_on) is False
    assert page.view_model() == {"title": "SCREENSAVER"}
