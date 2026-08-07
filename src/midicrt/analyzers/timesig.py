"""TimesigAnalyzer: heuristic time-signature estimation, ported from v1's
`~/codex/midicrt/plugins/ztimesig.py` (READ-ONLY reference on the Pi).

Two v1 implementations actually run at once (correcting the task brief's
assumption)
---------------------------------------------------------------------------
The task-6 brief says "port the one v1 actually runs, note the other" --
investigation shows v1's plugin LOADER (`midicrt.py`'s `load_plugins()`)
globs every `*.py` under `plugins/`, so BOTH `ztimesig.py` AND
`ztimesig_exp.py` load and run simultaneously; `config/settings.json` on
the Pi confirms both have live config sections (`timesig` and
`timesig_exp`). They are not alternates -- they feed two DIFFERENT v1 UI
surfaces: `ztimesig.get_timesig()` is read by `pages/transport.py` (the
"Time Signature: ..." line on v1's own dedicated Transport page, PAGE_ID
3), while `ztimesig_exp.get_timesig_exp()` is read by its own separate
page, `pages/timesig_exp.py` (PAGE_ID 15, "TimeSig Exp", showing a top-3
candidate-score breakdown). This module ports `ztimesig.py` only (the one
`pages/transport.py` -- v1's PRIMARY transport surface -- actually
displays); `ztimesig_exp.py`'s add-on (per-bar/beat/half-beat weighted
scoring with a prior table and its own dedicated page) is NOT ported here
and is flagged as a candidate for a future task, not lost logic.

Where this shows up in v2 (disclosed layout decision)
---------------------------------------------------------------------------
`ztimesig.py` itself never draws to the screen (its own `draw(state)` only
resets internal state on a transport start/stop mismatch -- no
`midicrt.draw_line()` call anywhere in the file). Its only real v1 UI
surface is `pages/transport.py`'s Transport page -- a page v2 never ported
as its own screen, because phase-3 task 3 already folded v1's transport
chrome (`plugins/timeclock.py`, the actual bottom-bar plugin, distinct
from the Transport PAGE) into `overlay.status`. Since the Transport page's
container was never carried into v2, the only place left for its one
UNIQUE piece of information (the time-signature line) to surface is
alongside that same chrome, as its own overlay analyzer/topic
(`overlay.timesig`) rendered as a second always-visible status row by both
clients (`clients/chrome.py`'s `timesig_text()`) -- a deliberate, disclosed
synthesis (v1 only shows this when the Transport page itself is selected;
v2 shows it on every page, same trade-off `analyzers/voices.py`'s and
`analyzers/harmony.py`'s own module docstrings already made when merging
multiple v1 sources into one v2 concern).

The core granularity problem: v1 needs 24-ppqn ticks, v2 only has one
event per BEAT
---------------------------------------------------------------------------
`ztimesig.py`'s candidate scoring (`_score_candidate`) works by measuring
each note onset's PHASE, in raw MIDI-clock ticks (v1's `PPQN = 24`),
relative to bar/beat/half-beat boundaries -- e.g. a 6/8 candidate's
`step_ticks = PPQN // 2 = 12` needs to know WHICH of the 24 sub-beat tick
positions a note landed on to detect swing/subdivision patterns. v1 reads
this directly from `midicrt.tick_counter`, a global incremented on every
RAW clock pulse. v2's `engine/midi_in.py` deliberately does NOT expose
per-pulse resolution to analyzers at all (see that module's docstring):
clock pulses are aggregated into one `clock_tick` MidiEvent per 24 pulses
(one beat) specifically so analyzers/pages/eventlog don't get flooded --
`analyzers/transport.py` hit this exact same granularity mismatch for its
own bpm math and adapted to it there; this module hits it again for tick
POSITION rather than tempo.

Adaptation (disclosed): reconstruct an approximate sub-beat tick position
by interpolating a note's timestamp within the CURRENT beat's span. Each
`clock_tick` gives an exact beat duration in seconds (`ev.ts -
ev.clock_batch_start`, for the batch that JUST completed) and marks `ev.ts`
as the instant the NEXT beat starts (`_beat_start_ts`). A `note_on`
arriving `elapsed` seconds after that boundary is assumed to fall
`elapsed / beat_duration` of the way through a 24-tick beat (the "constant
tempo within one beat" assumption -- tempo does not change instantaneously
in practice, and importantly, this is a REFINEMENT the algorithm's own
statistics (`SIGMA_TICKS`, tie-breaking, a decaying multi-beat window) are
already built to tolerate noisy attack timing on ordinary MIDI performance
input, so tick-position IMPRECISION here behaves exactly like the
humanized timing the algorithm was designed to smooth over in the first
place). Before ANY beat boundary has ever been observed
(`_beat_start_ts is None`) or before a beat duration is known
(`_last_beat_dur` is `None`/non-positive), a note projects to tick 0 of
its beat -- a conservative bootstrap default, inconsequential in practice
since `MIN_EVENTS=12` gates any estimate from forming during the first
fraction of a second of a session anyway. `PPQN`-multiple absolute tick
counting (`_beats_elapsed * PPQN + tick_in_beat`) is otherwise unaffected
by this -- only the SUB-beat component is approximate; `_score_candidate`
itself is ported byte-for-byte unchanged, since it only ever consumes tick
values modulo a candidate's bar/beat/step length, never their real-world
epoch.

Everything else uses `ev.ts`, never a live clock read (matches every
sibling analyzer)
---------------------------------------------------------------------------
v1's `handle()` calls `time.time()` directly for its window/decay/
eval-interval bookkeeping, but always immediately upon receiving a MIDI
message -- substituting `ev.ts` (the SAME instant, already on the event)
for every one of those reads is behavior-preserving, not a behavior
change, and keeps this analyzer "no I/O" like every other one (see
analyzers/harmony.py's `_harmonic_rhythm` for the identical substitution
on its own v1 source). No `tick(now)` hook is needed here (unlike
`analyzers/stucknotes.py`): v1's `ztimesig.py` only ever re-evaluates
INSIDE its `note_on` handler, never independently of new notes arriving,
so there is no "time passes with no events" case to cover.

A trivial, disclosed simplification vs. v1
---------------------------------------------------------------------------
v1's `handle()` also runs a SECOND, `_last_cleanup`-gated window-purge
pass (every >1s) that is provably redundant: the FIRST purge, run
unconditionally right above it on every qualifying note_on, already
removes every event older than `WINDOW_SECONDS` each time one arrives, so
by the time the second pass's 1-second gate would fire, there is nothing
left for it to do. Dropped here (not ported) -- same "drop a
provably-dead duplicate check" precedent as `analyzers/harmony.py`'s own
disclosed simplification of `zharmony.py`'s doubled scale-history filter.

Always-a-dict view model (disclosed, vs. v1's bare `None`)
---------------------------------------------------------------------------
v1's `get_timesig()` returns bare `None` until the very first estimate
locks in. This module instead always returns a dict (`labels: []`,
`confidence: 0.0` when nothing is known yet), matching the precedent
`analyzers/harmony.py` already set for `harmonic_rhythm`/`motif` ("a
renderer never has to branch on the field's TYPE, only its value").
"""
from __future__ import annotations

