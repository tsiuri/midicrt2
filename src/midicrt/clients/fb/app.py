"""fb/app.py -- midicrt-fb: the framebuffer CRT client entrypoint.

Renders the eventlog page view-model onto a pixel `Surface` (fb/surface.py)
using the vendored PSF console font (fb/text.py), then either writes the
packed RGB565 buffer to a real Linux framebuffer device or, in `--out` test
mode, saves a PNG and exits -- see `main()`/`run()` below.

Colour palette (Phase 8 Task 2: now sourced from the shared monochrome
green-luminance framework, `clients/fb/lum.py` -- see that module's
docstring for the full v1-constant provenance/rationale; this module's own
BG/HEADER_BG/NORMAL_FG/ACCENT_FG names below are unchanged, just
re-pointed at `lum.py`'s tiers):
    LUM_BRIGHT = (0, 255, 80)   # here: HEADER_BG and ACCENT_FG
    LUM_MID    = (0, 180, 50)   # here: NORMAL_FG
    LUM_OFF    = (0, 0, 0)      # here: BG
Accent (note_on) event lines reuse v1's "bright" tone so they read louder
than the "mid" tone used for ordinary lines, and the header bar's
reverse-video fill reuses the same bright tone with text punched out in the
background colour. The pianoroll and img2txtviz renderers additionally use
`lum.py`'s `lum(level)` continuous ramp -- see their own sections below.

This module does not write to /dev/fb0 during Phase 2 Task 3 -- v1 owns the
real CRT until Task 4's supervised smoke test exercises the real-device path
(`_run_device`/`_read_fb_geometry`) for real. All tests here use `--out` PNG
mode against a real (but socket-only) daemon.

Chrome (phase 3 task 3)
------------------------
A bottom status strip (`_draw_status`, `font.height + 2*STATUS_PAD` px
tall) now mirrors the TUI's bottom row: same shared text
(`clients/chrome.py`'s `status_text()`), same reverse-video treatment as
the header. `render_frame` reserves the strip's height when computing how
many event lines fit (`_status_strip_height`) so the page body never draws
under it, but does NOT draw the strip itself -- `render_frame`'s own
signature/contract is unchanged (still `(vm, Surface) -> None`, still only
the eventlog page's own content, no analyzer/overlay knowledge). The run
loops (`run()`'s `--out` branch and `_run_device`) call `_draw_status`
separately, right after the page renderer, same "page owns everything
except the reserved strip" split as tui.py's `render_lines`/
`render_status_row`. Both clients subscribe to `overlay.status` ALONGSIDE
the current page's topic (multi-topic subscribe).

Chrome, part 2 (phase 3 task 6): a second strip for alerts/time-signature
--------------------------------------------------------------------------
A second reserved strip (`_draw_secondary`, same height convention as
`_draw_status`) sits directly ABOVE the status strip, painting
`clients/chrome.py`'s `secondary_status_text()` -- v1's stuck-note
warnings (`overlay.alerts`) when any are active, else the time-signature
estimate (`overlay.timesig`); see that shared function's own docstring for
why the two share one row instead of getting a row each. Every page
renderer's usable-height math now subtracts BOTH strips
(`_secondary_strip_height(font) + _status_strip_height(font)`); the run
loops subscribe to both new overlay topics alongside `overlay.status` and
call `_draw_secondary` right after `_draw_status`.

Chrome, part 3 (phase 3 task 9): a THIRD strip, below status, for
beatflash + loopprogress
--------------------------------------------------------------------------
A third reserved strip (`_draw_beatprogress`, same height convention)
becomes the new TRUE BOTTOM-MOST strip -- `_draw_status`'s own strip moves
up by one strip-height to sit directly above it. This mirrors v1's actual
physical layout exactly: `~/codex/midicrt/plugins/beatflash.py`/
`loopprogress.py` draw at v1's literal bottom two screen rows, BELOW
`plugins/timeclock.py`'s own row (ported as the status strip) -- see
`clients/chrome.py`'s module comment above `beatprogress_row_text()` for
the full row-offset evidence. That shared function needs a character-count
`width` to center v1's loop-progress bar within (the same reason the TUI
client passes `term.width`); this strip passes `(surface.width - 2 *
LEFT_MARGIN) // font.width` -- the printable area in character units,
symmetric about `LEFT_MARGIN` on both sides -- as its closest FB analog.
Every page renderer's usable-height math now subtracts all THREE strips
(`_reserved_chrome_height`); the run loops subscribe to both new overlay
topics and call `_draw_beatprogress` right after `_draw_secondary`/
`_draw_status`.
"""
import argparse
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from midicrt import config as config_mod
from midicrt.behaviors.screensaver import SCREENSAVER_PAGE
from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    dispatch_key,
    drain_latest,
    fetch_keymap,
    switch_topic,
    wait_first_snapshot,
)
from midicrt.clients.fb.lum import (
    LUM_BAR_GUIDE,
    LUM_BRIGHT,
    LUM_DIM,
    LUM_FAINT,
    LUM_FAINT_C,
    LUM_MID,
    LUM_OFF,
    RAMPS,
    lum,
)
from midicrt.clients.fb.surface import Surface, open_fb_mmap
from midicrt.clients.fb.text import draw_text, load_font
from midicrt.clients.tui import _tail

_LOG = logging.getLogger("midicrt-fb")

# -- palette (Phase 8 Task 2: sourced from the shared monochrome
# green-luminance framework, clients/fb/lum.py -- see that module's
# docstring for the v1-constant provenance and the guard test,
# tests/test_fb_monochrome_guard.py, that keeps a colored literal from ever
# reappearing here). These four names/values are UNCHANGED from before this
# task -- every existing call site and golden fixture that uses them keeps
# rendering byte-identical pixels; only their definition now points at the
# one shared module instead of re-declaring the same RGB tuples locally.
BG = LUM_OFF
HEADER_BG = LUM_BRIGHT
NORMAL_FG = LUM_MID
ACCENT_FG = LUM_BRIGHT

# -- layout -----------------------------------------------------------------
HEADER_PAD = 2   # vertical inset (top+bottom) inside the header bar, px
LINE_GAP = 1     # extra vertical gap between event-line rows, px
LEFT_MARGIN = 4  # left inset for header + event text, px
STATUS_PAD = 2   # vertical inset (top+bottom) inside the status strip, px -- mirrors HEADER_PAD

# `--out` mode always renders at this fixed size (task brief: "--out mode
# uses 800x475 fixed"), independent of whatever a real /dev/fb0 reports.
OUT_SIZE = (800, 475)

_SYS_FB = Path("/sys/class/graphics/fb0")


def _header_text(marquee_text: str | None, fallback: str) -> str:
    """Phase 8 Task 4 (docs/visual-audit.md §20b): every page's reverse-
    video header row now shows the LIVE header marquee (v1's primary
    anti-burn-in device -- ported in full at `analyzers/marquee.py` +
    `clients/chrome.py::marquee_window_text`) instead of a permanently-
    static per-page title. `marquee_text` is computed ONCE per frame by
    `_paint_frame` below (the one place that knows both the surface width
    and has the live `overlay.marquee` snapshot) and threaded down into
    whichever `render_X_frame` the current page dispatches to.

    `marquee_text=None` -- every `render_X_frame` parameter's own default,
    i.e. every call site anywhere in this test suite that invokes a
    renderer directly as `render_X_frame(vm, surface)` with no third
    argument, completely unchanged by this task -- falls back to the
    renderer's own OLD static per-page header text instead, so every
    pre-existing test/golden fixture stays byte-identical with zero edits;
    only the real run loops (`_paint_frame`) ever pass a real marquee
    string."""
    return marquee_text if marquee_text is not None else fallback


