"""Chord+Key page (page name "chordkey"): v1's `~/codex/midicrt/pages/
chordkey.py` (PAGE_ID 11, "Chord+Key", 116 lines, READ-ONLY reference) --
a second, more compact chord/key display, DISTINCT from the harmony fields
`pages/harmony.py` (v2's port of v1's Notes page, PAGE_ID 1) already shows.

What T11's checklist called "consolidated-ish" -- read and resolved here
---------------------------------------------------------------------------
The task-11 inventory sweep's own note: "chordkey.py's own distinct compact
layout was never separately built... underlying analyzer data is available
(no new analyzer work needed), just no page presents it in this layout."
Reading `chordkey.py` in full for this task shows that claim was only
PARTLY right -- three things it shows are genuinely absent from
`analyzers/harmony.py`'s existing task-5 VM contract, not just a
presentation gap:

1. **Independent, cross-all-roots chord candidates** (`_chord_candidates`,
   this page's own local function, NOT a call into `zharmony.py`/
   `theory.py`'s `detect_harmony_info`): tries every root 0-11 regardless
   of whether it's actually sounding, has no minimum-ratio floor, and
   always ranks/returns up to 3 -- genuinely different selection semantics
   from `analyzers/harmony.py`'s "Chord:" history ticker (which requires
   the root to already be in the sounding pcs and reports "no match" on
   ambiguity). Ported as `analyzers.theory.chord_candidates_all_roots` --
   a THEORY-level pure function (it only needs `CHORDS`/`NOTE_NAMES`), not
   a `HarmonyAnalyzer` method.
2. **`get_stable_key()`'s full `top`/`ambiguous` picture**: `harmony.py`'s
   own docstring explicitly says these are "NOT surfaced -- no field for
   either in the task's VM contract." `chordkey.py`'s "Key: ? top:G major
   80%" fallback line and "~" (ambiguous) vs "=" (confident) tag need
   exactly this -- ported as `HarmonyAnalyzer.key_detail()` (see that
   module's own docstring), a NEW pure accessor, additive to the existing
   class.
3. **Roman-numeral harmonic function** (`zharmony.py::get_last_
   function_label()`): `harmony.py`'s own docstring explicitly lists this
   as "out of scope" for task 5. Ported as
   `analyzers.theory.roman_numeral_for_chord` (a pure function of
   `(chord_info, key_label)`, needing no new mutable analyzer state at
   all -- see that function's own docstring for a real, faithfully-ported
   v1 quirk: `"maj".startswith("m")` lower-cases even MAJOR-chord
   numerals in the real, deployed CHORDS asset).

Separate analyzer INSTANCE (disclosed, mirrors pages/ccmonitor.py's own
precedent)
---------------------------------------------------------------------------
`zharmony.py::get_recent_pcs()` is v1's SAME chord/scale-detection input
window `pages/harmony.py` already wraps via `HarmonyAnalyzer`. v2 has no
cross-page/analyzer state-sharing mechanism yet (the same "currently
latent" gap that module's own docstring calls out for cross-analyzer BPM
reads) -- so this page owns its OWN `HarmonyAnalyzer()` instance rather
than reaching into `pages/harmony.py`'s. Both pages see the identical
event stream (`Engine._handle` calls every page's `handle(ev)`, not just
the current page's), so they always agree; this just duplicates the
(cheap) computation, not the underlying MIDI data.

v1-field -> VM-field mapping
---------------------------------------------------------------------------
- `zharmony.get_recent_pcs()` (formatted as note names, "(none)" when
  empty) -> `recent_pcs`: a sorted list of note-name strings.
- `_chord_candidates(pcs)` (v1's own local scoring, top 3) ->
  `chords`: list of `{"label", "pct", "missing"}` (pct = ratio*100,
  rounded; missing = note names, not raw pitch classes).
- `get_stable_key()` -> `key`: `{"label", "pct", "threshold_pct",
  "ambiguous", "top": {"label","pct"}|None, "alternatives":
  [{"label","pct"}, ...]}` -- `pct`/`threshold_pct` adapt v1's own
  `int(round(ratio * 100))` display convention exactly.
- `get_last_function_label()` (v1's cached, change-triggered value) ->
  `function`: computed FRESH on every `view_model()` call from the
  CURRENT `chord_info`/`key_detail()["label"]` rather than cached on a
  chord-label-change trigger -- the same "eager vs v1's frame-cache"
  adaptation `analyzers/harmony.py`'s own docstring already makes for
  `_update_stable_key`: v2's `view_model()` has no per-frame redraw loop
  to cache against, and the two can only ever disagree in the same brief
  window v1's OWN cache would already be stale in (immediately after a
  key change, before the next chord-label change refreshes v1's cache
  too) -- a disclosed, not a behavior-changing, simplification.
"""
from midicrt.analyzers import theory
from midicrt.analyzers.harmony import HarmonyAnalyzer
from midicrt.analyzers.theory import NOTE_NAMES


def _pct(ratio: float) -> int:
    return round(ratio * 100)


class ChordKeyPage:
    name = "chordkey"

    def __init__(self) -> None:
        self._analyzer = HarmonyAnalyzer()

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def view_model(self) -> dict:
        pcs = self._analyzer.recent_pcs
        candidates = theory.chord_candidates_all_roots(pcs, top_n=3)
        chords_vm = [
            {
                "label": c["label"],
                "pct": _pct(c["ratio"]),
                "missing": [NOTE_NAMES[p % 12] for p in c["missing"]],
            }
            for c in candidates
        ]

        key = self._analyzer.key_detail()
        key_vm = {
            "label": key["label"],
            "pct": _pct(key["confidence"]) if key["label"] else None,
            "threshold_pct": _pct(key["threshold"]),
            "ambiguous": key["ambiguous"],
            "top": ({"label": key["top"]["label"], "pct": _pct(key["top"]["ratio"])}
                    if key["top"] else None),
            "alternatives": [{"label": a["label"], "pct": _pct(a["ratio"])}
                              for a in key["alternatives"]],
        }

        function = theory.roman_numeral_for_chord(self._analyzer.chord_info, key["label"])

        return {
            "title": "CHORD+KEY",
            "recent_pcs": sorted(NOTE_NAMES[pc] for pc in pcs),
            "chords": chords_vm,
            "key": key_vm,
            "function": function,
        }
