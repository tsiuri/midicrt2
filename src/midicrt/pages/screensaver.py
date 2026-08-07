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

Disclosed limitation vs. v1: chrome strips stay lit
---------------------------------------------------------------------------
v1's raw-framebuffer blank overwrites the ENTIRE screen, including the
area other plugins (timeclock/beatflash/etc.) would otherwise occupy --
there is nothing left for the compositor to draw over once `_blank_fb()`
runs. v2 cannot reproduce that: `clients/chrome.py`'s status/secondary/
beatprogress strips are painted by each client's run loop AFTER the page
renderer returns, unconditionally, regardless of which page is current
(the same "page owns only its body" split every other phase-3 page already
lives under -- see `docs/phase3-notes.md`'s "Factor frame chrome... BEFORE
cloning the eventlog layout" contract). Suppressing chrome specifically
for this one page would mean threading page-identity awareness into the
otherwise page-agnostic run loops, which is out of this task's scope --
disclosed here, not silently different from what a reader would expect.
This also means TUI's screensaver body still shows a reverse-videoed
(but textless) blank top row, since `run_tui`'s render loop always treats
a renderer's first returned line as "the header" and reverse-videos it
unconditionally -- of the two clients, this doesn't matter in practice:
TUI runs in an ordinary terminal emulator with no CRT burn-in risk at all,
so the FB client (which DOES draw a true, header-less black body) is the
one this page's real "avoid burn-in" purpose targets.

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
