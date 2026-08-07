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


# -- chord_candidates_all_roots (phase-3 task 12, gap ports) -----------------
#
# Ported from v1's `pages/chordkey.py::_chord_candidates` -- a SEPARATE
# algorithm from detect_harmony_info/_best_matches above (see this
# function's own module-level comment for the full reasoning): no
# require-root-in-pcs gate, no min-ratio floor, no ambiguity cutoff, always
# ranks and returns up to top_n.

def test_chord_candidates_all_roots_empty_pcs_returns_empty():
    assert theory.chord_candidates_all_roots(set()) == []


def test_chord_candidates_all_roots_finds_exact_major_triad_as_top():
    results = theory.chord_candidates_all_roots({0, 4, 7})
    assert results[0]["label"] == "C maj"
    assert results[0]["ratio"] == 1.0
    assert results[0]["match"] == 3
    assert results[0]["missing"] == []


def test_chord_candidates_all_roots_does_not_require_root_in_pcs():
    # Unlike detect_harmony_info's require_root_in_pcs=True gate, v1's
    # chordkey.py tries EVERY root 0-11 regardless of whether it's actually
    # sounding -- {4, 7} (E, G: a C major triad missing its root) still
    # finds "C maj" even though pitch class 0 is absent from the input.
    results = theory.chord_candidates_all_roots({4, 7})
    labels = [r["label"] for r in results]
    assert "C maj" in labels
    hit = next(r for r in results if r["label"] == "C maj")
    assert hit["missing"] == [0]


def test_chord_candidates_all_roots_respects_top_n():
    results = theory.chord_candidates_all_roots({0, 4, 7}, top_n=1)
    assert len(results) == 1


def test_chord_candidates_all_roots_never_returns_a_duplicate_label():
    results = theory.chord_candidates_all_roots({0, 4, 7}, top_n=50)
    labels = [r["label"] for r in results]
    assert len(labels) == len(set(labels))


def test_chord_candidates_all_roots_below_two_matches_is_excluded():
    # A lone pitch class can never reach match >= 2 against any chord shape.
    assert theory.chord_candidates_all_roots({0}) == []


# -- roman_numeral_for_chord (phase-3 task 12, gap ports) --------------------
#
# Ported verbatim from v1's `plugins/zharmony.py::_roman_for_chord`.

def test_roman_numeral_for_chord_returns_none_without_both_inputs():
    assert theory.roman_numeral_for_chord(None, "C maj") is None
    assert theory.roman_numeral_for_chord({"root": 0, "name": "maj"}, None) is None
    assert theory.roman_numeral_for_chord([], "C maj") is None


def test_roman_numeral_for_chord_tonic_major_in_c_major():
    # Real, faithfully-ported v1 quirk (see roman_numeral_for_chord's own
    # docstring): the lowercase gate is `startswith("m")`, and this
    # asset's major-chord entry is literally named "maj" -- so even a
    # MAJOR tonic renders lower-case "i", not "I". Verified against v1's
    # actual `_roman_for_chord` source, not a porting mistake.
    info = {"root": 0, "name": "maj"}
    assert theory.roman_numeral_for_chord(info, "C maj") == "i (T)"


def test_roman_numeral_for_chord_accepts_a_candidate_list_using_the_first():
    info_list = [{"root": 0, "name": "maj"}, {"root": 7, "name": "maj"}]
    assert theory.roman_numeral_for_chord(info_list, "C maj") == "i (T)"


def test_roman_numeral_for_chord_dominant_major_in_c_major():
    info = {"root": 7, "name": "maj"}   # G major -- the 5th degree
    assert theory.roman_numeral_for_chord(info, "C maj") == "v (D)"   # "maj".startswith("m") quirk


def test_roman_numeral_for_chord_subdominant_major_in_c_major():
    info = {"root": 5, "name": "maj"}   # F major -- the 4th degree
    assert theory.roman_numeral_for_chord(info, "C maj") == "iv (S)"   # same quirk


def test_roman_numeral_for_chord_minor_chord_lowercases_the_numeral():
    info = {"root": 2, "name": "m"}   # D minor -- ii in C major
    assert theory.roman_numeral_for_chord(info, "C maj") == "ii"


def test_roman_numeral_for_chord_a_name_that_truly_says_dim_or_aug_gets_the_suffix():
    # The suffix check is a plain substring test on the chord's `name` --
    # exercised here with hand-built dicts naming a chord "dim"/"aug"
    # explicitly, since THIS asset's real diminished-chord row is named
    # the symbol "°" (not the word "dim"), which never matches the
    # substring check at all (see this function's own docstring).
    dim = {"root": 11, "name": "dim"}
    assert theory.roman_numeral_for_chord(dim, "C maj") == "VII°"   # "dim" doesn't trigger lowercase
    aug = {"root": 0, "name": "aug"}
    assert theory.roman_numeral_for_chord(aug, "C maj") == "I+ (T)"   # root=0 is always tonic too


def test_roman_numeral_for_chord_real_chords_asset_diminished_row_gets_no_suffix():
    # Disclosed real-data interaction: this asset's ACTUAL diminished
    # entry is named "°" (assets/chords.csv), which does not contain the
    # substring "dim" -- so a real detect_harmony_info() hit for it never
    # gets a second "°" appended, unlike the synthetic "dim"-named case
    # above.
    info = {"root": 11, "name": "°"}
    assert theory.roman_numeral_for_chord(info, "C maj") == "VII"


def test_roman_numeral_for_chord_minor_key_flat_seven_is_dominant_substitute():
    # v1's special case: interval 10 (bVII) in a MINOR key is treated as
    # dominant -- the natural/harmonic-minor dominant substitute.
    info = {"root": 10, "name": "maj"}   # Bb major relative to C minor
    assert theory.roman_numeral_for_chord(info, "C min") == "bvii (D)"   # same "maj" quirk


def test_roman_numeral_for_chord_unparseable_key_label_returns_none():
    assert theory.roman_numeral_for_chord({"root": 0, "name": "maj"}, "not-a-key") is None
    assert theory.roman_numeral_for_chord({"root": 0, "name": "maj"}, "Z maj") is None


def test_roman_numeral_for_chord_missing_root_returns_none():
    assert theory.roman_numeral_for_chord({"name": "maj"}, "C maj") is None
