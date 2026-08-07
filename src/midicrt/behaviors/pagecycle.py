"""PageCycleBehavior: auto-advance through the page roster while idle,
ported (with a disclosed re-interpretation) from v1's
`~/codex/midicrt/plugins/pagecycle.py` (READ-ONLY reference on the Pi).

v1's actual mechanism (read before assuming this is a literal port)
---------------------------------------------------------------------------
v1's `draw(state)` runs on EVERY UI frame and switches
`midicrt.current_page` to the next entry in a FIXED list (`CYCLE_PAGES =
[1, 6, 8, 9]`, page IDs -- not "the whole roster") every `INTERVAL` seconds
(default 300s = 5 minutes) UNCONDITIONALLY -- it does not care whether MIDI
is flowing or not. The only thing that suppresses it is a recent KEYPRESS:
`notify_keypress()` (called from v1's main input-handling loop on any key)
stamps `_last_keypress`, and `draw()` skips switching entirely while `now -
_last_keypress < USER_PAUSE` (default 3600s = 1 hour) -- i.e. v1's real
behavior is "always cycling through a curated page subset on a fixed
timer, paused for an hour after the user last touched a key," not an
idle-triggered thing at all.

Disclosed re-interpretation for v2 (per this task's brief)
---------------------------------------------------------------------------
The task brief specifies a DIFFERENT, simpler contract: "idle timer ->
`page.next` when no MIDI activity for `config.pagecycle_idle_s`; any MIDI
activity resets." Two deliberate departures from v1, both required by the
brief and by v2's architecture:
1. **MIDI activity, not keypress.** v1's "activity" signal is a physical
   keypress on the box's own keyboard/input device (input is handled in
   `clients/fb/app.py`'s evdev thread, client-side); a behavior here runs
   engine-side and has no visibility into client input at all -- the only
   engine-side "someone is actively using this" signal is real MIDI
   traffic (`Engine._last_activity_ts`, see engine/core.py's module
   docstring). This is the same substitution `docs/phase3-notes.md`'s
   "behaviors act only through actions" framing implies: behaviors read
   what the engine actually tracks, not what a specific client happens to
   see.
2. **`page.next` through the WHOLE roster, not a curated 4-page subset on
   a fixed schedule paused by recent input.** v1's `CYCLE_PAGES`/
   `USER_PAUSE` model has no clean v2 analog (v2's roster composition is
   config-driven, no page IDs to hardcode a subset against), and the brief
   explicitly asks for `page.next` -- simply advancing one step in
   whatever roster `config.pages` currently holds.

What v2 KEEPS from v1's spirit: repeated cycling, not a one-shot hop
---------------------------------------------------------------------------
Interpreted strictly literally, "fire once after N idle seconds" would
leave the display parked on page 2 forever after a single auto-advance --
that is not a "cycle" in any useful sense, and does not match v1's own
"keep rotating pages" intent. This behavior instead re-arms its idle
window immediately after each auto-advance (see `tick()`), so it keeps
firing every `idle_s` for as long as idleness persists -- reproducing v1's
"periodic rotation while nobody's using it" spirit through the brief's
idle-gated (rather than v1's always-on/keypress-paused) trigger. `idle_s`
therefore plays the same role as v1's `INTERVAL` (the cadence of automatic
advances), which is why `config.pagecycle_idle_s` defaults to v1's
deployed `interval` value (300.0s, `config/settings.json` on the Pi) rather
than something new.

Deployed v1 default carried forward
---------------------------------------------------------------------------
`config/settings.json`'s `pagecycle` section on the Pi has `"enabled":
true` -- `config.pagecycle_enabled` defaults to `True` to match (see
`config.py`'s own comment for the full settings.json evidence).
"""
from __future__ import annotations


class PageCycleBehavior:
    """Pure state machine: `tick(now, last_activity_ts, current_page) ->
    (action_name, args) | None`. No I/O, no dispatch of its own -- see
    `behaviors/__init__.py`'s module docstring for the "returns an intent,
    never acts" contract. `now`/`last_activity_ts` are always injected by
    the caller (`Engine._tick_behaviors`), never read here."""

    def __init__(self, enabled: bool, idle_s: float) -> None:
        self.enabled = enabled
        self.idle_s = idle_s
        self._last_seen_activity: float | None = None
        self._idle_since: float | None = None

    def tick(self, now: float, last_activity_ts: float,
              current_page: str) -> tuple[str, dict] | None:
        if not self.enabled:
            return None
        if self._last_seen_activity is None or last_activity_ts != self._last_seen_activity:
            # Activity moved forward (or this is the very first call, where
            # there is nothing yet to measure idleness FROM) -- (re)arm the
            # idle window starting at the activity instant itself, and take
            # no action this tick. This is also what makes a freshly
            # constructed behavior safe to call immediately: it bootstraps
            # from whatever `last_activity_ts` already is (Engine seeds it
            # at construction time to `time.time()`, never epoch 0), rather
            # than firing spuriously on its first-ever tick.
            self._last_seen_activity = last_activity_ts
            self._idle_since = last_activity_ts
            return None
        if now - self._idle_since >= self.idle_s:
            self._idle_since = now   # re-arm immediately -- keep cycling while idle persists
            return ("page.next", {})
        return None
