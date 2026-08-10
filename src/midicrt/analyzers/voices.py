"""VoiceMonitorAnalyzer: per-channel polyphony + active-note tracking, ported
from v1's `~/codex/midicrt/plugins/zvoicemonitor.py` (poly counts + peak
hold) and `~/codex/midicrt/plugins/polydisplay.py` (per-channel active-note
set + transport-triggered clear) -- the two panels that together made up v1's
main screen (see docs/evidence-phase2-smoke/after.png). Both v1 files are
behavioral authority; read fully before touching a branch below.

Behavioral synthesis (read this before changing anything)
-----------------------------------------------------------
- **note-on/off pairing**: zvoicemonitor.py tracks `_active[(ch, note)]` as a
  COUNT, not a boolean -- the same pitch can be retriggered on a channel
  without an intervening note-off (an arpeggiator, or a synth that legally
  overlaps two voices on one pitch), and each overlapping note-on is a REAL
  additional voice that needs its OWN note-off before the pitch goes silent.
  This analyzer keeps that exact count-per-(channel, note) semantics
  internally so "active" genuinely counts held VOICES (matching v1's
  `_active_ch`), not distinct pitches -- `notes` (the dedup'd pitch list a
  display wants) is derived from the same dict's keys. A stray note-off for
  a pitch with count 0 (never seen, or already fully released) is a TRUE
  no-op, mirroring zvoicemonitor's `if key not in _active: return`.
- **velocity-0-as-off**: both v1 files treat `note_on` with `velocity == 0`
  identically to a real `note_off` (`_note_off(ch, msg.note)` in both) --
  ported verbatim via `ev.data2 == 0` (midi_in.py's `translate()` puts
  velocity in `data2` for note_on/note_off, same as `msg.velocity`).
- **sustain (CC64)**: NEITHER v1 source touches CC64 anywhere (grepped both
  files on the Pi -- no reference at all). This analyzer does not implement
  sustain-hold either: a note released while a sustain pedal is "down" turns
  off immediately, same as v1's real (if arguably incomplete) behavior. Do
  not add sustain handling here without new v1 evidence.
- **CC 120/123 (All Sound Off / All Notes Off)**: zvoicemonitor.py's
  `_clear_channel` fires on these two controller numbers, zeroing that
  channel's active notes/count (NOT its peak -- peak is a hold, see below).
  Ported verbatim as the per-channel clear path; any other CC is a no-op
  here (including CC64, per the point above).
- **transport start/stop clears ALL channels**: polydisplay.py clears every
  channel's active-note set on both "start" and "stop" (its own comments:
  "New transport run should start from a clean non-memory display state" /
  "Transport stop should clear held notes even when no note_off arrives").
  zvoicemonitor.py has no transport awareness at all (no start/stop
  handling) -- it only clears on CC120/123. Since this analyzer merges both
  v1 panels into one VM (`{"active", "peak", "notes"}` per channel), a
  transport start/stop clears the SAME merged state that CC120/123 clears.
  This is a deliberate synthesis, not a literal single-file port: applying
  polydisplay's reset to the combined model is the only way to avoid a
  `notes` list that never self-heals across a stop when a synth doesn't
  cleanly send note-offs -- exactly the "stuck notes" polydisplay's reset
  exists to prevent. (A DIFFERENT, not-yet-ported v1 plugin,
  `zstucknotes.py`, is a separate future page/analyzer per
  docs/phase3-notes.md's v1 source map -- this reset is not that feature,
  just carrying forward polydisplay's existing safety net.)
- **peak-hold**: neither v1 file ever decreases `_peak_total`/`_peak_ch` --
  they are session-lifetime highs, only ever `max()`'d upward at note-on
  time. No clear path (CC120/123 or start/stop) touches peak here either,
  matching v1 exactly -- "peak" only ever goes up.
- **event-log-of-voices** (Phase 9 Task 2 -- PORTED, see below): v1's
  `_events` deque recorded POLY-LIMIT-EXCEEDED warnings against
  `POLY_LIMIT_GLOBAL`/`POLY_LIMIT_CH`, with a SECOND "sustain" tag gated
  by an over-limit-DURATION measured in beats via a `tick` counter
  `draw()` received from elsewhere (`_update_over`/`OVER_LIMIT_BEATS`,
  zvoicemonitor.py:107-122). Only the "instant" tag (fired the moment a
  note-on's resulting count first crosses a limit, zvoicemonitor.py:78-81)
  is ported -- the beat-duration "sustain" re-notification needs a
  MIDI-clock/beat counter this analyzer has no access to, a disclosed
  simplification matching this task's scope (only `poly_limit_global`/
  `poly_limit_ch` are named config knobs -- no `per_channel_limits`
  override list, `event_log_len`, or `over_limit_beats` either).

Phase 9 Task 2 additions: poly-limit event log + chrome flash
---------------------------------------------------------------------------
`_note_on` now also checks the resulting total/channel count against
`poly_limit_global`/`poly_limit_ch` (constructor args, v1 defaults 16/8,
`config.poly_limit_global`/`config.poly_limit_ch` via
`engine/core.py::_ANALYZER_FACTORIES`) and, on an exceed, appends an
"instant"-shaped event to `self._events` (`deque(maxlen=EVENT_LOG_LEN)`,
v1's `EVENT_LOG_LEN=8`) and arms a short (`FLASH_DURATION_S`) chrome flash
window -- see `tick()`'s own docstring for how the flash's ON/OFF edges
are observed without this analyzer ever reading a clock itself, and
`flash_view_model()` for why the flash rides a SEPARATE, minimal view
rather than bloating the main `view_model()` (which `page.voices`'
subscribers already pull on every note).

Shared-instance dedup guard (Phase 9 Task 2, needed for the flash overlay)
---------------------------------------------------------------------------
`engine/core.py::Engine.__init__` wires ONE `VoiceMonitorAnalyzer`
instance into BOTH `pages/voices.py`'s `VoicesPage` (the `page.voices`
topic) and a thin `overlay.polylimit` wrapper (the chrome flash) so
poly-limit tracking is computed exactly once per event -- mirrors
`analyzers/harmony.py::HarmonyAnalyzer.handle()`'s own identical
"`ev is self._last_handled_event` -> return the cached dirty result"
guard verbatim (see that module's docstring for the full "why sharing an
instance across two roster entries needs this" rationale); without it, a
shared instance would double-count every note.

Channel indexing matches v1: `MidiEvent.channel` is 0-based (mido); this
module's public `view_model()` is 1-based via list position (index 0 ==
channel 1), matching zvoicemonitor's own `ch = msg.channel + 1`.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's own comment -- avoids a
    # circular import with engine.core, which builds _PAGE_FACTORIES from
    # modules like this one (via pages/voices.py).
    from midicrt.engine.core import MidiEvent

N_CHANNELS = 16
_ALL_NOTES_OFF_CONTROLS = {120, 123}   # All Sound Off, All Notes Off
_CLEAR_ALL_TYPES = {"start", "stop"}

# Phase 9 Task 2 (poly-limit log): v1's `plugins/zvoicemonitor.py:11-12,14`.
POLY_LIMIT_GLOBAL = 16
POLY_LIMIT_CH = 8
EVENT_LOG_LEN = 8

# Phase 9 Task 2 (chrome flash): NOT a v1 constant -- v1's zvoicemonitor.py
# has no visual/chrome home of its own at all (see module docstring), so
# this duration is a disclosed v2-native choice, not a port. Short enough
# to read as a genuine "flash" (burn-in rule: transient, not a persistent
# element) while still being long enough (well above one tick period at
# the default 30Hz `tick_hz`) for a client to reliably observe it.
FLASH_DURATION_S = 0.5


class VoiceMonitorAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `tick(now)
    -> bool` (dirty; poly-limit chrome-flash decay only, see `tick()`'s own
    docstring), `view_model() -> dict`, `flash_view_model() -> dict`
    (minimal chrome-flash projection). No I/O -- `tick`'s `now` is always
    injected by the caller, never read here. See module docstring for the
    v1 behaviors ported and the ones deliberately left out (sustain, v1's
    beat-duration "sustain"-tag poly-limit re-notification)."""

    def __init__(self, poly_limit_global: int = POLY_LIMIT_GLOBAL,
                 poly_limit_ch: int = POLY_LIMIT_CH) -> None:
        # 1-based channel -> {note: overlap count}. count > 1 means the same
        # pitch is held by more than one overlapping note-on (module
        # docstring's "note-on/off pairing").
        self._notes: dict[int, dict[int, int]] = {ch: {} for ch in range(1, N_CHANNELS + 1)}
        self._active: dict[int, int] = dict.fromkeys(range(1, N_CHANNELS + 1), 0)
        self._peak: dict[int, int] = dict.fromkeys(range(1, N_CHANNELS + 1), 0)
        self._total = 0
        self._total_peak = 0
        # -- poly-limit log (Phase 9 Task 2) ---------------------------------
        self._limit_global = poly_limit_global
        self._limit_ch = poly_limit_ch
        # (ts, ch, note, total, ch_total, ch_limit, hit_global, hit_ch) --
        # appendleft + maxlen auto-evicts the OLDEST (rightmost) entry,
        # matching v1's own `deque(maxlen=EVENT_LOG_LEN)` +
        # `_events.appendleft(...)` exactly.
        self._events: deque[dict] = deque(maxlen=EVENT_LOG_LEN)
        self._now: float | None = None
        self._flash_until: float = 0.0
        self._flash_active: bool = False
        # -- shared-instance dedup guard (Phase 9 Task 2, see module
        # docstring's own section) -------------------------------------------
        self._last_handled_event: object | None = None
        self._last_handled_dirty: bool = False

    def handle(self, ev: MidiEvent) -> bool:
        """Shared-instance dedup guard (Phase 9 Task 2): see module
        docstring and `analyzers/harmony.py::HarmonyAnalyzer.handle`'s own
        identical precedent -- `ev is self._last_handled_event` is a safe,
        O(1) dedup key valid for exactly the lifetime of one
        `Engine._handle()` call, and a pure no-op when this analyzer is
        used standalone (every test here constructs a fresh `MidiEvent`
        per call, so identity never accidentally matches)."""
        if ev is self._last_handled_event:
            return self._last_handled_dirty
        self._last_handled_event = ev
        dirty = self._handle_uncached(ev)
        self._last_handled_dirty = dirty
        return dirty

    def _handle_uncached(self, ev: MidiEvent) -> bool:
        if ev.type in _CLEAR_ALL_TYPES:
            return self._clear_all()
        if ev.type == "note_on":
            if ev.channel is None:
                return False
            ch = ev.channel + 1
            if ev.data2 == 0:
                return self._note_off(ch, ev.data1)
            return self._note_on(ch, ev.data1, ev.ts)
        if ev.type == "note_off":
            if ev.channel is None:
                return False
            return self._note_off(ev.channel + 1, ev.data1)
        if ev.type == "control_change" and ev.data1 in _ALL_NOTES_OFF_CONTROLS:
            if ev.channel is None:
                return False
            return self._clear_channel(ev.channel + 1)
        return False

    def _note_on(self, ch: int, note: int, ts: float) -> bool:
        counts = self._notes[ch]
        counts[note] = counts.get(note, 0) + 1
        self._active[ch] += 1
        self._total += 1
        self._peak[ch] = max(self._peak[ch], self._active[ch])
        self._total_peak = max(self._total_peak, self._total)
        self._check_poly_limit(ch, note, ts)
        return True

    def _check_poly_limit(self, ch: int, note: int, ts: float) -> None:
        """Poly-limit log (Phase 9 Task 2): v1's "instant" tag
        (zvoicemonitor.py:78-81) -- fires on EVERY note-on whose resulting
        count exceeds a limit, not just the first (matching v1's own
        `if hit_global or hit_ch:` check, unconditional every call, no
        "already logged" gate). A limit `<= 0` means "no limit" (v1's own
        `if POLY_LIMIT_GLOBAL > 0`/`if ch_limit and ch_limit > 0` guards),
        never "always exceeded"."""
        hit_global = self._total > self._limit_global if self._limit_global > 0 else False
        hit_ch = self._active[ch] > self._limit_ch if self._limit_ch > 0 else False
        if not (hit_global or hit_ch):
            return
        self._events.appendleft({
            "ts": ts, "ch": ch, "note": note, "total": self._total,
            "ch_total": self._active[ch], "ch_limit": self._limit_ch,
            "hit_global": hit_global, "hit_ch": hit_ch,
        })
        self._flash_until = ts + FLASH_DURATION_S

    def _note_off(self, ch: int, note: int) -> bool:
        counts = self._notes[ch]
        if note not in counts:
            return False   # stray/already-off note-off -- true no-op, mirrors v1
        counts[note] -= 1
        if counts[note] <= 0:
            del counts[note]
        self._active[ch] = max(0, self._active[ch] - 1)
        self._total = max(0, self._total - 1)
        return True

    def _clear_channel(self, ch: int) -> bool:
        if not self._notes[ch]:
            return False   # already clear -- true no-op
        removed = self._active[ch]
        self._notes[ch].clear()
        self._active[ch] = 0
        self._total = max(0, self._total - removed)
        return True

    def _clear_all(self) -> bool:
        if self._total == 0:
            return False   # true no-op: nothing held on any channel
        for ch in self._notes:
            self._notes[ch].clear()
            self._active[ch] = 0
        self._total = 0
        return True

    # -- wall-progress hook (Phase 9 Task 2 -- chrome-flash decay only) ------

    def tick(self, now: float) -> bool:
        """Reports the poly-limit chrome flash's ON/OFF EDGE only (mirrors
        `analyzers/ccmonitor.py::tick()`'s own "transition only" dirty
        convention, not `analyzers/stucknotes.py`'s "dirty every tick while
        alerting" one -- the flash has no live-updating value to push, just
        a binary state, so re-marking `overlay.polylimit` dirty every
        `tick_hz` period while flashing would be needless churn for a
        client that already got the ON transition). `self._now` is stored
        UNCONDITIONALLY every call (dirty or not) so `view_model()`'s
        `age_s` field (read by `page.voices`'s subscribers, a SEPARATE
        concern from the flash) stays accurate to within one tick period
        regardless of whether the flash itself changed -- same "not just
        the analyzer that went dirty" precedent as
        `analyzers/ccmonitor.py::tick()`'s own docstring."""
        self._now = now
        was_active = self._flash_active
        self._flash_active = now < self._flash_until
        return was_active != self._flash_active

    # -- view model -----------------------------------------------------------

    def _event_view(self, entry: dict) -> dict:
        age_s = 0.0 if self._now is None else max(0.0, self._now - entry["ts"])
        return {
            "ch": entry["ch"], "note": entry["note"], "total": entry["total"],
            "ch_total": entry["ch_total"], "ch_limit": entry["ch_limit"],
            "hit_global": entry["hit_global"], "hit_ch": entry["hit_ch"],
            "age_s": round(age_s, 1),
        }

    def view_model(self) -> dict:
        return {
            "channels": [
                {
                    "active": self._active[ch],
                    "peak": self._peak[ch],
                    "notes": sorted(self._notes[ch]),
                }
                for ch in range(1, N_CHANNELS + 1)
            ],
            "total": self._total,
            "total_peak": self._total_peak,
            # Phase 9 Task 2: v1's `_events` deque (poly-limit "instant" tag
            # only, see module docstring), newest-first -- matches v1's own
            # `appendleft` + `deque(maxlen=...)` ordering exactly.
            "events": [self._event_view(e) for e in self._events],
        }

    def flash_view_model(self) -> dict:
        """Minimal chrome-flash projection (Phase 9 Task 2) -- a SEPARATE,
        small VM from `view_model()` above, so a client subscribed only to
        the chrome flash (`overlay.polylimit`, see `engine/core.py`'s
        `_PolyLimitOverlay`) never pulls the full 16-channel breakdown
        `page.voices`'s own subscribers already get on every note played."""
        return {"flashing": self._flash_active}
