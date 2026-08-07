"""fb/app.py -- midicrt-fb: the framebuffer CRT client entrypoint.

Renders the eventlog page view-model onto a pixel `Surface` (fb/surface.py)
using the vendored PSF console font (fb/text.py), then either writes the
packed RGB565 buffer to a real Linux framebuffer device or, in `--out` test
mode, saves a PNG and exits -- see `main()`/`run()` below.

Colour palette ported from v1's default CRT-green scheme
(`~/codex/midicrt/fb/compositor.py` on the Pi, read-only reference):
    GREEN_BRIGHT_RGB = (0, 255, 80)   # here: HEADER_BG and ACCENT_FG
    GREEN_MID_RGB    = (0, 180, 50)   # here: NORMAL_FG
    BLACK_RGB        = (0, 0, 0)      # here: BG
Accent (note_on) event lines reuse v1's "bright" tone so they read louder
than the "mid" tone used for ordinary lines, and the header bar's
reverse-video fill reuses the same bright tone with text punched out in the
background colour.

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
from pathlib import Path

from midicrt import config as config_mod
from midicrt.behaviors.screensaver import SCREENSAVER_PAGE
from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    drain_latest,
    switch_topic,
    wait_first_snapshot,
)
from midicrt.clients.fb.surface import Surface, open_fb_mmap
from midicrt.clients.fb.text import draw_text, load_font
from midicrt.clients.tui import _tail

_LOG = logging.getLogger("midicrt-fb")

# -- palette (see module docstring for provenance) ------------------------
BG = (0, 0, 0)
HEADER_BG = (0, 255, 80)
NORMAL_FG = (0, 180, 50)
ACCENT_FG = (0, 255, 80)

# -- layout -----------------------------------------------------------------
HEADER_PAD = 2   # vertical inset (top+bottom) inside the header bar, px
LINE_GAP = 1     # extra vertical gap between event-line rows, px
LEFT_MARGIN = 4  # left inset for header + event text, px
STATUS_PAD = 2   # vertical inset (top+bottom) inside the status strip, px -- mirrors HEADER_PAD

# `--out` mode always renders at this fixed size (task brief: "--out mode
# uses 800x475 fixed"), independent of whatever a real /dev/fb0 reports.
OUT_SIZE = (800, 475)

_SYS_FB = Path("/sys/class/graphics/fb0")


def render_frame(vm: dict, surface: Surface) -> None:
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
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, header_text, BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _reserved_chrome_height(font)
    body_h = max(0, (usable_h - header_h) // line_h)
    for i, line in enumerate(_tail(vm["lines"], body_h)):
        color = ACCENT_FG if line["style"] == "accent" else NORMAL_FG
        draw_text(surface, LEFT_MARGIN, header_h + i * line_h, line["text"], color, font)


def _render_unknown(vm: dict, surface: Surface) -> None:
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


def render_voices_frame(vm: dict, surface: Surface) -> None:
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
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _voices_header_text(vm), BG, font)

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


def render_harmony_frame(vm: dict, surface: Surface) -> None:
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
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _harmony_header_text(vm), BG, font)

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


def render_tuner_frame(vm: dict, surface: Surface) -> None:
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
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _tuner_header_text(vm), BG, font)

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


# -- pianoroll page (phase-3 task 7) ------------------------------------------
#
# Layout: same reverse-video header convention as the other pages, then a
# pixel roll body drawn via `Surface.rect` (task brief: "fb: pixel roll via
# rect/hline primitives" -- `rect` alone suffices here since every note is
# already a filled bar, the same choice `render_harmony_frame`'s tension
# fill makes over a separate hline loop). One rect per note: x0/x1 mapped
# onto the full surface width, y mapped onto the usable body height sliced
# into `range.hi - range.lo + 1` equal rows (the same "usable_h // count"
# convention `render_voices_frame` already uses for its 16 channel rows).
# v1's real CRT compositor (`fb/compositor_renderer.py`, not read -- out of
# this task's ported source map, see pages/pianoroll.py's own module
# docstring) is a separate rendering pipeline this renderer does not need
# to match pixel-for-pixel; it follows the same "primitives only, never
# poke surface.image directly" convention every fb renderer here already
# established.
#
# Channel-colored + charred (task brief: "channel-colored/charred per v1's
# approach"): a real color CRT can combine BOTH of v1's mutually-exclusive
# TUI alt-styles at once -- `ui/renderers/pixel.py`'s "dense" mode's
# per-channel hue cycling, AND `ui/renderers/text/renderer.py`'s velocity-
# ramp "charred" intensity -- unlike the plain-text TUI renderer above
# (clients/tui.py's `render_pianoroll_lines`, which reproduces only v1's
# DEFAULT "text" style; see that renderer's own comment for why raw ANSI
# channel color isn't embedded there instead). `_ROLL_CHANNEL_PALETTE`
# cycles 8 distinct hues by channel (`(ch - 1) % 8`, mirroring `ui/
# renderers/pixel.py`'s own `(channel - 1) % len(palette)` cycling);
# `_roll_note_color` blends each hue from a DIM ("charred", low velocity)
# tone up to its full BRIGHT tone as velocity rises -- the pixel-color
# equivalent of the TUI renderer's four-glyph density ramp.
_ROLL_CHANNEL_PALETTE = [
    (255, 60, 60), (255, 170, 40), (255, 230, 40), (80, 230, 80),
    (60, 200, 255), (90, 110, 255), (200, 90, 255), (255, 90, 190),
]
_ROLL_DIM_FRACTION = 0.25   # velocity 0.0 -> 25% of the channel's full hue


def _roll_note_color(ch: int, vel: float) -> tuple[int, int, int]:
    hue = _ROLL_CHANNEL_PALETTE[(int(ch) - 1) % len(_ROLL_CHANNEL_PALETTE)]
    vel = max(0.0, min(1.0, vel))
    frac = _ROLL_DIM_FRACTION + (1.0 - _ROLL_DIM_FRACTION) * vel
    return tuple(round(c * frac) for c in hue)


def _pianoroll_header_text(vm: dict) -> str:
    w = vm["window"]
    rng = vm["range"]
    return f"{vm['title']}  ({w['mode']} zoom {w['zoom']:.2f}, range {rng['lo']}-{rng['hi']})"


def render_pianoroll_frame(vm: dict, surface: Surface) -> None:
    """Render the pianoroll page view-model (pages/pianoroll.py) onto
    `surface`. Pure: reads only `vm` and the cached default font, writes
    only to `surface`'s pixels -- no I/O, no clock, no global state beyond
    the font's (side-effect-free) glyph cache. See module comment above for
    the pixel-roll layout / channel-color + charred-velocity convention.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _pianoroll_header_text(vm), BG, font)

    usable_h = surface.height - _reserved_chrome_height(font) - header_h
    if usable_h <= 0:
        return
    rng = vm["range"]
    pitch_span = max(1, rng["hi"] - rng["lo"] + 1)
    note_h = max(1, usable_h // pitch_span)
    roll_w = surface.width
    for note in vm["notes"]:
        y = header_h + round(note["y"] * max(0, usable_h - note_h))
        x0 = round(note["x0"] * roll_w)
        x1 = round(note["x1"] * roll_w)
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


def render_spectrum_frame(vm: dict, surface: Surface) -> None:
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
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _spectrum_header_text(vm), BG, font)

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


