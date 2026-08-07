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

Arbitration with the screensaver (Critical fix, task-9 review)
---------------------------------------------------------------------------
`idle_s` (pagecycle's own idle threshold) and `after_s` (`behaviors/
screensaver.py`'s threshold) are two INDEPENDENT clocks measured from the
SAME `last_activity_ts` -- with the shipped defaults (`idle_s=300`,
`after_s=60`, both enabled), a fully idle engine crosses 60s (screensaver
activates) and then, inevitably, ALSO crosses 300s (pagecycle's own
threshold) with NO new activity in between. Before this fix, `tick()`
never looked at `current_page` at all, so at t=300 it dispatched
`page.next` unconditionally -- un-blanking the just-activated screensaver
and, since nothing resets `last_activity_ts` while genuinely idle, going
on to fire AGAIN every further `idle_s` (t=600, t=900, ...) for as long as
the engine stayed idle. That is the exact burn-in-defeating failure mode
`behaviors/screensaver.py` exists to prevent -- reproduced against the
shipped defaults by `test_engine_core.py::
test_pagecycle_does_not_unblank_screensaver_with_shipped_defaults`.

Fix: `tick()` now refuses to act at all while `current_page` IS the
screensaver page (checked FIRST, before any idle-time bookkeeping). This
was chosen over the alternative (asking `ScreensaverBehavior` whether it
considers itself "active") because it is strictly more robust: it also
covers a screensaver reached by a MANUAL `page.goto screensaver` (from any
client, independent of `ScreensaverBehavior`'s own latch), which an
active-flag check would miss entirely. While blocked this way,
`_idle_since`/`_last_seen_activity` are left untouched (not advanced, not
reset) -- idle time "spent" showing the screensaver neither counts for nor
against pagecycle's own timer.

Re-arming after a manual escape (2nd review pass -- a NEW Important
finding this first fix introduced)
---------------------------------------------------------------------------
The paragraph above ends "...the ordinary 'activity moved forward -> re-arm
from here' branch below picks the timer back up cleanly from that instant"
-- true ONLY when real MIDI activity is what ended the block (the normal
`ScreensaverBehavior`-driven restore, where `last_activity_ts` genuinely
advances). It is FALSE for a MANUAL escape (a client dispatching
`page.next`/`page.goto` directly, with NO new MIDI activity): `current_page`
leaves the screensaver page, but `last_activity_ts` does not move, so the
"activity moved forward" branch never fires, and `_idle_since` is still
whatever ancient value it was frozen at when blocking began. The very next
tick's elapsed check (`now - _idle_since >= idle_s`) is then almost always
already true (real wall-clock `now` has kept advancing the whole time the
behavior sat blocked) -- so `tick()` fired `page.next` on literally the
very next tick after a manual escape, discarding the user's manual choice
just as surely as the ORIGINAL Critical bug did, just via the sibling
behavior this time. Reproduced by `tests/test_behaviors_pagecycle.py::
test_rearms_after_a_manual_screensaver_escape_instead_of_firing_immediately`.

Fix: track whether the PREVIOUS tick was blocked (`_was_blocked`). On the
first tick where the block has just ended, check whether activity actually
advanced during the block:
- If it DID (the normal activity-driven restore case), fall through to the
  existing "activity moved forward" branch unchanged -- it already
  re-arms correctly from the real activity instant.
- If it did NOT (a manual escape), re-arm `_idle_since = now` directly and
  return `None` this tick -- exactly the same "reset the clock, take no
  action THIS tick" shape the activity-moved-forward branch already uses,
  just anchored to `now` (the escape instant) instead of an activity
  timestamp, since no real activity instant exists to anchor to here.
  This is what lets pagecycle resume normal operation a full, FRESH
  `idle_s` after a manual escape, rather than firing immediately or being
  permanently wedged.
"""
from __future__ import annotations

from midicrt.behaviors.screensaver import SCREENSAVER_PAGE


class PageCycleBehavior:
    """Pure state machine: `tick(now, last_activity_ts, current_page) ->
    (action_name, args) | None`. No I/O, no dispatch of its own -- see
    `behaviors/__init__.py`'s module docstring for the "returns an intent,
    never acts" contract. `now`/`last_activity_ts` are always injected by
    the caller (`Engine._tick_behaviors`), never read here."""

    def __init__(self, enabled: bool, idle_s: float,
                 screensaver_page: str = SCREENSAVER_PAGE) -> None:
        self.enabled = enabled
        self.idle_s = idle_s
        self._screensaver_page = screensaver_page
        self._last_seen_activity: float | None = None
        self._idle_since: float | None = None
        self._was_blocked = False

    def tick(self, now: float, last_activity_ts: float,
              current_page: str) -> tuple[str, dict] | None:
        if not self.enabled:
            return None
        if current_page == self._screensaver_page:
            # Critical fix (see module docstring's "Arbitration with the
            # screensaver" section) -- never act while the screensaver is
            # showing, however it got there.
            self._was_blocked = True
            return None
        activity_advanced = (self._last_seen_activity is None
                              or last_activity_ts != self._last_seen_activity)
        if self._was_blocked and not activity_advanced:
            # 2nd-review-pass fix (see module docstring's "Re-arming after
            # a manual escape" section): the block just ended, but NOT via
            # real activity -- a manual page.next/goto escape. `_idle_since`
            # is stale relative to `now`; falling through to the elapsed
            # check below would fire immediately, discarding the user's
            # manual choice exactly like the ORIGINAL Critical bug. Re-arm
            # from `now` (the escape instant) instead.
            self._was_blocked = False
            self._idle_since = now
            return None
        self._was_blocked = False
        if activity_advanced:
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
