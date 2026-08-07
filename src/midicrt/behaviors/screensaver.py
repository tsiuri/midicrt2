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
        # Active: the only way out is NEW activity (last_activity_ts moving
        # past the value recorded at the moment we activated) -- restore
        # whatever page was showing before, matching the brief's "activity
        # -> restore previous page".
        if last_activity_ts != self._activity_at_activation:
            self._active = False
            target = self._previous_page
            self._previous_page = None
            self._activity_at_activation = None
            return ("page.goto", {"name": target})
        return None