def render_screensaver_frame(vm: dict, surface: Surface) -> None:
    surface.clear(BG)


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# The engine already emits a FIXED `GRID_ROWS` x `GRID_COLS` grid of final
# [0,1] brightness values (analyzers/img2txtviz.py's own module docstring)
# -- this renderer's only job is filling one `rect` per grid cell, scaled up
# to fill the usable body area, colored by scaling `ACCENT_FG` (this
# module's CRT-green "bright" tone) by each cell's own value -- 0.0 renders
# black (BG), 1.0 renders full ACCENT_FG, matching the monochrome-CRT-green
# palette every other renderer here already uses (no new colors
# introduced). `invert` is already applied engine-side (the grid's own
# values are already final), so this renderer never reads that flag itself.
def _img2txtviz_header_text(vm: dict) -> str:
    flag = "  INV" if vm.get("invert") else ""
    return f"{vm['title']}  notes:{vm['active_notes']:02d}{flag}"


def _img2txtviz_cell_color(value: float) -> tuple[int, int, int]:
    v = max(0.0, min(1.0, value))
    return tuple(round(c * v) for c in ACCENT_FG)


def render_img2txtviz_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _img2txtviz_header_text(vm), BG, font)

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


def render_config_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _config_header_text(vm), BG, font)

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


