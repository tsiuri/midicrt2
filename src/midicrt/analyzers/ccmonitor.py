"""CCMonitorAnalyzer: raw MIDI CC (control-change) tracking, ported from
v1's `~/codex/midicrt/pages/ccmonitor.py` (PAGE_ID 4, "CC Monitor", 36
lines) and `~/codex/midicrt/pages/ccgraph.py` (PAGE_ID 5, "CC Dashboard",
55 lines) -- both v1 pages independently track incoming `control_change`
messages with their OWN storage shape and their OWN display, never sharing
state. Both v1 files are behavioral authority; read fully before touching
a branch below.

Two-shape synthesis (disclosed, mirrors analyzers/voices.py's own two-file
merge)
---------------------------------------------------------------------------
- **Per-channel recent window** (`ccmonitor.py`'s `_recent_ccs`, a
  `defaultdict(lambda: deque(maxlen=6))` keyed by 1-based channel): every
  `control_change` on a channel is appended verbatim -- NOT deduplicated by
  controller number, so the same CC firing repeatedly just pushes older
  entries out. Ported here as `self._recent[ch]`, same `maxlen=6`
  (`RECENT_PER_CHANNEL`).
- **Global insertion-ordered tracker** (`ccgraph.py`'s `_recent`, an
  `OrderedDict` keyed by `(channel, cc)`, capped at `MAX_ENTRIES=16`): a
  NEW `(ch, cc)` pair is only ever evicted (oldest first, FIFO) to make
  room for another NEW pair -- updating an ALREADY-tracked pair's value
  does NOT move it (plain dict/OrderedDict key reassignment never
  reorders), so the display order is "order FIRST SEEN", not "order most
  recently changed". Ported here as `self._tracked`, same `MAX_TRACKED=16`
  eviction rule, verified against v1's exact `if key not in _recent and
  len(_recent) >= MAX_ENTRIES: _recent.popitem(last=False)` guard.

`legacy_contract_bridge.py` import in `ccgraph.py` is dead, not a real
dependency (verified by reading, not assumed)
---------------------------------------------------------------------------
The phase-3 task 11 checklist sweep flagged `ccgraph.py` as depending on
`pages/legacy_contract_bridge.py`, "a v1 UI-framework shim with no v2
analogue." Reading `ccgraph.py` in full for this task shows
`build_widget_from_legacy_contract` is IMPORTED but never CALLED anywhere
in the file -- `build_widget()` constructs a plain `PageLinesWidget`
directly, exactly like every other v1 page here. This import is vestigial,
not a real blocker; both v1 pages are fully self-contained CC trackers with
no genuine cross-file dependency, and this task ports both in full.

`peak` per `(channel, cc)` -- a disclosed v2 addition
---------------------------------------------------------------------------
Neither v1 file tracks a peak/high-water value at all -- both only ever
show the LATEST value. The task brief's own suggested analyzer shape
("per-channel per-CC last/peak") asks for it anyway, and it is cheap and
genuinely useful (matches the "peak-hold" precedent already established by
`analyzers/voices.py`/`analyzers/spectrum.py`) -- `self._peak[(ch, cc)]`
never decreases, exactly like those two, and is exposed on BOTH view shapes
below.

Freshness ("LIVE" vs "N.Ns ago") needs an injected wall clock, not a live
read -- and is deliberately a cheaper, transition-only signal (disclosed
simplification vs v1's smooth per-frame counter)
---------------------------------------------------------------------------
`ccgraph.py`'s `draw()` computes `age = time.time() - ts` fresh on EVERY UI
frame and labels anything under 2s "LIVE" (`age < 2`). Like
`analyzers/stucknotes.py`'s escalation timing, this needs a clock value
injected from OUTSIDE (`tick(now)`, called by `engine/core.py`'s
`_tick_analyzers`/`_tick_pages` at `tick_hz`, see those modules'
docstrings) -- an analyzer must never read `time.time()` internally. Unlike
`analyzers/stucknotes.py` (which marks dirty on EVERY tick while anything
is alerting, so `held_s` climbs smoothly), `tick()` here only reports dirty
when an entry's LIVE/stale BUCKET actually flips (the `beatflash`-style
"transition only" convention) -- CC dashboards can see much higher-volume,
more continuous traffic than a rare stuck-note alert, and re-pushing this
page's whole snapshot 30x/sec forever after a single CC arrives would be
needless churn for a text label whose only real information is "still
fresh or not". `self._now` (the last injected `tick()` value) is still
stored UNCONDITIONALLY on every tick call (dirty or not) -- since
`engine/core.py::_tick_analyzers` runs every `tick_hz` period regardless of
any single analyzer's own dirty state, `age_s` in `view_model()` is always
accurate to within one tick period (~33ms at the default 30Hz) for any
READ, even one not itself triggered by this analyzer going dirty; only the
PUSH of a slowly-climbing "N.Ns ago" text between LIVE/stale transitions is
what is not chased here, exactly like `analyzers/beatflash.py`'s own
disclosed "decay reported via intensity, not a redrawn clock" choice.
Before the very first `tick()` call (e.g. a snapshot read immediately after
construction, before the engine's run loop has ticked even once),
`self._now` is `None` and every entry reports `age_s=0.0`/`fresh=True` --
a safe default, not a v1 behavior of any kind.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/voices.py's/stucknotes.py's own comment
    # -- avoids a circular import with engine.core, which builds
    # _PAGE_FACTORIES from modules like this one (via pages/ccmonitor.py/
    # pages/ccdashboard.py).
    from midicrt.engine.core import MidiEvent

N_CHANNELS = 16
RECENT_PER_CHANNEL = 6      # v1 ccmonitor.py's deque(maxlen=6)
MAX_TRACKED = 16            # v1 ccgraph.py's MAX_ENTRIES
FRESH_AFTER_S = 2.0         # v1 ccgraph.py's "age < 2" LIVE cutoff


class CCMonitorAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `tick(now)
    -> bool` (dirty; freshness-bucket transitions only, see module
    docstring), `view_model() -> dict`. No I/O -- `tick`'s `now` is always
    injected by the caller, never read here."""

    def __init__(self) -> None:
        self._recent: dict[int, deque] = {
            ch: deque(maxlen=RECENT_PER_CHANNEL) for ch in range(1, N_CHANNELS + 1)
        }
        self._peak: dict[tuple[int, int], int] = {}
        # Insertion-ordered (ch, cc) -> {"value": int, "ts": float}. Plain
        # dict preserves insertion order in Python 3.7+, matching v1's
        # OrderedDict semantics exactly -- see module docstring.
        self._tracked: dict[tuple[int, int], dict] = {}
        self._now: float | None = None

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type != "control_change":
            return False
        if ev.channel is None or ev.data1 is None or ev.data2 is None:
            return False
        ch = ev.channel + 1
        cc, value = ev.data1, ev.data2

        self._recent[ch].append({"cc": cc, "value": value})

        key = (ch, cc)
        self._peak[key] = max(self._peak.get(key, 0), value)

        if key not in self._tracked and len(self._tracked) >= MAX_TRACKED:
            oldest = next(iter(self._tracked))   # FIFO eviction, matches v1
            del self._tracked[oldest]
        entry = self._tracked.get(key)
        if entry is None:
            self._tracked[key] = {"value": value, "ts": ev.ts}
        else:
            entry["value"] = value
            entry["ts"] = ev.ts   # in-place update -- does NOT move position, matches v1
        return True

    # -- wall-progress hook (see module docstring) -----------------------------

    def tick(self, now: float) -> bool:
        self._now = now
        changed = False
        for entry in self._tracked.values():
            fresh = (now - entry["ts"]) < FRESH_AFTER_S
            was_fresh = entry.get("_fresh", True)
            if fresh != was_fresh:
                changed = True
            entry["_fresh"] = fresh
        return changed

    # -- view model -------------------------------------------------------------

    def _age_and_fresh(self, ts: float) -> tuple[float, bool]:
        if self._now is None:
            return 0.0, True
        age = max(0.0, self._now - ts)
        return round(age, 1), age < FRESH_AFTER_S

    def view_model(self) -> dict:
        per_channel = [
            {
                "ch": ch,
                "recent": [
                    {"cc": e["cc"], "value": e["value"],
                     "peak": self._peak.get((ch, e["cc"]), e["value"])}
                    for e in self._recent[ch]
                ],
            }
            for ch in range(1, N_CHANNELS + 1)
        ]
        tracked = []
        for (ch, cc), entry in self._tracked.items():
            age_s, fresh = self._age_and_fresh(entry["ts"])
            tracked.append({
                "ch": ch, "cc": cc, "value": entry["value"],
                "peak": self._peak.get((ch, cc), entry["value"]),
                "age_s": age_s, "fresh": fresh,
            })
        return {"per_channel": per_channel, "tracked": tracked}
