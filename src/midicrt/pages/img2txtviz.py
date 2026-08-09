"""Img2txtviz page: wraps `analyzers.img2txtviz.Img2TxtVizAnalyzer` -- v1's
`pages/img2txtviz.py` (PAGE_ID 17, "MIDI IMG2TXT"). See that analyzer
module's docstring for the full v1 investigation: despite the name and the
orphaned `~/codex/midicrt/imgbank/continue/` directory sitting next to it,
v1 never loads an image file at all -- it's a real-time MIDI-reactive
procedural ASCII generator, not an image viewer.

Roster placement (`config.pages` -- see config.py's own comment): v1's
`pagecycle` plugin (an unconditional wall-clock interval timer, NOT
idle-triggered -- see behaviors/pagecycle.py's own docstring, corrected
Phase 8 Task 5) does NOT include this page (PAGE_ID 17) in its curated
`cycle_pages=[1,6,8,9]` subset. At the time this page was ported (task 9),
`behaviors/pagecycle.py` didn't model that curated-subset concept at all
(it idle-advanced through the WHOLE `config.pages` roster instead, a
disclosed re-interpretation since REVERSED -- Phase 8 Task 5 restored the
curated-subset mechanism verbatim, `config.pagecycle_pages` defaulting to
exactly v1's four mapped names). Even under the restored mechanism,
img2txtviz's absence from `pagecycle_pages` is still correctly NOT the
signal that decided this page's presence in the default ROSTER
(`config.pages`, distinct from `pagecycle_pages` -- membership in the
roster only means `page.next`/`.prev`/`.goto` can reach it, not that
pagecycle auto-visits it): the actual precedent already established by
"voices"/"harmony"/"pianoroll"/"spectrum" (and the COUNTER-example,
"tuner") is "does this page show something real by default with no
unbuilt dependency." This page is a fully self-contained MIDI + wall-
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

    # -- page-declared actions (Phase 4 Task 0, docs/phase4-notes.md) -------
    #
    # These two used to be registered directly in `Engine.__init__`
    # (`_img2txtviz_charset`/`_img2txtviz_invert`), guarded by their own
    # `if "img2txtviz" in self.pages:` block and hand-marking
    # `page.img2txtviz` dirty themselves -- see pages/pianoroll.py's own
    # `actions()` docstring for the full rationale (same consolidation,
    # same generic dirty-marking now done once by `Engine._wrap_page_action`
    # instead of per-handler).

    def _action_charset(self) -> dict:
        return {"charset": self.cycle_charset()}

    def _action_invert(self) -> dict:
        return {"invert": self.toggle_invert()}

    def actions(self) -> list[tuple[str, object, str, dict[str, str]]]:
        return [
            ("img2txtviz.charset", self._action_charset,
             "Cycle the img2txtviz ASCII charset", {}),
            ("img2txtviz.invert", self._action_invert,
             "Toggle the img2txtviz invert flag", {}),
        ]
