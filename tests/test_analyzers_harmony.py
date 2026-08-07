"""TDD for HarmonyAnalyzer: a pure state machine fed `MidiEvent`s (no I/O,
no clock reads), ported from v1's `~/codex/midicrt/plugins/zharmony.py`
(chord/scale/key/tension/harmonic-rhythm/motif) plus, for the sounding-
pitch set tension reads, `~/codex/midicrt/plugins/polydisplay.py` -- see
`analyzers/harmony.py`'s module docstring for the full behavioral
synthesis (two-source merge, eager-vs-lazy recompute adaptation, the
always-forced key-hysteresis finding, and the deliberately-not-ported
list: roman-numeral function, time-based note pruning, draw-throttle
knobs, the UI-only tension hold).

Music-theory regression cases from the task brief: C-E-G -> C major,
A-C-E -> A minor, a C-D-E-F-G-A-B window -> C major scale ranked top, a
chromatic cluster -> low confidence/high tension, silence -> silent flag.
"""
from midicrt.analyzers.harmony import HarmonyAnalyzer
from midicrt.engine.core import MidiEvent


def note_on(ch0, note, vel=100, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def note_off(ch0, note, vel=64, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="note_off", channel=ch0,
                      data1=note, data2=vel, summary=f"note_off ch{ch0 + 1} n{note} v{vel}")


def cc(ch0, control, value, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type="control_change", channel=ch0,
                      data1=control, data2=value,
                      summary=f"control_change ch{ch0 + 1} cc{control} v{value}")


def transport(kind, ts=0.0, source="USB MIDI"):
    return MidiEvent(ts=ts, source=source, type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


# -- initial state / VM shape -------------------------------------------------

def test_initial_view_model_is_all_empty_and_silent():
    a = HarmonyAnalyzer()
    vm = a.view_model()
    assert vm["title"] == "HARMONY"
    assert vm["chords"] == [] and vm["scales"] == []
    assert vm["inside"] == [] and vm["outside"] == []
    assert vm["key"] is None and vm["key_conf"] == 0.0 and vm["key_alternatives"] == []
    assert vm["tension"] == 0.0 and vm["tension_label"] == "silent"
    assert vm["harmonic_rhythm"] == {"changes_per_bar": None, "label": ""}
    assert vm["motif"] == {"found": False, "pattern": None, "count": 0}
    assert vm["silent"] is True


def test_note_on_with_no_channel_is_a_safe_noop():
    a = HarmonyAnalyzer()
    changed = a.handle(MidiEvent(ts=0.0, source="x", type="note_on", channel=None,
                                 data1=60, data2=100, summary="note_on"))
    assert changed is False


def test_note_off_with_no_channel_is_a_safe_noop():
    a = HarmonyAnalyzer()
    changed = a.handle(MidiEvent(ts=0.0, source="x", type="note_off", channel=None,
                                 data1=60, data2=64, summary="note_off"))
    assert changed is False


def test_unrelated_event_types_are_a_pure_noop():
    a = HarmonyAnalyzer()
    changed = a.handle(MidiEvent(ts=0.0, source="clock", type="clock_tick", channel=None,
                                 data1=24, data2=None, summary="clock_tick"))
    assert changed is False


# -- chord detection -----------------------------------------------------------

def test_c_major_triad_is_recognised_as_top_chord():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))   # C4
    a.handle(note_on(0, 64))   # E4
    changed = a.handle(note_on(0, 67))   # G4
    assert changed is True
    vm = a.view_model()
    assert vm["chords"][0]["name"] == "C maj"
    assert vm["chords"][0]["conf"] == 1.0
    assert vm["chords"][0]["missing"] == []
    assert vm["silent"] is False


def test_a_minor_triad_is_recognised():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 69))   # A4
    a.handle(note_on(0, 60))   # C4 (up an octave doesn't matter -- pitch class only)
    a.handle(note_on(0, 64))   # E4
    vm = a.view_model()
    assert vm["chords"][0]["name"] == "A m"


