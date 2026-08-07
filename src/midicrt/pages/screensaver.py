"""Screensaver page: the visual `behaviors/screensaver.py` switches to via
`page.goto screensaver`, ported from v1's `~/codex/midicrt/plugins/
zscreensaver.py` (READ-ONLY reference on the Pi).

v1 draws NOTHING -- it writes raw zeros directly into `/dev/fb0`,
bypassing the compositor and every page/plugin entirely (true RGB565
black, no header, no text, no chrome). This page reproduces that as
faithfully as v2's page/chrome architecture allows: its `view_model()`
carries no content, and both clients' renderers (`clients/tui.py`'s
`render_screensaver_lines`, `clients/fb/app.py`'s
`render_screensaver_frame`) paint a blank body with no header bar at all --
unlike every other v2 page, which always draws a reverse-video title
header first.

Chrome strips are suppressed too -- a TRUE full blank (task-9 review fix)
---------------------------------------------------------------------------
v1's raw-framebuffer blank overwrites the ENTIRE screen, including the
area other plugins (timeclock/beatflash/etc.) would otherwise occupy --
there is nothing left for the compositor to draw over once `_blank_fb()`
runs. The first cut of this page shipped WITHOUT reproducing that (chrome
strips kept painting over the blank body every frame, regardless of
page -- a real CRT burn-in regression a reviewer caught): `clients/fb/
app.py`'s `_paint_frame` and `clients/tui.py`'s `run_tui` render loop now
both check `page == "screensaver"` at their one shared chrome-painting
call site and skip ALL THREE chrome strips entirely when it is, leaving
the frame exactly as `render_screensaver_frame`/`render_screensaver_lines`
already cleared it (background/blank) -- see those functions' own
docstrings. TUI additionally overwrites its three bottom rows with plain
blank text (not just skipping them) since a terminal, unlike a pixel
surface, does not get implicitly "cleared" by the page renderer alone --
stale content from a previous frame would otherwise persist there.

`handle()` is a pure no-op: this page has no MIDI-reactive content of its
own to update (unlike v1's plugin, which only ever reacts to wall-clock
idle time, not to specific MIDI messages) -- activation/deactivation is
entirely `behaviors/screensaver.py`'s job via `page.goto`, not this page's.
"""


class ScreensaverPage:
    name = "screensaver"

    def handle(self, ev) -> bool:
        return False

    def view_model(self) -> dict:
        return {"title": "SCREENSAVER"}
