"""TDD for HarmonyPage: a thin wrapper around
analyzers.harmony.HarmonyAnalyzer -- unlike VoicesPage (which binds
config.instruments to each row), harmony has no page-specific config to
bind, so this page is a pure passthrough. See analyzers/harmony.py and
pages/harmony.py's own module docstrings for the full v1 behavioral notes
and the v1-field -> VM-field mapping; this file only covers the page's own
delegation.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.harmony import HarmonyPage


def note_on(ch0, note, vel=100, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def test_view_model_shape_and_defaults():
    page = HarmonyPage()
    vm = page.view_model()
    assert vm["title"] == "HARMONY"
    assert vm["chords"] == [] and vm["scales"] == []
    assert vm["key"] is None
    assert vm["silent"] is True


def test_handle_delegates_to_the_analyzer():
    page = HarmonyPage()
    changed = page.handle(note_on(0, 60))
    changed = page.handle(note_on(0, 64)) or changed
    changed = page.handle(note_on(0, 67)) or changed
    assert changed is True
    vm = page.view_model()
    assert vm["chords"][0]["name"] == "C maj"
    assert vm["silent"] is False


def test_unrelated_event_is_not_dirty():
    page = HarmonyPage()
    changed = page.handle(MidiEvent(ts=0.0, source="x", type="clock_tick", channel=None,
                                    data1=24, data2=None, summary="clock_tick"))
    assert changed is False


def test_each_page_instance_gets_its_own_independent_analyzer():
    page_a = HarmonyPage()
    page_b = HarmonyPage()
    page_a.handle(note_on(0, 60))
    page_a.handle(note_on(0, 64))
    page_a.handle(note_on(0, 67))
    assert page_a.view_model()["chords"][0]["name"] == "C maj"
    assert page_b.view_model()["chords"] == []   # untouched