def test_chord_history_keeps_last_four_distinct_labels_most_recent_first():
    # Chord/scale detection reads `_recent_notes` -- a rolling window of the
    # last RECENT_NOTE_COUNT=24 NOTE-ONS ever played (union-of-history, per
    # zharmony.py; note-off is irrelevant to it, see module docstring) --
    # NOT the currently-held set. To observe a clean transition between
    # three distinct chords we must play enough repetitions of each triad
    # (9 * 3 = 27 > 24) that the window fully evicts the previous chord's
    # notes before we check the result; a single play-then-release per
    # chord would leave all three chords' pitch classes mixed in the same
    # 24-note window at once. Exact history contents verified against a
    # real run (see task-5 report) rather than hand-derived: early in each
    # phase's ramp-up, small transient pitch-class subsets legitimately
    # match OTHER chords first (real, honest few-note ambiguity -- not a
    # bug) before the window fully saturates on the intended triad; only
    # `history[0]` (the CURRENT, fully-settled identification) is asserted
    # precisely for that reason.
    a = HarmonyAnalyzer()
    for notes, ts in (([60, 64, 67], 0.0), ([62, 66, 69], 2.0), ([64, 67, 71], 4.0)):
        for _rep in range(9):
            for n in notes:
                a.handle(note_on(0, n, ts=ts))
    vm = a.view_model()
    names = [c["name"] for c in vm["chords"]]
    assert names[0] == "E m"
    assert "D maj" in names and "C maj" in names
    assert len(vm["chords"]) <= 4


def test_only_the_current_history_slot_carries_real_confidence():
    a = HarmonyAnalyzer()
    for n in (60, 64, 67):
        a.handle(note_on(0, n))
    for n in (60, 64, 67):
        a.handle(note_off(0, n))
    for n in (62, 66, 69):
        a.handle(note_on(0, n))
    vm = a.view_model()
    assert vm["chords"][0]["conf"] is not None   # current: D maj
    assert vm["chords"][1]["conf"] is None        # history-only: C maj
    assert vm["chords"][1]["missing"] == []


# -- scale detection + inside/outside -----------------------------------------

def test_c_major_scale_window_ranks_top_with_full_inside_no_outside():
    a = HarmonyAnalyzer()
    for n in (60, 62, 64, 65, 67, 69, 71):   # C D E F G A B
        a.handle(note_on(0, n))
    vm = a.view_model()
    assert vm["scales"][0]["name"] == "C Ionian"
    assert vm["scales"][0]["conf"] == 1.0
    assert sorted(vm["inside"]) == sorted(["C", "D", "E", "F", "G", "A", "B"])
    assert vm["outside"] == []


def test_note_outside_the_established_scale_is_classified_as_outside():
    a = HarmonyAnalyzer()
    for n in (60, 62, 64, 65, 67, 69, 71):   # establishes C major
        a.handle(note_on(0, n))
    a.handle(note_on(0, 61))   # C# -- not in C major
    vm = a.view_model()
    assert "C#" in vm["outside"]
    assert "C#" not in vm["inside"]


def test_no_scale_detected_yet_gives_empty_inside_and_outside():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    vm = a.view_model()
    assert vm["scales"] == []
    assert vm["inside"] == [] and vm["outside"] == []


# -- chromatic cluster: low confidence / high tension -------------------------

def test_chromatic_cluster_has_no_confident_chord_and_high_tension():
    # Full 4-note cluster {0,1,2,3}: direct theory.detect_harmony_info
    # confirms no chord clears CHORD_MIN_RATIO at this pcs (see
    # test_analyzers_theory.py's own chromatic-cluster case) -- so the
    # CURRENT confidence is None. `chords[0]["name"]` still shows a
    # remembered label from the 3-note ramp-up ("D 7 / D m7(b5)", a real,
    # honest match at the smaller 3-pc intermediate state reached on the
    # way to 4 notes -- verified against a real run) because history only
    # updates on a NEW truthy label, never clears on a later None; this is
    # the same real v1 quirk `_candidates_vm`'s docstring documents
    # (label persists, confidence does not). The "low confidence" half of
    # this test's brief is exactly `conf is None`, an even stronger
    # signal than a merely-low number.
    a = HarmonyAnalyzer()
    for n in (60, 61, 62, 63):   # tight semitone cluster, all held
        a.handle(note_on(0, n))
    vm = a.view_model()
    assert vm["chords"][0]["conf"] is None
    assert vm["tension"] > 0.5
    assert vm["tension_label"] in ("tense", "dissonant", "harsh")
    assert vm["silent"] is False   # notes ARE sounding -- just dissonant