def render_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render an eventlog view-model onto `surface`. Pure: reads only `vm`
    and the cached default font, writes only to `surface`'s pixels -- no
    I/O, no clock, no global state beyond the font's (side-effect-free)
    glyph cache.

    Layout: a reverse-video header bar (`HEADER_BG` fill, text painted in
    `BG` so it reads as inverted) showing "<title>  (<count> events)", then
    event lines below it, oldest-to-newest top-to-bottom, tailed to
    whatever fits the remaining height at one text-line per row. The
    bottom `_reserved_chrome_height(font)` px (BOTH chrome strips, phase-3
    task 6) are reserved (left as background -- NOT drawn here, see
    `_draw_status`/`_draw_secondary`) so the page body never overlaps the
    chrome the run loops paint after this.
    Tailing reuses `clients.tui._tail`'s exact slicing (imported, not
    duplicated) so the fb and TUI clients agree on "what's visible" for
    the same view-model. Accent-styled lines (currently note_on events --
    see pages/eventlog.py) draw in the brighter tone.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    header_text = f"{vm['title']}  ({vm['count']} events)"
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, header_text), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    body_h = max(0, (usable_h - header_h) // line_h)
    for i, line in enumerate(_tail(vm["lines"], body_h)):
        color = ACCENT_FG if line["style"] == "accent" else NORMAL_FG
        draw_text(surface, LEFT_MARGIN, header_h + i * line_h, line["text"], color, font)


def _render_unknown(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Fallback for a page name this client build has no renderer for --
    see clients/tui.py's `_render_unknown` for the rationale (wire compat
    is additive-only, so an older client can meet a newer server's extra
    page without crashing). Just clears to background; no text drawn since
    an unrecognised vm shape may not have a "title"/"count" to show."""
    surface.clear(BG)


# -- voices page (phase-3 task 4) --------------------------------------------
#
# Layout: the same reverse-video header convention as `render_frame`'s
# eventlog header, then one row per channel -- "<ch> <name>" text plus a
# bordered vertical poly-meter (`box` for the frame, `fill_column` for the
# live fill, `hline` for the peak-hold tick) and a numeric "<active>/<peak>"
# readout. BAR_MAX=8 matches v1's zvoicemonitor.py per-channel poly-limit
# default (POLY_LIMIT_CH) -- a fixed visual scale only, not an enforced
# limit (no limit/warning behavior is ported -- see analyzers/voices.py's
# module docstring); a channel with 8+ held voices just shows a full meter,
# the numeric label stays exact. Deliberately NOT sharing bar math with the
# TUI renderer's own `_voices_bar` (see that function's comment) -- this
# isn't the page-agnostic chrome status text clients/chrome.py exists to
# keep byte-identical across clients.
ROW_PAD = 1            # vertical inset (top+bottom) inside each row's meter box, px
NAME_COL_CHARS = 15    # "01 " + up to a 12-char instrument name
NAME_MAX_CHARS = 12    # matches TUI's own name-column width (clients/tui.py's
                       # _VOICES_NAME_WIDTH) -- keeps "01 <name>" within
                       # NAME_COL_CHARS so it can never paint into `bar_x`'s
                       # gap or the meter box itself (review fix: a
                       # config-overridden name longer than this used to draw
                       # untruncated, corrupting the layout).
BAR_GAP = 8            # px between the name column and the meter, and meter and label
BAR_W = 40             # px, includes the 1px outline on each side
BAR_MAX = 8            # visual scale -- see comment above


def _voices_header_text(vm: dict) -> str:
    return f"{vm['title']}  (poly {vm['total']}/{vm['total_peak']})"


def render_voices_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render the voices page view-model (pages/voices.py, wrapping
    analyzers/voices.py's VoiceMonitorAnalyzer) onto `surface`. Pure: reads
    only `vm` and the cached default font, writes only to `surface`'s
    pixels -- no I/O, no clock, no global state beyond the font's
    (side-effect-free) glyph cache. See module docstring for the layout.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _voices_header_text(vm)), BG, font)

    rows = vm["rows"]
    if not rows:
        return
    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    row_h = usable_h // len(rows)
    if row_h <= 0:
        return

    bar_x = LEFT_MARGIN + NAME_COL_CHARS * font.width + BAR_GAP
    for i, row in enumerate(rows):
        row_y = header_h + i * row_h
        text_y = row_y + max(0, (row_h - font.height) // 2)
        name = row["name"][:NAME_MAX_CHARS]
        draw_text(surface, LEFT_MARGIN, text_y, f"{row['ch']:02d} {name}", NORMAL_FG, font)

        bar_y = row_y + ROW_PAD
        bar_h = row_h - 2 * ROW_PAD
        if bar_h <= 0:
            continue
        surface.box(bar_x, bar_y, BAR_W, bar_h, NORMAL_FG)
        inner_h = max(0, bar_h - 2)
        inner_w = max(0, BAR_W - 2)
        fill_h = round(inner_h * min(row["active"], BAR_MAX) / BAR_MAX)
        if fill_h > 0:
            fill_color = ACCENT_FG if row["active"] > 0 else NORMAL_FG
            surface.fill_column(bar_x + 1, bar_y + bar_h - 2, fill_h, fill_color, width=inner_w)
        peak_h = round(inner_h * min(row["peak"], BAR_MAX) / BAR_MAX)
        if peak_h > 0:
            peak_y = bar_y + bar_h - 1 - peak_h
            surface.hline(bar_x + 1, peak_y, inner_w, ACCENT_FG)

        label_x = bar_x + BAR_W + BAR_GAP
        draw_text(surface, label_x, text_y, f"{row['active']}/{row['peak']}", NORMAL_FG, font)


# -- harmony page (phase-3 task 5) -------------------------------------------
#
# Layout: same reverse-video header convention as `render_frame`/
# `render_voices_frame`, then one text row per v1 Notes-page harmony field
# (Chord/Scale "Last/2nd/3rd/4th" slots -- collapsed to one text line each
# here rather than the TUI's separate label+values rows, since a pixel
# surface doesn't need a second row just to spell out "Last 2nd 3rd 4th"
# -- Inside/Outside, conf+missing, Key), a PRIMITIVES-drawn tension bar
# (`box` for the outline + `rect` for the live fill, per the task brief:
# "fb: text rows + tension bar via rect/fill primitives" -- deliberately
# NOT the TUI's block-character bar), then Harm.rhy/Motif. Row TEXT is
# generated independently of `clients/tui.py`'s own harmony helpers --
# same non-sharing convention as `render_voices_frame`/`_voices_bar` vs
# `render_voices_lines`/`_voices_bar` (a per-page body widget isn't the
# page-agnostic chrome status text `clients/chrome.py` exists to keep
# byte-identical across clients). Rows are drawn top-down and simply
# stop (rather than wrap/scroll) once they'd cross into the reserved
# bottom status strip, mirroring `render_frame`'s own reservation
# convention.
HARMONY_TENSION_BAR_W = 160   # px


def _harmony_header_text(vm: dict) -> str:
    return f"{vm['title']}  (key: {vm['key'] or '?'})"


def _harmony_slots_text(prefix: str, items: list[dict]) -> str:
    names = []
    for i in range(4):
        name = items[i]["name"] if i < len(items) else None
        names.append(name or "--")
    return f"{prefix} " + "  ".join(names)


def _harmony_conf_missing_text(prefix: str, items: list[dict]) -> str:
    if items and items[0]["conf"] is not None:
        missing = " ".join(items[0]["missing"]) or "-"
        return f"{prefix} {items[0]['conf']:.2f}  missing: {missing}"
    return f"{prefix} --  missing: -"


def _harmony_key_text(vm: dict) -> str:
    text = f"Key: {vm['key'] or '?'}"
    if vm.get("key_alternatives"):
        text += f"  (alts: {', '.join(vm['key_alternatives'])})"
    return text


def _harmony_rhythm_text(vm: dict) -> str:
    hr = vm["harmonic_rhythm"]
    if hr and hr.get("changes_per_bar") is not None:
        return f"Harm.rhy: {hr['changes_per_bar']:.1f} ch/bar  {hr['label']}"
    return "Harm.rhy: --"


def _harmony_motif_text(vm: dict) -> str:
    motif = vm["motif"]
    if motif and motif.get("found"):
        return f"Motif: {motif['pattern']}  [x{motif['count']}]"
    return "Motif: --"


def render_harmony_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render the harmony page view-model (pages/harmony.py, wrapping
    analyzers/harmony.py's HarmonyAnalyzer) onto `surface`. Pure: reads
    only `vm` and the cached default font, writes only to `surface`'s
    pixels -- no I/O, no clock, no global state beyond the font's
    (side-effect-free) glyph cache. See module comment above for layout.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _harmony_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row(_harmony_slots_text("Chord:", vm["chords"]))
    _row(_harmony_slots_text("Scale:", vm["scales"]))
    _row(f"Inside: {' '.join(vm['inside']) or '-'}")
    _row(f"Outside: {' '.join(vm['outside']) or '-'}")
    _row(_harmony_conf_missing_text("Chord conf:", vm["chords"]))
    _row(_harmony_conf_missing_text("Scale conf:", vm["scales"]))
    _row(_harmony_key_text(vm))

    if y + font.height <= usable_h:
        bar_h = font.height
        prefix_w = draw_text(surface, LEFT_MARGIN, y, "Tension:", NORMAL_FG, font)
        bar_x = LEFT_MARGIN + prefix_w + BAR_GAP
        surface.box(bar_x, y, HARMONY_TENSION_BAR_W, bar_h, NORMAL_FG)
        inner_w = max(0, HARMONY_TENSION_BAR_W - 2)
        fill_w = round(inner_w * min(max(vm["tension"], 0.0), 1.0))
        if fill_w > 0:
            fill_color = ACCENT_FG if vm["tension"] > 0.5 else NORMAL_FG
            surface.rect(bar_x + 1, y + 1, fill_w, bar_h - 2, fill_color)
        label = f" {vm['tension']:.2f}  {vm.get('tension_label', '')}"
        draw_text(surface, bar_x + HARMONY_TENSION_BAR_W + BAR_GAP, y, label,
                  NORMAL_FG, font)
        y += line_h

    _row(_harmony_rhythm_text(vm))
    _row(_harmony_motif_text(vm))


# -- tuner page (phase-3 task 6) ----------------------------------------------
#
# Layout mirrors v1's `pages/tuner.py::draw()` text rows -- same adaptation
# clients/tui.py's own tuner renderer makes (see that module's comment):
# a status line, then either the locked note/cents/meter or a second blank
# row (idle -- the state this page shows in production today, see
# pages/tuner.py's/analyzers/tuner.py's module docstrings). The tuning
# meter is drawn as TEXT (`tuning_meter()`, reused from analyzers/tuner.py)
# rather than a pixel gauge with box/rect primitives -- unlike
# `render_harmony_frame`'s tension bar, the task brief does not call for a
# pixel-primitive meter here, and a monospace ASCII gauge is legible on the
# CRT font same as any other row; a disclosed, simpler choice.


def _tuner_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def render_tuner_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render the tuner page view-model (pages/tuner.py, wrapping
    analyzers/tuner.py's TunerAnalyzer) onto `surface`. Pure: reads only
    `vm` and the cached default font, writes only to `surface`'s pixels --
    no I/O, no clock, no global state beyond the font's (side-effect-free)
    glyph cache."""
    from midicrt.analyzers.tuner import tuning_meter

    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _tuner_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    if not vm.get("has_signal"):
        _row(f"Listening...  Conf:{vm['confidence']:.2f}  Level:{vm['db']:5.1f} dB")
        return

    _row(f"Note:{vm['note']:<4}  Pitch:{vm['hz']:7.2f} Hz  Cents:{vm['cents']:+6.1f}  "
         f"Conf:{vm['confidence']:.2f}  Level:{vm['db']:5.1f} dB")
    _row("Tuning: " + tuning_meter(vm["cents"], 40))


# -- pianoroll page (phase-3 task 7; paper-grid + label column: phase-8 task 3) -
#
# Layout: same reverse-video header convention as the other pages, then a
# LEFT LABEL COLUMN (`PIANOROLL_LABEL_MARGIN_CHARS` wide, one char wider
# than the label text itself -- v1's own margin convention) holding one
# `"{note_name:>7} │"` row per visible pitch, then the pixel roll body drawn
# via `Surface.rect` (task brief: "fb: pixel roll via rect/hline
# primitives" -- `rect` alone suffices here since every note is already a
# filled bar, the same choice `render_harmony_frame`'s tension fill makes
# over a separate hline loop). One rect per note: x0/x1 mapped onto the
# roll body's width (surface width MINUS the label column, not the full
# surface width as before this task), y mapped onto the usable body height
# sliced into `range.hi - range.lo + 1` equal rows (the same "usable_h //
# count" convention `render_voices_frame` already uses for its 16 channel
# rows). v1's real CRT compositor (`fb/compositor_renderer.py`, READ for
# this task -- see `docs/visual-audit.md` §9c and `pages/pianoroll.py`'s
# own module docstring "Paper grid" section) is a separate rendering
# pipeline this renderer does not need to match pixel-for-pixel; it follows
# the same "primitives only, never poke surface.image directly" convention
# every fb renderer here already established -- the dotted-guide look v1
# gets from a raw numpy buffer stride (`compositor_renderer.py`'s
# `buf[y, x0::stride] = color`) is reproduced here via `Surface.dotted_
# hline`/`dotted_vline` (new primitives, `clients/fb/surface.py`) instead.
#
# Draw order (bottom to top): grid (dotted pitch-row + bar/beat guides) ->
# label column -> notes. Grid strictly UNDER notes matches v1's own
# layering (`_render_pianoroll` draws its dotted guides, THEN its note
# rects, in that order) and is exercised directly by
# `test_render_pianoroll_frame_grid_is_drawn_under_notes_not_over_them`.
#
# Monochrome velocity-brightness (Phase 8 Task 2 -- 2026-08-08
# gui-phase-decisions doc ruling #1, "the only 'color' is shading/
# brightness level of green"): this renderer used to cycle 8 rainbow hues
# by channel (`_ROLL_CHANNEL_PALETTE`, replaced by that task) -- but
# `docs/visual-audit.md` §9c's build-priority #1 finding is that v1's REAL
# CRT compositor was never doing that: `_CH_BASE_RGB = [(0, 255, 80)] * 16`
# is already one hue for every channel, and `_velocity_scale()` is a
# straight linear 50%->100% brightness ramp (`_VEL_BRIGHTNESS_FLOOR = 0.5`)
# onto that single base color. `_roll_note_color` below ports that
# mechanism exactly via `clients/fb/lum.py`'s `lum()`/`RAMPS["pianoroll"]`
# -- `ch` is kept in the signature (existing call sites still pass it) but
# no longer affects the output color at all; channel identity now lives in
# the per-pitch label column THIS task adds (v1's own C-row brightness
# split + active-pitch invert, `compositor_renderer.py:857-876`), not as a
# note-fill hue.
_PIANOROLL_RAMP = RAMPS["pianoroll"]

# `"{note_name:>7} │"` is always exactly 9 characters (7 right-justified +
# 1 space + 1 pipe) -- the label TEXT's own printed width.
PIANOROLL_LABEL_CHARS = 9
# v1's own `LEFT_CHARS` margin (`compositor_renderer.py::_render_pianoroll`'s
# `getattr(widget, "left_margin", 10)`) is ONE CHARACTER WIDER than the
# label text itself -- a blank buffer column between the pipe and the roll
# body (review fix: an earlier draft of this port used
# `PIANOROLL_LABEL_CHARS` for BOTH the printed text width AND the roll's
# x-origin/invert-rect width, a disclosed deviation from v1's exact margin
# that this fixes). v1's own invert rect also spans this WIDER margin, not
# just the text's own width (`comp.rect(0, px_y, LEFT_CHARS * cw, cell_h,
# GREEN_MID)` -- confirmed by direct read, `compositor_renderer.py:868`),
# so both `roll_x0` (below) and `_draw_pianoroll_labels`'s invert-fill width
# use THIS constant, not `PIANOROLL_LABEL_CHARS`.
PIANOROLL_LABEL_MARGIN_CHARS = 10
# v1's exact dotted-guide strides (`compositor_renderer.py`'s
# `row_guide_step`/`bar_guide_step`) -- reused for BOTH beat and bar
# vertical guides here (a disclosed simplification: v1 only dots bar
# boundaries in the roll body itself, reserving beat ticks for a separate
# solid timeline strip this task does not port -- see pages/pianoroll.py's
# module docstring "Not ported" section. This task's own brief asks for
# BOTH beat and bar dotted guides in the roll body, so v1's bar-guide
# geometry is reused for both, distinguished only by brightness tier).
PIANOROLL_GRID_ROW_STRIDE = 4
PIANOROLL_GRID_LINE_STRIDE = 3


def _roll_note_color(ch: int, vel: float) -> tuple[int, int, int]:
    floor = _PIANOROLL_RAMP["velocity_floor"]
    ceiling = _PIANOROLL_RAMP["velocity_ceiling"]
    vel = max(0.0, min(1.0, vel))
    level = floor + (ceiling - floor) * vel
    return lum(level)


def _pianoroll_header_text(vm: dict) -> str:
    w = vm["window"]
    rng = vm["range"]
    return f"{vm['title']}  ({w['mode']} zoom {w['zoom']:.2f}, range {rng['lo']}-{rng['hi']})"


def _pianoroll_label_text(guide: dict) -> str:
    return f"{guide['name']:>7} │"


def _draw_pianoroll_grid(
    surface: Surface, grid: dict, roll_x0: int, roll_w: int,
    header_h: int, usable_h: int, row_span_h: int,
) -> None:
    """The "paper" backdrop -- dotted pitch-row separators (brighter on C
    rows) plus dotted beat/bar vertical guides -- drawn UNDER the note rects
    `render_pianoroll_frame` paints afterward. Pure primitive-drawing, no
    music math: every position here is already a projected fraction from
    `pages/pianoroll.py`'s `grid` VM (see that module's own docstring).
    """
    if roll_w <= 0 or usable_h <= 0:
        return
    guides = grid["pitch_guide_ys"]
    # Separator sits at each row's TOP edge (same y a note at that pitch
    # would use) -- v1 skips the very first row (no line above the highest
    # visible pitch), see pages/pianoroll.py's grid docstring cross-ref.
    for k in range(1, len(guides)):
        guide = guides[k]
        y = header_h + round(guide["y"] * row_span_h)
        color = LUM_FAINT_C if guide["is_c"] else LUM_FAINT
        surface.dotted_hline(roll_x0, y, roll_w, color, stride=PIANOROLL_GRID_ROW_STRIDE, phase=k & 1)

    roll_top = header_h
    for x_frac in grid["beat_xs"]:
        x = roll_x0 + round(x_frac * roll_w)
        surface.dotted_vline(x, roll_top, usable_h, LUM_FAINT, stride=PIANOROLL_GRID_LINE_STRIDE)
    for x_frac in grid["bar_xs"]:
        x = roll_x0 + round(x_frac * roll_w)
        surface.dotted_vline(x, roll_top, usable_h, LUM_BAR_GUIDE, stride=PIANOROLL_GRID_LINE_STRIDE)


def _draw_pianoroll_labels(
    surface: Surface, guides: list[dict], font, header_h: int, row_span_h: int,
    note_h: int, active_ys: set[float],
) -> None:
    """The per-pitch note-name label column, v1's exact 2-level brightness
    split (C rows bright, others dim) plus the active-pitch invert (a
    `LUM_MID`-filled cell behind `BG`-colored text) while ANY note data for
    that pitch is currently visible in the roll -- `active_ys` is `vm["notes"]`'s
    own already-projected `y` fractions, matched by set membership (exact,
    not approximate -- both are the SAME `_y(pitch)` expression on the
    engine side, see pages/pianoroll.py's grid docstring). The invert fill
    spans the full v1 margin (`PIANOROLL_LABEL_MARGIN_CHARS`), not just the
    9-char label text -- matches v1's own `LEFT_CHARS * cw` rect width.
    """
    label_w = PIANOROLL_LABEL_MARGIN_CHARS * font.width
    for guide in guides:
        y = header_h + round(guide["y"] * row_span_h)
        label = _pianoroll_label_text(guide)
        if guide["y"] in active_ys:
            surface.rect(0, y, label_w, note_h, LUM_MID)
            draw_text(surface, 0, y, label, BG, font)
        else:
            color = LUM_BRIGHT if guide["is_c"] else LUM_DIM
            draw_text(surface, 0, y, label, color, font)


def render_pianoroll_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render the pianoroll page view-model (pages/pianoroll.py) onto
    `surface`. Pure: reads only `vm` and the cached default font, writes
    only to `surface`'s pixels -- no I/O, no clock, no global state beyond
    the font's (side-effect-free) glyph cache. See module comment above for
    the label-column / paper-grid / note-rect layering.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _pianoroll_header_text(vm)), BG, font)

    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    if usable_h <= 0:
        return
    rng = vm["range"]
    pitch_span = max(1, rng["hi"] - rng["lo"] + 1)
    note_h = max(1, usable_h // pitch_span)
    row_span_h = max(0, usable_h - note_h)
    label_w = PIANOROLL_LABEL_MARGIN_CHARS * font.width
    roll_x0 = label_w
    roll_w = max(0, surface.width - label_w)

    grid = vm["grid"]
    _draw_pianoroll_grid(surface, grid, roll_x0, roll_w, header_h, usable_h, row_span_h)

    active_ys = {n["y"] for n in vm["notes"]}
    _draw_pianoroll_labels(surface, grid["pitch_guide_ys"], font, header_h, row_span_h, note_h, active_ys)

    for note in vm["notes"]:
        y = header_h + round(note["y"] * row_span_h)
        x0 = roll_x0 + round(note["x0"] * roll_w)
        x1 = roll_x0 + round(note["x1"] * roll_w)
        w = max(1, x1 - x0)
        surface.rect(x0, y, w, note_h, _roll_note_color(note["ch"], note["vel"]))


# -- spectrum page (phase-3 task 8) -------------------------------------------
#
# Layout: same reverse-video header convention as the other pages, then N
# vertical bars spanning the surface width, one per `vm["bins"]` entry --
# `Surface.fill_column` (task brief: "fb via fill_column bars" -- its own
# docstring literally calls out "the shape a spectrum-analyzer bar needs")
# for the live fill, `Surface.hline` for the peak-hold tick (v2 addition,
# v1 has no peak-hold at all -- see analyzers/spectrum.py's module
# docstring), mirroring `render_voices_frame`'s own live-fill-plus-peak-tick
# convention (that page's per-channel meter, transposed here into one bar
# per frequency bin instead of one bar per channel). No box outline (unlike
# voices' bordered meter) -- the task brief only asks for bars + ticks.
# `available: false` -> v1's "no audio input" placeholder (task brief's
# explicit degrade-gracefully contract) drawn as plain text, no bars at
# all.
SPECTRUM_BAR_GAP = 1     # px gap between adjacent bar columns
SPECTRUM_MIN_BAR_W = 1


def _spectrum_header_text(vm: dict) -> str:
    if not vm.get("available"):
        return f"{vm['title']}"
    device = vm.get("device") or "default"
    return f"{vm['title']}  (device: {device})"


def render_spectrum_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    """Render the spectrum page view-model (pages/spectrum.py, wrapping
    analyzers/spectrum.py's SpectrumAnalyzer) onto `surface`. Pure: reads
    only `vm` and the cached default font, writes only to `surface`'s
    pixels -- no I/O, no clock, no global state beyond the font's
    (side-effect-free) glyph cache. See module comment above for the
    fill_column-bars + peak-hold-tick layout.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _spectrum_header_text(vm)), BG, font)

    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    if usable_h <= 0:
        return

    if not vm.get("available"):
        draw_text(surface, LEFT_MARGIN, header_h + LINE_GAP, "no audio input", NORMAL_FG, font)
        return

    levels = vm["bins"]
    n = len(levels)
    if n == 0:
        return
    peaks = vm.get("peak_hold") or [0.0] * n
    plot_w = max(0, surface.width - 2 * LEFT_MARGIN)
    col_w = max(SPECTRUM_MIN_BAR_W, plot_w // n)
    bar_w = max(SPECTRUM_MIN_BAR_W, col_w - SPECTRUM_BAR_GAP)
    baseline = header_h + usable_h - 1
    for i, val in enumerate(levels):
        x = LEFT_MARGIN + i * col_w
        fill_h = round(usable_h * min(max(val, 0.0), 1.0))
        if fill_h > 0:
            surface.fill_column(x, baseline, fill_h, NORMAL_FG, width=bar_w)
        peak_val = peaks[i] if i < len(peaks) else 0.0
        peak_h = round(usable_h * min(max(peak_val, 0.0), 1.0))
        if peak_h > 0:
            peak_y = header_h + usable_h - peak_h
            surface.hline(x, peak_y, bar_w, ACCENT_FG)


# -- screensaver page (phase-3 task 9) ---------------------------------------
#
# Clears to background and draws NOTHING else -- no header, no text -- the
# closest v2 can get to v1's literal `_blank_fb()` (zeroing the entire real
# framebuffer). Task-9 review (Important fix): the run loops now SKIP all
# three chrome strips whenever this page is current (see `_paint_frame`
# below) instead of always painting them after the page renderer -- v1's
# blank is a TRUE full-screen blank (raw fb zeroing bypasses the
# compositor and every plugin, chrome included), and leaving three
# brightly-lit reverse-video bars burning at the bottom of the CRT while
# "screensaving" would defeat the whole burn-in-avoidance purpose. Because
# this renderer already clears the WHOLE surface (not just the body) via
# `surface.clear(BG)`, simply not calling the chrome `_draw_*` functions is
# sufficient -- there is nothing left underneath for them to paint over.
# `SCREENSAVER_PAGE` is imported from `behaviors.screensaver` (2nd review
# pass, Minor fix) rather than hardcoded here a second time -- the same
# anti-drift precedent `behaviors/pagecycle.py`'s own `screensaver_page`
# parameter already set for the engine-side behaviors.


def render_screensaver_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    surface.clear(BG)


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# The engine already emits a FIXED `GRID_ROWS` x `GRID_COLS` grid of final
# [0,1] brightness values (analyzers/img2txtviz.py's own module docstring)
# -- this renderer's only job is filling one `rect` per grid cell, scaled up
# to fill the usable body area, colored via `clients/fb/lum.py`'s `lum()`
# (Phase 8 Task 2: was an inline `ACCENT_FG`-scale, now routed through the
# shared framework -- `RAMPS["img2txtviz"]` declares the "0.0 -> black,
# 1.0 -> full LUM_BRIGHT" contract as data instead of leaving it implicit;
# `lum(1.0) == ACCENT_FG` exactly, so this is byte-identical at both
# endpoints, matching the monochrome-CRT-green palette every other renderer
# here already uses (no new colors introduced). `invert` is already applied
# engine-side (the grid's own values are already final), so this renderer
# never reads that flag itself.
_IMG2TXTVIZ_RAMP = RAMPS["img2txtviz"]


def _img2txtviz_header_text(vm: dict) -> str:
    flag = "  INV" if vm.get("invert") else ""
    return f"{vm['title']}  notes:{vm['active_notes']:02d}{flag}"


def _img2txtviz_cell_color(value: float) -> tuple[int, int, int]:
    lo, hi = _IMG2TXTVIZ_RAMP["cell_min"], _IMG2TXTVIZ_RAMP["cell_max"]
    v = max(lo, min(hi, value))
    return lum(v)


def render_img2txtviz_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _img2txtviz_header_text(vm)), BG, font)

    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    if usable_h <= 0:
        return
    grid = vm["grid"]
    gh = len(grid)
    gw = len(grid[0]) if gh else 0
    if gh == 0 or gw == 0:
        return
    cell_w = max(1, surface.width // gw)
    cell_h = max(1, usable_h // gh)
    for ry in range(gh):
        y = header_h + ry * cell_h
        row = grid[ry]
        for rx in range(gw):
            surface.rect(rx * cell_w, y, cell_w, cell_h, _img2txtviz_cell_color(row[rx]))


# -- config page (phase-3 task 10) -------------------------------------------
#
# A plain "label: value" text dump -- see pages/configview.py's module
# docstring for why this is a fixed flat list rather than v1's recursive
# JSON tree/editor. Same row-stepping `_row` closure convention as
# `render_harmony_frame`.
def _config_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def render_config_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _config_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row("-- Config --")
    for r in vm["config_rows"]:
        _row(f"{r['label']}: {r['value']}")
    _row("")
    _row("-- Engine --")
    for r in vm["engine_rows"]:
        _row(f"{r['label']}: {r['value']}")


# -- help page (phase-3 task 12, gap ports; keymap section: Phase 5 Task 3) ---
#
# Same "-- Section --" + "label: value" row-dump convention as
# `render_config_frame` (pages/help.py's view_model is deliberately the same
# `{page_rows, action_rows, keymap_rows}`-shaped list-of-dicts). See
# pages/help.py's own module docstring for why this describe-data reference
# IS the v1 Help page's parity port, not a literal keybinding-list
# transcription.
def _help_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def render_help_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _help_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row("-- Pages --")
    for r in vm["page_rows"]:
        _row(f"{r['label']}: {r['value']}")
    _row("")
    _row("-- Actions --")
    for r in vm["action_rows"]:
        _row(f"{r['label']}: {r['value']}")
    # Phase 5 Task 3 (docs/phase5-notes.md cheap-wins bundle: "help page
    # renders live keymap"): a THIRD section, appended AFTER Actions --
    # `.get(...)`, not `vm["keymap_rows"]`, matching `clients/tui.py::
    # _help_body_lines`'s own defensive-default reasoning (an older/
    # test-double vm dict missing this key entirely renders byte-identical
    # to before -- this is exactly why the pre-existing golden fixture
    # (`fb_help_frame_golden.png`, built from a `keymap_rows`-less HELP_VM)
    # needed no re-freeze for this task). Skipped ENTIRELY (no "--
    # Keymap --" header drawn either) when the list is empty, not just
    # when the key is absent -- an empty section header with nothing
    # under it would be confusing chrome, not useful information.
    keymap_rows = vm.get("keymap_rows", [])
    if keymap_rows:
        _row("")
        _row("-- Keymap --")
        for r in keymap_rows:
            _row(f"{r['label']}: {r['value']}")


# -- program changes page (phase-3 task 12, gap ports) ------------------------
#
# Byte-for-byte the same layout as `render_frame` (eventlog's own renderer):
# reverse-video header + tailed lines, one text row per line, since
# pages/progchanges.py deliberately reuses eventlog's own `{title, count,
# lines}` VM shape (see that module's docstring). No accent styling -- v1's
# proglog.py has no per-line color distinction, every line uses
# `"style": "normal"`.
def _progchanges_header_text(vm: dict) -> str:
    return f"{vm['title']}  ({vm['count']} events)"


def render_progchanges_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _progchanges_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    body_h = max(0, (usable_h - header_h) // line_h)
    for i, line in enumerate(_tail(vm["lines"], body_h)):
        color = ACCENT_FG if line["style"] == "accent" else NORMAL_FG
        draw_text(surface, LEFT_MARGIN, header_h + i * line_h, line["text"], color, font)


# -- CC monitor page (phase-3 task 12, gap ports) ------------------------------
#
# Layout: same reverse-video header convention as `render_voices_frame`, then
# one row per MIDI channel showing its recent-CC window as plain text (e.g.
# "01  CC74:100  CC01:064") -- unlike voices' bordered poly-meter, a CC
# monitor has no natural bar-scale (a controller number isn't a magnitude),
# so this stays pure text per row, matching v1's own `pages/ccmonitor.py`
# text-table layout (see analyzers/ccmonitor.py's module docstring).
def _ccmonitor_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def _ccmonitor_row_text(ch_vm: dict) -> str:
    cells = " ".join(f"CC{e['cc']:02d}:{e['value']:03d}" for e in ch_vm["recent"])
    return f"{ch_vm['ch']:02d}  {cells}"


def render_ccmonitor_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _ccmonitor_header_text(vm)), BG, font)

    rows = vm["channels"]
    if not rows:
        return
    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    row_h = usable_h // len(rows)
    if row_h <= 0:
        return
    for i, row in enumerate(rows):
        row_y = header_h + i * row_h
        text_y = row_y + max(0, (row_h - font.height) // 2)
        draw_text(surface, LEFT_MARGIN, text_y, _ccmonitor_row_text(row), NORMAL_FG, font)


# -- CC dashboard page (phase-3 task 12, gap ports) -----------------------------
#
# Layout: reverse-video header, then one row per tracked (channel, cc) pair --
# a "Ch01 CC074:100" label, a `fill_column`-style horizontal bar (task
# brief's "CC Monitor/Dashboard -> ... two page layouts" naturally maps
# ccgraph.py's own proportional bar onto `Surface.rect`, mirroring
# `render_spectrum_frame`'s bar-fill convention transposed horizontally),
# and a "LIVE"/"N.Ns ago" freshness label in the brighter accent tone while
# fresh -- matching v1's `ccgraph.py` "perfectly aligned" bars-plus-age
# layout (see analyzers/ccmonitor.py's module docstring).
CCDASH_BAR_W = 120   # px
CCDASH_BAR_GAP = 8


def _ccdashboard_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def _ccdashboard_age_text(entry: dict) -> str:
    return "LIVE" if entry["fresh"] else f"{entry['age_s']:.1f}s"


def render_ccdashboard_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _ccdashboard_header_text(vm)), BG, font)

    entries = vm["entries"]
    if not entries:
        return
    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    row_h = usable_h // len(entries)
    if row_h <= 0:
        return

    label_w = 15 * font.width   # "Ch01 CC074:100" fits comfortably
    bar_x = LEFT_MARGIN + label_w
    for i, entry in enumerate(entries):
        row_y = header_h + i * row_h
        text_y = row_y + max(0, (row_h - font.height) // 2)
        label = f"Ch{entry['ch']:02d} CC{entry['cc']:03d}:{entry['value']:03d}"
        draw_text(surface, LEFT_MARGIN, text_y, label, NORMAL_FG, font)

        bar_h = max(1, row_h - 2)
        bar_y = row_y + (row_h - bar_h) // 2
        fill_w = round(CCDASH_BAR_W * min(max(entry["value"], 0), 127) / 127)
        if fill_w > 0:
            fill_color = ACCENT_FG if entry["fresh"] else NORMAL_FG
            surface.rect(bar_x, bar_y, fill_w, bar_h, fill_color)

        age_x = bar_x + CCDASH_BAR_W + CCDASH_BAR_GAP
        age_color = ACCENT_FG if entry["fresh"] else NORMAL_FG
        draw_text(surface, age_x, text_y, _ccdashboard_age_text(entry), age_color, font)


# -- chord+key page (phase-3 task 12, gap ports) -------------------------------
#
# Layout: reverse-video header, then text rows -- "Recent PCs:", up to 3
# ranked chord-candidate lines, a "Stabilized key:" section (either
# "Key= <label> NN% (thr NN%)"/"Key~ ..." when ambiguous, or a "Key: ?
# top:<label> NN%" fallback when nothing has stabilized yet, plus an
# "alts:" line), and "Function:" -- mirroring v1's own `chordkey.py`
# text-row layout (see pages/chordkey.py's module docstring for the full
# v1-field mapping). Same `_row` closure convention as
# `render_harmony_frame`/`render_config_frame`.
def _chordkey_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def _chordkey_key_lines(key: dict) -> tuple[str, str]:
    if not key["label"]:
        top = key["top"]
        line1 = f"Key: ?  top:{top['label']} {top['pct']}%" if top else "Key: ?"
    else:
        tag = "~" if key["ambiguous"] else "="
        line1 = f"Key{tag} {key['label']}  {key['pct']}% (thr {key['threshold_pct']}%)"
    if key["alternatives"]:
        alt_txt = " | ".join(f"{a['label']} {a['pct']}%" for a in key["alternatives"][:2])
        line2 = f"alts: {alt_txt}"
    elif key["ambiguous"]:
        line2 = "alts: near-threshold / ambiguous"
    else:
        line2 = "alts: -"
    return line1, line2


def render_chordkey_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _chordkey_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row(f"Recent PCs: {' '.join(vm['recent_pcs']) or '(none)'}")
    _row("Chord candidates:")
    if not vm["chords"]:
        _row("(no chord match yet)")
    else:
        for i, c in enumerate(vm["chords"]):
            miss = " ".join(c["missing"]) or "-"
            _row(f"{i + 1}) {c['label']}  {c['pct']:3d}%  missing:{miss}")

    _row("")
    _row("Stabilized key:")
    k1, k2 = _chordkey_key_lines(vm["key"])
    _row(k1)
    _row(k2)
    _row(f"Function: {vm['function'] or '?'}")


# -- send notes page (phase-3 task 12, gap ports) -------------------------------
#
# Layout: reverse-video header, then a status line (device/channel/octave/
# velocity/gate/active, matching v1's own `pages/sendnotes.py::_build_
# widget_lines`'s status line) plus a keymap hint row -- see that module's
# module docstring for the full v1 field mapping. No interactive input is
# wired to this renderer (Phase 4's key->action table, same "reachable via
# `midicrt action` only" precedent as pianoroll/img2txtviz's own controls).
def _sendnotes_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def _sendnotes_status_text(vm: dict) -> str:
    dev = vm["device"] or "(not open)"
    return (f"Dev: {dev}  Ch:{vm['channel']:02d}  Oct:{vm['octave']:+d}  "
            f"Vel:{vm['velocity']:03d}  Gate:{vm['gate_ms']}ms  Active:{vm['active']}")


def render_sendnotes_frame(vm: dict, surface: Surface, marquee_text: str | None = None) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _header_text(marquee_text, _sendnotes_header_text(vm)), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row(_sendnotes_status_text(vm))
    _row("Keys: z s x d c v g b h n j m (, l . ; /) -- white/black keys")
    _row("[,] ch-+/  [-]/[+] oct  [-]/[=] vel  g/h gate")


RENDERERS = {"eventlog": render_frame, "voices": render_voices_frame,
             "harmony": render_harmony_frame, "tuner": render_tuner_frame,
             "pianoroll": render_pianoroll_frame, "spectrum": render_spectrum_frame,
             "screensaver": render_screensaver_frame,
             "img2txtviz": render_img2txtviz_frame, "config": render_config_frame,
             "help": render_help_frame, "progchanges": render_progchanges_frame,
             "ccmonitor": render_ccmonitor_frame, "ccdashboard": render_ccdashboard_frame,
             "chordkey": render_chordkey_frame, "sendnotes": render_sendnotes_frame}


# -- chrome: status strip (phase-3 task 3) -----------------------------------


def _status_strip_height(font) -> int:
    """Pixel height of the bottom status strip -- mirrors the header's own
    `font.height + 2*HEADER_PAD` sizing convention (see module docstring)."""
    return font.height + 2 * STATUS_PAD


def _draw_status(surface: Surface, vm: dict, font) -> None:
    """Paint the status strip onto `surface`: a reverse-video bar (same
    `HEADER_BG` fill / `BG` text convention as the page header) showing the
    shared chrome status text (`clients/chrome.py` -- word-for-word
    identical to the TUI's status row, per the task-3 brief's "mirrors
    it"). Sits directly ABOVE the beatprogress strip (phase-3 task 9 made
    THAT the new true bottom-most strip, matching v1's own physical
    layout -- see module docstring), not pinned to `surface.height` itself
    any more. Pure aside from the font glyph cache, same contract as
    `render_frame`.
    """
    strip_h = _status_strip_height(font)
    y = surface.height - _beatprogress_strip_height(font) - strip_h
    surface.rect(0, y, surface.width, strip_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, y + STATUS_PAD, chrome.status_text(vm), BG, font)


# -- chrome: secondary (alerts/timesig) strip (phase-3 task 6) ---------------


def _secondary_strip_height(font) -> int:
    """Pixel height of the second reserved strip -- same sizing convention
    as `_status_strip_height`."""
    return font.height + 2 * STATUS_PAD


def _reserved_chrome_height(font) -> int:
    """Total px reserved at the bottom of the surface for ALL THREE chrome
    strips -- every page renderer's usable-height math subtracts this
    (see module docstring)."""
    return (_secondary_strip_height(font) + _status_strip_height(font)
            + _beatprogress_strip_height(font))


def _draw_secondary(surface: Surface, alerts_vm: dict, timesig_vm: dict, font) -> None:
    """Paint the second reserved strip, directly ABOVE the status strip --
    same reverse-video convention, showing `clients/chrome.py`'s
    `secondary_status_text()`."""
    strip_h = _secondary_strip_height(font)
    y = surface.height - _reserved_chrome_height(font)
    surface.rect(0, y, surface.width, strip_h, HEADER_BG)
    text = chrome.secondary_status_text(alerts_vm, timesig_vm)
    draw_text(surface, LEFT_MARGIN, y + STATUS_PAD, text, BG, font)


# -- chrome: beatflash/loopprogress strip (phase-3 task 9) -------------------


def _beatprogress_strip_height(font) -> int:
    """Pixel height of the third reserved strip -- same sizing convention
    as `_status_strip_height`/`_secondary_strip_height`."""
    return font.height + 2 * STATUS_PAD


def _draw_beatprogress(surface: Surface, beatflash_vm: dict, loopprogress_vm: dict, font) -> None:
    """Paint the third reserved strip -- the new TRUE BOTTOM-MOST strip
    (see module docstring for why this, not the status strip, now owns
    `surface.height`'s own edge) -- same reverse-video convention, showing
    `clients/chrome.py`'s `beatprogress_row_text()`. `num_chars` is the
    printable width in CHARACTER units (symmetric about `LEFT_MARGIN` on
    both sides) -- the FB analog of the TUI client's `term.width`, needed
    because that shared function centers v1's loop-progress bar within it.
    """
    strip_h = _beatprogress_strip_height(font)
    y = surface.height - strip_h
    surface.rect(0, y, surface.width, strip_h, HEADER_BG)
    num_chars = max(0, (surface.width - 2 * LEFT_MARGIN) // font.width)
    text = chrome.beatprogress_row_text(beatflash_vm, loopprogress_vm, num_chars)
    draw_text(surface, LEFT_MARGIN, y + STATUS_PAD, text, BG, font)


def _header_char_capacity(surface: Surface, font) -> int:
    """How many characters the header row can print, starting at
    `LEFT_MARGIN` -- the fb analog of v1's `SCREEN_COLS` (the marquee's
    OWN "does this need to scroll" engage condition, `analyzers/
    marquee.py`'s module docstring). Mirrors `_draw_beatprogress`'s own
    `num_chars` computation for the same reason: a shared function's width
    parameter can't assume a screen size, so each fb strip derives its own
    character budget from the real surface/font."""
    return max(0, (surface.width - LEFT_MARGIN) // font.width)


def _paint_frame(surface: Surface, page: str, vm: dict, font, status_vm: dict,
                  alerts_vm: dict, timesig_vm: dict, beatflash_vm: dict,
                  loopprogress_vm: dict, marquee_vm: dict) -> None:
    """Render `page`'s body, then all THREE chrome strips -- UNLESS `page`
    is the screensaver page (Important fix, task-9 review), in which case
    NO chrome is painted at all, matching v1's true full-screen blank --
    see the module comment above `render_screensaver_frame`/
    `SCREENSAVER_PAGE`. Single call site for this "page owns everything,
    chrome paints after, except when screensaving" rule so the three real
    run-loop call sites (`_run_device`'s initial paint + its redraw loop,
    and `run()`'s `--out` one-shot path) can never drift from each other.

    Phase 8 Task 4: `marquee_vm` (`overlay.marquee`) is sliced into
    `marquee_text` HERE -- the one place that knows both the real surface
    width (`_header_char_capacity`) and the live scroll-offset snapshot --
    then threaded into `renderer(...)`'s new optional third argument, so
    EVERY page's header shows the live scrolling marquee (v1's primary
    anti-burn-in device) instead of a permanently-static per-page title.
    The screensaver page's own renderer ignores `marquee_text` entirely
    (true full-screen blank, chrome included -- see its own module
    comment), so it is still passed through uniformly rather than
    special-cased here.
    """
    renderer = RENDERERS.get(page, _render_unknown)
    marquee_text = chrome.marquee_window_text(marquee_vm, _header_char_capacity(surface, font))
    renderer(vm, surface, marquee_text)
    if page == SCREENSAVER_PAGE:
        return
    _draw_secondary(surface, alerts_vm, timesig_vm, font)
    _draw_status(surface, status_vm, font)
    _draw_beatprogress(surface, beatflash_vm, loopprogress_vm, font)


# -- real-device geometry (coded here, exercised only in Task 4) -----------

def _read_fb_geometry() -> tuple[int, int, int]:
    """Read (width, height, stride) for the real /dev/fb0 device from sysfs
    at runtime, rather than hardcoding -- v1 confirmed 800x475/stride 1600
    on this hardware (task-2 report), but this must survive a kernel/driver
    change. `stride` falls back to width*2 (tightly packed rows, matching
    `Surface.write_fb`'s own default) when the sysfs node is absent.
    """
    w_str, h_str = (_SYS_FB / "virtual_size").read_text().strip().split(",")
    width, height = int(w_str), int(h_str)
    stride_path = _SYS_FB / "stride"
    stride = int(stride_path.read_text().strip()) if stride_path.exists() else width * 2
    return width, height, stride


# -- evdev input (background thread; never fatal) --------------------------
#
# Key dispatch (Phase 4 Task 1, docs/phase4-notes.md): `_input_loop` used to
# hardcode three `if event.code == evdev.ecodes.KEY_*` branches (Q=quit
# locally, C=`eventlog.clear`, N=`page.next`). All three are now driven by
# the engine's OWN keymap (`engine/keymap.py`), fetched once via `describe`
# at connect (`fetch_keymap`, `clients/base.py`) and re-fetched whenever a
# `keymap_changed` event arrives (`_make_page_switcher`'s `on_event` below
# now handles that alongside its existing `page_changed` case). This module
# still deliberately keeps its `import evdev` LOCAL (never top-level) --
# same "don't require evdev to succeed at import time" convention `_run_
# device`/`_find_input_device` already established, now shared by
# `_build_evdev_char_table` too (it takes the already-imported module as a
# plain parameter rather than importing it a second time).


def _find_input_device():
    """Return the first evdev device exposing KEY_Q, or None if none do."""
    import evdev

    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if evdev.ecodes.KEY_Q in dev.capabilities().get(evdev.ecodes.EV_KEY, []):
            return dev
    return None


def _build_evdev_char_table(evdev) -> dict[int, str]:
    """Evdev keycode -> single ASCII char lookup table for `_input_loop`'s
    generic `dispatch_key` call below -- covers the ASCII letters a-z and
    digits 0-9 (documented here as the full range: any `keymap.toml` entry
    naming a key outside that set has no physical-keyboard scancode this
    table can ever produce, and simply never matches -- same "unmapped ->
    silently ignored" contract `dispatch_key` itself already has for a
    char with no keymap entry). Pure and directly testable with the REAL
    `evdev` module (a hard runtime dependency, see pyproject.toml) -- no
    real input device needed, unlike `_find_input_device`/`_input_loop`
    themselves."""
    table = {}
    for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
        code = getattr(evdev.ecodes, f"KEY_{ch.upper()}", None)
        if code is not None:
            table[code] = ch
    return table


# How often `_dispatch_evdev_key` is allowed to log a rejected-action
# warning, PER `rate_state` dict (one per `_input_loop` call, i.e. one per
# process lifetime -- see that function's own docstring) -- a stuck/
# repeating key hammering the same rejected action many times a second
# must not flood the journal (Important finding, bindings review).
_KEY_ERROR_LOG_INTERVAL_S = 1.0


def _dispatch_evdev_key(client: EngineClient, key: str, keymap: dict[str, str],
                        rate_state: dict) -> bool:
    """The per-keypress body of `_input_loop`'s read loop, extracted so the
    fix below is directly unit-testable without a real evdev device
    (bindings review, live-reproduced Important finding: the OLD `except
    ClientError: pass` here was a SILENT, PERMANENT no-op with zero
    diagnostic -- unlike the TUI's own exit bug, nothing ever surfaced
    this failure at all, forever).

    Resolves `key` via the SAME `dispatch_key` (`clients/base.py`) the TUI
    client uses. A `ClientError` (a rejected action -- bad/missing args,
    unknown action -- indistinguishable BY TYPE from a lost connection,
    same ambiguity `clients/tui.py::_handle_key_press` documents at
    length) is ALWAYS treated as "an action failed, not fatal": logged as
    a warning (rate-capped via `rate_state["last_warn_ts"]`, a plain
    mutable dict `_input_loop` keeps ONE of across its whole
    `read_loop()`, not a fresh one per call) and this function returns
    `False` -- never signals quit for an action failure. A genuine
    disconnect is still caught, just not here: it surfaces via the render
    loop's own EOF check on the main thread, unaffected by this always
    absorbing the exception. Returns `True` only for the `client.quit`
    pseudo-action (`dispatch_key`'s own return value, never raises for
    that case)."""
    try:
        return dispatch_key(client, key, keymap)
    except ClientError as exc:
        now = time.time()
        if now - rate_state.get("last_warn_ts", 0.0) >= _KEY_ERROR_LOG_INTERVAL_S:
            _LOG.warning("action dispatch failed for key %r: %s", key, exc)
            rate_state["last_warn_ts"] = now
        return False


def _input_loop(client: EngineClient, quit_event: threading.Event,
                 get_keymap: Callable[[], dict[str, str]]) -> None:
    """Background-thread evdev reader (brief: "Input runs in a thread").
    Every keydown is translated through `_build_evdev_char_table` into a
    single char, then resolved via `_dispatch_evdev_key` -- `get_keymap()`
    is a zero-arg callable (not a frozen dict) so a `keymap_changed`
    refetch landing on the MAIN thread mid-read-loop (via
    `_make_page_switcher`'s `on_event`, which reassigns `state["keymap"]`
    rather than mutating it in place) is picked up on this thread's very
    next keypress -- same "callable, not a frozen snapshot" pattern
    `drain_latest`/`wait_first_snapshot` already use for `topics`/`topic`
    elsewhere in this module. `_dispatch_evdev_key` returning `True` (the
    `client.quit` pseudo-action) sets `quit_event` for a clean exit,
    exactly like the old hardcoded `KEY_Q` branch did. Any discovery/
    permission/IO failure logs one line and returns -- input is a
    nice-to-have, never fatal to the render loop.
    """
    try:
        import evdev

        dev = _find_input_device()
    except Exception as exc:  # noqa: BLE001 -- any evdev/discovery failure is non-fatal
        _LOG.info("input unavailable (%s); continuing without input", exc)
        return
    if dev is None:
        _LOG.info("no input device with KEY_Q capability found; continuing without input")
        return
    char_table = _build_evdev_char_table(evdev)
    rate_state: dict = {}   # one shared "last warned" timestamp for this whole read_loop
    try:
        for event in dev.read_loop():
            if event.type != evdev.ecodes.EV_KEY or event.value != 1:  # key-down only
                continue
            key = char_table.get(event.code)
            if key is None:
                continue
            if _dispatch_evdev_key(client, key, get_keymap(), rate_state):
                quit_event.set()
                return
    except OSError as exc:
        _LOG.info("input device error (%s); continuing without input", exc)


# -- run loops ---------------------------------------------------------------
#
# Page dispatch mirrors clients/tui.py: `RENDERERS` maps page name -> its
# `(vm, Surface) -> None` renderer, connect asks `describe` for the CURRENT
# page instead of assuming "eventlog" (`current_page_topic`), and a
# `page_changed` event triggers `switch_topic` (unsubscribe old, subscribe
# new) via `drain_latest`'s `on_event` callback. `_drain_latest`/
# `_wait_first_snapshot` used to be private copies of this exact logic --
# now shared with the TUI client via `clients/base.py`.


def _make_page_switcher(client: EngineClient, state: dict, max_rate: float):
    """Return a `drain_latest(on_event=...)` callback that reacts to
    `page_changed` by resubscribing and updating `state["page"]`/`
    state["topic"]` in place, and (Phase 4 Task 1, docs/phase4-notes.md)
    to `keymap_changed` by REASSIGNING `state["keymap"]` to a freshly
    fetched table -- reassignment, not in-place mutation, is deliberate:
    `_input_loop`'s background thread reads `state["keymap"]` through a
    `get_keymap` callable (`lambda: state["keymap"]`) on every keypress
    with no lock, and CPython's GIL makes a single dict-KEY read/write
    atomic -- swapping in a whole new dict object is therefore safe to
    observe from that other thread without any synchronization of its
    own, whereas mutating the SAME dict's contents key-by-key would risk
    the input thread observing a half-updated table mid-swap."""

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, max_rate)
            state["page"], state["topic"] = new_page, new_topic
        elif msg.get("kind") == "event" and msg.get("name") == "keymap_changed":
            state["keymap"] = fetch_keymap(client)

    return on_event


def _run_device(client: EngineClient, inbox: queue.Queue, fb_path: str,
                 no_input: bool, fps: float, page: str, topic: str,
                 keymap: dict[str, str]) -> int:
    """Real-/dev/fb0 render loop. Coded per the task brief's geometry spec
    but NOT exercised by this task's tests -- v1 owns the CRT until Task
    4's supervised smoke window runs this path for real.

    Opens and mmaps the framebuffer device ONCE for the life of this loop
    (`open_fb_mmap`) and writes each frame into that mapping
    (`Surface.write_to_mmap`) rather than `write_fb()`'s reopen-the-device-
    every-frame path -- reopening a character device 30 times a second is
    needless syscall overhead once the pixel pack itself is fast (see
    surface.py's module docstring for the to_rgb565 benchmark that made
    this worth doing).
    """
    width, height, stride = _read_fb_geometry()
    surface = Surface(width, height)
    font = load_font()
    state = {"page": page, "topic": topic, "keymap": dict(keymap),
             "status_vm": dict(chrome.DEFAULT_STATUS_VM),
             "alerts_vm": dict(chrome.DEFAULT_ALERTS_VM), "timesig_vm": dict(chrome.DEFAULT_TIMESIG_VM),
             "beatflash_vm": dict(chrome.DEFAULT_BEATFLASH_VM),
             "loopprogress_vm": dict(chrome.DEFAULT_LOOPPROGRESS_VM),
             "marquee_vm": dict(chrome.DEFAULT_MARQUEE_VM)}
    on_event = _make_page_switcher(client, state, fps)

    fb_file, fb_mm = open_fb_mmap(fb_path, stride * height)
    try:
        quit_event = threading.Event()
        if not no_input:
            # `on_event` is already constructed above (before this thread
            # starts): the input thread can fire `page.next` immediately, and
            # the main thread is about to block in `wait_first_snapshot` below
            # -- without event-awareness there, that page_changed would be
            # silently dropped and the client would stay on the stale topic
            # forever (this was the actual freeze: the input thread calling
            # client.action() while the main thread's own switch_topic() call
            # from a later on_event races it over the same connection).
            # `lambda: state["keymap"]` (Phase 4 Task 1): a callable, not a
            # frozen dict, so a `keymap_changed` refetch (via `on_event`
            # above, reassigning `state["keymap"]`) is visible to this
            # thread's very next keypress -- see `_make_page_switcher`'s own
            # docstring for why reassignment (not in-place mutation) is
            # what makes that safe with no lock.
            threading.Thread(target=_input_loop,
                            args=(client, quit_event, lambda: state["keymap"]),
                            daemon=True).start()

        vm = wait_first_snapshot(inbox, lambda: state["topic"], on_event)
        vm_topic = state["topic"]
        _paint_frame(surface, state["page"], vm, font, state["status_vm"],
                     state["alerts_vm"], state["timesig_vm"],
                     state["beatflash_vm"], state["loopprogress_vm"], state["marquee_vm"])
        surface.write_to_mmap(fb_mm, stride=stride)

        period = 1.0 / fps
        while not quit_event.is_set():
            if quit_event.wait(period):
                break
            # Callable, not a frozen `{state["topic"]}` snapshot: `on_event`
            # (invoked from inside this very call) can switch `state["topic"]`
            # mid-drain, and a same-batch snapshot for the NEW topic must
            # still be recognised, not dropped by a stale membership check.
            # The six overlay topics are fixed members -- each updates on
            # its own schedule, independent of the page topic.
            drained = drain_latest(
                inbox, lambda: {state["topic"], chrome.OVERLAY_STATUS_TOPIC,
                                chrome.OVERLAY_ALERTS_TOPIC, chrome.OVERLAY_TIMESIG_TOPIC,
                                chrome.OVERLAY_BEATFLASH_TOPIC, chrome.OVERLAY_LOOPPROGRESS_TOPIC,
                                chrome.OVERLAY_MARQUEE_TOPIC},
                on_event=on_event)
            page_updated = state["topic"] in drained
            if page_updated:
                vm = drained[state["topic"]]
                vm_topic = state["topic"]
            status_updated = chrome.OVERLAY_STATUS_TOPIC in drained
            if status_updated:
                state["status_vm"] = drained[chrome.OVERLAY_STATUS_TOPIC]
            secondary_updated = False
            if chrome.OVERLAY_ALERTS_TOPIC in drained:
                state["alerts_vm"] = drained[chrome.OVERLAY_ALERTS_TOPIC]
                secondary_updated = True
            if chrome.OVERLAY_TIMESIG_TOPIC in drained:
                state["timesig_vm"] = drained[chrome.OVERLAY_TIMESIG_TOPIC]
                secondary_updated = True
            beatprogress_updated = False
            if chrome.OVERLAY_BEATFLASH_TOPIC in drained:
                state["beatflash_vm"] = drained[chrome.OVERLAY_BEATFLASH_TOPIC]
                beatprogress_updated = True
            if chrome.OVERLAY_LOOPPROGRESS_TOPIC in drained:
                state["loopprogress_vm"] = drained[chrome.OVERLAY_LOOPPROGRESS_TOPIC]
                beatprogress_updated = True
            # Phase 8 Task 4: the marquee ticks on its OWN wall-clock
            # schedule (`analyzers/marquee.py::MarqueeAnalyzer.tick`),
            # roughly `HEADER_SCROLL_SPEED` times/sec once scrolling is
            # engaged -- independent of every other overlay/page update.
            # Without its own `marquee_updated` flag feeding the repaint
            # gate below, the header would only ever move as a side effect
            # of SOME OTHER vm changing, defeating the whole point of a
            # continuously-scrolling anti-burn-in header.
            marquee_updated = chrome.OVERLAY_MARQUEE_TOPIC in drained
            if marquee_updated:
                state["marquee_vm"] = drained[chrome.OVERLAY_MARQUEE_TOPIC]
            # `page_changed` (via `on_event`, inside `drain_latest` above)
            # can flip `state["page"]`/`state["topic"]` immediately, but the
            # NEW topic's own first snapshot can arrive "up to 1/max_rate
            # later" (docs/phase2-notes.md) -- not necessarily this same
            # tick. `vm`/`vm_topic` still describe the OLD page in that gap.
            # An unrelated overlay-only update (status/alerts/timesig/
            # beatflash/loopprogress/marquee, each ticking independently)
            # must not repaint the body against that stale, mismatched vm --
            # found live in phase-3 task 11's supervised CRT smoke
            # (page="eventlog" painted with a screensaver vm, crashing
            # `render_frame` on the missing `vm['count']`; see
            # test_run_device_survives_page_switch_before_new_topics_
            # snapshot_arrives). Skip the WHOLE repaint (chrome included --
            # body+chrome are one `_paint_frame` call) until vm_topic
            # catches up; the overlay update itself isn't lost, it just
            # gets folded into the next tick that also carries (or already
            # has) a topic-matching vm.
            vm_is_current = vm_topic == state["topic"]
            if vm_is_current and (page_updated or status_updated or secondary_updated
                                   or beatprogress_updated or marquee_updated):
                # `render_frame` clears the WHOLE surface, so all THREE
                # chrome strips must be repainted on every redraw, not just
                # when their own vm changed (`_paint_frame` skips them
                # entirely on the screensaver page -- see its own docstring).
                _paint_frame(surface, state["page"], vm, font, state["status_vm"],
                             state["alerts_vm"], state["timesig_vm"],
                             state["beatflash_vm"], state["loopprogress_vm"], state["marquee_vm"])
                surface.write_to_mmap(fb_mm, stride=stride)
        return 0
    finally:
        fb_mm.close()
        fb_file.close()


def run(socket_path: str, fb_path: str, out_path: str | None,
        no_input: bool, fps: float) -> int:
    client = EngineClient(socket_path)
    overlay_topics = [chrome.OVERLAY_STATUS_TOPIC, chrome.OVERLAY_ALERTS_TOPIC,
                       chrome.OVERLAY_TIMESIG_TOPIC, chrome.OVERLAY_BEATFLASH_TOPIC,
                       chrome.OVERLAY_LOOPPROGRESS_TOPIC, chrome.OVERLAY_MARQUEE_TOPIC]
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic, *overlay_topics], max_rate=fps)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    try:
        if out_path is not None:
            # Headless test/acceptance mode: render exactly one frame from
            # the first snapshot and exit -- never touches evdev or a real
            # fb device regardless of --no-input/--fb. No input thread
            # exists in this path (deliberately, per the docstring above),
            # so there's no concurrent source of a page_changed here -- a
            # plain fixed `topic` for the PAGE wait is correct as-is; the
            # overlay snapshots (seeded server-side at subscribe() time,
            # same as the page's) are captured opportunistically via
            # `on_event` -- typically delivered in the very first push
            # tick, but a sane default covers the case one hasn't landed
            # yet by the time the page snapshot does.
            secondary = {"alerts_vm": dict(chrome.DEFAULT_ALERTS_VM),
                        "timesig_vm": dict(chrome.DEFAULT_TIMESIG_VM)}
            status = {"vm": dict(chrome.DEFAULT_STATUS_VM)}
            beatprogress = {"beatflash_vm": dict(chrome.DEFAULT_BEATFLASH_VM),
                            "loopprogress_vm": dict(chrome.DEFAULT_LOOPPROGRESS_VM)}
            marquee = {"vm": dict(chrome.DEFAULT_MARQUEE_VM)}

            def _capture_status(msg: dict) -> None:
                if msg.get("kind") != "snapshot":
                    return
                if msg.get("topic") == chrome.OVERLAY_STATUS_TOPIC:
                    status["vm"] = msg["data"]
                elif msg.get("topic") == chrome.OVERLAY_ALERTS_TOPIC:
                    secondary["alerts_vm"] = msg["data"]
                elif msg.get("topic") == chrome.OVERLAY_TIMESIG_TOPIC:
                    secondary["timesig_vm"] = msg["data"]
                elif msg.get("topic") == chrome.OVERLAY_BEATFLASH_TOPIC:
                    beatprogress["beatflash_vm"] = msg["data"]
                elif msg.get("topic") == chrome.OVERLAY_LOOPPROGRESS_TOPIC:
                    beatprogress["loopprogress_vm"] = msg["data"]
                elif msg.get("topic") == chrome.OVERLAY_MARQUEE_TOPIC:
                    marquee["vm"] = msg["data"]

            vm = wait_first_snapshot(inbox, topic, _capture_status)
            surface = Surface(*OUT_SIZE)
            _paint_frame(surface, page, vm, load_font(), status["vm"],
                         secondary["alerts_vm"], secondary["timesig_vm"],
                         beatprogress["beatflash_vm"], beatprogress["loopprogress_vm"],
                         marquee["vm"])
            surface.save_png(out_path)
            return 0
        # Fetched only on this (interactive-device) path -- the headless
        # `--out` branch above never dispatches a key at all, so a
        # `describe` round trip for it there would be pure overhead.
        keymap = fetch_keymap(client)
        return _run_device(client, inbox, fb_path, no_input, fps, page, topic, keymap)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        return 1
    finally:
        client.close()


def _fps_type(value: str) -> float:
    """argparse `type=` for --fps: must parse as a finite, positive float.
    Rejecting here (before connect()) keeps a bad value off the wire --
    the server rejects non-positive/non-finite max_rate too, but the
    client never checked that response, so a rejected subscribe() left
    `run()` blocked forever in `_wait_first_snapshot` (connection alive,
    no snapshot ever coming). See EngineClient.request()/subscribe() for
    the other half of this fix -- callers should no longer get a silent
    ok:false response either way, but validating here means a bad --fps
    never has to round-trip to find that out.
    """
    try:
        fps = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if not math.isfinite(fps) or fps <= 0:
        raise argparse.ArgumentTypeError(f"--fps must be a finite number > 0, got {value!r}")
    return fps


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="midicrt-fb")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--fb", default="/dev/fb0")
    ap.add_argument("--out", default=None, metavar="PATH",
                     help="render one frame to this PNG and exit (headless test mode)")
    ap.add_argument("--no-input", action="store_true")
    ap.add_argument("--fps", type=_fps_type, default=30.0)
    args = ap.parse_args()

    socket_path = args.socket or config_mod.load(None).socket_path
    raise SystemExit(run(socket_path, args.fb, args.out, args.no_input, args.fps))


if __name__ == "__main__":
    main()
