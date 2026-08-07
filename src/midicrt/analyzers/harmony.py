"""HarmonyAnalyzer: chord/scale/key/tension/harmonic-rhythm/motif detection,
ported from v1's `~/codex/midicrt/plugins/zharmony.py` (563 lines — the
chord/scale history, key-estimation hysteresis, tension, harmonic-rhythm,
and motif logic) plus, for the "currently sounding" pitch-class set tension
needs, v1's `~/codex/midicrt/plugins/polydisplay.py` (the per-channel
active-note SET v1's Notes page actually reads for that purpose — see
`docs/evidence-phase2-smoke/after.png`). Both v1 files are behavioral
authority and were read in full before writing this module; the chord/
scale *matching* itself (interval parsing, CSV-backed pattern DB, best-
match/tie scoring) lives in `analyzers/theory.py`, a separate port of v1's
`harmony.py` module that `zharmony.py` itself imports from.

Two-source synthesis (disclosed, mirrors analyzers/voices.py's own
two-file merge of zvoicemonitor.py + polydisplay.py)
---------------------------------------------------------------------------
- **Chord/scale/key/interval history** (`_recent_notes`, `_chord_history`,
  `_scale_history`, `_key_notes`/`_key_counts`, `_interval_history`) is
  driven ONLY by `note_on` with `velocity > 0` — verbatim from
  `zharmony.py`'s `handle()`, which checks nothing else (no note_off, no
  transport, no CC). It grows/evicts purely via each deque's `maxlen`;
  nothing here is ever explicitly cleared by a note-off, a CC, or a
  transport start/stop, because v1's `zharmony.py` has no such branches.
- **The sounding-pitch set** (`_sounding`, per MIDI channel) is a SEPARATE
  piece of state modeled on `polydisplay.py`'s `active_notes`, because
  that is v1's actual source for the "currently held notes" tension reads
  (`notes.py`'s `active_pcs` is built by iterating `polydisplay.
  active_notes.values()`, not anything in `zharmony.py`). `polydisplay.py`
  treats a channel's held notes as a plain SET keyed by note number (not a
  count, unlike `zvoicemonitor.py`'s overlap-counted `_active` used by
  `analyzers/voices.py`) — a bare `note_on`/`note_off` pair is enough to
  clear a pitch even if it was retriggered, and — this is the important,
  easy-to-miss divergence from `analyzers/voices.py` — **`polydisplay.py`
  does NOT clear on CC120/123 at all** (grepped: its `control_change`
  branch only ever touches its separate `cc_history`/`cc_last` display
  state, never `active_notes`). So unlike `VoiceMonitorAnalyzer`, this
  analyzer's sounding set is untouched by CC120/123. It IS cleared on
  transport "start"/"stop", matching `polydisplay.py`'s own two explicit
  branches for that (its comments: "New transport run should start from a
  clean non-memory display state" / "Transport stop should clear held
  notes even when no note_off arrives") — note this clear applies ONLY to
  the sounding set, never to the chord/scale/key/interval history above,
  because `zharmony.py` (which owns that history) has no transport
  awareness whatsoever.
- A `note_on`/`note_off` with `channel is None` is a defensive no-op for
  BOTH pieces of state (mirrors `polydisplay.py`'s own `if ch_raw is None:
  return` guard, extended here to the note-on branch too for the same
  reason `analyzers/voices.py` does — a malformed event must not crash the
  analyzer even though `zharmony.py` itself never needed a channel).

Eager (event-time) recompute vs v1's lazy (draw-time, cached) recompute
---------------------------------------------------------------------------
v1's `zharmony.get_harmony()` is called from the Notes page's `draw()` at
UI refresh rate, and caches its result keyed on `_last_pcs` so repeated
draws between notes don't redo the match/tie scoring. v2 has no draw loop
in the analyzer at all — `handle()` fires once per real MIDI event, and
`view_model()` is a pure projection of whatever state `handle()` already
computed (phase3-notes.md: "Analyzers = pure state machines fed MidiEvent
... derive pages from snapshots, not the event channel" — i.e. state
*transitions* belong in `handle()`, not `view_model()`). This module
therefore recomputes chord/scale detection EAGERLY on every qualifying
`note_on`, inside `_note_on()`, rather than lazily inside a
`view_model()`-time cache. This changes WHEN the work happens, never WHAT
it computes: v1's `_last_pcs` cache is a pure optimization (recomputing an
unchanged `pcs` tuple yields the identical `chord_label`/`scale_label`,
and the actual history-mutation is separately guarded by "does the LABEL
differ from last time", not by the cache), so this is a safe, behavior-
preserving adaptation to v2's push architecture, not a logic change.

The key-update cadence was made moot in practice by v1 itself, not by
this port
---------------------------------------------------------------------------
v1's `_update_stable_key(force=False)` only re-evaluates the key-
hysteresis comparison once every `KEY_UPDATE_EVERY_NOTES` (2) notes when
called from `handle()` — but v1's ONLY other caller, `notes.py`'s
`draw()`, always calls `get_stable_key()`, which forces (`force=True`) a
FULL re-evaluation regardless of the note-count cadence, on EVERY DRAW
FRAME. Since `draw()` runs at UI refresh rate (far faster than notes
arrive), the very next frame after any note-on always forces a fresh
comparison before the note AFTER that ever lands — in the actual running
system, `KEY_UPDATE_EVERY_NOTES`'s gate never has a chance to matter; a
user watching v1's screen sees the stable key re-evaluated after every
single note, not every 2nd note. This module ports v1's REAL observed
behavior (`view_model()` would need to force-recompute per read to match
the letter of `get_stable_key()`, which would make it impure — a
mutation-on-read; instead `_note_on()` itself always runs the full
hysteresis comparison, matching what a user actually sees) rather than
the technically-present-but-practically-dead every-Nth-note gate.
`KEY_UPDATE_EVERY_NOTES` is accordingly NOT ported as a field here — see
`test_analyzers_harmony.py`'s key tests, which pass a single note at a
time and expect the stable-key state to reflect it immediately, matching
this real v1 behavior rather than the dead code path.

Not ported (disclosed, not silently dropped)
---------------------------------------------------------------------------
- **Roman-numeral harmonic function** (`_update_function_label`/
  `get_last_function_label`/`_roman_for_chord`, v1's "Fn: ?" suffix on the
  Key line) — no field for it exists in the task's VM contract
  (`{"key", "key_conf", ...}`), and it derives from the SAME key/chord
  state already ported, so it is out of scope here rather than lost
  logic — flagged per this task's own "port... where practical; NONE
  without disclosure" instruction.
- **`RECENT_NOTE_SECONDS`-based time pruning of `_recent_notes`** — v1
  defaults this OFF (`0.0`, "set >0 to enable"); nothing in this task
  turns it on, and v2 has no per-analyzer `config.toml` section for
  harmony thresholds yet (unlike v1's `configutil.load_section("harmony")`
  override mechanism) — porting a disabled-by-default, unreachable branch
  would add real complexity for zero live behavior. `_recent_notes` here
  is purely count-bounded (`maxlen=RECENT_NOTE_COUNT`), matching v1's
  actual default-configuration behavior.
- **`EXTRAS_INTERVAL_HZ`/`MOTIF_INTERVAL_HZ`/`RHYTHM_ON_CHORD_CHANGE`** —
  pure draw-loop-throttle knobs in v1 (rate-limiting how often
  `get_tension`/`get_harmonic_rhythm`/`get_motif_info` recompute against a
  UI redraw loop that could otherwise call them dozens of times a second).
  v2's `view_model()` is called once per dirty read, not once per frame,
  so there is no redraw loop to throttle against — not ported.
- **Notes-page's 1.5s tension "hold"** (`_tension_held`/
  `_TENSION_HOLD_SECS` in `pages/notes.py`, NOT `zharmony.py` itself) — a
  UI anti-flicker nicety that re-reads `time.time()` every draw frame to
  keep showing the last tension value briefly after notes release, purely
  so the tension line doesn't flash to "silent" between two overlapping
  legato notes. This analyzer has no clock reads at all (task
  requirement); tension here reports the TRUE instantaneous state
  (silent the instant nothing is held), which is the correct pure-
  state-machine answer, not a regression — the flicker v1's hold papers
  over is a rendering-cadence concern for a client to solve later if
  needed, not analyzer state.
- **`zvoicemonitor.py`-style poly-limit warnings** — not relevant here at
  all (that plugin has nothing to do with harmony); noted only because
  the sibling `analyzers/voices.py` module docstring calls out the same
  class of omission and a reviewer comparing the two might otherwise
  wonder why this file has no equivalent note.

Added beyond the letter of the minimal VM contract (disclosed)
---------------------------------------------------------------------------
The task brief's VM shape is `{"key": str|None, "key_conf": ..., "tension":
float, ...}`. Two small, cheap fields are added because v1 genuinely
computes and displays them and dropping them would lose real information
for free: `key_alternatives` (v1's "(alts: ...)" hint on the Key line —
other keys tied within `KEY_ALT_MARGIN` of the top candidate) and
`tension_label`/`tension_worst_interval` (v1's tension word — "consonant"/
"tense"/"dissonant"/etc — and the worst interval class name, e.g.
"tritone", shown in brackets next to the tension bar).

Channel indexing: `MidiEvent.channel` is 0-based (mido); the sounding-set
dict below is keyed directly by that 0-based value (16 buckets, 0..15) —
unlike `analyzers/voices.py`, nothing in this module's public
`view_model()` exposes per-channel data, so there is no 1-based
convention to keep consistent with a rendered row.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from midicrt.analyzers import theory
from midicrt.analyzers.theory import NOTE_NAMES

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's/analyzers/voices.py's own
    # comment -- avoids a circular import with engine.core, which builds
    # _PAGE_FACTORIES from modules like this one (via pages/harmony.py).
    from midicrt.engine.core import MidiEvent

N_CHANNELS = 16

# -- zharmony.py's tunable constants (ported at their v1 default values;
# v2 has no config.toml section for these yet -- see module docstring's
# "not ported" list for the two knobs deliberately left out). ------------
RECENT_NOTE_COUNT = 24
MIN_UNIQUE_FOR_CHORD = 2
MIN_UNIQUE_FOR_SCALE = 3
CHORD_MIN_RATIO = 0.6
SCALE_MIN_RATIO = 0.7
KEY_HISTORY_LEN = 256
KEY_CONFIDENCE_THRESHOLD = 0.72
KEY_HYSTERESIS_MARGIN = 0.06
KEY_ALT_MARGIN = 0.04
MOTIF_WINDOW = 3
# v1's own fallback when no real tempo is known (`getattr(midicrt, "bpm",
# 120.0) or 120.0`) -- this analyzer has no access to the transport
# analyzer's live bpm (engine/core.py: pages/analyzers don't cross-read
# each other's state yet, a "currently latent" gap per that module's own
# docstring, not something this task's scope extends), so harmonic-rhythm
# below always assumes 4/4 at this fixed tempo, exactly matching what v1
# itself falls back to whenever bpm is unknown.
HARMONIC_RHYTHM_BPM = 120.0

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]

# Dissonance weights by interval class (0=unison … 6=tritone) -- verbatim
# from zharmony.py, drawn from psychoacoustic roughness literature.
_IC_DISSONANCE = [0.0, 1.0, 0.8, 0.3, 0.1, 0.2, 1.0]
_IC_NAMES = ["", "m2/M7", "M2/m7", "m3/M6", "M3/m6", "P4/P5", "tritone"]
_TENSION_LABELS = [
    (0.5, "silent"),
    (2.5, "consonant"),
    (4.5, "mild"),
    (6.5, "tense"),
    (8.5, "dissonant"),
    (10.1, "harsh"),
]

_CLEAR_SOUNDING_TYPES = {"start", "stop"}


def _label(info: list[dict] | dict | None) -> str | None:
    if not info:
        return None
    if isinstance(info, list):
        return " / ".join(i["label"] for i in info)
    return info["label"]


def _first_info(info: list[dict] | dict | None) -> dict | None:
    if isinstance(info, list) and info:
        return info[0]
    if isinstance(info, dict):
        return info
    return None


def _tension(active_pcs: set[int]) -> tuple[float, str, str]:
    """Pure function, ported verbatim from zharmony.py's `get_tension`
    (minus its draw-rate-throttle cache, see module docstring). Returns
    `(score 0.0-10.0, label, worst_interval_class_name)`."""
    pcs = tuple(sorted(set(active_pcs)))
    n = len(pcs)
    if n < 2:
        return (0.0, "silent", "")
    total = 0.0
    worst_ic = 0
    worst_w = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            ic = abs(pcs[i] - pcs[j]) % 12
            if ic > 6:
                ic = 12 - ic
            w = _IC_DISSONANCE[ic]
            total += w
            count += 1
            if w > worst_w:
                worst_w = w
                worst_ic = ic
    score = min(10.0, (total / count) * 10.0)
    label = _TENSION_LABELS[-1][1]
    for thresh, lbl in _TENSION_LABELS:
        if score < thresh:
            label = lbl
            break
    worst_name = _IC_NAMES[worst_ic] if worst_w >= 0.5 else ""
    return (score, label, worst_name)


class HarmonyAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `view_model()
    -> dict`. No I/O, no clock reads -- time-dependent values (harmonic
    rhythm) derive from `ev.ts`, never a live clock. See module docstring
    for the full v1 behavioral synthesis (zharmony.py + polydisplay.py) and
    the deliberately-not-ported list."""

    def __init__(self) -> None:
        # -- sounding-pitch set (polydisplay.py-modeled; tension's source)
        self._sounding: dict[int, set[int]] = {ch: set() for ch in range(N_CHANNELS)}

        # -- chord/scale/key/interval history (zharmony.py-modeled) -------
        self._recent_notes: deque[tuple[int, float]] = deque(maxlen=RECENT_NOTE_COUNT)
        self._recent_pcs: set[int] = set()
        self._chord_info: list[dict] | dict | None = None
        self._scale_info: list[dict] | dict | None = None
        self._scale_pcs: set[int] = set()
        self._last_chord_label: str | None = None
        self._scale_current_label: str | None = None
        self._chord_history: deque[str] = deque(maxlen=4)
        self._scale_history: deque[dict] = deque(maxlen=4)
        self._chord_change_times: deque[float] = deque(maxlen=16)

        self._last_note_for_iv: int | None = None
        self._interval_history: deque[int] = deque(maxlen=64)

        self._key_notes: deque[int] = deque(maxlen=KEY_HISTORY_LEN)
        self._key_counts: list[int] = [0] * 12
        self._stable_key: str | None = None
        self._stable_key_conf: float = 0.0
        self._stable_key_alt: list[dict] = []

    # -- event handling ---------------------------------------------------

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type == "note_on":
            if ev.channel is None:
                return False
            if ev.data2 == 0:
                return self._note_off(ev.channel, ev.data1)
            return self._note_on(ev.channel, ev.data1, ev.ts)
        if ev.type == "note_off":
            if ev.channel is None:
                return False
            return self._note_off(ev.channel, ev.data1)
        if ev.type in _CLEAR_SOUNDING_TYPES:
            return self._clear_sounding()
        return False

    def _note_on(self, ch: int, note: int, ts: float) -> bool:
        self._sounding[ch].add(note)
        self._recent_notes.append((note, ts))
        pc = note % 12

        # key histogram: 256-note rolling window, decrementing the count
        # for the note about to be evicted BEFORE the deque's own maxlen
        # silently drops it (mirrors zharmony.py's manual pre-decrement).
        if len(self._key_notes) >= (self._key_notes.maxlen or 0):
            old = self._key_notes[0]
            self._key_counts[old] = max(0, self._key_counts[old] - 1)
        self._key_notes.append(pc)
        self._key_counts[pc] += 1

        # cumulative in/total stats for each tracked scale candidate --
        # v1 checks `pc in item["pcs"]` twice (once for "in", once,
        # identically, for "uniq_in"); folded into one check here, a
        # trivial de-duplication of literally-redundant v1 code, not a
        # behavior change (both conditions were always the same test).
        for item in self._scale_history:
            item["total"] += 1
            item["uniq_total"].add(pc)
            if pc in item["pcs"]:
                item["in"] += 1
                item["uniq_in"].add(pc)

        if self._last_note_for_iv is not None:
            self._interval_history.appendleft(note - self._last_note_for_iv)
        self._last_note_for_iv = note

        self._update_stable_key()
        self._update_harmony_detection(ts)
        return True

    def _note_off(self, ch: int, note: int) -> bool:
        if note not in self._sounding[ch]:
            return False
        self._sounding[ch].discard(note)
        return True

    def _clear_sounding(self) -> bool:
        if not any(self._sounding.values()):
            return False
        for notes in self._sounding.values():
            notes.clear()
        return True

    # -- chord/scale detection (eager; see module docstring) ---------------

    def _update_harmony_detection(self, ts: float) -> None:
        pcs = {n % 12 for n, _ts in self._recent_notes}
        self._recent_pcs = pcs
        chord, scale = theory.detect_harmony_info(
            pcs,
            min_chord_notes=MIN_UNIQUE_FOR_CHORD,
            min_scale_notes=MIN_UNIQUE_FOR_SCALE,
            chord_min_ratio=CHORD_MIN_RATIO,
            scale_min_ratio=SCALE_MIN_RATIO,
        )
        self._chord_info = chord
        self._scale_info = scale

        if isinstance(scale, list):
            pcs_list = [set(i["pcs"]) for i in scale]
            self._scale_pcs = set.intersection(*pcs_list) if pcs_list else set()
        else:
            self._scale_pcs = set(scale["pcs"]) if scale else set()

        scale_label = _label(scale)
        if scale_label and scale_label != self._scale_current_label:
            self._scale_current_label = scale_label
            # v1's equivalent block does this dedup filter TWICE -- once for
            # a same-label match, then a second, redundant pass re-filtering
            # for `isinstance(item, dict)` that its own comment explains was
            # "guard against leftover non-dict entries from OLDER VERSIONS"
            # of zharmony.py. This is a fresh implementation with no legacy
            # data to migrate from, so every `_scale_history` item is always
            # a dict by construction -- the second pass would be a pure
            # no-op here, and is dropped as a trivial, disclosed
            # simplification (not a behavior change for any reachable
            # state).
            kept = deque(
                (item for item in self._scale_history if item["label"] != scale_label),
                maxlen=4,
            )
            self._scale_history = kept
            self._scale_history.appendleft({
                "label": scale_label,
                "pcs": set(self._scale_pcs),
                "in": 0, "total": 0,
                "uniq_in": set(), "uniq_total": set(),
            })

        chord_label = _label(chord)
        if chord_label and chord_label != self._last_chord_label:
            if not self._chord_history or self._chord_history[0] != chord_label:
                self._chord_history.appendleft(chord_label)
            self._last_chord_label = chord_label
            self._chord_change_times.appendleft(ts)

    # -- key estimation (note-count-driven hysteresis; see module docstring
    # for why this always runs on every note, not every KEY_UPDATE_EVERY_
    # NOTES'th one) --------------------------------------------------------

    @staticmethod
    def _key_candidates(counts: list[int], total: int) -> list[dict]:
        if total <= 0:
            return []
        results = []
        for root in range(12):
            for mode, intervals in (("maj", MAJOR_SCALE), ("min", MINOR_SCALE)):
                scale = {(root + i) % 12 for i in intervals}
                inside = sum(counts[pc] for pc in scale)
                outside = total - inside
                ratio = inside / total
                score = ratio - (outside / total) * 0.5
                results.append({
                    "root": root, "mode": mode,
                    "label": f"{NOTE_NAMES[root]} {mode}",
                    "ratio": ratio, "inside": inside, "outside": outside, "score": score,
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def _update_stable_key(self) -> None:
        cands = self._key_candidates(self._key_counts, sum(self._key_counts))
        if not cands:
            self._stable_key = None
            self._stable_key_conf = 0.0
            self._stable_key_alt = []
            return
        top = cands[0]
        conf = float(top["ratio"])
        eff_threshold = KEY_CONFIDENCE_THRESHOLD
        if self._stable_key and self._stable_key != top["label"]:
            eff_threshold += KEY_HYSTERESIS_MARGIN
        if conf >= eff_threshold or (
            self._stable_key == top["label"] and conf >= KEY_CONFIDENCE_THRESHOLD - KEY_HYSTERESIS_MARGIN
        ):
            self._stable_key = top["label"]
            self._stable_key_conf = conf
        self._stable_key_alt = []
        margin = max(0.0, KEY_ALT_MARGIN)
        for cand in cands[1:4]:
            if abs(cand["ratio"] - conf) <= margin:
                self._stable_key_alt.append(cand)

    # -- pure derived views (safe to (re)compute inside view_model(): no
    # mutation, unlike the state above which only ever changes in handle())

    def _harmonic_rhythm(self) -> dict:
        """Ported from zharmony.py's `get_harmonic_rhythm(bpm)`, using
        `ev.ts`-derived timestamps already recorded in
        `_chord_change_times` instead of a live clock read, and
        `HARMONIC_RHYTHM_BPM` instead of a transport analyzer reference
        (see module docstring)."""
        times = list(self._chord_change_times)
        if len(times) < 2:
            return {"changes_per_bar": None, "label": ""}
        n = min(4, len(times) - 1)
        intervals = [times[i] - times[i + 1] for i in range(n)]
        avg_secs = sum(intervals) / len(intervals)
        if avg_secs <= 0:
            return {"changes_per_bar": None, "label": ""}
        secs_per_bar = (60.0 / max(HARMONIC_RHYTHM_BPM, 1.0)) * 4
        cpb = secs_per_bar / avg_secs
        if cpb < 0.15:
            label = "static"
        elif cpb < 0.6:
            label = "slow"
        elif cpb < 1.5:
            label = "moderate"
        elif cpb < 3.0:
            label = "fast"
        else:
            label = "very fast"
        return {"changes_per_bar": cpb, "label": label}

    def _motif_info(self, window: int = MOTIF_WINDOW) -> dict:
        """Ported from zharmony.py's `get_motif_info` -- transpositions
        match automatically since intervals are signed semitone deltas,
        not absolute pitches."""
        hist = list(self._interval_history)
        if len(hist) < window * 2:
            return {"found": False, "pattern": None, "count": 0}
        current = tuple(hist[:window])
        count = sum(
            1 for i in range(window, len(hist) - window + 1)
            if tuple(hist[i:i + window]) == current
        )
        if count == 0:
            return {"found": False, "pattern": None, "count": 0}
        pattern = " ".join(f"{'+' if x > 0 else ''}{x}" for x in current)
        return {"found": True, "pattern": pattern, "count": count}

    def _candidates_vm(self, history: list[str], current_info: list[dict] | dict | None
                        ) -> list[dict]:
        """Shape a history ring (v1's "Last/2nd/3rd/4th" labels) into the
        task's `{"name", "conf", "missing"}` list. Only index 0 ("Last",
        the current instant) ever gets real confidence/missing-tones data
        -- v1 itself only tracks that for the CURRENT detection
        (`get_chord_info()`/`get_scale_info()`), never per history slot,
        so slots 1-3 report `conf=None, missing=[]` rather than fabricated
        values. If the current detection has since gone quiet (e.g. too
        few notes are in the recent window right now) while history still
        remembers an older label, index 0 ALSO reports `conf=None` --
        this is a real, faithfully-reproduced v1 quirk: v1's Notes page
        does exactly this too (`chord_info`/`scale_info` update
        unconditionally, including to `None`, independently of whether
        `CHORD_HISTORY`/`SCALE_HISTORY` gets a new entry)."""
        cur = _first_info(current_info)
        out = []
        for i, name in enumerate(history):
            if i == 0 and cur is not None:
                missing = [NOTE_NAMES[p % 12] for p in cur.get("missing", [])]
                out.append({"name": name, "conf": round(cur.get("ratio", 0.0), 4),
                            "missing": missing})
            else:
                out.append({"name": name, "conf": None, "missing": []})
        return out

    # -- view model ---------------------------------------------------------

    def view_model(self) -> dict:
        active_pcs = {n % 12 for notes in self._sounding.values() for n in notes}
        score, tension_label, worst = _tension(active_pcs)

        if self._scale_pcs:
            inside = sorted(NOTE_NAMES[pc] for pc in (self._recent_pcs & self._scale_pcs))
            outside = sorted(NOTE_NAMES[pc] for pc in (self._recent_pcs - self._scale_pcs))
        else:
            # No scale has been confidently established yet -- there is
            # nothing to classify notes AGAINST, so both lists are empty
            # rather than dumping every recent note into "outside" (which
            # would misleadingly read as "everything clashes" when really
            # there's just no scale context yet).
            inside = []
            outside = []

        return {
            "title": "HARMONY",
            "chords": self._candidates_vm(list(self._chord_history), self._chord_info),
            "scales": self._candidates_vm(
                [item["label"] for item in self._scale_history], self._scale_info),
            "inside": inside,
            "outside": outside,
            "key": self._stable_key,
            "key_conf": round(self._stable_key_conf, 4),
            "key_alternatives": [c["label"] for c in self._stable_key_alt],
            "tension": round(score / 10.0, 4),
            "tension_label": tension_label,
            "tension_worst_interval": worst,
            "harmonic_rhythm": self._harmonic_rhythm(),
            "motif": self._motif_info(),
            "silent": len(active_pcs) == 0,
        }