# -- help page (phase-3 task 12, gap ports) -----------------------------------
#
# Same "-- Section --" + "label: value" row-dump convention as
# `render_config_frame` (pages/help.py's view_model is deliberately the same
# `{page_rows, action_rows}`-shaped list-of-dicts). See pages/help.py's own
# module docstring for why this describe-data reference IS the v1 Help page's
# parity port, not a literal keybinding-list transcription.
def _help_header_text(vm: dict) -> str:
    return f"{vm['title']}"


def render_help_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _help_header_text(vm), BG, font)

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


def render_progchanges_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _progchanges_header_text(vm), BG, font)

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


def render_ccmonitor_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _ccmonitor_header_text(vm), BG, font)

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


def render_ccdashboard_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _ccdashboard_header_text(vm), BG, font)

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


def render_chordkey_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _chordkey_header_text(vm), BG, font)

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


def render_sendnotes_frame(vm: dict, surface: Surface) -> None:
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _sendnotes_header_text(vm), BG, font)

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


def _paint_frame(surface: Surface, page: str, vm: dict, font, status_vm: dict,
                  alerts_vm: dict, timesig_vm: dict, beatflash_vm: dict,
                  loopprogress_vm: dict) -> None:
    """Render `page`'s body, then all THREE chrome strips -- UNLESS `page`
    is the screensaver page (Important fix, task-9 review), in which case
    NO chrome is painted at all, matching v1's true full-screen blank --
    see the module comment above `render_screensaver_frame`/
    `SCREENSAVER_PAGE`. Single call site for this "page owns everything,
    chrome paints after, except when screensaving" rule so the three real
    run-loop call sites (`_run_device`'s initial paint + its redraw loop,
    and `run()`'s `--out` one-shot path) can never drift from each other.
    """
    renderer = RENDERERS.get(page, _render_unknown)
    renderer(vm, surface)
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

def _find_input_device():
    """Return the first evdev device exposing KEY_Q, or None if none do."""
    import evdev

    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if evdev.ecodes.KEY_Q in dev.capabilities().get(evdev.ecodes.EV_KEY, []):
            return dev
    return None


def _input_loop(client: EngineClient, quit_event: threading.Event) -> None:
    """Background-thread evdev reader (brief: "Input runs in a thread").
    `q` sets `quit_event` for a clean exit; `c` fires `eventlog.clear`; `n`
    fires `page.next` (the resulting `page_changed` event and resubscribe
    are handled by the render loop's `drain_latest(on_event=...)`, not
    here). Any discovery/permission/IO failure logs one line and returns --
    input is a nice-to-have, never fatal to the render loop.
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
    try:
        for event in dev.read_loop():
            if event.type != evdev.ecodes.EV_KEY or event.value != 1:  # key-down only
                continue
            if event.code == evdev.ecodes.KEY_Q:
                quit_event.set()
                return
            if event.code == evdev.ecodes.KEY_C:
                try:
                    client.action("eventlog.clear")
                except ClientError:
                    pass  # connection loss surfaces via the render loop's EOF check
            if event.code == evdev.ecodes.KEY_N:
                try:
                    client.action("page.next")
                except ClientError:
                    pass  # connection loss surfaces via the render loop's EOF check
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
    state["topic"]` in place."""

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, max_rate)
            state["page"], state["topic"] = new_page, new_topic

    return on_event


