"""TDD for ProgChangesPage (page name "progchanges") -- see pages/
progchanges.py's own module docstring for the full v1 (`pages/proglog.py`)
behavioral synthesis: a rolling program-change log reusing eventlog's own
`{title, count, lines}` VM shape.
"""
import time

from midicrt.engine.core import MidiEvent
from midicrt.pages.progchanges import MAX_LOG, ProgChangesPage


def program_change(ch0, program, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="program_change", channel=ch0,
                      data1=program, data2=None, summary=f"program_change ch{ch0 + 1} p{program}")


def note_on(ch0=0, note=60, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=100, summary=f"note_on ch{ch0 + 1} n{note} v100")


def test_view_model_shape_and_defaults():
    page = ProgChangesPage()
    vm = page.view_model()
    assert vm["title"] == "PROGRAM CHANGES"
    assert vm["count"] == 0
    assert vm["lines"] == []


def test_default_capacity_matches_v1s_max_log():
    page = ProgChangesPage()
    for i in range(MAX_LOG + 10):
        page.handle(program_change(0, i % 128))
    assert len(page.view_model()["lines"]) == MAX_LOG


def test_program_change_appends_a_formatted_line():
    page = ProgChangesPage()
    changed = page.handle(program_change(0, 12, ts=1731000000.0))
    assert changed is True
    vm = page.view_model()
    assert vm["count"] == 1
    assert len(vm["lines"]) == 1
    line = vm["lines"][0]
    assert line["style"] == "normal"
    ts_text = time.strftime("%H:%M:%S", time.localtime(1731000000.0))
    assert line["text"] == f"[{ts_text}]  Ch01 → Program 012"


def test_channel_and_program_number_format_with_leading_zeros():
    page = ProgChangesPage()
    page.handle(program_change(9, 3))   # channel 10 (0-based 9), program 3
    line = page.view_model()["lines"][0]
    assert "Ch10" in line["text"]
    assert "Program 003" in line["text"]


def test_non_program_change_events_are_ignored():
    page = ProgChangesPage()
    changed = page.handle(note_on())
    assert changed is False
    assert page.view_model()["count"] == 0


def test_count_keeps_growing_past_the_ring_buffer_capacity():
    page = ProgChangesPage(capacity=3)
    for i in range(5):
        page.handle(program_change(0, i))
    vm = page.view_model()
    assert vm["count"] == 5          # true lifetime total, not just what's retained
    assert len(vm["lines"]) == 3     # ring buffer capped at capacity
    assert "Program 002" in vm["lines"][0]["text"]   # oldest 2 evicted
    assert "Program 004" in vm["lines"][-1]["text"]


def test_malformed_event_with_no_channel_is_a_defensive_no_op():
    page = ProgChangesPage()
    ev = MidiEvent(ts=0.0, source="x", type="program_change", channel=None,
                    data1=5, data2=None, summary="program_change p5")
    assert page.handle(ev) is False
    assert page.view_model()["count"] == 0
