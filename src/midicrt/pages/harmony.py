"""Harmony page: v1's Notes-page harmony fields (chord/scale/key/tension/
harmonic-rhythm/motif -- see docs/evidence-phase2-smoke/after.png for v1's
layout: "Chord: Last/2nd/3rd/4th", "Scale: Last/2nd/3rd/4th", "Inside:",
"Chord conf/missing", "Scale conf/missing", "Key: ?", the tension bar +
value + label, "Harm.rhy:", "Motif:"). Wraps `analyzers.harmony.
HarmonyAnalyzer` -- see that module's docstring for the full v1 behavioral
synthesis (zharmony.py + polydisplay.py), the eager-recompute adaptation,
and the deliberately-not-ported list.

Scope adaptation (disclosed): v1 combines the harmony fields onto the SAME
screen as the per-channel note display (`pages/notes.py`). v2 already
split that per-channel display into its own page in task 4
(`pages/voices.py`) -- this task continues that per-concern page split
(matching the `eventlog`/`voices` precedent) rather than reassembling v1's
single combined screen, so `harmony` shows ONLY the harmony fields.

v1-field -> VM-field mapping (see analyzers/harmony.py's own docstring for
the deeper "why" behind each choice)
---------------------------------------------------------------------------
- `zharmony.get_chord_history()` (v1's "Chord: Last/2nd/3rd/4th" labels,
  a change-history ticker, NOT four simultaneously-tied candidates) ->
  `chords`: list of 0-4 `{"name", "conf", "missing"}`. Only index 0 has
  real `conf`/`missing` (from `zharmony.get_chord_info()`'s current
  detection) -- v1 itself never tracked confidence per history slot.
- `zharmony.get_scale_history()`/`get_scale_stats_list()` -> `scales`:
  same shape/rule as `chords`. v1's numeric "Inside:" row (per-scale-slot
  cumulative in/total and unique-in/unique-total fractions) is NOT
  reproduced 1:1 -- see the next mapping instead.
- `zharmony.get_scale_pcs()` + the recent-note pitch-class window (the
  same set chord/scale detection itself runs on) -> `inside`/`outside`:
  note-name lists, the recent window's pitch classes split by membership
  in the CURRENT top scale's pitch classes. This is an honest adaptation
  of v1's numeric fraction display into the list-shaped contract the task
  brief asks for (`"inside": [...], "outside": [...]`), built from the
  same underlying data v1 computes, not new logic. Empty/empty (not
  everything-into-"outside") when no scale is established yet -- see
  `HarmonyAnalyzer.view_model()`'s own comment.
- `zharmony.get_stable_key()`'s `label`/`confidence` -> `key`/`key_conf`
  directly. Its `alternatives` -> the added `key_alternatives` field (see
  analyzers/harmony.py's "added beyond the letter" note). Its
  `top`/`ambiguous` (v1's "?→G major" hint) and the roman-numeral
  `get_last_function_label()` ("Fn: ?") are NOT surfaced -- no field for
  either in the task's VM contract.
- `zharmony.get_tension(active_pcs)`'s `(score 0-10, label, worst_ic)` ->
  `tension` (normalized to 0..1, per the task's "tension 0..1"),
  `tension_label`, `tension_worst_interval` (the latter two added beyond
  the letter of the minimal contract, see analyzers/harmony.py). The
  top-level `silent` bool is TRUE silence (no notes sounding at all,
  `len(active_pcs) == 0`) -- distinct from v1's tension LABEL "silent",
  which (a real v1 quirk, ported faithfully) also fires for any score
  below 0.5 even with 2+ notes held (e.g. a quiet octave/unison).
- `zharmony.get_harmonic_rhythm(bpm)` -> `harmonic_rhythm`:
  `{"changes_per_bar", "label"}`, always a dict (never bare `None`,
  unlike v1's `(None, "")` tuple) so a renderer never has to branch on
  the field's TYPE, only its `changes_per_bar` value.
- `zharmony.get_motif_info()` -> `motif`: `{"found", "pattern", "count"}`,
  same always-a-dict rationale as `harmonic_rhythm`.
"""
from midicrt.analyzers.harmony import HarmonyAnalyzer


class HarmonyPage:
    name = "harmony"

    def __init__(self, analyzer: HarmonyAnalyzer | None = None) -> None:
        # Finding 2b perf fix (2026-08-07 fix wave): `analyzer` is
        # OPTIONAL, defaulting to a fresh, independent instance -- every
        # existing standalone use (this page's own tests, any custom
        # roster with "harmony" but no "chordkey") keeps working exactly
        # as before. `engine/core.py`'s `Engine.__init__` passes in a
        # SHARED instance (also handed to `pages/chordkey.py`'s
        # `ChordKeyPage`) when both pages make the roster -- see that
        # wiring site's own comment and `analyzers/harmony.py`'s
        # shared-instance dedup guard for why sharing is safe, not just
        # cheaper.
        self._analyzer = analyzer if analyzer is not None else HarmonyAnalyzer()

    @property
    def analyzer(self) -> HarmonyAnalyzer:
        """Exposes the underlying analyzer so Engine.__init__ can hand the
        SAME instance to ChordKeyPage -- see __init__'s own comment."""
        return self._analyzer

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def view_model(self) -> dict:
        return self._analyzer.view_model()
