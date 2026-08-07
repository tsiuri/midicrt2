"""theory.py — chord/scale pitch-class matching engine.

Faithful port of v1's `~/codex/midicrt/harmony.py` (READ-ONLY reference on
the Pi) — the pure interval-string-parsing + best-match/scoring logic that
`analyzers/harmony.py`'s `HarmonyAnalyzer` (itself a port of v1's
`plugins/zharmony.py`) builds on. No behavior changes versus v1: interval
notation parsing (`1-3-5`, `1-b3-b5`, accidentals `b`/`#`/`x`), CSV loading,
and the match-scoring/tie logic below are transcribed branch-for-branch —
only the data-file path changed (v1's `config/chords.csv`/`config/
scales.csv` on the Pi filesystem -> this repo's `assets/chords.csv`/
`assets/scales.csv`, copied out verbatim, byte-for-byte diffed against the
Pi originals before this file was written).

Data loaded once at import time and cached at module level (`CHORDS`,
`SCALES`) — same "load a static shipped asset once" convention as
`clients/fb/text.py`'s vendored PSF font. `_load_db` mirrors v1's own
silent-empty-on-failure fallback (`except Exception: return []`) rather
than raising — matches v1 exactly, even though that means a corrupted/
missing asset would silently disable all chord/scale detection rather
than crashing loudly. `test_analyzers_theory.py` guards against this
being an unnoticed regression with a sanity check that `CHORDS`/`SCALES`
are non-empty after a real import.
"""
import csv
from pathlib import Path

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

DEGREE_BASE = {
    1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11,
    8: 12, 9: 14, 10: 16, 11: 17, 12: 19, 13: 21,
}

# assets/ lives at the repo root, a sibling of src/ (this file's path is
# src/midicrt/analyzers/theory.py -> parents[3] is the repo root -- same
# "count parents from this file" convention as clients/fb/text.py's
# _DEFAULT_FONT_PATH, one level shallower since this file lives one
# directory closer to src/ than clients/fb/text.py does).
_ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"
CHORDS_CSV = _ASSETS_DIR / "chords.csv"
SCALES_CSV = _ASSETS_DIR / "scales.csv"


def _note_name(pc: int) -> str:
    return NOTE_NAMES[pc % 12]


def _parse_intervals(intervals: str) -> set[int]:
    """Parse a v1 interval-notation string (e.g. `"1-b3-5-b7"`) into a set
    of pitch classes relative to the root (root == pitch class 0)."""
    pcs: set[int] = set()
    for raw in intervals.split("-"):
        token = raw.strip()
        if not token or token.lower() == "x":
            continue
        acc = 0
        i = 0
        while i < len(token) and token[i] in ("b", "#", "x"):
            if token[i] == "b":
                acc -= 1
            elif token[i] == "#":
                acc += 1
            elif token[i] == "x":
                acc += 2
            i += 1
        deg = token[i:]
        if not deg.isdigit():
            continue
        base = DEGREE_BASE.get(int(deg))
        if base is None:
            continue
        pcs.add((base + acc) % 12)
    return pcs


def _load_db(path: Path) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("name") or "").strip()
                intervals = (row.get("intervals") or "").strip()
                aka = (row.get("aka") or "").strip()
                if not name or not intervals:
                    continue
                pcs = _parse_intervals(intervals)
                if not pcs:
                    continue
                rows.append({"name": name, "intervals": intervals, "aka": aka, "pcs": pcs})
    except Exception:  # noqa: BLE001 -- verbatim fidelity to v1's own blind `except Exception`
        return []
    return rows


CHORDS = _load_db(CHORDS_CSV)
SCALES = _load_db(SCALES_CSV)


def _best_matches(pcs: set[int], db: list[dict], require_root_in_pcs: bool = False,
                   min_match: int = 2, min_ratio: float = 0.5,
                   max_ties: int = 3) -> list[dict] | None:
    """Return the tied best-scoring `{root, name, score}` matches for `pcs`
    against `db`, or `None` if nothing clears the threshold or there are
    more than `max_ties` equally-good matches (an ambiguous chord/scale is
    reported as "no match" rather than an arbitrary pick, matching v1)."""
    best_score = None
    hits: list[dict] = []
    for item in db:
        base = item["pcs"]
        for root in range(12):
            if require_root_in_pcs and root not in pcs:
                continue
            pattern = {(root + p) % 12 for p in base}
            match = len(pcs & pattern)
            if match < min_match:
                continue
            ratio = match / max(1, len(pcs))
            if ratio < min_ratio:
                continue
            extra = len(pcs - pattern)
            missing = len(pattern - pcs)
            score = (match, -extra, -missing, -len(pattern))
            if best_score is None or score > best_score:
                best_score = score
                hits = [{"root": root, "name": item["name"], "score": score}]
            elif score == best_score:
                hits.append({"root": root, "name": item["name"], "score": score})
    if not hits:
        return None
    if len(hits) > max_ties:
        return None
    return hits


