"""ScreensaverBehavior: idle-triggered burn-in guard, ported (with a
disclosed re-interpretation of the ACTUATION mechanism) from v1's
`~/codex/midicrt/plugins/zscreensaver.py` (READ-ONLY reference on the Pi).

v1's actual mechanism
---------------------------------------------------------------------------
v1's `draw(state)` latches `_active = True` once `time.time() -
_last_activity >= IDLE_TIMEOUT` (default 60.0s, `config/settings.json`'s
`screensaver.idle_timeout` on the Pi) and, while active, writes RAW ZEROS
directly into `/dev/fb0` via an mmap (`_blank_fb`) -- bypassing the
compositor and every plugin/page entirely, so literally nothing else on
screen is visible while blanked. `handle(msg)` treats `note_on`/`note_off`/
`control_change` as "activity" (nothing else -- NOT `clock_tick`/start/
stop/songpos, so a running MIDI clock alone never prevents the screensaver
from kicking in); waking calls `deactivate()`, which just flips `_active`
back to `False` (the compositor resumes drawing over the now-stale
framebuffer on its own next frame -- v1 does not "restore a page", it has
no page-switching mechanism at all here, it just stops overwriting the
one screen it's always been drawing to).

Disclosed re-interpretation for v2 (per this task's brief)
---------------------------------------------------------------------------
v2 has no engine-side way to poke pixels directly (an engine-side
behavior "acts only through actions", see `behaviors/__init__.py`) and
routes ALL display state through the ordinary page mechanism -- so v1's
"blank the raw framebuffer" becomes, per the brief, `page.goto
screensaver` (a real page, `pages/screensaver.py`, whose renderers clear
to background with no content -- see that page's own module docstring for
why it draws nothing) plus, on waking, `page.goto <the page that was
showing before>` -- a "remember and restore" step v1 never needed (it
never left the page it was already drawing on top of). `IDLE_TIMEOUT`
carries over unchanged as `config.screensaver_after_s`'s default (60.0s).

"MIDI activity" reuses the SAME engine-tracked signal as pagecycle
---------------------------------------------------------------------------
v1's own `handle()` only counts `note_on`/`note_off`/`control_change` as
activity -- explicitly NOT `clock_tick`/transport messages, so a MIDI
clock left running (with nobody actually playing) still screensaves after
`IDLE_TIMEOUT`. `Engine._last_activity_ts` (engine/core.py) is filtered to
that exact same event-type set for that exact same reason (see that
module's docstring) -- this behavior and `behaviors/pagecycle.py` both
read it as-is, with no independent activity tracking of their own.

Deployed v1 default: no enable/disable knob at all -- always live
---------------------------------------------------------------------------
`config/settings.json`'s `screensaver` section on the Pi has ONLY
`idle_timeout` -- no `enabled` key exists in v1 at all (unlike
`pagecycle.py`, which reads its own `ENABLED` flag from config).
v1's screensaver plugin is simply always loaded and always live as long as
`zscreensaver.py` is present under `plugins/` (v1's loader globs every
`*.py` there unconditionally -- see `analyzers/timesig.py`'s module
docstring for the same loader mechanic, confirmed against `midicrt.py`'s
`load_plugins()`). `config.screensaver_enabled` therefore defaults to
`True` to match that always-on deployed reality, even though v2 -- unlike
v1 -- does give it an explicit config knob to turn off (the task brief's
"Behaviors must be disable-able via config" requirement; v1 offers no such
switch short of deleting the plugin file).

Manual page changes while active are a user override, not noise (Important
fix, task-9 review)
---------------------------------------------------------------------------
v1 has no analog for this at all -- it never leaves the one screen it
overwrites, so there is no "someone navigated away from the screensaver
by other means" case to consider. v2's `page.goto`/`page.next` are global
actions any connected client can dispatch at any time, independent of this
behavior -- a real TUI/fb client's own `n`/`c` keys, or another script on
the control socket, can change `current_page` away from `screensaver`
WITHOUT any new MIDI activity ever happening. Before this fix, `tick()`'s
"active" branch only ever watched `last_activity_ts` for the restore
trigger, so a manual navigation like that was invisible to it: internally
`_active` stayed `True` and `_previous_page` stayed whatever page was
current at ACTIVATION time, and the next real MIDI activity would
dispatch `page.goto` back to that STALE remembered page -- silently
discarding the user's manual choice out from under them. Reproduced by
`tests/test_behaviors_screensaver.py::
test_manual_page_change_while_active_is_treated_as_user_override_no_restore`.

Fix: `tick()` now checks, on every call while `_active`, whether
`current_page` is STILL the screensaver page. The moment it isn't --
regardless of why -- that is treated as "the user (or some other actor)
already took over," matching what a human would expect: deactivate
quietly (no dispatch of this behavior's own; the page has already changed
to whatever the override set it to) and forget `_previous_page`, so a
LATER activity tick has nothing stale left to restore.

Disclosed consequence: a manual override buys no grace period on its own
---------------------------------------------------------------------------
Idle time is measured PURELY from `last_activity_ts` (real MIDI traffic,
`Engine._last_activity_ts`) -- a behavior has no channel to bump that
shared, engine-owned clock itself (see `behaviors/__init__.py`'s "acts
only through actions" contract). A manual page-change escape therefore
does NOT reset the idle timer the way v1's own `deactivate()` resets
`_last_activity = time.time()` on ANY wake trigger including a keypress
(`~/codex/midicrt/plugins/zscreensaver.py`) -- if `last_activity_ts`
genuinely never advances, this behavior is free to reclaim the display
again on the very next tick after a manual override, with no cooldown.
This is accepted, not fixed: it only matters for the narrow window between
a manual escape and the next real MIDI event, and the alternative (this
behavior tracking its own separate "last manual override" timestamp to
manufacture a synthetic grace period) would be a second, independent
notion of "activity" this codebase has deliberately avoided everywhere
else (see `engine/core.py`'s own single-source-of-truth
`_last_activity_ts`).
"""
from __future__ import annotations