# -- tension / silence ----------------------------------------------------------

def test_single_held_note_is_silent_tension():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    vm = a.view_model()
    assert vm["tension"] == 0.0 and vm["tension_label"] == "silent"
    assert vm["silent"] is False   # a note IS sounding -- "silent" means NO notes


def test_releasing_all_notes_sets_silent_flag():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    assert a.view_model()["silent"] is False
    a.handle(note_off(0, 60))
    changed = a.handle(note_off(0, 64))
    assert changed is True
    vm = a.view_model()
    assert vm["silent"] is True
    assert vm["tension"] == 0.0 and vm["tension_label"] == "silent"


def test_velocity_zero_note_on_is_treated_as_note_off():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 60, vel=0))
    assert a.view_model()["silent"] is True


def test_tritone_dyad_names_the_worst_interval():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))   # C
    a.handle(note_on(0, 66))   # F# -- tritone
    vm = a.view_model()
    assert vm["tension_worst_interval"] == "tritone"
    assert vm["tension"] == 1.0   # tritone weight is 1.0 -> max score


# -- sounding-set semantics (polydisplay-modeled; diverges from voices.py) ----

def test_cc120_does_not_clear_the_sounding_set_unlike_voices_analyzer():
    # Regression: polydisplay.py's active_notes (this analyzer's source
    # for the sounding-pitch set) has NO control_change handling at all --
    # only note_off/velocity-0 clears a note. This is a deliberate
    # divergence from analyzers/voices.py's VoiceMonitorAnalyzer, which
    # DOES clear on CC120/123 (that's zvoicemonitor.py's behavior, a
    # different v1 source file).
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(cc(0, 120, 0))
    assert changed is False
    assert a.view_model()["silent"] is False


def test_start_clears_sounding_set_but_not_chord_scale_history():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    a.handle(note_on(0, 67))   # C major triad established
    changed = a.handle(transport("start"))
    assert changed is True
    vm = a.view_model()
    assert vm["silent"] is True                 # sounding set cleared
    assert vm["chords"][0]["name"] == "C maj"    # zharmony.py has no transport awareness


def test_stop_clears_sounding_set_but_not_chord_scale_history():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    changed = a.handle(transport("stop"))
    assert changed is True
    assert a.view_model()["silent"] is True


def test_clear_sounding_is_a_noop_when_nothing_is_held():
    a = HarmonyAnalyzer()
    changed = a.handle(transport("start"))
    assert changed is False


def test_note_off_for_a_note_never_on_is_a_true_noop():
    a = HarmonyAnalyzer()
    changed = a.handle(note_off(0, 60))
    assert changed is False


def test_overlapping_retrigger_needs_only_one_note_off_unlike_voices_analyzer():
    # polydisplay.py's active_notes is a plain SET (add-replaces), not a
    # count like zvoicemonitor.py's -- a single note-off fully clears the
    # pitch even after two overlapping note-ons, unlike
    # analyzers/voices.py's VoiceMonitorAnalyzer.
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 60))
    a.handle(note_off(0, 60))
    assert a.view_model()["silent"] is True


# -- key estimation --------------------------------------------------------------

def test_sustained_c_major_material_converges_on_c_major_key():
    a = HarmonyAnalyzer()
    scale = [60, 62, 64, 65, 67, 69, 71]
    for _ in range(6):
        for n in scale:
            a.handle(note_on(0, n))
            a.handle(note_off(0, n))
    vm = a.view_model()
    assert vm["key"] == "C maj"
    assert vm["key_conf"] > 0.7


