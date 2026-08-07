"""Img2txtviz page: wraps `analyzers.img2txtviz.Img2TxtVizAnalyzer` -- v1's
`pages/img2txtviz.py` (PAGE_ID 17, "MIDI IMG2TXT"). See that analyzer
module's docstring for the full v1 investigation: despite the name and the
orphaned `~/codex/midicrt/imgbank/continue/` directory sitting next to it,
v1 never loads an image file at all -- it's a real-time MIDI-reactive
procedural ASCII generator, not an image viewer.

Roster placement (`config.pages` -- see config.py's own comment): v1's
idle-triggered `pagecycle` plugin does NOT include this page (PAGE_ID 17)
in its curated `cycle_pages=[1,6,8,9]` subset -- but task 9's own report
already established that v2's `behaviors/pagecycle.py` does not model that
curated-subset concept at all (it idle-advances through the WHOLE
`config.pages` roster instead, a disclosed re-interpretation made in task
9's port). So "was this page in v1's cycle_pages" is not the signal that
decides v2 default-roster membership -- the actual precedent already
established by "voices"/"harmony"/"pianoroll"/"spectrum" (and the COUNTER-
example, "tuner") is "does this page show something real by default with
no unbuilt dependency." This page is a fully self-contained MIDI + wall-
clock animation with no missing dependency (unlike "tuner", which is
permanently idle until a future audio-capture task), so it DOES join
`config.pages`'s default list.
"""
from midicrt.analyzers.img2txtviz import Img2TxtVizAnalyzer


class Img2TxtVizPage:
    name = "img2txtviz"

    def __init__(self) -> None:
        self._analyzer = Img2TxtVizAnalyzer()

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def tick(self, now: float) -> bool:
        return self._analyzer.tick(now)

    def view_model(self) -> dict:
        return {"title": "IMG2TXT", **self._analyzer.view_model()}

    # -- runtime-adjustable controls (spec §5), mirrored from v1's real
    # keypress()-driven charset-cycle ('c') and invert-toggle ('i') --
    # wired as page-specific actions in engine/core.py, mirroring the
    # pianoroll.zoom/.projection/.channels precedent (task 7).
    def cycle_charset(self) -> str:
        return self._analyzer.cycle_charset()

    def toggle_invert(self) -> bool:
        return self._analyzer.toggle_invert()