def _make_info(hit: dict, db: list[dict], pcs: set[int] | None = None) -> dict | None:
    base = None
    for item in db:
        if item["name"] == hit["name"]:
            base = item["pcs"]
            break
    if base is None:
        return None
    root = hit["root"]
    pattern = {(root + p) % 12 for p in base}
    label = f"{_note_name(root)} {hit['name']}"
    info = {"label": label, "root": root, "name": hit["name"], "pcs": pattern}
    if pcs is not None:
        pcs_set = set(pcs)
        missing = sorted(pattern - pcs_set)
        match = len(pcs_set & pattern)
        ratio = (match / len(pattern)) if pattern else 0.0
        info["missing"] = missing
        info["match"] = match
        info["ratio"] = ratio
    return info


def _make_info_list(hits: list[dict] | None, db: list[dict],
                     pcs: set[int] | None = None) -> list[dict] | None:
    if not hits:
        return None
    infos = [info for hit in hits if (info := _make_info(hit, db, pcs=pcs))]
    return infos if infos else None


def detect_harmony_info(pcs: set[int], min_chord_notes: int = 2, min_scale_notes: int = 3,
                         chord_min_ratio: float = 0.6, scale_min_ratio: float = 0.7
                         ) -> tuple[list[dict] | None, list[dict] | None]:
    """Return `(chord_candidates, scale_candidates)` for `pcs` — each either
    `None` (no confident match) or a list of 1+ TIED best matches (see
    `_best_matches`'s `max_ties` gate), each a dict with `label`, `root`,
    `name`, `pcs`, `missing`, `match`, `ratio`."""
    pcs = set(pcs)
    chord = None
    scale = None

    if len(pcs) >= min_chord_notes:
        hits = _best_matches(pcs, CHORDS, require_root_in_pcs=True,
                              min_match=min_chord_notes, min_ratio=chord_min_ratio, max_ties=3)
        chord = _make_info_list(hits, CHORDS, pcs=pcs)

    if len(pcs) >= min_scale_notes:
        hits = _best_matches(pcs, SCALES, require_root_in_pcs=False,
                              min_match=min_scale_notes, min_ratio=scale_min_ratio, max_ties=3)
        scale = _make_info_list(hits, SCALES, pcs=pcs)

    return chord, scale


