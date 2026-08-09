"""MarqueeAnalyzer: the header page-title scrolling marquee, ported from
v1's `~/codex/midicrt/midicrt.py` (READ-ONLY reference on the Pi), row 0 of
its `_ui_loop_body()` -- lines 888-913 (header marquee) and 920-948
(autoconnect-log's own independent scroll, same mechanic, see the bottom of
this docstring). `docs/visual-audit.md` §20b calls this v1's "single
most-visible, always-on, every-page anti-burn-in device" -- v2's page
headers were static (current-page-title-only, never moving) before this
task, "the literal opposite of v1's design intent" per that same audit row.

v1's exact mechanics (read before touching HEADER_SCROLL_SPEED/GAP/the
offset formula)
---------------------------------------------------------------------------
    page_titles = "  ".join(f"[{pid}:{p.PAGE_NAME}]" for pid, p in sorted(PAGES.items()))
    doubled = (page_titles + "    ") * 2          # midicrt.py:901
    if len(page_titles) <= SCREEN_COLS:
        draw_line(0, page_titles)                  # fits -- static, no scroll
    else:
        dt = now - last_time
        offset_accum += HEADER_SCROLL_SPEED * dt    # 4.0 chars/sec (midicrt.py:169)
        offset = int(offset_accum) % (len(page_titles) + 4)
        draw_line(0, doubled[offset:offset + SCREEN_COLS])

Three mechanics this module ports exactly: the **speed** (4.0 chars/sec,
`HEADER_SCROLL_SPEED` below, byte-for-byte v1's own constant), the **gap
text** (`GAP = "    "`, 4 literal spaces -- the seam between the last title
and the repeated first one when the doubled string wraps), and the
**engage condition** (scroll only when the content is wider than the
available width -- v1's `len(page_titles) <= SCREEN_COLS` check). The
engage condition is NOT decided here (see "Screen width is a renderer
concern" below).

Adapted, not a literal port: accumulator -> anchor+elapsed
---------------------------------------------------------------------------
v1 accumulates `offset_accum += HEADER_SCROLL_SPEED * dt` once per UI-loop
iteration, where `dt` is measured between successive real frame renders --
a per-frame accumulator that (in principle) can drift under irregular frame
timing. This analyzer instead anchors to the first `tick(now)` call and
computes `offset = int(HEADER_SCROLL_SPEED * (now - anchor)) % modulo` fresh
every time -- a PURE function of the injected `now`, matching this
project's existing dual-clock precedent (`pages/pianoroll.py`'s
`_beat_zero_ts` anchor, task-3-report.md §3) rather than v1's own
stateful-accumulator mechanism. Mathematically equivalent when ticks are
regular (which `engine/core.py::_tick_analyzers` guarantees at `tick_hz`),
strictly more robust when they aren't -- a disclosed, deliberate
improvement, not a v1 mismatch.

Screen width is a renderer concern, not this analyzer's
---------------------------------------------------------------------------
v1 only ever had ONE screen width live at a time (`SCREEN_COLS`, the
ncurses terminal). v2's fb and TUI clients have DIFFERENT character
budgets for the identical roster text, so "does this need to scroll at
all" (the engage condition) and "how many characters are visible" cannot
be decided here -- see `clients/chrome.py`'s `marquee_window_text(vm,
width)`, which takes the width explicitly, the same "beatprogress_row_text
already established" precedent that module's own docstring cites. This
analyzer always advances `offset` regardless of whether any particular
client would actually need to scroll at its own width -- harmless (the
offset is simply unused by a client wide enough not to need it), and
correct architecturally: v1's single-screen-width assumption doesn't
generalize to multiple simultaneous client widths.

Page roster + titles: PAGE_IDS / PAGE_TITLES
---------------------------------------------------------------------------
v1's `p.PAGE_NAME` is a per-page-module constant; v2 pages have no
equivalent class attribute (each page's `view_model()` embeds a "title"
string, e.g. `pages/pianoroll.py`'s `"title": "PIANOROLL"`, but that isn't
callable before a page has ticked/produced a view_model, and this
analyzer is constructed at `Engine.__init__` time, before any event has
ever flowed). `PAGE_TITLES` below is a disclosed, deliberate small
duplication of those already-literal per-page title strings (grepped from
each page module directly, not invented) -- see
`tests/test_analyzers_marquee.py`'s own consistency test, which
constructs the real roster and asserts each entry equals that page's own
`view_model()["title"]`, so a future page-title edit that forgets this
table fails loudly instead of silently drifting.

`PAGE_IDS` reuses v1's OWN page-ID numbering (`engine/core.py`'s
`_SYSEX_PAGE_ID_MAP`, built for `engine/sysex.py`'s CMD_SWITCH_PAGE Cirklon
compatibility layer) rather than inventing a second, v2-only numbering
scheme -- `engine/core.py` now derives `_SYSEX_PAGE_ID_MAP` FROM this
table (inverted) instead of the other way around, making this the single
source of truth for "v1 page ID <-> v2 page name" (an anti-drift cleanup,
not a behavior change -- `test_engine_sysex_dispatch.py`'s existing
ID-based tests are the regression coverage). A page with no v1 ID at all
(only "screensaver" today -- v1 has no page concept for it, see
`pages/screensaver.py`'s own module docstring) is simply absent from
`PAGE_IDS` and therefore never appears in the marquee text, matching v1
exactly (screensaver was never a member of v1's own `PAGES` dict either).

Autoconnect-log's independent scroll (v1 row 1, right-aligned window) --
mechanism ported, DORMANT (no v2 data source)
---------------------------------------------------------------------------
v1's `AUTOCONNECT_LOG` (a rolling list of MIDI port hot-plug messages) gets
its OWN independent scroll offset/timer, same `HEADER_SCROLL_SPEED`, same
doubled-string-with-trailing-gap trick as the header, but a DIFFERENT
window-SIZING formula (`midicrt.py:924-948`): `window = min(len(msg),
max_avail)` when `len(msg) <= 12`, else `min(max_avail, max(8, len(msg) //
2))`. `autoconnect_window_size()` below ports that formula exactly (tested
in isolation, `tests/test_analyzers_marquee.py`) -- but v2 has no
"autoconnect log" concept at all client-side (no MIDI hot-plug log stream
exists anywhere in this engine, confirmed absent from `engine/midi_in.py`),
so there is nothing to feed it. Ported here, disclosed, and left
unwired -- `docs/visual-audit.md`'s own row for this item is marked
DIFFERENT (mechanism ported, no live consumer), not silently dropped and
not falsely claimed PRESENT.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/beatflash.py's/transport.py's own
    # comment -- avoids a circular import with engine.core, which builds
    # _ANALYZER_FACTORIES from modules like this one.
    from midicrt.engine.core import MidiEvent

HEADER_SCROLL_SPEED = 4.0   # chars/sec -- v1's own HEADER_SCROLL_SPEED (midicrt.py:169)
GAP = "    "                # v1's literal 4-space seam (midicrt.py:901's "    ")

# v1 page ID -> v2 page name, INVERTED here (name -> id) so this module can
# build "[pid:TITLE]" text without a second lookup. `engine/core.py`'s
# `_SYSEX_PAGE_ID_MAP` derives from this table (see module docstring) --
# values copied from that map's own already-verified v1 IDs, not
# re-derived. "tuner" is included (has a real v1 ID, `10`) even though it
# is NOT in `config.py`'s default page roster -- `MarqueeAnalyzer` only
# ever sees whatever roster it's actually constructed with, so an
# unreachable page name simply never appears in that instance's text; the
# table itself lists every v1-numbered page v2 knows about.
PAGE_IDS: dict[str, int] = {
    "help": 0, "harmony": 1, "sendnotes": 2, "ccmonitor": 4, "ccdashboard": 5,
    "eventlog": 6, "progchanges": 7, "pianoroll": 8, "spectrum": 9, "tuner": 10,
    "chordkey": 11, "voices": 13, "config": 14, "img2txtviz": 17,
}

# v2 page name -> display title, copied VERBATIM from each page's own
# view_model() "title" field (module docstring's own citation list; see
# that docstring section for the anti-drift consistency test). A page
# absent from this table (there are none among PAGE_IDS' keys today) falls
# back to `name.upper()` in `MarqueeAnalyzer.__init__` -- forward-
# compatible with a future v1-numbered page this table simply hasn't been
# updated for yet, rather than crashing the whole marquee over one gap.
PAGE_TITLES: dict[str, str] = {
    "help": "HELP", "harmony": "HARMONY", "sendnotes": "SEND NOTES",
    "ccmonitor": "CC MONITOR", "ccdashboard": "CC DASHBOARD", "eventlog": "EVENT LOG",
    "progchanges": "PROGRAM CHANGES", "pianoroll": "PIANOROLL", "spectrum": "SPECTRUM",
    "tuner": "TUNER", "chordkey": "CHORD+KEY", "voices": "VOICES", "config": "CONFIG",
    "img2txtviz": "IMG2TXT",
}


def _marquee_text(roster: list[str]) -> str:
    """v1's exact `"  ".join(f"[{pid}:{name}]" for pid, p in sorted(...))`
    -- entries sorted by v1 page ID (NOT roster/insertion order), names
    absent from `PAGE_IDS` (no v1 page concept, e.g. "screensaver") silently
    excluded, matching v1's own `PAGES` dict never having contained them
    either."""
    entries = sorted(
        ((PAGE_IDS[name], PAGE_TITLES.get(name, name.upper()))
         for name in roster if name in PAGE_IDS),
        key=lambda e: e[0],
    )
    return "  ".join(f"[{pid}:{title}]" for pid, title in entries)


def autoconnect_window_size(msg_len: int, max_avail: int) -> int:
    """v1's exact autoconnect-log window-sizing formula (`midicrt.py`'s
    `if len(msg) <= 12: window = min(len(msg), max_avail) else: window =
    min(max_avail, max(8, len(msg) // 2))`) -- see module docstring's
    "Autoconnect-log" section for why this is ported but unwired."""
    if max_avail <= 0:
        return 0
    if msg_len <= 12:
        return min(msg_len, max_avail)
    return min(max_avail, max(8, msg_len // 2))


class MarqueeAnalyzer:
    """Pure state: `handle(ev) -> bool` is always a no-op (v1's marquee has
    NO MIDI dependency at all -- purely wall-clock + the page roster, see
    module docstring), `tick(now) -> bool` advances the scroll position
    from an anchor timestamp, `view_model() -> dict` reports the joined
    title text + pre-doubled string + current integer offset for CLIENT
    renderers to slice (screen-width-aware slicing happens in
    `clients/chrome.py::marquee_window_text` -- see module docstring's
    "Screen width is a renderer concern" section)."""

    def __init__(self, roster: list[str], speed_cps: float = HEADER_SCROLL_SPEED) -> None:
        self._text = _marquee_text(roster)
        self._doubled = (self._text + GAP) * 2
        self._modulo = len(self._text) + len(GAP)
        self._speed = float(speed_cps)
        self._anchor: float | None = None
        self._offset: int = 0

    def handle(self, ev: MidiEvent) -> bool:
        return False   # no MIDI dependency, mirrors v1 exactly (see module docstring)

    def tick(self, now: float) -> bool:
        if self._anchor is None:
            self._anchor = now
        if self._modulo <= 0 or self._speed <= 0:
            changed = self._offset != 0
            self._offset = 0
            return changed
        elapsed = now - self._anchor
        offset = int(self._speed * elapsed) % self._modulo
        changed = offset != self._offset
        self._offset = offset
        return changed

    def view_model(self) -> dict:
        return {"text": self._text, "doubled": self._doubled, "offset": self._offset}