SCREENSAVER_PAGE = "screensaver"


class ScreensaverBehavior:
    """Pure state machine: `tick(now, last_activity_ts, current_page) ->
    (action_name, args) | None`. No I/O, no dispatch of its own -- see
    `behaviors/__init__.py`'s module docstring for the "returns an intent,
    never acts" contract. `now`/`last_activity_ts`/`current_page` are
    always injected by the caller (`Engine._tick_behaviors`), never read
    or held as an `Engine` reference here."""

    def __init__(self, enabled: bool, after_s: float,
                 screensaver_page: str = SCREENSAVER_PAGE) -> None:
        self.enabled = enabled
        self.after_s = after_s
        self._screensaver_page = screensaver_page
        self._active = False
        self._previous_page: str | None = None
        self._activity_at_activation: float | None = None

    def tick(self, now: float, last_activity_ts: float,
              current_page: str) -> tuple[str, dict] | None:
        if not self.enabled:
            return None
        if not self._active:
            if current_page == self._screensaver_page:
                return None   # already there (e.g. manual navigation) -- nothing to latch
            if now - last_activity_ts < self.after_s:
                return None
            self._active = True
            self._previous_page = current_page
            self._activity_at_activation = last_activity_ts
            return ("page.goto", {"name": self._screensaver_page})
        # Active, but something other than our own restore already moved
        # the page away from "screensaver" (Important fix, see module
        # docstring's "Manual page changes while active" section) -- treat
        # it as a user override: deactivate quietly, forget the stale
        # remembered page, dispatch nothing (the page is already correct).
        if current_page != self._screensaver_page:
            self._active = False
            self._previous_page = None
            self._activity_at_activation = None
            return None
        # Active AND still showing the screensaver: the only way out is NEW
        # activity (last_activity_ts moving past the value recorded at the
        # moment we activated) -- restore whatever page was showing before,
        # matching the brief's "activity -> restore previous page".
        if last_activity_ts != self._activity_at_activation:
            self._active = False
            target = self._previous_page
            self._previous_page = None
            self._activity_at_activation = None
            return ("page.goto", {"name": target})
        return None