import math
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

PPQN = 24

# (label, bar_ticks, beat_ticks, step_ticks) -- verbatim from v1's
# DEFAULT_CANDIDATES.
_CANDIDATES = [
    ("2/4",  2 * PPQN,  PPQN,  PPQN // 2),
    ("3/4",  3 * PPQN,  PPQN,  PPQN // 2),
    ("4/4",  4 * PPQN,  PPQN,  PPQN // 2),
    ("5/4",  5 * PPQN,  PPQN,  PPQN // 2),
    ("7/4",  7 * PPQN,  PPQN,  PPQN // 2),
    ("6/8",  6 * (PPQN // 2),  3 * (PPQN // 2),  (PPQN // 2)),
    ("7/8",  7 * (PPQN // 2),  (PPQN // 2),      (PPQN // 2)),
    ("9/8",  9 * (PPQN // 2),  3 * (PPQN // 2),  (PPQN // 2)),
    ("12/8", 12 * (PPQN // 2), 3 * (PPQN // 2),  (PPQN // 2)),
]

MAX_EVENTS = 256
MIN_EVENTS = 12
SIGMA_TICKS = 4.0
TIE_THRESH = 0.02
WINDOW_SECONDS = 20.0
DECAY_SECONDS = 15.0
EVAL_INTERVAL = 0.5
MIN_CONF = 0.35
CHANGE_CONFIRM = 3
COLLAPSE_SAME_TICK = True


def _gauss(d: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.0
    return math.exp(-(d * d) / (2.0 * sigma * sigma))


def _score_candidate(events: list[tuple[int, float]], bar_ticks: int, beat_ticks: int,
                      step_ticks: int, sigma: float) -> float:
    """Pure function, ported verbatim from v1's `_score_candidate`. `events`
    is a list of `(tick, weight)` pairs."""
    if not events:
        return 0.0
    offsets = range(0, bar_ticks, max(1, step_ticks))
    best = 0.0
    total_w = sum(w for _t, w in events) + 1e-6
    for off in offsets:
        score = 0.0
        for tick, w in events:
            phase = (tick - off) % bar_ticks
            d_bar = min(phase, bar_ticks - phase)
            d_beat = phase % beat_ticks
            d_beat = min(d_beat, beat_ticks - d_beat)
            s = w * (1.0 * _gauss(d_bar, sigma) + 0.6 * _gauss(d_beat, sigma))
            if step_ticks < beat_ticks:
                d_sub = phase % step_ticks
                d_sub = min(d_sub, step_ticks - d_sub)
                s += w * 0.25 * _gauss(d_sub, sigma)
            score += s
        best = max(best, score)
    return best / total_w


class TimesigAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `view_model()
    -> dict`. No I/O, no clock reads -- all timing derives from `ev.ts`/
    `ev.clock_batch_start`, never a live clock (see module docstring for
    the tick-position reconstruction this requires)."""

    def __init__(self) -> None:
        self._events: deque[tuple[int, float, float]] = deque()   # (tick, ts, weight)
        self._last_result: tuple[list[str], float] | None = None
        self._last_eval: float = 0.0
        self._locked: tuple[list[str], float] | None = None
        self._pending: tuple[list[str], int] | None = None
        self._total_events = 0
        self._last_window_count = 0
        self._running = False

        # -- beat-boundary bookkeeping for tick-position reconstruction --
        self._beats_elapsed = 0
        self._beat_start_ts: float | None = None
        self._last_beat_dur: float | None = None

    # -- event handling -----------------------------------------------------

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type == "start":
            self._reset()
            self._running = True
            return True
        if ev.type == "stop":
            if not self._running:
                return False   # already stopped -- true no-op
            self._running = False   # v1 keeps the last known signature
            return True
        if ev.type == "continue":
            if self._running:
                return False   # already running -- true no-op
            self._running = True   # v1 has no "continue" branch at all --
            return True             # resuming does not reset event history
        if ev.type == "clock_tick":
            self._beats_elapsed += 1
            if ev.clock_batch_start is not None:
                self._last_beat_dur = ev.ts - ev.clock_batch_start
            self._beat_start_ts = ev.ts
            return False   # v1's ztimesig.py never reacts to clock itself
        if ev.type == "note_on" and (ev.data2 or 0) > 0:
            if not self._running:
                return False   # mirrors v1's `if not midicrt.running: return`
            tick = self._project_tick(ev.ts)
            self._on_note(tick, ev.ts, ev.data2)
            return True
        return False

    def _reset(self) -> None:
        self._events.clear()
        self._last_result = None
        self._last_eval = 0.0
        self._locked = None
        self._pending = None
        self._total_events = 0
        self._beats_elapsed = 0
        self._beat_start_ts = None
        self._last_beat_dur = None

    def _project_tick(self, ts: float) -> int:
        """Reconstruct an approximate 24-ppqn tick position from beat-
        boundary bookkeeping -- see module docstring."""
        if self._beat_start_ts is None:
            tick_in_beat = 0
        else:
            beat_dur = self._last_beat_dur
            if not beat_dur or beat_dur <= 0:
                tick_in_beat = 0
            else:
                frac = (ts - self._beat_start_ts) / beat_dur
                tick_in_beat = round(frac * PPQN) % PPQN
        return self._beats_elapsed * PPQN + tick_in_beat

    def _on_note(self, tick: int, ts: float, velocity: int) -> None:
        w = 1.0 + (velocity / 127.0)
        if COLLAPSE_SAME_TICK and self._events and self._events[-1][0] == tick:
            prev_tick, prev_ts, prev_w = self._events[-1]
            self._events[-1] = (prev_tick, prev_ts, max(prev_w, w))
        else:
            self._events.append((tick, ts, w))
            self._total_events += 1

        if WINDOW_SECONDS > 0:
            while self._events and (ts - self._events[0][1]) > WINDOW_SECONDS:
                self._events.popleft()
        elif MAX_EVENTS > 0:
            while len(self._events) > MAX_EVENTS:
                self._events.popleft()

        if ts - self._last_eval > EVAL_INTERVAL:
            self._reevaluate(ts)

    def _reevaluate(self, now: float) -> None:
        est = self._estimate(now)
        self._last_result = est
        if est is None:
            if len(self._events) < MIN_EVENTS:
                self._locked = None
                self._pending = None
        else:
            labels, score = est
            if score < MIN_CONF:
                self._pending = None
            elif self._locked is None:
                self._locked = (labels, score)
            else:
                locked_labels, _locked_score = self._locked
                if labels == locked_labels:
                    self._locked = (labels, score)
                    self._pending = None
                else:
                    if self._pending and self._pending[0] == labels:
                        self._pending = (labels, self._pending[1] + 1)
                    else:
                        self._pending = (labels, 1)
                    if self._pending[1] >= CHANGE_CONFIRM:
                        self._locked = (labels, score)
                        self._pending = None
        self._last_eval = now

    def _estimate(self, now: float) -> tuple[list[str], float] | None:
        if len(self._events) < MIN_EVENTS:
            self._last_window_count = len(self._events)
            return None
        events: list[tuple[int, float]] = []
        for tick, ts, w in self._events:
            if WINDOW_SECONDS > 0 and (now - ts) > WINDOW_SECONDS:
                continue
            if DECAY_SECONDS > 0:
                dw = w * math.exp(-(now - ts) / DECAY_SECONDS)
                if dw <= 1e-6:
                    continue
                events.append((tick, dw))
            else:
                events.append((tick, w))
        self._last_window_count = len(events)
        if len(events) < MIN_EVENTS:
            return None
        scores = []
        for label, bar_ticks, beat_ticks, step_ticks in _CANDIDATES:
            score = _score_candidate(events, bar_ticks, beat_ticks, step_ticks, SIGMA_TICKS)
            scores.append((label, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        _best_label, best_score = scores[0]
        ties = [label for label, sc in scores if (best_score - sc) <= (best_score * TIE_THRESH)]
        if len(ties) > 3:
            return None
        return (ties, best_score)

    # -- view model -----------------------------------------------------------

    def view_model(self) -> dict:
        if self._locked is not None:
            labels, conf = self._locked
        elif self._last_result is not None:
            labels, conf = self._last_result
        else:
            labels, conf = [], 0.0
        return {
            "labels": list(labels),
            "confidence": round(conf, 4),
            "events": len(self._events),
            "events_window": self._last_window_count,
            "events_total": self._total_events,
            "pending": list(self._pending[0]) if self._pending else None,
        }
