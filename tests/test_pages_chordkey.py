"""TDD for ChordKeyPage -- see pages/chordkey.py's own module docstring for
the full v1 (`pages/chordkey.py`) behavioral synthesis: chord candidates
via analyzers.theory.chord_candidates_all_roots, key detail via
HarmonyAnalyzer.key_detail(), harmonic function via
analyzers.theory.roman_numeral_for_chord.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.chordkey import ChordKeyPage


def note_on(ch0, note, vel=100, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0+1} n{note} v{vel}")


def test_view_model_shape_before_any_note():
    page = ChordKeyPage()
    vm = page.view_model()
    assert vm["title"] == "CHORD+KEY"
    assert vm["recent_pcs"] == []
    assert vm["chords"] == []
    assert vm["key"] == {
        "label": None, "pct": None, "threshold_pct": 72,
        "ambiguous": True, "top": None, "alternatives": [],
    }
    assert vm["function"] is None


def test_handle_delegates_to_the_analyzer_and_updates_recent_pcs():
    page = ChordKeyPage()
    changed = page.handle(note_on(0, 60))   # C
    assert changed is True
    changed = page.handle(note_on(0, 64))   # E
    assert changed is True
    vm = page.view_model()
    assert vm["recent_pcs"] == ["C", "E"]


def test_c_major_triad_finds_c_maj_as_top_chord_candidate():
    page = ChordKeyPage()
    for n in (60, 64, 67):
        page.handle(note_on(0, n))
    vm = page.view_model()
    assert vm["chords"][0]["label"] == "C maj"
    assert vm["chords"][0]["pct"] == 100
    assert vm["chords"][0]["missing"] == []


def test_key_detail_flows_through_with_percent_formatting():
    page = ChordKeyPage()
    page.handle(note_on(0, 60))   # single note -- trivially meets threshold, v1 quirk
    vm = page.view_model()
    assert vm["key"]["label"] == "C maj"
    assert vm["key"]["pct"] == 100
    assert vm["key"]["top"] == {"label": "C maj", "pct": 100}
    assert vm["key"]["ambiguous"] is False


def test_function_is_none_until_a_chord_and_key_both_exist():
    page = ChordKeyPage()
    assert page.view_model()["function"] is None


def test_function_reflects_the_current_chord_relative_to_the_current_key():
    page = ChordKeyPage()
    for n in (60, 64, 67):   # C major triad -- also a single trivially-confident C maj key
        page.handle(note_on(0, n))
    vm = page.view_model()
    # Real, faithfully-ported v1 quirk (see analyzers/theory.py's
    # roman_numeral_for_chord docstring): "maj".startswith("m") lowercases
    # even a major-chord tonic numeral.
    assert vm["function"] == "i (T)"


def test_unrelated_event_is_not_dirty():
    page = ChordKeyPage()
    ev = MidiEvent(ts=0.0, source="x", type="control_change", channel=0, data1=1, data2=1,
                    summary="control_change ch1 cc1 v1")
    assert page.handle(ev) is False
