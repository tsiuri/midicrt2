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

A manual override now buys a FRESH `after_s` grace period (2nd review
pass -- corrects the previous "disclosed consequence" below, which turned
out to be a real bug in the sibling behavior)
---------------------------------------------------------------------------
An earlier version of this section argued idle time is measured PURELY
from `last_activity_ts`, so a manual escape gets no grace period and this
behavior is "free to reclaim the display again on the very next tick."
That turned out to be more than an accepted quirk: fixing
`behaviors/pagecycle.py`'s OWN "re-arm after a manual escape" bug (see
that module's docstring) exposed that pagecycle can only ever actually
resume normal operation once it gets a genuinely UNINTERRUPTED stretch of
`idle_s` on a non-screensaver page -- and an instantly-reclaiming
screensaver would re-block it before that stretch could ever complete,
i.e. pagecycle would appear "fixed" in isolation but remain functionally
dead forever in practice whenever the deployed defaults' zero-activity
idle window runs long. This behavior needed the EXACT SAME fix pagecycle
did, for the exact same reason: `_idle_reference_ts` (NOT `last_activity_ts`
directly) is now what the "not active" branch's threshold check compares
`now` against. It tracks `last_activity_ts` under normal operation
(updated whenever real activity advances, so the ordinary "idle for
`after_s` since the last real event" behavior is unchanged), but is ALSO
re-armed to `now` at the moment of a manual-override deactivation --
giving that escape a genuine fresh `after_s` window, mirroring
`behaviors/pagecycle.py`'s `_idle_since` re-arm precisely. Covered by
`tests/test_behaviors_screensaver.py::
test_manual_override_re_arms_the_idle_reference_a_full_after_s_required`
(replaces the now-obsolete "reactivates on the very next tick" test this
section used to point to).
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
        self._last_seen_activity: float | None = None
        # The "not active" branch's idle threshold compares `now` against
        # THIS, not `last_activity_ts` directly -- see module docstring's
        # "A manual override now buys a FRESH after_s grace period" section
        # for why the two can diverge (a manual-override deactivation
        # re-arms this to `now`, independent of whether real activity ever
        # advances).
        self._idle_reference_ts: float | None = None

    def tick(self, now: float, last_activity_ts: float,
              current_page: str) -> tuple[str, dict] | None:
        if not self.enabled:
            return None
        if self._idle_reference_ts is None or last_activity_ts != self._last_seen_activity:
            # Real activity advanced (or this is the very first call) --
            # track it as the idle reference point, same "(re)arm from the
            # activity instant" convention behaviors/pagecycle.py uses.
            self._last_seen_activity = last_activity_ts
            self._idle_reference_ts = last_activity_ts
        if not self._active:
            if current_page == self._screensaver_page:
                return None   # already there (e.g. manual navigation) -- nothing to latch
            if now - self._idle_reference_ts < self.after_s:
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
            # 2nd-review-pass fix: re-arm the idle reference to NOW, giving
            # this manual escape a genuine fresh `after_s` window instead of
            # letting the (possibly ancient) last_activity_ts immediately
            # satisfy the threshold check above on the very next tick.
            self._idle_reference_ts = now
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
