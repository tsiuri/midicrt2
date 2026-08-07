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
- **event-log-of-voices**: zvoicemonitor.py's `_events` deque records
  POLY-LIMIT-EXCEEDED warnings (comparing live counts against
  `POLY_LIMIT_GLOBAL`/`POLY_LIMIT_CH`, gated by an over-limit-duration
  measured in beats via a `tick` counter `draw()` receives from elsewhere)
  -- a limit/warning feature, not a note event log. The task's VM contract
  (`{"ch", "name", "active", "peak", "notes"}` + totals) has no limit or
  warning field, and no `poly_limit_*` config exists in v2 -- NOT ported.
  Flagged here rather than silently dropped.

Channel indexing matches v1: `MidiEvent.channel` is 0-based (mido); this
module's public `view_model()` is 1-based via list position (index 0 ==
channel 1), matching zvoicemonitor's own `ch = msg.channel + 1`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's own comment -- avoids a
    # circular import with engine.core, which builds _PAGE_FACTORIES from
    # modules like this one (via pages/voices.py).
    from midicrt.engine.core import MidiEvent

N_CHANNELS = 16
_ALL_NOTES_OFF_CONTROLS = {120, 123}   # All Sound Off, All Notes Off
_CLEAR_ALL_TYPES = {"start", "stop"}


class VoiceMonitorAnalyzer:
    """Pure state machine: `handle(MidiEvent) -> bool` (dirty), `view_model()
    -> dict`. No I/O -- see module docstring for the v1 behaviors ported and
    the ones deliberately left out (sustain, poly-limit warnings)."""

    def __init__(self) -> None:
        # 1-based channel -> {note: overlap count}. count > 1 means the same
        # pitch is held by more than one overlapping note-on (module
        # docstring's "note-on/off pairing").
        self._notes: dict[int, dict[int, int]] = {ch: {} for ch in range(1, N_CHANNELS + 1)}
        self._active: dict[int, int] = dict.fromkeys(range(1, N_CHANNELS + 1), 0)
        self._peak: dict[int, int] = dict.fromkeys(range(1, N_CHANNELS + 1), 0)
        self._total = 0
        self._total_peak = 0

    def handle(self, ev: MidiEvent) -> bool:
        if ev.type in _CLEAR_ALL_TYPES:
            return self._clear_all()
        if ev.type == "note_on":
            if ev.channel is None:
                return False
            ch = ev.channel + 1
            if ev.data2 == 0:
                return self._note_off(ch, ev.data1)
            return self._note_on(ch, ev.data1)
        if ev.type == "note_off":
            if ev.channel is None:
                return False
            return self._note_off(ev.channel + 1, ev.data1)
        if ev.type == "control_change" and ev.data1 in _ALL_NOTES_OFF_CONTROLS:
            if ev.channel is None:
                return False
            return self._clear_channel(ev.channel + 1)
        return False

    def _note_on(self, ch: int, note: int) -> bool:
        counts = self._notes[ch]
        counts[note] = counts.get(note, 0) + 1
        self._active[ch] += 1
        self._total += 1
        self._peak[ch] = max(self._peak[ch], self._active[ch])
        self._total_peak = max(self._total_peak, self._total)
        return True

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
        }