def _run_device(client: EngineClient, inbox: queue.Queue, fb_path: str,
                 no_input: bool, fps: float, page: str, topic: str) -> int:
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
    state = {"page": page, "topic": topic, "status_vm": dict(chrome.DEFAULT_STATUS_VM),
             "alerts_vm": dict(chrome.DEFAULT_ALERTS_VM), "timesig_vm": dict(chrome.DEFAULT_TIMESIG_VM),
             "beatflash_vm": dict(chrome.DEFAULT_BEATFLASH_VM),
             "loopprogress_vm": dict(chrome.DEFAULT_LOOPPROGRESS_VM)}
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
            threading.Thread(target=_input_loop, args=(client, quit_event), daemon=True).start()

        vm = wait_first_snapshot(inbox, lambda: state["topic"], on_event)
        vm_topic = state["topic"]
        _paint_frame(surface, state["page"], vm, font, state["status_vm"],
                     state["alerts_vm"], state["timesig_vm"],
                     state["beatflash_vm"], state["loopprogress_vm"])
        surface.write_to_mmap(fb_mm, stride=stride)

        period = 1.0 / fps
        while not quit_event.is_set():
            if quit_event.wait(period):
                break
            # Callable, not a frozen `{state["topic"]}` snapshot: `on_event`
            # (invoked from inside this very call) can switch `state["topic"]`
            # mid-drain, and a same-batch snapshot for the NEW topic must
            # still be recognised, not dropped by a stale membership check.
            # The five overlay topics are fixed members -- each updates on
            # its own schedule, independent of the page topic.
            drained = drain_latest(
                inbox, lambda: {state["topic"], chrome.OVERLAY_STATUS_TOPIC,
                                chrome.OVERLAY_ALERTS_TOPIC, chrome.OVERLAY_TIMESIG_TOPIC,
                                chrome.OVERLAY_BEATFLASH_TOPIC, chrome.OVERLAY_LOOPPROGRESS_TOPIC},
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
            # `page_changed` (via `on_event`, inside `drain_latest` above)
            # can flip `state["page"]`/`state["topic"]` immediately, but the
            # NEW topic's own first snapshot can arrive "up to 1/max_rate
            # later" (docs/phase2-notes.md) -- not necessarily this same
            # tick. `vm`/`vm_topic` still describe the OLD page in that gap.
            # An unrelated overlay-only update (status/alerts/timesig/
            # beatflash/loopprogress, each ticking independently) must not
            # repaint the body against that stale, mismatched vm -- found
            # live in phase-3 task 11's supervised CRT smoke (page="eventlog"
            # painted with a screensaver vm, crashing `render_frame` on the
            # missing `vm['count']`; see test_run_device_survives_page_
            # switch_before_new_topics_snapshot_arrives). Skip the WHOLE
            # repaint (chrome included -- body+chrome are one `_paint_frame`
            # call) until vm_topic catches up; the overlay update itself
            # isn't lost, it just gets folded into the next tick that also
            # carries (or already has) a topic-matching vm.
            vm_is_current = vm_topic == state["topic"]
            if vm_is_current and (page_updated or status_updated or secondary_updated or beatprogress_updated):
                # `render_frame` clears the WHOLE surface, so all THREE
                # chrome strips must be repainted on every redraw, not just
                # when their own vm changed (`_paint_frame` skips them
                # entirely on the screensaver page -- see its own docstring).
                _paint_frame(surface, state["page"], vm, font, state["status_vm"],
                             state["alerts_vm"], state["timesig_vm"],
                             state["beatflash_vm"], state["loopprogress_vm"])
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
                       chrome.OVERLAY_LOOPPROGRESS_TOPIC]
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

            vm = wait_first_snapshot(inbox, topic, _capture_status)
            surface = Surface(*OUT_SIZE)
            _paint_frame(surface, page, vm, load_font(), status["vm"],
                         secondary["alerts_vm"], secondary["timesig_vm"],
                         beatprogress["beatflash_vm"], beatprogress["loopprogress_vm"])
            surface.save_png(out_path)
            return 0
        return _run_device(client, inbox, fb_path, no_input, fps, page, topic)
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