# -- phase-3 task 12 (gap ports, v1 page 11 "Chord+Key") --------------------
#
# `pages/chordkey.py`'s OWN chord-candidate scoring, ported verbatim from
# `~/codex/midicrt/pages/chordkey.py::_chord_candidates` -- a SEPARATE
# algorithm from `detect_harmony_info` above, not a wrapper around it.
# Confirmed by reading both v1 sources: `detect_harmony_info`/
# `_best_matches` (this file, backing `analyzers/harmony.py`'s "Chord:"
# ticker) requires the root to already be IN `pcs`
# (`require_root_in_pcs=True`), enforces a minimum match RATIO
# (`chord_min_ratio`), and reports "no match" outright when more than
# `max_ties` candidates tie (an ambiguous chord becomes `None`, not a
# guess). `chordkey.py`'s own `_chord_candidates` does none of that: every
# root 0-11 is tried regardless of whether it's actually sounding, there is
# no ratio floor (score is purely for RANKING, not gating), and it always
# returns up to `top_n` candidates with no ambiguity cutoff -- genuinely
# different selection semantics, not just a different presentation of the
# same computation, so this is its own function rather than a thin
# adapter over `_best_matches`.
def chord_candidates_all_roots(pcs: set[int], top_n: int = 3) -> list[dict]:
    """Rank every (root, chord-shape) combination in `CHORDS` against `pcs`
    by `(ratio, match, -extra, -len(pattern-missing))`, deduplicated by
    label, and return the top `top_n` — pure port of v1's
    `pages/chordkey.py::_chord_candidates`."""
    pcs_set = set(pcs)
    if not pcs_set:
        return []
    seen: set[str] = set()
    results: list[dict] = []
    for item in CHORDS:
        base = item["pcs"]
        for root in range(12):
            pattern = {(root + p) % 12 for p in base}
            match = len(pcs_set & pattern)
            if match < 2:
                continue
            label = f"{_note_name(root)} {item['name']}"
            if label in seen:
                continue
            missing = sorted(pattern - pcs_set)
            extra = len(pcs_set - pattern)
            ratio = match / max(1, len(pattern))
            seen.add(label)
            results.append({
                "label": label, "ratio": ratio, "match": match,
                "missing": missing, "extra": extra,
                "score": (ratio, match, -extra, -len(missing)),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_n]


# Roman-numeral scale degrees, ported verbatim from v1's `plugins/
# zharmony.py::ROMAN_DEGREES` — used by `roman_numeral_for_chord` below to
# label a chord's harmonic function relative to a key (v1's "Fn: I (T)"
# suffix on the Chord+Key page, `_update_function_label`/`_roman_for_chord`
# — NOT ported into `analyzers/harmony.py` itself, since that module's task
# 5 VM contract has no field for it; `pages/chordkey.py` is the one page
# that actually shows it).
ROMAN_DEGREES = {
    0: "I", 1: "bII", 2: "II", 3: "bIII", 4: "III", 5: "IV", 6: "#IV",
    7: "V", 8: "bVI", 9: "VI", 10: "bVII", 11: "VII",
}
_ROMAN_FUNCTION_BY_INTERVAL = {0: "T", 5: "S", 7: "D"}   # Tonic/Subdominant/Dominant


def roman_numeral_for_chord(chord_info: list[dict] | dict | None,
                             key_label: str | None) -> str | None:
    """Pure port of v1's `plugins/zharmony.py::_roman_for_chord` — the
    chord's root expressed as a roman numeral relative to `key_label`
    ("C maj"/"A min"), lower-cased for a minor chord, with `°`/`+`
    suffixes for diminished/augmented and a "(T)"/"(S)"/"(D)"
    tonic/subdominant/dominant function tag where one applies (the minor
    v7 chord, interval 10, is treated as dominant in a minor key — v1's
    own special case for the natural/harmonic-minor dominant substitute).
    Returns `None` if either input is missing/unparseable, exactly
    matching v1's own early-return guards.

    A real, faithfully-ported v1 quirk (verified against the actual
    deployed CHORDS asset, not assumed): the lowercase gate is `"min" in
    chord_name or chord_name.startswith("m")` — and this file's OWN major
    triad entry (`assets/chords.csv`, byte-diffed against v1's
    `config/chords.csv`) is literally named `"maj"`, which also starts
    with `"m"`. So a MAJOR chord's numeral gets lower-cased too (e.g. the
    tonic in C major renders `"i (T)"`, not `"I (T)"`) — this is v1's own
    real, observable behavior (confirmed by reading `_roman_for_chord`
    byte-for-byte, not a porting mistake), preserved here rather than
    "corrected" to conventional roman-numeral capitalization. Separately,
    the `"dim" in chord_name` diminished-suffix check never actually fires
    for THIS asset's own diminished row either — its `name` column is the
    symbol `"°"`, not the word `"dim"` — so on real, shipped chord data
    from `CHORDS`, no roman numeral here ever gets a `°` suffix; the
    suffix logic is exercised only by a hand-built `chord_info` dict
    naming a chord `"dim"`/`"aug"` explicitly (which `detect_harmony_info`
    could never actually hand back from THIS asset, but the function
    itself still honors correctly, matching v1's own generic string check
    rather than a CHORDS-asset-specific one).
    """
    if not chord_info or not key_label:
        return None
    if isinstance(chord_info, list):
        chord_info = chord_info[0] if chord_info else None
    if not isinstance(chord_info, dict):
        return None
    try:
        key_root_name, key_mode = key_label.split(" ", 1)
        key_root = NOTE_NAMES.index(key_root_name)
    except Exception:  # noqa: BLE001 -- verbatim fidelity to v1's own blind `except Exception`
        return None
    chord_root = chord_info.get("root")
    chord_name = (chord_info.get("name") or "").lower()
    if chord_root is None:
        return None
    interval = (int(chord_root) - int(key_root)) % 12
    roman = ROMAN_DEGREES.get(interval, "?")
    if "min" in chord_name or chord_name.startswith("m"):
        roman = roman.lower()
    if "dim" in chord_name:
        roman += "°"
    elif "aug" in chord_name:
        roman += "+"
    func = _ROMAN_FUNCTION_BY_INTERVAL.get(interval)
    if key_mode.strip() == "min" and interval == 10:
        func = "D"
    if func:
        return f"{roman} ({func})"
    return roman
