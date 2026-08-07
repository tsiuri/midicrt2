"""TDD for analyzers/theory.py: the pure chord/scale pitch-class matching
engine ported from v1's `~/codex/midicrt/harmony.py`. Pure functions only
(no state) -- these tests exercise interval parsing, the CSV-backed chord/
scale databases, and the match/tie/confidence scoring `HarmonyAnalyzer`
(tests/test_analyzers_harmony.py) builds on.
"""
from midicrt.analyzers import theory


def test_chords_and_scales_databases_loaded_from_vendored_assets():
    # Sanity guard against theory.py's v1-matched silent-empty-on-failure
    # fallback (_load_db) masking a broken/missing asset copy-out.
    #
    # Exact counts (8 chords, 9 scales) verified against a real run, NOT
    # guessed: most rows in v1's own chords.csv/scales.csv have a BLANK
    # "name" column (only an "aka" alternate-spelling list) -- e.g.
    # "1-3-5-6,\"add6, maj6, add13\"," has no canonical short name --
    # and `_load_db`'s `if not name or not intervals: continue` guard
    # (ported verbatim from v1's harmony.py) intentionally skips those.
    # This is real v1 behavior, not a copy-out defect: v1's own loader
    # would drop exactly the same rows from the exact same file.
    assert [c["name"] for c in theory.CHORDS] == [
        "maj", "m", "7", "°", "m7(b5)", "+", "1-b3-x", "1-5",
    ]
    assert len(theory.SCALES) == 9
    assert theory.CHORDS_CSV.exists()
    assert theory.SCALES_CSV.exists()


def test_parse_intervals_simple_major_triad():
    assert theory._parse_intervals("1-3-5") == {0, 4, 7}


def test_parse_intervals_handles_flats_sharps_and_x():
    assert theory._parse_intervals("1-b3-5") == {0, 3, 7}          # minor triad
    assert theory._parse_intervals("1-3-#5") == {0, 4, 8}          # augmented
    assert theory._parse_intervals("1-b3-bb7") == {0, 3, 9}        # double-flat 7th
    assert theory._parse_intervals("1-x-5") == {0, 7}              # "x" token is skipped


def test_parse_intervals_extended_degrees_wrap_to_pitch_class():
    # degree 9 == root + 14 semitones == pitch class 2 (same as a "2").
    assert theory._parse_intervals("1-9") == {0, 2}
    assert theory._parse_intervals("1-13") == {0, 9}   # 13 == root + 21 semitones -> pc 9


def test_detect_harmony_info_c_major_triad():
    chord, _scale = theory.detect_harmony_info({0, 4, 7})
    assert chord is not None
    assert any(c["label"] == "C maj" for c in chord)
    top = chord[0]
    assert top["ratio"] == 1.0
    assert top["missing"] == []


def test_detect_harmony_info_a_minor_triad():
    # A minor triad pitch classes: A=9, C=0, E=4.
    chord, _scale = theory.detect_harmony_info({9, 0, 4})
    assert chord is not None
    assert any(c["label"] == "A m" for c in chord)


def test_detect_harmony_info_c_major_scale_window():
    pcs = {0, 2, 4, 5, 7, 9, 11}   # C D E F G A B
    _chord, scale = theory.detect_harmony_info(pcs)
    assert scale is not None
    assert any(s["label"] == "C Ionian" for s in scale)
    top = next(s for s in scale if s["label"] == "C Ionian")
    assert top["ratio"] == 1.0
    assert top["missing"] == []


def test_detect_harmony_info_too_few_notes_for_chord_or_scale():
    chord, scale = theory.detect_harmony_info({0})
    assert chord is None
    assert scale is None


def test_detect_harmony_info_chromatic_cluster_has_no_confident_chord():
    # A tight semitone cluster matches nothing in the chord DB at a >=0.6
    # match ratio -- v1's own low-confidence/ambiguous case.
    chord, _scale = theory.detect_harmony_info({0, 1, 2, 3})
    assert chord is None


def test_best_matches_ambiguous_tie_returns_none_beyond_max_ties():
    # Construct a DB with 4 identical-scoring entries at different roots so
    # the tie-count exceeds max_ties=3 -- v1 treats "too many equally good
    # guesses" as no match at all, not an arbitrary pick.
    db = [{"name": f"x{i}", "pcs": {0, 4, 7}} for i in range(4)]
    hits = theory._best_matches({0, 4, 7}, db, require_root_in_pcs=True, min_match=3, min_ratio=1.0)
    assert hits is None