def test_a_single_note_trivially_meets_v1s_key_confidence_threshold():
    # Real, disclosed v1 quirk (not a v2 regression): KEY_CONFIDENCE_
    # THRESHOLD (0.72) is a bare ratio with no minimum-evidence floor --
    # with exactly one note played, EVERY major/minor scale containing
    # that pitch class scores ratio=1.0 (100% of the tiny sample fits),
    # so the very first note ever played immediately snaps `_stable_key`
    # to whichever tied candidate sorts first (root ascending, "maj"
    # before "min" -- see `_key_candidates`'s iteration order), here "C
    # maj" for a single C. v1's own `_update_stable_key` does the
    # identical bare threshold check, so this is a faithful port of a
    # real (if perhaps surprising) v1 behavior, verified against a real
    # run rather than assumed.
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    vm = a.view_model()
    assert vm["key"] == "C maj"
    assert vm["key_conf"] == 1.0


# -- harmonic rhythm (ev.ts-derived, no clock reads) ------------------------------

def test_harmonic_rhythm_is_none_with_fewer_than_two_chord_changes():
    a = HarmonyAnalyzer()
    for n in (60, 64, 67):
        a.handle(note_on(0, n, ts=0.0))
    vm = a.view_model()
    assert vm["harmonic_rhythm"] == {"changes_per_bar": None, "label": ""}


def test_harmonic_rhythm_computed_from_event_timestamps_not_a_clock_read():
    # Same saturation strategy as the chord-history test above (each chord
    # change's timestamp is recorded via `ev.ts`, never a clock read).
    # Every note-on within a phase shares that phase's `ts`, so it doesn't
    # matter exactly which note inside the ramp-up first flips the label --
    # whichever one does, it stamps the SAME ts as the rest of its phase.
    # Expected changes_per_bar=2.0 verified against a real run (not hand-
    # derived): the ramp-up's transient intermediate labels (see the
    # chord-history test above) contribute their own same-phase-timestamp
    # entries into `_chord_change_times`, so the averaged interval is 1.0s
    # (not the naive "2s between phases"), giving 2.0 changes/bar at
    # 120bpm 4/4 (2s/bar / 1.0s = 2.0) -- label "fast" (1.5 <= cpb < 3.0).
    a = HarmonyAnalyzer()
    for notes, ts in (([60, 64, 67], 0.0), ([62, 66, 69], 2.0), ([64, 67, 71], 4.0)):
        for _rep in range(9):
            for n in notes:
                a.handle(note_on(0, n, ts=ts))
    vm = a.view_model()
    hr = vm["harmonic_rhythm"]
    assert hr["changes_per_bar"] is not None
    assert abs(hr["changes_per_bar"] - 2.0) < 1e-9
    assert hr["label"] == "fast"


# -- motif detection ---------------------------------------------------------------

def test_motif_detected_when_an_interval_pattern_recurs():
    a = HarmonyAnalyzer()
    # Pattern +2 -1 +4 (transposable, signed semitone deltas) played twice.
    for n in (60, 62, 61, 65, 67, 69, 68, 72):
        a.handle(note_on(0, n))
    vm = a.view_model()
    assert vm["motif"]["found"] is True
    assert vm["motif"]["count"] >= 1
    assert vm["motif"]["pattern"] is not None


def test_no_motif_with_too_short_an_interval_history():
    a = HarmonyAnalyzer()
    for n in (60, 62, 64):
        a.handle(note_on(0, n))
    vm = a.view_model()
    assert vm["motif"] == {"found": False, "pattern": None, "count": 0}


# -- phase-3 task 12 accessors (recent_pcs / chord_info / key_detail) -------
#
# Additive, read-only accessors for pages/chordkey.py -- see
# analyzers/harmony.py's own docstring section for why these exist
# alongside (not instead of) the task-5 view_model() contract.

def test_recent_pcs_matches_the_current_note_on_window():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))   # C
    a.handle(note_on(0, 64))   # E
    assert a.recent_pcs == {0, 4}


def test_recent_pcs_is_empty_before_any_note():
    a = HarmonyAnalyzer()
    assert a.recent_pcs == set()


def test_chord_info_matches_the_current_chord_detection():
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 64))
    a.handle(note_on(0, 67))   # C major triad
    info = a.chord_info
    assert info is not None
    assert any(c["label"] == "C maj" for c in info)


def test_chord_info_is_none_before_any_note():
    a = HarmonyAnalyzer()
    assert a.chord_info is None


