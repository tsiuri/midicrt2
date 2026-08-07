"""TDD for VoicesPage: the page-level wrapper around
analyzers.voices.VoiceMonitorAnalyzer -- adds the one thing that's page-, not
analyzer-, specific: binding each channel row to its configured instrument
NAME (config `instruments`). See analyzers/voices.py for the v1 behavioral
notes; this file only covers the page's own VM shaping and delegation.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.voices import VoicesPage

NAMES_16 = [f"Inst{i}" for i in range(1, 17)]


def note_on(ch0, note, vel=100):
    return MidiEvent(ts=0.0, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def test_view_model_shape_and_defaults():
    page = VoicesPage(instruments=NAMES_16)
    vm = page.view_model()
    assert vm["title"] == "VOICES"
    assert vm["total"] == 0 and vm["total_peak"] == 0
    assert len(vm["rows"]) == 16
    assert vm["rows"][0] == {"ch": 1, "name": "Inst1", "active": 0, "peak": 0, "notes": []}
    assert vm["rows"][15]["ch"] == 16 and vm["rows"][15]["name"] == "Inst16"


def test_handle_delegates_to_the_analyzer_and_updates_the_named_row():
    page = VoicesPage(instruments=NAMES_16)
    changed = page.handle(note_on(2, 60))   # channel 3 (0-based 2)
    assert changed is True
    vm = page.view_model()
    row = vm["rows"][2]
    assert row == {"ch": 3, "name": "Inst3", "active": 1, "peak": 1, "notes": [60]}
    assert vm["total"] == 1 and vm["total_peak"] == 1


def test_unrelated_event_is_not_dirty():
    page = VoicesPage(instruments=NAMES_16)
    changed = page.handle(MidiEvent(ts=0.0, source="x", type="clock_tick", channel=None,
                                    data1=24, data2=None, summary="clock_tick"))
    assert changed is False


def test_fewer_than_16_names_falls_back_to_a_generated_label():
    page = VoicesPage(instruments=["Only One"])
    vm = page.view_model()
    assert vm["rows"][0]["name"] == "Only One"
    assert vm["rows"][1]["name"] == "CH 2"
    assert vm["rows"][15]["name"] == "CH 16"


def test_extra_names_beyond_16_are_ignored():
    page = VoicesPage(instruments=[f"N{i}" for i in range(1, 21)])   # 20 names
    vm = page.view_model()
    assert len(vm["rows"]) == 16
    assert vm["rows"][15]["name"] == "N16"


def test_default_config_instruments_produce_the_v1_names():
    from midicrt.config import Config

    page = VoicesPage(instruments=Config().instruments)
    names = [r["name"] for r in page.view_model()["rows"]]
    assert names[0] == "Kawai XD5"
    assert names[15] == "Akai CD4"


def test_set_instruments_swaps_names_live_without_losing_analyzer_state():
    page = VoicesPage(instruments=NAMES_16)
    page.handle(note_on(0, 60))   # live poly state on channel 1

    page.set_instruments(["Renamed"])

    vm = page.view_model()
    assert vm["rows"][0]["name"] == "Renamed"
    assert vm["rows"][1]["name"] == "CH 2"           # fewer-than-16 fallback, same as ctor
    assert vm["rows"][0]["active"] == 1              # analyzer state untouched by the swap
    assert vm["total"] == 1