def test_key_detail_shape_before_any_note():
    a = HarmonyAnalyzer()
    detail = a.key_detail()
    assert detail == {
        "label": None, "confidence": 0.0, "threshold": 0.72,
        "alternatives": [], "top": None, "ambiguous": True,
    }


def test_key_detail_reports_top_and_confidence_for_a_single_note():
    # Mirrors test_a_single_note_trivially_meets_v1s_key_confidence_
    # threshold's own finding: one note snaps _stable_key immediately, and
    # key_detail()'s "top" candidate agrees with it exactly.
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    detail = a.key_detail()
    assert detail["label"] == "C maj"
    assert detail["confidence"] == 1.0
    assert detail["top"] == {"label": "C maj", "ratio": 1.0}
    assert detail["ambiguous"] is False


def test_key_detail_matches_view_models_own_key_fields_once_established():
    a = HarmonyAnalyzer()
    scale = [60, 62, 64, 65, 67, 69, 71]
    for _ in range(6):
        for n in scale:
            a.handle(note_on(0, n))
            a.handle(note_off(0, n))
    vm = a.view_model()
    detail = a.key_detail()
    assert detail["label"] == vm["key"]
    assert detail["confidence"] == vm["key_conf"]
    assert [alt["label"] for alt in detail["alternatives"]] == vm["key_alternatives"]


# -- shared-instance dedup guard (Important, finding 2b, 2026-08-07 fix
# wave) -------------------------------------------------------------------
#
# engine/core.py now shares ONE HarmonyAnalyzer instance between
# pages/harmony.py's HarmonyPage and pages/chordkey.py's ChordKeyPage when
# both make the roster (a live-measured perf fix: each page used to own an
# independent instance, doubling every note_on's chord/scale detection
# cost for no behavioral benefit -- both pages see the identical event
# stream regardless). Engine._handle() still calls EVERY page's own
# handle(ev) once per event (the "every page sees every event" dirty-
# tracking contract, engine/core.py's own module docstring) -- so both
# pages' handle() calls land on this SAME analyzer, with the exact SAME
# MidiEvent object reference (Engine passes one shared `ev` to the whole
# roster for a given tick). Without a dedup guard this analyzer would
# process one real note TWICE, corrupting `_recent_notes`/`_key_counts`
# (duplicate entries), not merely wasting cycles.

def test_handle_is_idempotent_for_the_same_event_object():
    # `view_model()`'s public fields (key_conf/tension/etc.) are computed
    # from SETS that don't distinguish "seen once" from "seen twice" for
    # the exact same pitch class -- `_recent_notes`'s length is the most
    # direct observable of the dedup guard actually doing its job,
    # deliberately checked at the internal-state level here since no
    # black-box observable cleanly isolates this specific invariant.
    a = HarmonyAnalyzer()
    ev = note_on(0, 60)
    first = a.handle(ev)
    second = a.handle(ev)          # SAME object reference -- must be a no-op
    assert first is True
    assert second is True          # reports the SAME dirty result, not False
    assert len(a._recent_notes) == 1


def test_handle_processes_two_distinct_event_objects_normally():
    # Two DIFFERENT MidiEvent objects (as every real distinct MIDI event
    # naturally is, even with identical field values) must both be
    # processed -- the dedup guard keys on object IDENTITY, not equality.
    a = HarmonyAnalyzer()
    a.handle(note_on(0, 60))
    a.handle(note_on(0, 60))       # a DIFFERENT object, same field values
    assert len(a._recent_notes) == 2


def test_handle_reprocesses_normally_after_an_intervening_different_event():
    # The dedup guard must only ever suppress the IMMEDIATELY-repeated
    # same object, not latch permanently -- a real engine tick always
    # moves on to a genuinely new MidiEvent next.
    a = HarmonyAnalyzer()
    ev1 = note_on(0, 60)
    ev2 = note_on(0, 64)
    a.handle(ev1)
    a.handle(ev1)   # dedup no-op
    a.handle(ev2)
    a.handle(ev1)   # a THIRD, later call with the original object -- processes again
    assert len(a._recent_notes) == 3
