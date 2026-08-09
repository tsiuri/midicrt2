"""Tests for the fb client app: `render_frame` (pure) + the `--out` headless
end-to-end path (the acceptance path for this task -- real device writes are
coded but NOT exercised here; v1 owns /dev/fb0 until Task 4's supervised
smoke window).
"""
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from midicrt.clients import chrome
from midicrt.clients.base import ClientError
from midicrt.clients.chrome import DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, DEFAULT_MARQUEE_VM
from midicrt.clients.fb import app
from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import draw_text, load_font
from midicrt.clients.tui import _tail as tui_tail

VENVPY = sys.executable
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_FRAME = FIXTURES / "fb_frame_golden.png"
GOLDEN_EMPTY = FIXTURES / "fb_frame_empty_golden.png"

# Phase-3 task 9 note (applies to every *_SURFACE_SIZE constant in this
# file): each fixed surface height below grew by exactly one strip's worth
# of pixels (`font.height + 2*STATUS_PAD` = 12px at the vendored PSF font's
# 8px height) to make room for the new `_draw_beatprogress` strip WITHOUT
# shrinking any page's usable body height -- `_reserved_chrome_height(font)`
# grew by that same 12px, so `usable_h = surface.height -
# _reserved_chrome_height(font) - header_h` is unchanged from before this
# task, and every row-count/geometry assertion below (and the frozen
# golden PNGs, re-frozen per this task's report) still lines up exactly.

EMPTY_VM = {"title": "EVENT LOG", "count": 0, "lines": []}

# The golden fixture renders this exact view-model at this exact surface
# size -- see the freeze procedure recorded in the task-3 report.
VM = {
    "title": "EVENT LOG",
    "count": 12,
    "lines": [
        {"text": "note_off ch1 60", "style": "normal"},
        {"text": "note_on  ch1 64", "style": "accent"},
        {"text": "cc      ch1 7=100", "style": "normal"},
    ],
}
GOLDEN_SURFACE_SIZE = (220, 72)   # +12px for the new beatprogress strip (see note above)

# The golden-frame fixture now also exercises the chrome status strip
# (phase-3 task 3: "golden updates for both renderers -- chrome now
# present") -- a fixed, representative overlay.status view-model so the
# frozen PNG shows real BPM/BAR/BEAT/running/source content, not just the
# all-defaults idle state (that case is covered separately by the
# `--out`-mode golden against a real `--no-midi` daemon, which never sees
# a start/clock event and so IS the all-defaults case).
GOLDEN_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}

# Phase-3 task 6: a representative ACTIVE alert -- exercises the secondary
# strip's "alerts win over timesig" branch (clients/chrome.py's
# `secondary_status_text()`); the timesig VM here is deliberately non-empty
# too, to prove it's the alert that wins, not merely the only content.
GOLDEN_ALERTS_VM = {"alerts": [{"ch": 3, "note": 60, "level": "warn", "held_s": 2.3}]}
GOLDEN_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                      "events_window": 24, "events_total": 40, "pending": None}

# Phase-3 task 9: a representative MID-DECAY beat flash + a running
# loop-progress bar -- exercises the new third strip's real-content branch
# on this (the eventlog) golden only, same "one golden shows the
# interesting case, the rest show idle/default" precedent phase-3 task 6
# already set for GOLDEN_ALERTS_VM/VOICES_ALERTS_VM.
GOLDEN_BEATFLASH_VM = {"intensity": 0.8, "is_bar": False}
GOLDEN_LOOPPROGRESS_VM = {"fraction": 0.375, "running": True}


def test_renderers_dispatch_table_has_eventlog():
    assert app.RENDERERS["eventlog"] is app.render_frame


def test_render_unknown_fallback_does_not_crash_on_bare_vm():
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app._render_unknown({}, surf)  # must not raise


def test_render_frame_reuses_tui_tail_not_a_duplicate():
    # Task brief: "mirrors the TUI's _tail semantics (reuse
    # clients/tui._tail -- import it, do not duplicate)." Assert identity,
    # not just matching behaviour, so a future refactor that reintroduces a
    # copy fails loudly here.
    assert app._tail is tui_tail


def test_render_frame_header_bar_is_reverse_video():
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(EMPTY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = f"{EMPTY_VM['title']}  ({EMPTY_VM['count']} events)"
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width  # sanity: fixture wide enough for a clear zone
    # Right of the header text, still inside the bar -- solid reverse-video fill.
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    # A lit stroke pixel of the header text is painted in the background
    # colour (that's what makes it read as "reverse video").
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_frame_body_background_untouched_with_no_lines():
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(EMPTY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert px[0, header_h + 5] == app.BG


def test_render_frame_tails_to_whatever_fits_newest_only():
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    strip_h = app._reserved_chrome_height(font)   # phase-3 task 6: BOTH chrome strips
    line_h = font.height + app.LINE_GAP
    size = (200, header_h + strip_h + line_h)  # room for exactly one body line
    many = {"title": "EVENT LOG", "count": 5,
            "lines": [{"text": f"line{i}", "style": "normal"} for i in range(5)]}

    got = Surface(*size)
    app.render_frame(many, got)

    want = Surface(*size)
    want.clear(app.BG)
    want.rect(0, 0, size[0], header_h, app.HEADER_BG)
    draw_text(want, app.LEFT_MARGIN, app.HEADER_PAD,
              f"{many['title']}  ({many['count']} events)", app.BG, font)
    draw_text(want, app.LEFT_MARGIN, header_h, "line4", app.NORMAL_FG, font)  # newest only

    assert got.image.tobytes() == want.image.tobytes()


def test_render_frame_orders_tail_oldest_to_newest_top_down():
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    strip_h = app._reserved_chrome_height(font)   # phase-3 task 6: BOTH chrome strips
    line_h = font.height + app.LINE_GAP
    size = (200, header_h + strip_h + 2 * line_h)  # room for exactly two body lines
    many = {"title": "EVENT LOG", "count": 5,
            "lines": [{"text": f"line{i}", "style": "normal"} for i in range(5)]}

    got = Surface(*size)
    app.render_frame(many, got)

    want = Surface(*size)
    want.clear(app.BG)
    want.rect(0, 0, size[0], header_h, app.HEADER_BG)
    draw_text(want, app.LEFT_MARGIN, app.HEADER_PAD,
              f"{many['title']}  ({many['count']} events)", app.BG, font)
    draw_text(want, app.LEFT_MARGIN, header_h, "line3", app.NORMAL_FG, font)
    draw_text(want, app.LEFT_MARGIN, header_h + line_h, "line4", app.NORMAL_FG, font)

    assert got.image.tobytes() == want.image.tobytes()


def test_render_frame_reserves_the_bottom_status_strip_as_background():
    # The strip itself is reserved (left BG) by render_frame, not drawn --
    # `_draw_status` (called separately by the run loops) paints it.
    font = load_font()
    strip_h = app._status_strip_height(font)
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    many = {"title": "EVENT LOG", "count": 50,
            "lines": [{"text": f"line{i}", "style": "normal"} for i in range(50)]}
    app.render_frame(many, surf)  # enough lines to fill all available body rows
    px = surf.image.load()
    y_in_strip = surf.height - strip_h + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_status_strip_height_matches_header_sizing_convention():
    font = load_font()
    assert app._status_strip_height(font) == font.height + 2 * app.STATUS_PAD


def test_draw_status_paints_reverse_video_bar_at_the_bottom():
    # Phase-3 task 9: the status strip no longer sits at `surf.height`'s own
    # edge -- the new beatprogress strip does (see app._draw_beatprogress's
    # own docstring for why, matching v1's physical row order).
    font = load_font()
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    surf.clear(app.BG)
    app._draw_status(surf, GOLDEN_STATUS_VM, font)
    px = surf.image.load()
    strip_h = app._status_strip_height(font)
    y = surf.height - app._beatprogress_strip_height(font) - strip_h
    # Fill matches the header's reverse-video bar colour.
    assert px[surf.width - 1, y] == app.HEADER_BG
    assert px[surf.width - 1, y + strip_h - 1] == app.HEADER_BG
    # Above the strip is untouched.
    assert px[0, y - 1] == app.BG
    # A lit stroke pixel of the status text is painted in the background
    # colour (reverse video), same convention as the header.
    assert any(
        px[x, yy] == app.BG
        for x in range(app.LEFT_MARGIN, surf.width)
        for yy in range(y + app.STATUS_PAD, y + app.STATUS_PAD + font.height)
    )


def test_draw_status_text_matches_shared_chrome_status_text():
    from midicrt.clients import chrome

    font = load_font()
    surf_a = Surface(300, GOLDEN_SURFACE_SIZE[1])
    surf_a.clear(app.BG)
    app._draw_status(surf_a, GOLDEN_STATUS_VM, font)

    surf_b = Surface(300, GOLDEN_SURFACE_SIZE[1])
    surf_b.clear(app.BG)
    strip_h = app._status_strip_height(font)
    y = surf_b.height - app._beatprogress_strip_height(font) - strip_h
    surf_b.rect(0, y, surf_b.width, strip_h, app.HEADER_BG)
    draw_text(surf_b, app.LEFT_MARGIN, y + app.STATUS_PAD,
              chrome.status_text(GOLDEN_STATUS_VM), app.BG, font)

    assert surf_a.image.tobytes() == surf_b.image.tobytes()


# -- third chrome strip: beatflash/loopprogress (phase-3 task 9) ------------

def test_beatprogress_strip_height_matches_the_other_strips_sizing_convention():
    font = load_font()
    assert app._beatprogress_strip_height(font) == font.height + 2 * app.STATUS_PAD
    assert app._beatprogress_strip_height(font) == app._status_strip_height(font)


def test_reserved_chrome_height_sums_all_three_strips():
    font = load_font()
    assert app._reserved_chrome_height(font) == (
        app._secondary_strip_height(font) + app._status_strip_height(font)
        + app._beatprogress_strip_height(font)
    )


def test_draw_beatprogress_paints_reverse_video_bar_at_the_true_bottom():
    font = load_font()
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    surf.clear(app.BG)
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, font)
    px = surf.image.load()
    strip_h = app._beatprogress_strip_height(font)
    y = surf.height - strip_h
    # Pinned to surface.height's own edge -- the new true bottom-most strip.
    assert y + strip_h == surf.height
    assert px[surf.width - 1, y] == app.HEADER_BG
    assert px[surf.width - 1, surf.height - 1] == app.HEADER_BG
    assert px[0, y - 1] == app.BG   # above the strip is untouched


def test_draw_beatprogress_text_matches_shared_chrome_row_text():
    font = load_font()
    beatflash_vm = {"intensity": 1.4, "is_bar": True}
    loopprogress_vm = {"fraction": 0.5, "running": True}

    surf_a = Surface(300, GOLDEN_SURFACE_SIZE[1])
    surf_a.clear(app.BG)
    app._draw_beatprogress(surf_a, beatflash_vm, loopprogress_vm, font)

    surf_b = Surface(300, GOLDEN_SURFACE_SIZE[1])
    surf_b.clear(app.BG)
    strip_h = app._beatprogress_strip_height(font)
    y = surf_b.height - strip_h
    surf_b.rect(0, y, surf_b.width, strip_h, app.HEADER_BG)
    num_chars = (surf_b.width - 2 * app.LEFT_MARGIN) // font.width
    text = app.chrome.beatprogress_row_text(beatflash_vm, loopprogress_vm, num_chars)
    draw_text(surf_b, app.LEFT_MARGIN, y + app.STATUS_PAD, text, app.BG, font)

    assert surf_a.image.tobytes() == surf_b.image.tobytes()


# -- screensaver page (phase-3 task 9) ---------------------------------------

def test_screensaver_renderers_dispatch_table_has_screensaver():
    assert app.RENDERERS["screensaver"] is app.render_screensaver_frame


def test_render_screensaver_frame_is_entirely_background():
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    # Paint something first so a no-op renderer would be caught red-handed.
    surf.rect(0, 0, surf.width, surf.height, app.HEADER_BG)
    app.render_screensaver_frame({"title": "SCREENSAVER"}, surf)
    want = Surface(*GOLDEN_SURFACE_SIZE)
    want.clear(app.BG)
    assert surf.image.tobytes() == want.image.tobytes()


def test_paint_frame_skips_all_chrome_on_the_screensaver_page():
    # IMPORTANT fix (task-9 review): a screensaver-page "golden" proving
    # the frame is entirely background -- header, body, AND all three
    # chrome strips -- even when handed ACTIVE, non-default status/alerts/
    # timesig/beatflash/loopprogress VMs (so there is no way for
    # "unrelated content" to leak through the chrome strips by accident).
    active_status = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
    active_alerts = {"alerts": [{"ch": 3, "note": 60, "level": "crit", "held_s": 12.0}]}
    active_timesig = {"labels": ["4/4"], "confidence": 0.9, "events": 30,
                       "events_window": 30, "events_total": 60, "pending": None}
    active_beatflash = {"intensity": 1.4, "is_bar": True}
    active_loopprogress = {"fraction": 0.5, "running": True}

    active_marquee = {"text": "[0:HELP]  [1:HARMONY]", "doubled": "[0:HELP]  [1:HARMONY]    " * 2,
                       "offset": 3}
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app._paint_frame(surf, "screensaver", {"title": "SCREENSAVER"}, load_font(),
                      active_status, active_alerts, active_timesig,
                      active_beatflash, active_loopprogress, active_marquee)

    want = Surface(*GOLDEN_SURFACE_SIZE)
    want.clear(app.BG)
    assert surf.image.tobytes() == want.image.tobytes()
    # Belt and suspenders: every single pixel is BG, not just byte-equal
    # to a second "clear" call (in case both happened to share a bug).
    assert set(surf.image.get_flattened_data()) == {app.BG}


def test_paint_frame_paints_chrome_normally_on_an_ordinary_page():
    # Sanity check for the guard's OTHER branch: an ordinary page still
    # gets all three chrome strips (proves the fix is a page-scoped
    # exception, not a regression that silently dropped chrome everywhere).
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app._paint_frame(surf, "eventlog", EMPTY_VM, load_font(),
                      GOLDEN_STATUS_VM, GOLDEN_ALERTS_VM, GOLDEN_TIMESIG_VM,
                      GOLDEN_BEATFLASH_VM, GOLDEN_LOOPPROGRESS_VM, DEFAULT_MARQUEE_VM)
    assert set(surf.image.get_flattened_data()) != {app.BG}
    px = surf.image.load()
    assert px[surf.width - 1, surf.height - 1] == app.HEADER_BG


# -- header marquee wiring (Phase 8 Task 4, docs/visual-audit.md §20b) ------
#
# v1's primary anti-burn-in device: every page's reverse-video header row
# now shows the live scrolling page-title marquee (`analyzers/marquee.py` +
# `clients/chrome.py::marquee_window_text`) instead of a permanently-static
# per-page title. `_paint_frame` is the one place that slices the marquee
# vm into text and threads it into whichever `render_X_frame` the current
# page dispatches to -- see that function's own docstring.

_LONG_MARQUEE_TEXT = "[0:HELP]  [1:HARMONY]  [2:SEND NOTES]  [4:CC MONITOR]"
_LONG_MARQUEE_GAP = "    "
_LONG_MARQUEE_VM = {
    "text": _LONG_MARQUEE_TEXT,
    "doubled": (_LONG_MARQUEE_TEXT + _LONG_MARQUEE_GAP) * 2,
    "offset": 0,
}


def test_header_text_falls_back_to_page_text_when_marquee_text_is_none():
    assert app._header_text(None, "EVENT LOG  (0 events)") == "EVENT LOG  (0 events)"


def test_header_text_uses_marquee_text_when_given():
    assert app._header_text("[0:HELP]  [1:HAR", "EVENT LOG  (0 events)") == "[0:HELP]  [1:HAR"


def test_render_frame_draws_marquee_text_instead_of_static_title_when_given():
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(EMPTY_VM, surf, marquee_text="[0:HELP]  [1:HARMONY]")
    font = load_font()
    # The old static title text ("EVENT LOG  (0 events)") must NOT have been
    # drawn -- proven by re-rendering with marquee_text=None (the old
    # behavior, still exercised by every pre-existing 2-arg call site) and
    # confirming the two frames differ.
    surf_old = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(EMPTY_VM, surf_old)
    assert surf.image.tobytes() != surf_old.image.tobytes()
    # Sanity: the marquee text itself was actually painted somewhere in the
    # header row (BG-colored text punched into the HEADER_BG reverse fill).
    header_h = font.height + 2 * app.HEADER_PAD
    assert any(surf.image.load()[x, y] == app.BG
               for x in range(app.LEFT_MARGIN, surf.width)
               for y in range(app.HEADER_PAD, header_h))


def test_paint_frame_computes_marquee_text_from_width_aware_slice():
    # A roster string wider than the surface -- proves _paint_frame
    # actually calls marquee_window_text with the REAL header character
    # capacity, not just passing the raw (unsliced) text straight through.
    # (144px tall -- same convention as PIANOROLL_SURFACE_SIZE below --
    # leaves enough room for the header AND all three chrome strips without
    # overlap; a too-short surface would make _paint_frame's chrome strips
    # spill upward into the header band, an unrelated test-construction
    # artifact this size avoids.)
    surf = Surface(200, 144)   # narrower than _LONG_MARQUEE_TEXT -- forces the scroll branch
    font = load_font()
    header_chars = app._header_char_capacity(surf, font)
    assert header_chars < len(_LONG_MARQUEE_TEXT)   # sanity: scroll IS engaged
    app._paint_frame(surf, "eventlog", EMPTY_VM, font,
                      chrome.DEFAULT_STATUS_VM, chrome.DEFAULT_ALERTS_VM, chrome.DEFAULT_TIMESIG_VM,
                      chrome.DEFAULT_BEATFLASH_VM, chrome.DEFAULT_LOOPPROGRESS_VM, _LONG_MARQUEE_VM)
    expected_text = chrome.marquee_window_text(_LONG_MARQUEE_VM, header_chars)
    want = Surface(200, 144)
    app.render_frame(EMPTY_VM, want, marquee_text=expected_text)
    # Only compare the header band -- _paint_frame also paints chrome strips
    # render_frame alone does not.
    header_h = font.height + 2 * app.HEADER_PAD
    got_header = surf.image.crop((0, 0, surf.width, header_h)).tobytes()
    want_header = want.image.crop((0, 0, surf.width, header_h)).tobytes()
    assert got_header == want_header


def test_screensaver_page_ignores_marquee_text_true_blank_preserved():
    # Task-9's burn-in-safe true-blank guarantee must survive this task:
    # the screensaver page draws NOTHING, marquee included.
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app._paint_frame(surf, "screensaver", {"title": "SCREENSAVER"}, load_font(),
                      chrome.DEFAULT_STATUS_VM, chrome.DEFAULT_ALERTS_VM, chrome.DEFAULT_TIMESIG_VM,
                      chrome.DEFAULT_BEATFLASH_VM, chrome.DEFAULT_LOOPPROGRESS_VM, _LONG_MARQUEE_VM)
    assert set(surf.image.get_flattened_data()) == {app.BG}


# -- THE BURN-IN TRIPWIRE (Phase 8 Task 4 brief) -----------------------------
#
# "a test rendering the same VM at t and t+N seconds (tick-driven) must
# assert pixel-position DIFFERENCE for every audit-marked anti-burn-in
# mover." The header marquee is v1's primary anti-burn-in device
# (docs/visual-audit.md §20b) -- this is its tripwire, exercising the REAL
# `MarqueeAnalyzer` (not a hand-built vm) ticked at two real timestamps,
# through the REAL `_paint_frame` dispatch, comparing REAL rendered pixels.

def test_burn_in_tripwire_marquee_header_pixels_differ_at_t_and_t_plus_n():
    from midicrt.analyzers.marquee import MarqueeAnalyzer

    roster = ["eventlog", "voices", "harmony", "pianoroll", "spectrum", "img2txtviz",
              "config", "help", "progchanges", "ccmonitor", "ccdashboard", "chordkey", "sendnotes"]
    analyzer = MarqueeAnalyzer(roster, speed_cps=4.0)
    font = load_font()
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    header_chars = app._header_char_capacity(surf, font)
    assert header_chars < len(analyzer.view_model()["text"])   # sanity: scroll IS engaged

    analyzer.tick(1000.0)
    frame_t = Surface(*GOLDEN_SURFACE_SIZE)
    app._paint_frame(frame_t, "eventlog", EMPTY_VM, font,
                      chrome.DEFAULT_STATUS_VM, chrome.DEFAULT_ALERTS_VM, chrome.DEFAULT_TIMESIG_VM,
                      chrome.DEFAULT_BEATFLASH_VM, chrome.DEFAULT_LOOPPROGRESS_VM, analyzer.view_model())

    analyzer.tick(1003.0)   # +3s * 4 chars/sec = +12 chars of genuine scroll
    frame_t_plus_n = Surface(*GOLDEN_SURFACE_SIZE)
    app._paint_frame(frame_t_plus_n, "eventlog", EMPTY_VM, font,
                      chrome.DEFAULT_STATUS_VM, chrome.DEFAULT_ALERTS_VM, chrome.DEFAULT_TIMESIG_VM,
                      chrome.DEFAULT_BEATFLASH_VM, chrome.DEFAULT_LOOPPROGRESS_VM, analyzer.view_model())

    header_h = font.height + 2 * app.HEADER_PAD
    header_t = frame_t.image.crop((0, 0, frame_t.width, header_h)).tobytes()
    header_t_plus_n = frame_t_plus_n.image.crop((0, 0, frame_t_plus_n.width, header_h)).tobytes()
    assert header_t != header_t_plus_n
    # Everything BELOW the header (body + chrome, none of which reads the
    # marquee) must be pixel-IDENTICAL between the two frames -- proves the
    # difference above is isolated to the marquee's own motion, not some
    # unrelated nondeterminism.
    body_t = frame_t.image.crop((0, header_h, frame_t.width, frame_t.height)).tobytes()
    body_t_plus_n = frame_t_plus_n.image.crop((0, header_h, frame_t_plus_n.width,
                                                frame_t_plus_n.height)).tobytes()
    assert body_t == body_t_plus_n


def test_render_frame_accent_color_is_brighter_than_normal():
    assert app.ACCENT_FG != app.NORMAL_FG
    assert sum(app.ACCENT_FG) > sum(app.NORMAL_FG)


def test_render_frame_golden_matches_frozen_fixture():
    # Phase-3 task 3: the golden now composes BOTH renderers, page body
    # (render_frame) + chrome status strip (_draw_status), the same way the
    # real run loops do -- "golden updates for both renderers, chrome now
    # present" (task-3 brief). Phase-3 task 6 re-froze it again to add the
    # secondary alerts/timesig strip (`_draw_secondary`); phase-3 task 9
    # re-froze it once more to add the third beatflash/loopprogress strip
    # (`_draw_beatprogress`) the run loops now also always paint.
    assert GOLDEN_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(VM, surf)
    app._draw_secondary(surf, GOLDEN_ALERTS_VM, GOLDEN_TIMESIG_VM, load_font())
    app._draw_status(surf, GOLDEN_STATUS_VM, load_font())
    app._draw_beatprogress(surf, GOLDEN_BEATFLASH_VM, GOLDEN_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_FRAME).convert("RGB")
    assert golden.size == GOLDEN_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


def _write_isolated_daemon_config(tmp_path) -> str:
    """Fix round, item 1 (subprocess config isolation): a `midicrt.daemon`
    subprocess started with no `--config` inherits the REAL
    `~/.config/midicrt/config.toml` -- on this box that file has
    `capture_auto_start = true`, a real, user-intended PRODUCTION setting,
    not a test default. `tests/conftest.py`'s repo-wide autouse isolation
    fixture (`_isolate_default_midicrt_config_paths`) cannot help here: it
    monkeypatches module attributes in THIS process, but `_start_daemon`
    spawns a genuinely separate OS process that re-imports `config.py`
    fresh -- see that fixture's own (now-corrected) docstring for the full
    explanation. Confirmed live: before this fix, every subprocess daemon
    this file spawns silently wrote a real (if tiny) session file into
    `/var/lib/midicrt/sessions` on every test run. Both keys are declared
    explicitly (not left to `Config()`'s own default, which happens to
    also be `capture_auto_start=False` today) so this test file's
    isolation is an explicit, auditable fact, not a coincidence that would
    silently break if that default ever changed.

    Phase 8 Task 4: also pins `header_scroll_speed_cps = 0.0` -- the header
    marquee (`analyzers/marquee.py`) is real wall-clock-driven motion by
    design (v1's primary anti-burn-in device), which makes its scroll
    OFFSET at the exact instant `--out` captures a frame a function of
    real subprocess-startup timing, not just the vm content -- exactly the
    kind of flakiness a byte-exact golden PNG test cannot tolerate. Pinning
    the speed to 0 here freezes the marquee at a fixed, deterministic
    slice (offset 0 -- the roster string's own first N characters) without
    disabling the marquee mechanism itself (still real `MarqueeAnalyzer`
    state, still real `overlay.marquee` wire traffic) -- the scroll-speed
    MATH itself is covered by `tests/test_analyzers_marquee.py`'s own
    dedicated mechanics tests, not this end-to-end pipeline smoke test."""
    config_path = tmp_path / "isolated-config.toml"
    config_path.write_text(
        "capture_auto_start = false\n"
        f'capture_dir = "{tmp_path / "sessions"}"\n'
        "header_scroll_speed_cps = 0.0\n"
    )
    return str(config_path)


def _start_daemon(sock, tmp_path):
    config_path = _write_isolated_daemon_config(tmp_path)
    p = subprocess.Popen(
        [VENVPY, "-m", "midicrt.daemon", "--socket", sock, "--no-midi", "--no-audio",
         "--config", config_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if subprocess.run(
            [VENVPY, "-m", "midicrt.clients.cli", "--socket", sock, "status"],
            capture_output=True, check=False).returncode == 0:
            return p
        time.sleep(0.1)
    p.terminate()
    raise RuntimeError("daemon did not come up")


def test_out_mode_renders_one_frame_and_exits_against_real_daemon(tmp_path):
    # A fresh `--no-midi` daemon's TransportAnalyzer never sees a start/
    # clock event, so this golden's status strip is the all-defaults idle
    # state (chrome.DEFAULT_STATUS_VM) -- the non-idle case is covered by
    # test_render_frame_golden_matches_frozen_fixture above.
    assert GOLDEN_EMPTY.exists(), (
        "golden fixture missing -- see freeze procedure in task-3-report.md"
    )
    sock = str(tmp_path / "ctl.sock")
    out_png = tmp_path / "frame.png"
    daemon = _start_daemon(sock, tmp_path)
    try:
        result = subprocess.run(
            [VENVPY, "-m", "midicrt.clients.fb.app",
             "--socket", sock, "--out", str(out_png), "--no-input"],
            capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        assert out_png.exists()
        got = Image.open(out_png).convert("RGB")
        golden = Image.open(GOLDEN_EMPTY).convert("RGB")
        assert got.size == golden.size == app.OUT_SIZE
        assert got.tobytes() == golden.tobytes()
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)


# -- voices page (phase-3 task 4) --------------------------------------------

GOLDEN_VOICES_FRAME = FIXTURES / "fb_voices_frame_golden.png"
VOICES_SURFACE_SIZE = (240, 228)   # header(12) + 16 rows*~11 + reserved chrome(36)

VOICES_ROWS = [
    {"ch": i, "name": f"Instr{i}", "active": 0, "peak": 0, "notes": []}
    for i in range(1, 17)
]
VOICES_ROWS[0] = {"ch": 1, "name": "Kawai XD5", "active": 3, "peak": 8, "notes": [60, 64, 67]}
VOICES_ROWS[2] = {"ch": 3, "name": "BassStaRack", "active": 8, "peak": 12, "notes": list(range(30, 38))}
VOICES_VM = {"title": "VOICES", "total": 11, "total_peak": 20, "rows": VOICES_ROWS}

VOICES_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
# Phase-3 task 6: no active alerts here -- exercises the secondary strip's
# "falls back to timesig" branch (the voices golden already covers the
# "alerts win" branch via the eventlog golden above).
VOICES_ALERTS_VM = {"alerts": []}
VOICES_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                      "events_window": 24, "events_total": 40, "pending": None}


def test_voices_renderers_dispatch_table_has_voices():
    assert app.RENDERERS["voices"] is app.render_voices_frame


def test_render_voices_frame_header_bar_is_reverse_video():
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._voices_header_text(VOICES_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_voices_frame_empty_rows_does_not_crash():
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame({"title": "VOICES", "total": 0, "total_peak": 0, "rows": []}, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert px[0, header_h + 2] == app.BG   # nothing drawn below the header


def test_render_voices_frame_draws_a_meter_box_outline_per_row():
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    row_h = usable_h // len(VOICES_ROWS)
    bar_x = app.LEFT_MARGIN + app.NAME_COL_CHARS * font.width + app.BAR_GAP
    for i in range(len(VOICES_ROWS)):
        row_y = header_h + i * row_h
        bar_y = row_y + app.ROW_PAD
        # top-left corner of the box outline for this row's meter.
        assert px[bar_x, bar_y] == app.NORMAL_FG


def test_render_voices_frame_truncates_long_instrument_names():
    # Review fix: `bar_x` is computed assuming the name column is bounded
    # to NAME_COL_CHARS -- a config-overridden name longer than that must
    # not paint glyph pixels into the gap before the meter box (or the box
    # itself), or the layout corrupts. The gap zone [bar_x-BAR_GAP, bar_x)
    # is pure background space with NO box/label content ever drawn there
    # (BAR_GAP is a deliberate clear buffer), so ANY non-background pixel
    # there can only be stray, untruncated name text.
    long_name_row = {"ch": 1, "name": "A" * 50, "active": 0, "peak": 0, "notes": []}
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame({"title": "VOICES", "total": 0, "total_peak": 0,
                             "rows": [long_name_row]}, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    row_h = usable_h // 1
    text_y = header_h + max(0, (row_h - font.height) // 2)
    bar_x = app.LEFT_MARGIN + app.NAME_COL_CHARS * font.width + app.BAR_GAP
    for x in range(bar_x - app.BAR_GAP, bar_x):
        for y in range(text_y, text_y + font.height):
            assert px[x, y] == app.BG, (
                f"stray name-text pixel at ({x},{y}) -- long name not truncated"
            )


def test_render_voices_frame_live_fill_reflects_active_count():
    # Channel 3 (index 2) is maxed at BAR_MAX active voices -> the topmost
    # interior row of its meter box must be filled; an idle channel's must
    # stay background.
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    row_h = usable_h // len(VOICES_ROWS)
    bar_x = app.LEFT_MARGIN + app.NAME_COL_CHARS * font.width + app.BAR_GAP

    maxed_row_y = header_h + 2 * row_h   # channel 3, active=8=BAR_MAX
    top_interior_y = maxed_row_y + app.ROW_PAD + 1
    assert px[bar_x + 1, top_interior_y] == app.ACCENT_FG

    idle_row_y = header_h + 3 * row_h   # channel 4, active=0
    idle_top_interior_y = idle_row_y + app.ROW_PAD + 1
    assert px[bar_x + 1, idle_top_interior_y] == app.BG


def test_render_voices_frame_peak_tick_visible_above_live_fill():
    # Channel 1: active=3, peak=8 (maxed scale) -- the peak tick must sit
    # ABOVE (smaller y than) the live fill's top edge.
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    row_h = usable_h // len(VOICES_ROWS)
    bar_x = app.LEFT_MARGIN + app.NAME_COL_CHARS * font.width + app.BAR_GAP

    row_y = header_h + 0 * row_h   # channel 1
    bar_y = row_y + app.ROW_PAD
    bar_h = row_h - 2 * app.ROW_PAD
    inner_h = bar_h - 2
    fill_h = round(inner_h * 3 / app.BAR_MAX)
    peak_h = round(inner_h * 8 / app.BAR_MAX)
    assert peak_h > fill_h   # sanity: this fixture actually exercises the tick-above-fill case
    peak_y = bar_y + bar_h - 1 - peak_h
    assert px[bar_x + 1, peak_y] == app.ACCENT_FG


def test_render_voices_frame_reserves_the_bottom_status_strip_as_background():
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    strip_h = app._status_strip_height(font)
    y_in_strip = surf.height - strip_h + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_voices_frame_golden_matches_frozen_fixture():
    assert GOLDEN_VOICES_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    app._draw_secondary(surf, VOICES_ALERTS_VM, VOICES_TIMESIG_VM, load_font())
    app._draw_status(surf, VOICES_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_VOICES_FRAME).convert("RGB")
    assert golden.size == VOICES_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- harmony page (phase-3 task 5) --------------------------------------------

GOLDEN_HARMONY_FRAME = FIXTURES / "fb_harmony_frame_golden.png"
HARMONY_SURFACE_SIZE = (420, 126)   # header(12) + 10 rows*9 + reserved chrome(36)

HARMONY_VM = {
    "title": "HARMONY",
    "chords": [
        {"name": "C maj", "conf": 1.0, "missing": []},
        {"name": "A m", "conf": None, "missing": []},
    ],
    "scales": [
        {"name": "C Ionian", "conf": 0.86, "missing": ["D"]},
        {"name": "A Aeolian 7", "conf": None, "missing": []},
    ],
    "inside": ["C", "E", "G"],
    "outside": ["C#"],
    "key": "C maj",
    "key_conf": 0.83,
    "key_alternatives": ["A min"],
    "tension": 0.65,
    "tension_label": "tense",
    "tension_worst_interval": "tritone",
    "harmonic_rhythm": {"changes_per_bar": 1.2, "label": "moderate"},
    "motif": {"found": True, "pattern": "+2 -1 +4", "count": 2},
    "silent": False,
}
HARMONY_EMPTY_VM = {
    "title": "HARMONY", "chords": [], "scales": [], "inside": [], "outside": [],
    "key": None, "key_conf": 0.0, "key_alternatives": [],
    "tension": 0.0, "tension_label": "silent", "tension_worst_interval": "",
    "harmonic_rhythm": {"changes_per_bar": None, "label": ""},
    "motif": {"found": False, "pattern": None, "count": 0},
    "silent": True,
}
HARMONY_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
# Phase-3 task 6: exercises the secondary strip's "pending change" text
# (clients/chrome.py's `timesig_text()` "-> ..." suffix) -- distinct
# coverage from the eventlog/voices goldens above.
HARMONY_ALERTS_VM = {"alerts": []}
HARMONY_TIMESIG_VM = {"labels": ["3/4"], "confidence": 0.5, "events": 12,
                       "events_window": 12, "events_total": 12, "pending": ["4/4"]}


def test_harmony_renderers_dispatch_table_has_harmony():
    assert app.RENDERERS["harmony"] is app.render_harmony_frame


def test_render_harmony_frame_header_bar_is_reverse_video():
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._harmony_header_text(HARMONY_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_harmony_frame_empty_state_does_not_crash():
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_EMPTY_VM, surf)  # must not raise
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert px[0, header_h + 2] == app.BG   # body area starts as background


def test_render_harmony_frame_draws_a_tension_bar_outline():
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    prefix_w = len("Tension:") * font.width
    bar_x = app.LEFT_MARGIN + prefix_w + app.BAR_GAP
    bar_y = header_h + 7 * line_h   # 7 text rows precede the tension row
    assert px[bar_x, bar_y] == app.NORMAL_FG          # top-left outline corner
    assert px[bar_x + app.HARMONY_TENSION_BAR_W - 1, bar_y] == app.NORMAL_FG


def test_render_harmony_frame_tension_fill_reflects_value():
    surf_low = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(
        {**HARMONY_VM, "tension": 0.1, "tension_label": "consonant"}, surf_low)
    surf_high = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_VM, surf_high)   # tension 0.65
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    prefix_w = len("Tension:") * font.width
    bar_x = app.LEFT_MARGIN + prefix_w + app.BAR_GAP
    bar_y = header_h + 7 * line_h
    inner_w = app.HARMONY_TENSION_BAR_W - 2
    # A pixel just past the low-tension fill's edge must be background for
    # the low-tension surface but filled (ACCENT_FG, since 0.65 > 0.5) for
    # the high-tension one -- proves the fill width tracks vm["tension"].
    probe_x = bar_x + 1 + round(inner_w * 0.3)
    probe_y = bar_y + 1
    assert surf_low.image.load()[probe_x, probe_y] == app.BG
    assert surf_high.image.load()[probe_x, probe_y] == app.ACCENT_FG


def test_render_harmony_frame_zero_tension_draws_no_fill():
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_EMPTY_VM, surf)   # tension == 0.0
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    prefix_w = len("Tension:") * font.width
    bar_x = app.LEFT_MARGIN + prefix_w + app.BAR_GAP
    bar_y = header_h + 7 * line_h
    px = surf.image.load()
    assert px[bar_x + 1, bar_y + 1] == app.BG


def test_render_harmony_frame_reserves_the_bottom_status_strip_as_background():
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_VM, surf)
    px = surf.image.load()
    font = load_font()
    strip_h = app._status_strip_height(font)
    y_in_strip = surf.height - strip_h + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_harmony_frame_golden_matches_frozen_fixture():
    assert GOLDEN_HARMONY_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*HARMONY_SURFACE_SIZE)
    app.render_harmony_frame(HARMONY_VM, surf)
    app._draw_secondary(surf, HARMONY_ALERTS_VM, HARMONY_TIMESIG_VM, load_font())
    app._draw_status(surf, HARMONY_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_HARMONY_FRAME).convert("RGB")
    assert golden.size == HARMONY_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- tuner page (phase-3 task 6) ----------------------------------------------

GOLDEN_TUNER_FRAME = FIXTURES / "fb_tuner_frame_golden.png"
TUNER_SURFACE_SIZE = (560, 66)   # header(12) + 2 rows*9 + reserved chrome(36)

TUNER_IDLE_VM = {"title": "TUNER", "note": "", "cents": 0.0, "hz": 0.0,
                 "confidence": 0.0, "db": -120.0, "has_signal": False}
TUNER_LOCKED_VM = {"title": "TUNER", "note": "A4", "cents": -3.2, "hz": 439.2,
                   "confidence": 0.82, "db": -18.4, "has_signal": True}
TUNER_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
TUNER_ALERTS_VM = {"alerts": []}
TUNER_TIMESIG_VM = {"labels": [], "confidence": 0.0, "events": 0,
                     "events_window": 0, "events_total": 0, "pending": None}


def test_tuner_renderers_dispatch_table_has_tuner():
    assert app.RENDERERS["tuner"] is app.render_tuner_frame


def test_render_tuner_frame_header_bar_is_reverse_video():
    surf = Surface(*TUNER_SURFACE_SIZE)
    app.render_tuner_frame(TUNER_LOCKED_VM, surf)
    px = surf.image.load()
    font = load_font()
    text_px_end = app.LEFT_MARGIN + len(app._tuner_header_text(TUNER_LOCKED_VM)) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_tuner_frame_idle_state_draws_only_one_row():
    surf = Surface(*TUNER_SURFACE_SIZE)
    app.render_tuner_frame(TUNER_IDLE_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    # Second body row (the "Tuning:" meter when locked) stays background.
    assert px[app.LEFT_MARGIN, header_h + line_h] == app.BG


def test_render_tuner_frame_locked_state_draws_note_and_meter_rows():
    surf = Surface(*TUNER_SURFACE_SIZE)
    app.render_tuner_frame(TUNER_LOCKED_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    # A lit glyph pixel exists on both text rows (not just background).
    assert any(px[x, header_h + 2] == app.NORMAL_FG for x in range(app.LEFT_MARGIN, surf.width))
    assert any(px[x, header_h + line_h + 2] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_tuner_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*TUNER_SURFACE_SIZE)
    app.render_tuner_frame(TUNER_LOCKED_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_tuner_frame_golden_matches_frozen_fixture():
    assert GOLDEN_TUNER_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*TUNER_SURFACE_SIZE)
    app.render_tuner_frame(TUNER_LOCKED_VM, surf)
    app._draw_secondary(surf, TUNER_ALERTS_VM, TUNER_TIMESIG_VM, load_font())
    app._draw_status(surf, TUNER_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_TUNER_FRAME).convert("RGB")
    assert golden.size == TUNER_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- pianoroll page (phase-3 task 7) ------------------------------------------
#
# Same FIXED synthetic note set as tests/test_tui_render.py's pianoroll
# section (three notes spread across pitch rows/velocity tiers/an active-
# vs-closed span), exercised in wallclock mode -- the renderer only ever
# reads already-projected x0/x1/y/vel floats (clients/fb/app.py's own
# module comment), so a single golden proves the pixel-roll geometry/
# color mapping; the mode-specific squish/stretch MATH itself is already
# covered by tests/test_pages_pianoroll.py's tempo-mode tests.
GOLDEN_PIANOROLL_FRAME = FIXTURES / "fb_pianoroll_frame_golden.png"
GOLDEN_PIANOROLL_TEMPO_FRAME = FIXTURES / "fb_pianoroll_tempo_frame_golden.png"
PIANOROLL_SURFACE_SIZE = (420, 144)   # header(12) + usable(96, 13 pitch rows) + chrome(36)

PIANOROLL_NOTES = [
    {"ch": 1, "y": 0.0, "x0": 0.1, "x1": 0.9, "vel": 1.0, "active": False},
    {"ch": 2, "y": 0.5, "x0": 0.4, "x1": 0.6, "vel": 0.5, "active": False},
    {"ch": 3, "y": 1.0, "x0": 0.7, "x1": 1.0, "vel": 0.2, "active": True},
]
# Phase 8 Task 3: `range.lo=60`/`hi=72` (13 semitones) -- `y` fractions here
# are k/12 for k=0..12, top-down, EXACTLY matching pages/pianoroll.py's own
# `_y(pitch)` for pitch=72..60 -- so PIANOROLL_NOTES' 0.0/0.5/1.0 rows land
# on pitch 72 (C5), 66 (F#4), 60 (C4) respectively, letting the "active row"
# tests below assert against real note names instead of synthetic ones.
_PIANOROLL_PITCH_NAMES = [
    "C5", "B4", "A#4", "A4", "G#4", "G4", "F#4", "F4", "E4", "D#4", "D4", "C#4", "C4",
]
PIANOROLL_GRID = {
    "beat_xs": [0.2, 0.3, 0.65, 0.95],
    "bar_xs": [0.5],
    "pitch_guide_ys": [
        {"y": k / 12, "is_c": (72 - k) % 12 == 0, "pitch": 72 - k, "name": name}
        for k, name in enumerate(_PIANOROLL_PITCH_NAMES)
    ],
    "running": True,
}
PIANOROLL_VM = {
    "title": "PIANOROLL",
    "notes": PIANOROLL_NOTES,
    "window": {"mode": "wallclock", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
    "range": {"lo": 60, "hi": 72},
    "grid": PIANOROLL_GRID,
}
# Same synthetic geometry, "tempo" window metadata -- the renderer only ever
# consumes already-projected x0/x1/y/vel (see the module comment above
# render_pianoroll_frame), so this proves the HEADER text tracks
# `window.mode` on the fb path too (mirrors tests/test_tui_render.py's own
# GOLDEN_PIANOROLL_WALLCLOCK_FRAME/GOLDEN_PIANOROLL_TEMPO_FRAME split) --
# body pixels are expected to be identical to the wallclock golden, only
# the header row differs.
PIANOROLL_VM_TEMPO = {
    **PIANOROLL_VM,
    "window": {"mode": "tempo", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
}
PIANOROLL_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
PIANOROLL_ALERTS_VM = {"alerts": []}
PIANOROLL_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                         "events_window": 24, "events_total": 40, "pending": None}


def test_pianoroll_renderers_dispatch_table_has_pianoroll():
    assert app.RENDERERS["pianoroll"] is app.render_pianoroll_frame


def test_render_pianoroll_frame_header_bar_is_reverse_video():
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._pianoroll_header_text(PIANOROLL_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_pianoroll_frame_empty_notes_does_not_crash():
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame({**PIANOROLL_VM, "notes": []}, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert px[0, header_h + 2] == app.BG   # nothing drawn below the header


def test_render_pianoroll_frame_draws_a_rect_per_note_in_its_velocity_color():
    # Renamed (Phase 8 Task 2): this used to be "...its_channel_color" --
    # channel no longer drives hue at all under the monochrome mandate, see
    # test_roll_note_color_is_uniform_hue_across_channels above. The test
    # body itself was already channel-agnostic (it just calls the real
    # _roll_note_color function rather than duplicating a formula), so no
    # assertion changes, only the name catching up to what it actually
    # verifies now.
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    # Phase 8 Task 4: the roll body no longer starts right below the header
    # -- the new "Bars" timeline strip sits between them.
    roll_top = header_h + app._pianoroll_bars_strip_height(font)
    usable_h = surf.height - app._reserved_chrome_height(font) - roll_top
    # Phase 8 Task 3: the roll body no longer starts at x=0 -- the label
    # column (app.PIANOROLL_LABEL_MARGIN_CHARS wide -- v1's margin, one
    # char wider than the label text itself) now sits to its left.
    roll_x0 = app.PIANOROLL_LABEL_MARGIN_CHARS * font.width
    roll_w = surf.width - roll_x0
    note = PIANOROLL_NOTES[0]   # ch1, y=0.0, loud -- top-left-most rect
    x = roll_x0 + round(note["x0"] * roll_w) + 2
    y = roll_top + round(note["y"] * usable_h) + 1
    assert px[x, y] == app._roll_note_color(note["ch"], note["vel"])


def test_roll_note_color_velocity_floor_matches_v1_constant():
    # Phase 8 Task 2 (monochrome conversion): v1's real CRT compositor
    # floors velocity brightness at 50% (`_VEL_BRIGHTNESS_FLOOR = 0.5`,
    # docs/visual-audit.md §9c) rather than going fully dark at vel=0 --
    # ported here via clients/fb/lum.py's RAMPS["pianoroll"].
    from midicrt.clients.fb.lum import RAMPS, lum

    dim = app._roll_note_color(1, 0.0)
    bright = app._roll_note_color(1, 1.0)
    assert bright == app.ACCENT_FG   # == LUM_BRIGHT, full velocity
    assert dim == lum(RAMPS["pianoroll"]["velocity_floor"])
    assert sum(dim) < sum(bright)


def test_roll_note_color_is_uniform_hue_across_channels():
    # Monochrome mandate (2026-08-08 gui-phase-decisions doc ruling #1):
    # channel identity is NOT encoded as hue any more -- a later task (T3,
    # per this task's own brief) moves it to a per-pitch label column
    # instead. This replaces the old test_roll_note_color_cycles_by_channel,
    # which asserted the very rainbow-cycling behavior this task removes.
    colors = {app._roll_note_color(ch, 0.8) for ch in range(1, 17)}
    assert len(colors) == 1


def test_render_pianoroll_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


# -- paper grid + label column (Phase 8 Task 3, docs/visual-audit.md §9c) --
#
# Geometry shared by the tests below: PIANOROLL_SURFACE_SIZE=(420, 144),
# PIANOROLL_LABEL_MARGIN_CHARS=10 * font.width(8) = 80px label column (v1's
# own margin -- one char wider than the 9-char label text itself, review
# fix), so roll_x0=80, roll_w=340. `roll_top` (Phase 8 Task 4: header_h +
# the new "Bars" strip height, NOT just header_h any more -- see
# render_pianoroll_frame's own module comment) is the actual y-origin of
# the pitch-guide roll body. usable_h/pitch_span=13 -> note_h/row_span_h
# below it. Only guide rows 0 (C5), 6 (F#4), 12 (C4) have a note in
# PIANOROLL_NOTES (y=0.0/0.5/1.0) -- every other row is a clean probe point
# for "is the grid visible with nothing drawn over it."

def _pianoroll_layout():
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    bars_strip_h = app._pianoroll_bars_strip_height(font)
    roll_top = header_h + bars_strip_h
    usable_h = PIANOROLL_SURFACE_SIZE[1] - app._reserved_chrome_height(font) - roll_top
    note_h = usable_h // 13
    row_span_h = usable_h - note_h
    roll_x0 = app.PIANOROLL_LABEL_MARGIN_CHARS * font.width
    roll_w = PIANOROLL_SURFACE_SIZE[0] - roll_x0
    return font, roll_top, usable_h, note_h, row_span_h, roll_x0, roll_w


def test_render_pianoroll_frame_draws_dotted_horizontal_pitch_row_guide():
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, _roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    # Separator at the TOP of guide row 1 ("B4", not-C, no note on this row)
    # -- phase = 1 & 1 = 1, stride 4, so a dot sits at roll_x0 + 1.
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    y = roll_top + round(guide1["y"] * row_span_h)
    assert px[roll_x0 + 1, y] == app.LUM_FAINT


def test_render_pianoroll_frame_c_row_guide_is_brighter():
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, _roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    # Separator at the TOP of guide row 12 ("C4", is_c -- x=76 is not under
    # note2's rect, which only covers x>=316 at this row).
    guide12 = PIANOROLL_GRID["pitch_guide_ys"][12]
    y = roll_top + round(guide12["y"] * row_span_h)
    assert px[roll_x0 + 4, y] == app.LUM_FAINT_C


def test_render_pianoroll_frame_draws_dotted_beat_and_bar_verticals():
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    # beat_x=0.3 at guide row 1 ("B4") -- no note ever touches that row, so
    # this probes the guide's dotted vertical column undisturbed.
    x_beat = roll_x0 + round(0.3 * roll_w)
    y_row1 = roll_top + round(PIANOROLL_GRID["pitch_guide_ys"][1]["y"] * row_span_h)
    assert px[x_beat, y_row1 + 2] == app.LUM_FAINT   # a stride-3 dot from roll_top (Phase 8 Task 4: now header_h + the Bars strip height)
    # bar_x=0.5 at the same clean row -- brighter tier.
    x_bar = roll_x0 + round(0.5 * roll_w)
    assert px[x_bar, y_row1 + 2] == app.LUM_BAR_GUIDE


def test_render_pianoroll_frame_grid_is_drawn_under_notes_not_over_them():
    # Proves draw order: bar_x=0.5 lands inside note[0]'s rect both
    # horizontally (x0=0.1..x1=0.9) and vertically (its own row) -- if the
    # grid were drawn AFTER notes, this pixel would show LUM_BAR_GUIDE
    # instead of the note's own velocity color.
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    note0 = PIANOROLL_NOTES[0]
    x_bar = roll_x0 + round(0.5 * roll_w)
    y = roll_top + round(note0["y"] * row_span_h) + 3   # +3: inside the note_h=7 tall rect
    assert x_bar not in (0,)  # sanity: a real column, not a degenerate probe
    assert px[x_bar, y] == app._roll_note_color(note0["ch"], note0["vel"])
    assert px[x_bar, y] != app.LUM_BAR_GUIDE


def test_render_pianoroll_frame_label_column_non_c_row_is_dim():
    font, roll_top, _usable_h, note_h, row_span_h, _roll_x0, _roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]   # "B4", not-C, no note
    y = roll_top + round(guide1["y"] * row_span_h)
    label = app._pianoroll_label_text(guide1)
    text_px_end = len(label) * font.width
    assert any(
        px[x, yy] == app.LUM_DIM
        for x in range(text_px_end)
        for yy in range(y, y + note_h)
    )
    assert not any(
        px[x, yy] == app.LUM_BRIGHT
        for x in range(text_px_end)
        for yy in range(y, y + note_h)
    )


def test_render_pianoroll_frame_label_column_c_row_is_bright():
    # Both C rows in PIANOROLL_GRID (0 and 12) also have a note sitting on
    # them, so they exercise the ACTIVE invert (covered by its own test
    # below), not the plain static C-row brightness this test wants in
    # isolation -- hence a synthetic single-row, no-notes grid instead of
    # reusing PIANOROLL_GRID.
    font, roll_top, _usable_h, note_h, _row_span_h, _roll_x0, _roll_w = _pianoroll_layout()
    lone_guide = {"y": 0.0, "is_c": True, "pitch": 84, "name": "C6"}
    vm = {**PIANOROLL_VM, "notes": [], "range": {"lo": 84, "hi": 84},
          "grid": {**PIANOROLL_GRID, "pitch_guide_ys": [lone_guide]}}
    surf2 = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf2)
    px2 = surf2.image.load()
    label = app._pianoroll_label_text(lone_guide)
    text_px_end = len(label) * font.width
    usable_h2 = PIANOROLL_SURFACE_SIZE[1] - app._reserved_chrome_height(font) - roll_top
    assert any(
        px2[x, yy] == app.LUM_BRIGHT
        for x in range(text_px_end)
        for yy in range(roll_top, roll_top + min(usable_h2, note_h))
    )


def test_render_pianoroll_frame_label_column_active_pitch_is_inverted():
    _font, roll_top, _usable_h, _note_h, row_span_h, _roll_x0, _roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    # Guide row 6 ("F#4") matches note[1]'s y=0.5 exactly -- active, so its
    # label cell is LUM_MID-filled (invert) rather than left as background
    # in the leading-space region (x=0..3, well before any glyph ink).
    guide6 = PIANOROLL_GRID["pitch_guide_ys"][6]
    y = roll_top + round(guide6["y"] * row_span_h)
    assert px[2, y + 1] == app.LUM_MID
    # A non-active row's same leading-space probe stays untouched background.
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    y1 = roll_top + round(guide1["y"] * row_span_h)
    assert px[2, y1 + 1] == app.BG


# -- active-row tint + fade (Phase 8 Task 4, docs/visual-audit.md §9c) -----

def test_render_pianoroll_frame_draws_row_tint_at_full_intensity():
    _font, roll_top, _usable_h, note_h, row_span_h, roll_x0, _roll_w = _pianoroll_layout()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]   # "B4" -- clean row, no note
    vm = {**PIANOROLL_VM, "row_tint": [{"y": guide1["y"], "intensity": 1.0}]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    px = surf.image.load()
    y = roll_top + round(guide1["y"] * row_span_h)
    from midicrt.clients.fb.lum import RAMPS, lum

    peak = RAMPS["pianoroll_row_tint"]["peak"]
    # roll_x0+2 avoids the row's own dotted-guide dot columns (phase 1,
    # stride 4 -- dots sit at roll_x0+1, +5, +9, ... per the dotted-guide
    # test above), so this probes the PURE tint fill.
    assert px[roll_x0 + 2, y + note_h // 2] == lum(peak * 1.0)


def test_render_pianoroll_frame_row_tint_intensity_scales_brightness():
    _font, roll_top, _usable_h, note_h, row_span_h, roll_x0, _roll_w = _pianoroll_layout()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    from midicrt.clients.fb.lum import RAMPS, lum

    peak = RAMPS["pianoroll_row_tint"]["peak"]
    y = roll_top + round(guide1["y"] * row_span_h)

    vm_half = {**PIANOROLL_VM, "row_tint": [{"y": guide1["y"], "intensity": 0.5}]}
    surf_half = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_half, surf_half)
    assert surf_half.image.load()[roll_x0 + 2, y + note_h // 2] == lum(peak * 0.5)

    vm_full = {**PIANOROLL_VM, "row_tint": [{"y": guide1["y"], "intensity": 1.0}]}
    surf_full = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_full, surf_full)
    full_color = surf_full.image.load()[roll_x0 + 2, y + note_h // 2]
    half_color = surf_half.image.load()[roll_x0 + 2, y + note_h // 2]
    assert sum(full_color) > sum(half_color) > 0


def test_render_pianoroll_frame_row_tint_drawn_under_the_dotted_grid():
    # roll_x0+1 IS a dot column for guide row 1 (phase 1&1=1, stride 4 --
    # same position the dotted-guide test above already proves is
    # LUM_FAINT with NO tint present). With a full-intensity tint added
    # underneath, the grid dot must still win -- draw order is tint, THEN
    # grid, matching v1's own layering (module comment in clients/fb/app.py).
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, _roll_w = _pianoroll_layout()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    vm = {**PIANOROLL_VM, "row_tint": [{"y": guide1["y"], "intensity": 1.0}]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    y = roll_top + round(guide1["y"] * row_span_h)
    assert surf.image.load()[roll_x0 + 1, y] == app.LUM_FAINT


def test_render_pianoroll_frame_row_tint_drawn_under_notes():
    # guide row 12 ("C4") has note[2] (PIANOROLL_NOTES) covering x0=0.7..1.0
    # -- a tint on that SAME row must not visually cover the note's own
    # velocity color where the note actually is.
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    guide12 = PIANOROLL_GRID["pitch_guide_ys"][12]
    vm = {**PIANOROLL_VM, "row_tint": [{"y": guide12["y"], "intensity": 1.0}]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    note2 = PIANOROLL_NOTES[2]
    x = roll_x0 + round(note2["x0"] * roll_w) + 2
    y = roll_top + round(note2["y"] * row_span_h) + 1
    assert surf.image.load()[x, y] == app._roll_note_color(note2["ch"], note2["vel"])


def test_render_pianoroll_frame_row_tint_absent_key_renders_unchanged():
    # Defensive .get(..., []) -- an older/synthetic vm with no "row_tint"
    # key at all (every pre-Phase-8-Task-4 fixture) must render identically
    # to one with an explicit empty list.
    surf_missing = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf_missing)
    surf_empty = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame({**PIANOROLL_VM, "row_tint": []}, surf_empty)
    assert surf_missing.image.tobytes() == surf_empty.image.tobytes()


# -- overlap flash (Phase 8 Task 4, docs/visual-audit.md §9c) ---------------

def test_render_pianoroll_frame_draws_overlap_flash_note_colored_phase():
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]   # clean row, no note
    region = {"y": guide1["y"], "x0": 0.1, "x1": 0.3, "ch": 5, "vel": 0.9}
    vm = {**PIANOROLL_VM, "overlap_flash": [region]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    y = roll_top + round(guide1["y"] * row_span_h) + 1
    x = roll_x0 + round(0.2 * roll_w)
    assert surf.image.load()[x, y] == app._roll_note_color(5, 0.9)


def test_render_pianoroll_frame_draws_overlap_flash_blink_to_bg_phase():
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    region = {"y": guide1["y"], "x0": 0.1, "x1": 0.3, "ch": None, "vel": None}
    vm = {**PIANOROLL_VM, "overlap_flash": [region]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    y = roll_top + round(guide1["y"] * row_span_h) + 1
    x = roll_x0 + round(0.2 * roll_w)
    assert surf.image.load()[x, y] == app.BG


def test_render_pianoroll_frame_overlap_flash_drawn_over_plain_notes():
    # note[1] (PIANOROLL_NOTES: ch2, y=0.5, x0=0.4..0.6) drawn as usual,
    # then a BG-phase overlap-flash region right on top of it -- proves
    # flash paints AFTER (on top of) the plain note rects, v1's own "last
    # pass" layering.
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    note1 = PIANOROLL_NOTES[1]
    region = {"y": note1["y"], "x0": 0.45, "x1": 0.55, "ch": None, "vel": None}
    vm = {**PIANOROLL_VM, "overlap_flash": [region]}
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm, surf)
    x = roll_x0 + round(0.5 * roll_w)
    y = roll_top + round(note1["y"] * row_span_h) + 1
    assert surf.image.load()[x, y] == app.BG
    # Sanity: WITHOUT the flash region, that same pixel is the note's color.
    surf_plain = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf_plain)
    assert surf_plain.image.load()[x, y] == app._roll_note_color(note1["ch"], note1["vel"])


def test_render_pianoroll_frame_overlap_flash_absent_key_renders_unchanged():
    surf_missing = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf_missing)
    surf_empty = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame({**PIANOROLL_VM, "overlap_flash": []}, surf_empty)
    assert surf_missing.image.tobytes() == surf_empty.image.tobytes()


# -- the "Bars" solid timeline strip (Phase 8 Task 4, docs/visual-audit.md §9c) --
#
# Distinct from the DOTTED backdrop guides tested above: one solid-color
# row between the header and the pitch-guide roll body.

def test_render_pianoroll_frame_bars_strip_draws_label():
    font, roll_top, _usable_h, _note_h, _row_span_h, _roll_x0, _roll_w = _pianoroll_layout()
    header_h = font.height + 2 * app.HEADER_PAD
    strip_h = app._pianoroll_bars_strip_height(font)
    assert roll_top == header_h + strip_h   # sanity: the strip is really reserved
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    label = f"{'Bars':>7} │"
    label_px_end = len(label) * font.width
    assert any(
        px[x, y] == app.LUM_DIM
        for x in range(label_px_end)
        for y in range(header_h, roll_top)
    )


def test_render_pianoroll_frame_bars_strip_draws_solid_bar_and_beat_ticks():
    _font, roll_top, _usable_h, _note_h, _row_span_h, roll_x0, roll_w = _pianoroll_layout()
    header_h = roll_top - app._pianoroll_bars_strip_height(load_font())
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    # bar_x=0.5 -- SOLID (every pixel down the strip), not dotted like the
    # roll-body guide at the same x fraction.
    x_bar = roll_x0 + round(0.5 * roll_w)
    for y in range(header_h, roll_top):
        assert px[x_bar, y] == app.LUM_MID
    # beat_x=0.3 -- solid LUM_DIM.
    x_beat = roll_x0 + round(0.3 * roll_w)
    for y in range(header_h, roll_top):
        assert px[x_beat, y] == app.LUM_DIM


def test_render_pianoroll_frame_bars_strip_ticks_do_not_extend_into_roll_body():
    # The Bars strip's solid ticks are confined to ITS OWN strip height --
    # the roll body below it gets the DOTTED guide instead (drawn by
    # _draw_pianoroll_grid, a completely separate call).
    _font, roll_top, _usable_h, _note_h, row_span_h, roll_x0, roll_w = _pianoroll_layout()
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    px = surf.image.load()
    x_bar = roll_x0 + round(0.5 * roll_w)
    # A few pixels into the roll body, at a clean (no-note) row -- must NOT
    # be the strip's own SOLID LUM_MID (the roll body only ever gets the
    # dotted LUM_BAR_GUIDE tier at this x).
    guide1 = PIANOROLL_GRID["pitch_guide_ys"][1]
    y_in_body = roll_top + round(guide1["y"] * row_span_h) + 2
    assert px[x_bar, y_in_body] != app.LUM_MID


def test_render_pianoroll_frame_bars_strip_reuses_grid_bar_and_beat_xs_verbatim():
    # No engine-side change needed for this strip (pages/pianoroll.py's own
    # module docstring) -- an empty bar_xs/beat_xs must draw NO ticks at all.
    empty_grid_vm = {**PIANOROLL_VM, "grid": {**PIANOROLL_GRID, "bar_xs": [], "beat_xs": []}}
    font, roll_top, _usable_h, _note_h, _row_span_h, _roll_x0, _roll_w = _pianoroll_layout()
    header_h = roll_top - app._pianoroll_bars_strip_height(font)
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(empty_grid_vm, surf)
    px = surf.image.load()
    label_px_end = len(f"{'Bars':>7} │") * font.width
    for x in range(label_px_end, surf.width):
        for y in range(header_h, roll_top):
            assert px[x, y] == app.BG


def test_render_pianoroll_frame_golden_matches_frozen_fixture():
    assert GOLDEN_PIANOROLL_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM, surf)
    app._draw_secondary(surf, PIANOROLL_ALERTS_VM, PIANOROLL_TIMESIG_VM, load_font())
    app._draw_status(surf, PIANOROLL_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_PIANOROLL_FRAME).convert("RGB")
    assert golden.size == PIANOROLL_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


def test_render_pianoroll_frame_golden_matches_frozen_fixture_in_tempo_mode():
    assert GOLDEN_PIANOROLL_TEMPO_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(PIANOROLL_VM_TEMPO, surf)
    app._draw_secondary(surf, PIANOROLL_ALERTS_VM, PIANOROLL_TIMESIG_VM, load_font())
    app._draw_status(surf, PIANOROLL_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_PIANOROLL_TEMPO_FRAME).convert("RGB")
    assert golden.size == PIANOROLL_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


def test_pianoroll_tempo_and_wallclock_goldens_share_identical_body_pixels():
    # The renderer only ever consumes already-projected coordinates -- see
    # module comment above render_pianoroll_frame -- so the two golden
    # fixtures must differ ONLY in the header row's text (mode string),
    # never in the note-rect body below it.
    wallclock = Image.open(GOLDEN_PIANOROLL_FRAME).convert("RGB")
    tempo = Image.open(GOLDEN_PIANOROLL_TEMPO_FRAME).convert("RGB")
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    box = (0, header_h, PIANOROLL_SURFACE_SIZE[0], PIANOROLL_SURFACE_SIZE[1])
    assert wallclock.crop(box).tobytes() == tempo.crop(box).tobytes()


# -- spectrum page (phase-3 task 8) -------------------------------------------

GOLDEN_SPECTRUM_FRAME = FIXTURES / "fb_spectrum_frame_golden.png"
SPECTRUM_SURFACE_SIZE = (300, 128)   # header(12) + usable(80) + reserved chrome(36)

SPECTRUM_VM = {
    "title": "SPECTRUM", "available": True, "device": "USB Audio Device",
    "bins": [0.0, 0.25, 0.5, 0.75, 1.0, 0.5, 0.25, 0.0],
    "peak_hold": [0.1, 0.4, 0.6, 0.9, 1.0, 0.7, 0.4, 0.1],
}
SPECTRUM_IDLE_VM = {
    "title": "SPECTRUM", "available": False, "device": None,
    "bins": [0.0] * 8, "peak_hold": [0.0] * 8,
}
SPECTRUM_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
SPECTRUM_ALERTS_VM = {"alerts": []}
SPECTRUM_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                        "events_window": 24, "events_total": 40, "pending": None}


def test_spectrum_renderers_dispatch_table_has_spectrum():
    assert app.RENDERERS["spectrum"] is app.render_spectrum_frame


def test_render_spectrum_frame_header_bar_is_reverse_video():
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._spectrum_header_text(SPECTRUM_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG
    assert any(
        px[x, y] == app.BG
        for x in range(app.LEFT_MARGIN, text_px_end)
        for y in range(app.HEADER_PAD, app.HEADER_PAD + font.height)
    )


def test_render_spectrum_frame_idle_shows_no_audio_input_placeholder():
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_IDLE_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    # A lit glyph pixel exists somewhere in the placeholder text row.
    assert any(px[x, header_h + app.LINE_GAP + 2] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))
    # No bars anywhere further down -- the whole body below stays background.
    assert px[app.LEFT_MARGIN, surf.height - app._reserved_chrome_height(font) - 1] == app.BG


def test_render_spectrum_frame_empty_bins_does_not_crash():
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame({**SPECTRUM_VM, "bins": [], "peak_hold": []}, surf)  # must not raise


def test_render_spectrum_frame_tallest_bin_reaches_the_full_usable_height():
    # Bin 4 has both bins==1.0 and peak_hold==1.0 (a bar currently sitting
    # at its own peak) -- the peak tick's ACCENT_FG cap is drawn LAST and
    # lands on the exact same top row as the full-height live fill, so the
    # very top pixel reads as the (correct, intentional) accent cap; one
    # row down is unambiguously the live fill's own NORMAL_FG.
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    plot_w = surf.width - 2 * app.LEFT_MARGIN
    n = len(SPECTRUM_VM["bins"])
    col_w = max(1, plot_w // n)
    peak_idx = max(range(n), key=lambda i: SPECTRUM_VM["bins"][i])   # bin index 4, val 1.0
    x = app.LEFT_MARGIN + peak_idx * col_w + 1
    assert px[x, header_h] == app.ACCENT_FG                  # peak-at-own-cap tick on top
    assert px[x, header_h + 1] == app.NORMAL_FG              # live fill immediately below it
    assert px[x, header_h + usable_h - 1] == app.NORMAL_FG   # bottom row too


def test_render_spectrum_frame_silent_bin_draws_no_fill():
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    plot_w = surf.width - 2 * app.LEFT_MARGIN
    n = len(SPECTRUM_VM["bins"])
    col_w = max(1, plot_w // n)
    zero_idx = 0   # bin 0, val 0.0
    x = app.LEFT_MARGIN + zero_idx * col_w + 1
    assert px[x, header_h + 2] == app.BG


def test_render_spectrum_frame_peak_tick_visible_above_live_fill():
    # Bin 1: level=0.25, peak=0.4 -- the peak tick must sit strictly above
    # (smaller y than) the live fill's own top edge.
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    plot_w = surf.width - 2 * app.LEFT_MARGIN
    n = len(SPECTRUM_VM["bins"])
    col_w = max(1, plot_w // n)
    idx = 1
    x = app.LEFT_MARGIN + idx * col_w + 1
    fill_h = round(usable_h * SPECTRUM_VM["bins"][idx])
    peak_h = round(usable_h * SPECTRUM_VM["peak_hold"][idx])
    assert peak_h > fill_h   # sanity: this fixture actually exercises the tick-above-fill case
    fill_top_y = header_h + usable_h - fill_h
    peak_y = header_h + usable_h - peak_h
    assert peak_y < fill_top_y
    assert px[x, peak_y] == app.ACCENT_FG
    assert px[x, fill_top_y] == app.NORMAL_FG


def test_render_spectrum_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_spectrum_frame_golden_matches_frozen_fixture():
    assert GOLDEN_SPECTRUM_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-9-report.md"
    )
    surf = Surface(*SPECTRUM_SURFACE_SIZE)
    app.render_spectrum_frame(SPECTRUM_VM, surf)
    app._draw_secondary(surf, SPECTRUM_ALERTS_VM, SPECTRUM_TIMESIG_VM, load_font())
    app._draw_status(surf, SPECTRUM_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_SPECTRUM_FRAME).convert("RGB")
    assert golden.size == SPECTRUM_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# A tiny hand-picked 2x2 grid (decoupled from the real analyzer's wave math,
# same style as SPECTRUM_VM above) exercises the per-cell `rect` fill,
# scaled up to fill the usable body area -- see
# analyzers/img2txtviz.py's/clients/fb/app.py's own module comments.
GOLDEN_IMG2TXTVIZ_FRAME = FIXTURES / "fb_img2txtviz_frame_golden.png"
IMG2TXTVIZ_SURFACE_SIZE = (300, 128)   # same fixed size as SPECTRUM_SURFACE_SIZE

IMG2TXTVIZ_VM = {
    "title": "IMG2TXT", "active_notes": 3, "invert": False,
    "grid": [[0.0, 1.0], [0.5, 0.25]],
}
IMG2TXTVIZ_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
IMG2TXTVIZ_ALERTS_VM = {"alerts": []}
IMG2TXTVIZ_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                          "events_window": 24, "events_total": 40, "pending": None}


def test_img2txtviz_renderers_dispatch_table_has_img2txtviz():
    assert app.RENDERERS["img2txtviz"] is app.render_img2txtviz_frame


def test_render_img2txtviz_frame_header_bar_is_reverse_video():
    surf = Surface(*IMG2TXTVIZ_SURFACE_SIZE)
    app.render_img2txtviz_frame(IMG2TXTVIZ_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._img2txtviz_header_text(IMG2TXTVIZ_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_img2txtviz_frame_header_shows_invert_flag_only_when_set():
    assert "INV" not in app._img2txtviz_header_text(IMG2TXTVIZ_VM)
    inverted = {**IMG2TXTVIZ_VM, "invert": True}
    assert "INV" in app._img2txtviz_header_text(inverted)


def test_render_img2txtviz_frame_cell_colors_scale_by_value():
    surf = Surface(*IMG2TXTVIZ_SURFACE_SIZE)
    app.render_img2txtviz_frame(IMG2TXTVIZ_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    cell_w = surf.width // 2
    cell_h = usable_h // 2
    # (row 0, col 0) = 0.0 -> pure background (black).
    assert px[cell_w // 2, header_h + cell_h // 2] == app.BG
    # (row 0, col 1) = 1.0 -> full ACCENT_FG.
    assert px[cell_w + cell_w // 2, header_h + cell_h // 2] == app.ACCENT_FG
    # (row 1, col 0) = 0.5 -> ACCENT_FG scaled by 0.5.
    expected_half = tuple(round(c * 0.5) for c in app.ACCENT_FG)
    assert px[cell_w // 2, header_h + cell_h + cell_h // 2] == expected_half
    # (row 1, col 1) = 0.25 -> ACCENT_FG scaled by 0.25.
    expected_quarter = tuple(round(c * 0.25) for c in app.ACCENT_FG)
    assert px[cell_w + cell_w // 2, header_h + cell_h + cell_h // 2] == expected_quarter


def test_render_img2txtviz_frame_empty_grid_does_not_crash():
    surf = Surface(*IMG2TXTVIZ_SURFACE_SIZE)
    app.render_img2txtviz_frame({**IMG2TXTVIZ_VM, "grid": []}, surf)   # must not raise


def test_render_img2txtviz_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*IMG2TXTVIZ_SURFACE_SIZE)
    app.render_img2txtviz_frame(IMG2TXTVIZ_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_img2txtviz_frame_golden_matches_frozen_fixture():
    assert GOLDEN_IMG2TXTVIZ_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-10-report.md"
    )
    surf = Surface(*IMG2TXTVIZ_SURFACE_SIZE)
    app.render_img2txtviz_frame(IMG2TXTVIZ_VM, surf)
    app._draw_secondary(surf, IMG2TXTVIZ_ALERTS_VM, IMG2TXTVIZ_TIMESIG_VM, load_font())
    app._draw_status(surf, IMG2TXTVIZ_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_IMG2TXTVIZ_FRAME).convert("RGB")
    assert golden.size == IMG2TXTVIZ_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- config page (phase-3 task 10) -------------------------------------------
#
# A plain "label: value" text dump -- see pages/configview.py's module
# docstring for why this is a fixed flat list rather than v1's recursive
# JSON tree/editor.
GOLDEN_CONFIG_FRAME = FIXTURES / "fb_config_frame_golden.png"
CONFIG_SURFACE_SIZE = (420, 220)

CONFIG_VM = {
    "title": "CONFIG",
    "config_rows": [
        {"label": "socket_path", "value": "/run/midicrt/ctl.sock"},
        {"label": "midi_sources", "value": "*"},
        {"label": "tick_hz", "value": "30"},
        {"label": "pages", "value": "eventlog, voices, harmony"},
        {"label": "instruments", "value": "16 configured"},
        {"label": "audio_device", "value": "(default)"},
        {"label": "spectrum_bins", "value": "96"},
        {"label": "eventlog_capacity", "value": "200"},
        {"label": "pagecycle", "value": "on (idle 300s)"},
        {"label": "screensaver", "value": "on (after 60s)"},
    ],
    "engine_rows": [
        {"label": "engine_version", "value": "2.0.0.dev0"},
        {"label": "proto_version", "value": "1.0"},
        {"label": "uptime_s", "value": "12.3"},
        {"label": "current_page", "value": "eventlog"},
        {"label": "pages_live", "value": "eventlog, voices, harmony"},
        {"label": "analyzers_live", "value": "status, alerts, timesig"},
    ],
}
CONFIG_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
CONFIG_ALERTS_VM = {"alerts": []}
CONFIG_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                      "events_window": 24, "events_total": 40, "pending": None}


def test_config_renderers_dispatch_table_has_config():
    assert app.RENDERERS["config"] is app.render_config_frame


def test_render_config_frame_header_bar_is_reverse_video():
    surf = Surface(*CONFIG_SURFACE_SIZE)
    app.render_config_frame(CONFIG_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._config_header_text(CONFIG_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_config_frame_empty_rows_does_not_crash():
    surf = Surface(*CONFIG_SURFACE_SIZE)
    empty_vm = {"title": "CONFIG", "config_rows": [], "engine_rows": []}
    app.render_config_frame(empty_vm, surf)   # must not raise


def test_render_config_frame_draws_text_for_each_row():
    surf = Surface(*CONFIG_SURFACE_SIZE)
    app.render_config_frame(CONFIG_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    # The "-- Config --" row (first body row) must have at least one lit
    # NORMAL_FG pixel -- proves text was actually drawn, not just background.
    y = header_h
    assert any(px[x, y] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))
    # A later row (the 3rd config row, "tick_hz") likewise.
    y2 = header_h + 3 * line_h
    assert any(px[x, y2] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_config_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*CONFIG_SURFACE_SIZE)
    app.render_config_frame(CONFIG_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_config_frame_golden_matches_frozen_fixture():
    assert GOLDEN_CONFIG_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-10-report.md"
    )
    surf = Surface(*CONFIG_SURFACE_SIZE)
    app.render_config_frame(CONFIG_VM, surf)
    app._draw_secondary(surf, CONFIG_ALERTS_VM, CONFIG_TIMESIG_VM, load_font())
    app._draw_status(surf, CONFIG_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_CONFIG_FRAME).convert("RGB")
    assert golden.size == CONFIG_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- help page (phase-3 task 12, gap ports) -----------------------------------
#
# Same "-- Section --" + "label: value" text-dump convention as
# `render_config_frame` -- see pages/help.py's own module docstring for why
# this describe-data reference IS the v1 Help page's parity port.
GOLDEN_HELP_FRAME = FIXTURES / "fb_help_frame_golden.png"
HELP_SURFACE_SIZE = (420, 220)

HELP_VM = {
    "title": "HELP",
    "page_rows": [
        {"label": "pages (page.goto <name>)", "value": "eventlog, voices, harmony"},
    ],
    "action_rows": [
        {"label": "eventlog.clear", "value": "Clear the event log"},
        {"label": "page.goto", "value": "Jump to a named page  (name:str)"},
        {"label": "page.next", "value": "Advance to the next page in the roster"},
    ],
}
HELP_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
HELP_ALERTS_VM = {"alerts": []}
HELP_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                    "events_window": 24, "events_total": 40, "pending": None}


def test_help_renderers_dispatch_table_has_help():
    assert app.RENDERERS["help"] is app.render_help_frame


def test_render_help_frame_header_bar_is_reverse_video():
    surf = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame(HELP_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._help_header_text(HELP_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_help_frame_empty_rows_does_not_crash():
    surf = Surface(*HELP_SURFACE_SIZE)
    empty_vm = {"title": "HELP", "page_rows": [], "action_rows": []}
    app.render_help_frame(empty_vm, surf)   # must not raise


def test_render_help_frame_draws_text_for_each_row():
    surf = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame(HELP_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    # The "-- Pages --" row (first body row) must have at least one lit
    # NORMAL_FG pixel -- proves text was actually drawn, not just background.
    y = header_h
    assert any(px[x, y] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))
    # A later row (the "-- Actions --" section header) likewise.
    y2 = header_h + 3 * line_h
    assert any(px[x, y2] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_help_frame_draws_the_keymap_section_when_populated():
    """Phase 5 Task 3 (docs/phase5-notes.md cheap-wins bundle): the new
    THIRD section, appended after Actions -- HELP_VM itself has no
    `keymap_rows` key at all (see this file's own comment on why that
    fixture is deliberately left untouched, to avoid re-freezing the
    golden PNG for an unrelated feature), so this test builds its own
    vm with the section populated and checks it actually draws, at the
    row position right after HELP_VM's 3 action rows end."""
    surf = Surface(*HELP_SURFACE_SIZE)
    vm = {**HELP_VM, "keymap_rows": [{"label": "n", "value": "page.next"}]}
    app.render_help_frame(vm, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    line_h = font.height + app.LINE_GAP
    # Row order: "-- Pages --"(0), 1 page row(1), ""(2), "-- Actions --"(3),
    # 3 action rows(4,5,6), ""(7), "-- Keymap --"(8).
    y_keymap_header = header_h + 8 * line_h
    assert any(px[x, y_keymap_header] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_help_frame_omits_the_keymap_section_when_keymap_rows_is_absent():
    """The exact fixture the frozen golden was built from -- proves the
    new section draws NOTHING extra (byte-identical to pre-Task-3
    behavior) when `keymap_rows` is simply not in the vm at all."""
    surf_without = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame(HELP_VM, surf_without)
    surf_with_empty = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame({**HELP_VM, "keymap_rows": []}, surf_with_empty)
    assert surf_without.image.tobytes() == surf_with_empty.image.tobytes()


def test_render_help_frame_reserves_the_bottom_chrome_as_background():
    surf = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame(HELP_VM, surf)
    px = surf.image.load()
    font = load_font()
    reserved = app._reserved_chrome_height(font)
    y_in_strip = surf.height - reserved + 1
    for x in (0, surf.width // 2, surf.width - 1):
        assert px[x, y_in_strip] == app.BG


def test_render_help_frame_golden_matches_frozen_fixture():
    assert GOLDEN_HELP_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*HELP_SURFACE_SIZE)
    app.render_help_frame(HELP_VM, surf)
    app._draw_secondary(surf, HELP_ALERTS_VM, HELP_TIMESIG_VM, load_font())
    app._draw_status(surf, HELP_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_HELP_FRAME).convert("RGB")
    assert golden.size == HELP_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- program changes page (phase-3 task 12, gap ports) ------------------------
#
# Byte-for-byte the same layout as `render_frame` -- see pages/
# progchanges.py's own module docstring for why it reuses eventlog's exact
# `{title, count, lines}` VM shape.
GOLDEN_PROGCHANGES_FRAME = FIXTURES / "fb_progchanges_frame_golden.png"
PROGCHANGES_SURFACE_SIZE = (260, 72)   # wider than eventlog's own golden -- "PROGRAM
                                        # CHANGES  (N events)" is a longer header string

PROGCHANGES_VM = {
    "title": "PROGRAM CHANGES",
    "count": 3,
    "lines": [
        {"text": "[10:00:00]  Ch01 → Program 000", "style": "normal"},
        {"text": "[10:00:05]  Ch02 → Program 012", "style": "normal"},
        {"text": "[10:00:09]  Ch10 → Program 127", "style": "normal"},
    ],
}
PROGCHANGES_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
PROGCHANGES_ALERTS_VM = {"alerts": []}
PROGCHANGES_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                           "events_window": 24, "events_total": 40, "pending": None}


def test_progchanges_renderers_dispatch_table_has_progchanges():
    assert app.RENDERERS["progchanges"] is app.render_progchanges_frame


def test_render_progchanges_frame_header_bar_is_reverse_video():
    surf = Surface(*PROGCHANGES_SURFACE_SIZE)
    app.render_progchanges_frame(PROGCHANGES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._progchanges_header_text(PROGCHANGES_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_progchanges_frame_empty_lines_does_not_crash():
    surf = Surface(*PROGCHANGES_SURFACE_SIZE)
    empty_vm = {"title": "PROGRAM CHANGES", "count": 0, "lines": []}
    app.render_progchanges_frame(empty_vm, surf)   # must not raise


def test_render_progchanges_frame_draws_text_for_a_line():
    surf = Surface(*PROGCHANGES_SURFACE_SIZE)
    app.render_progchanges_frame(PROGCHANGES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert any(px[x, header_h] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_progchanges_frame_golden_matches_frozen_fixture():
    assert GOLDEN_PROGCHANGES_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*PROGCHANGES_SURFACE_SIZE)
    app.render_progchanges_frame(PROGCHANGES_VM, surf)
    app._draw_secondary(surf, PROGCHANGES_ALERTS_VM, PROGCHANGES_TIMESIG_VM, load_font())
    app._draw_status(surf, PROGCHANGES_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_PROGCHANGES_FRAME).convert("RGB")
    assert golden.size == PROGCHANGES_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- CC monitor page (phase-3 task 12, gap ports) ------------------------------
GOLDEN_CCMONITOR_FRAME = FIXTURES / "fb_ccmonitor_frame_golden.png"
CCMONITOR_SURFACE_SIZE = (400, 220)

CCMONITOR_VM = {
    "title": "CC MONITOR",
    "channels": [
        {"ch": ch, "recent": []} for ch in range(1, 17)
    ],
}
CCMONITOR_VM["channels"][0]["recent"] = [
    {"cc": 74, "value": 100, "peak": 120}, {"cc": 1, "value": 64, "peak": 64}]
CCMONITOR_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
CCMONITOR_ALERTS_VM = {"alerts": []}
CCMONITOR_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                         "events_window": 24, "events_total": 40, "pending": None}


def test_ccmonitor_renderers_dispatch_table_has_ccmonitor():
    assert app.RENDERERS["ccmonitor"] is app.render_ccmonitor_frame


def test_render_ccmonitor_frame_header_bar_is_reverse_video():
    surf = Surface(*CCMONITOR_SURFACE_SIZE)
    app.render_ccmonitor_frame(CCMONITOR_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._ccmonitor_header_text(CCMONITOR_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_ccmonitor_frame_empty_channels_does_not_crash():
    surf = Surface(*CCMONITOR_SURFACE_SIZE)
    app.render_ccmonitor_frame({"title": "CC MONITOR", "channels": []}, surf)   # must not raise


def test_render_ccmonitor_frame_draws_text_for_a_row_with_recent_ccs():
    surf = Surface(*CCMONITOR_SURFACE_SIZE)
    app.render_ccmonitor_frame(CCMONITOR_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._reserved_chrome_height(font) - header_h
    row_h = usable_h // len(CCMONITOR_VM["channels"])
    text_y = header_h + max(0, (row_h - font.height) // 2)
    assert any(px[x, y] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width)
               for y in range(text_y, text_y + font.height))


def test_render_ccmonitor_frame_golden_matches_frozen_fixture():
    assert GOLDEN_CCMONITOR_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*CCMONITOR_SURFACE_SIZE)
    app.render_ccmonitor_frame(CCMONITOR_VM, surf)
    app._draw_secondary(surf, CCMONITOR_ALERTS_VM, CCMONITOR_TIMESIG_VM, load_font())
    app._draw_status(surf, CCMONITOR_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_CCMONITOR_FRAME).convert("RGB")
    assert golden.size == CCMONITOR_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- CC dashboard page (phase-3 task 12, gap ports) -----------------------------
GOLDEN_CCDASHBOARD_FRAME = FIXTURES / "fb_ccdashboard_frame_golden.png"
CCDASHBOARD_SURFACE_SIZE = (400, 150)

CCDASHBOARD_VM = {
    "title": "CC DASHBOARD",
    "entries": [
        {"ch": 1, "cc": 74, "value": 100, "peak": 120, "age_s": 0.3, "fresh": True},
        {"ch": 2, "cc": 1, "value": 40, "peak": 64, "age_s": 5.2, "fresh": False},
    ],
}
CCDASHBOARD_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
CCDASHBOARD_ALERTS_VM = {"alerts": []}
CCDASHBOARD_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                           "events_window": 24, "events_total": 40, "pending": None}


def test_ccdashboard_renderers_dispatch_table_has_ccdashboard():
    assert app.RENDERERS["ccdashboard"] is app.render_ccdashboard_frame


def test_render_ccdashboard_frame_header_bar_is_reverse_video():
    surf = Surface(*CCDASHBOARD_SURFACE_SIZE)
    app.render_ccdashboard_frame(CCDASHBOARD_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._ccdashboard_header_text(CCDASHBOARD_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_ccdashboard_frame_empty_entries_does_not_crash():
    surf = Surface(*CCDASHBOARD_SURFACE_SIZE)
    app.render_ccdashboard_frame({"title": "CC DASHBOARD", "entries": []}, surf)   # must not raise


def test_render_ccdashboard_frame_fresh_entry_bar_uses_accent_color():
    surf = Surface(*CCDASHBOARD_SURFACE_SIZE)
    app.render_ccdashboard_frame(CCDASHBOARD_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    row_h = (surf.height - app._reserved_chrome_height(font) - header_h) // 2
    bar_y = header_h + row_h // 2
    label_w = 15 * font.width
    bar_x = app.LEFT_MARGIN + label_w
    assert px[bar_x + 2, bar_y] == app.ACCENT_FG   # first (fresh) entry's bar


def test_render_ccdashboard_frame_golden_matches_frozen_fixture():
    assert GOLDEN_CCDASHBOARD_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*CCDASHBOARD_SURFACE_SIZE)
    app.render_ccdashboard_frame(CCDASHBOARD_VM, surf)
    app._draw_secondary(surf, CCDASHBOARD_ALERTS_VM, CCDASHBOARD_TIMESIG_VM, load_font())
    app._draw_status(surf, CCDASHBOARD_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_CCDASHBOARD_FRAME).convert("RGB")
    assert golden.size == CCDASHBOARD_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- chord+key page (phase-3 task 12, gap ports) -------------------------------
GOLDEN_CHORDKEY_FRAME = FIXTURES / "fb_chordkey_frame_golden.png"
CHORDKEY_SURFACE_SIZE = (300, 150)

CHORDKEY_VM = {
    "title": "CHORD+KEY",
    "recent_pcs": ["C", "E", "G"],
    "chords": [
        {"label": "C maj", "pct": 100, "missing": []},
        {"label": "A m", "pct": 67, "missing": ["A"]},
    ],
    "key": {
        "label": "C maj", "pct": 92, "threshold_pct": 72, "ambiguous": False,
        "top": {"label": "C maj", "pct": 92},
        "alternatives": [{"label": "G maj", "pct": 70}],
    },
    "function": "i (T)",
}
CHORDKEY_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
CHORDKEY_ALERTS_VM = {"alerts": []}
CHORDKEY_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                        "events_window": 24, "events_total": 40, "pending": None}


def test_chordkey_renderers_dispatch_table_has_chordkey():
    assert app.RENDERERS["chordkey"] is app.render_chordkey_frame


def test_render_chordkey_frame_header_bar_is_reverse_video():
    surf = Surface(*CHORDKEY_SURFACE_SIZE)
    app.render_chordkey_frame(CHORDKEY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._chordkey_header_text(CHORDKEY_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_chordkey_frame_empty_does_not_crash():
    surf = Surface(*CHORDKEY_SURFACE_SIZE)
    empty_vm = {
        "title": "CHORD+KEY", "recent_pcs": [], "chords": [],
        "key": {"label": None, "pct": None, "threshold_pct": 72, "ambiguous": True,
                "top": None, "alternatives": []},
        "function": None,
    }
    app.render_chordkey_frame(empty_vm, surf)   # must not raise


def test_render_chordkey_frame_draws_text_for_a_row():
    surf = Surface(*CHORDKEY_SURFACE_SIZE)
    app.render_chordkey_frame(CHORDKEY_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert any(px[x, header_h] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_chordkey_frame_golden_matches_frozen_fixture():
    assert GOLDEN_CHORDKEY_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*CHORDKEY_SURFACE_SIZE)
    app.render_chordkey_frame(CHORDKEY_VM, surf)
    app._draw_secondary(surf, CHORDKEY_ALERTS_VM, CHORDKEY_TIMESIG_VM, load_font())
    app._draw_status(surf, CHORDKEY_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_CHORDKEY_FRAME).convert("RGB")
    assert golden.size == CHORDKEY_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


# -- send notes page (phase-3 task 12, gap ports) -------------------------------
GOLDEN_SENDNOTES_FRAME = FIXTURES / "fb_sendnotes_frame_golden.png"
SENDNOTES_SURFACE_SIZE = (300, 100)

SENDNOTES_VM = {
    "title": "SEND NOTES",
    "device": "midicrt2 Output",
    "channel": 1, "octave": 4, "velocity": 96, "gate_ms": 120, "active": 2,
}
SENDNOTES_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
SENDNOTES_ALERTS_VM = {"alerts": []}
SENDNOTES_TIMESIG_VM = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
                         "events_window": 24, "events_total": 40, "pending": None}


def test_sendnotes_renderers_dispatch_table_has_sendnotes():
    assert app.RENDERERS["sendnotes"] is app.render_sendnotes_frame


def test_render_sendnotes_frame_header_bar_is_reverse_video():
    surf = Surface(*SENDNOTES_SURFACE_SIZE)
    app.render_sendnotes_frame(SENDNOTES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_text = app._sendnotes_header_text(SENDNOTES_VM)
    text_px_end = app.LEFT_MARGIN + len(header_text) * font.width
    assert text_px_end < surf.width
    assert px[text_px_end + 4, 0] == app.HEADER_BG


def test_render_sendnotes_frame_shows_not_open_when_device_is_none():
    surf = Surface(*SENDNOTES_SURFACE_SIZE)
    vm = dict(SENDNOTES_VM, device=None)
    app.render_sendnotes_frame(vm, surf)   # must not raise
    assert "(not open)" in app._sendnotes_status_text(vm)


def test_render_sendnotes_frame_draws_text_for_the_status_row():
    surf = Surface(*SENDNOTES_SURFACE_SIZE)
    app.render_sendnotes_frame(SENDNOTES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    assert any(px[x, header_h] == app.NORMAL_FG
               for x in range(app.LEFT_MARGIN, surf.width))


def test_render_sendnotes_frame_golden_matches_frozen_fixture():
    assert GOLDEN_SENDNOTES_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-12-report.md"
    )
    surf = Surface(*SENDNOTES_SURFACE_SIZE)
    app.render_sendnotes_frame(SENDNOTES_VM, surf)
    app._draw_secondary(surf, SENDNOTES_ALERTS_VM, SENDNOTES_TIMESIG_VM, load_font())
    app._draw_status(surf, SENDNOTES_STATUS_VM, load_font())
    app._draw_beatprogress(surf, DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, load_font())

    golden = Image.open(GOLDEN_SENDNOTES_FRAME).convert("RGB")
    assert golden.size == SENDNOTES_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


def test_fps_zero_rejected_before_connect_no_hang(tmp_path):
    # Regression: `--fps 0` (or negative/nan) used to reach the wire, get
    # rejected by the server's max_rate check, and then hang forever in
    # _wait_first_snapshot because nobody checked subscribe()'s response.
    # A *real* daemon is running here (like the golden-frame test above) so
    # that, pre-fix, connect() would succeed and the hang would actually
    # happen; argparse must now reject --fps before a socket is ever opened.
    # The short timeout is the hang detector: a regression makes this test
    # error out with TimeoutExpired instead of failing a clean assertion.
    sock = str(tmp_path / "ctl.sock")
    out_png = tmp_path / "frame.png"
    daemon = _start_daemon(sock, tmp_path)
    try:
        result = subprocess.run(
            [VENVPY, "-m", "midicrt.clients.fb.app",
             "--socket", sock, "--fps", "0", "--out", str(out_png), "--no-input"],
            capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode != 0
        assert "fps" in result.stderr.lower()
        assert not out_png.exists()
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)


def test_run_device_survives_page_switch_before_new_topics_snapshot_arrives(tmp_path, monkeypatch):
    """Regression (found live, phase-3 task 11 supervised CRT smoke): the
    redraw loop only refreshed `vm` `if page_updated` (a fresh snapshot for
    the CURRENT topic arrived THIS tick), but `state["page"]`/`state["topic"]`
    flip immediately on a `page_changed` event via `on_event`. Per
    docs/phase2-notes.md, a (re)subscribed topic's own first snapshot can
    arrive "up to 1/max_rate later" -- not synchronously with the event that
    triggered the resubscribe. Any of the four independently-ticking overlay
    topics (status/alerts/timesig/beatflash/loopprogress) firing in that gap
    used to trigger `_paint_frame` anyway, pairing the NEW page name with the
    OLD page's stale vm -- e.g. page="eventlog" painted with
    vm={"title": "SCREENSAVER"}, crashing `render_frame` on the missing
    `vm['count']` key. Reproduced live against the real daemon/fb0; this test
    reproduces it deterministically via a scripted inbox."""
    monkeypatch.setattr(app, "_read_fb_geometry", lambda: (10, 10, 20))

    calls = []
    real_paint_frame = app._paint_frame

    def spy_paint_frame(surface, page, vm, *rest):
        calls.append((page, dict(vm)))
        return real_paint_frame(surface, page, vm, *rest)

    monkeypatch.setattr(app, "_paint_frame", spy_paint_frame)

    class FakeClient:
        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

    inbox = queue.Queue()
    # 1) Initial snapshot for the STARTING page (screensaver) -- lets
    #    wait_first_snapshot return immediately, self-consistent.
    inbox.put({"kind": "snapshot", "topic": "page.screensaver", "data": {"title": "SCREENSAVER"}})
    # 2) A page_changed event -- flips state to eventlog -- with NO
    #    accompanying "page.eventlog" snapshot in this same batch (the
    #    real-world delivery gap this test reproduces).
    inbox.put({"kind": "event", "name": "page_changed", "data": {"page": "eventlog"}})
    # 3) An unrelated overlay update in the SAME batch -- this alone must
    #    not be sufficient to repaint the page body against the stale vm.
    inbox.put({"kind": "snapshot", "topic": "overlay.status",
               "data": {"bpm": 120.0, "bar": 1, "beat": 1, "running": True, "source": "test"}})
    # 4) Clean-shutdown sentinel, queued from a timer so it lands on a
    #    LATER tick than 2+3 above -- `drain_latest` drains the whole queue
    #    in one non-blocking pass, so a sentinel queued up-front would be
    #    consumed (and raise) in the SAME batch as 2+3, before `_run_device`
    #    ever reaches the `_paint_frame` call this test is checking.
    timer = threading.Timer(0.05, inbox.put, args=(None,))
    timer.start()

    fb_path = str(tmp_path / "fb0")
    try:
        app._run_device(FakeClient(), inbox, fb_path, True, 1000.0, "screensaver", "page.screensaver",
                        {"q": "client.quit", "c": "eventlog.clear", "n": "page.next"})
    except ClientError:
        pass  # expected clean shutdown via the `None` sentinel
    finally:
        timer.cancel()

    # The fix's actual contract: _paint_frame must never be called with
    # page="eventlog" paired with a vm that isn't eventlog-shaped.
    for page, vm in calls:
        if page == "eventlog":
            assert "count" in vm, (
                f"eventlog page painted with a non-eventlog (stale) vm: {vm!r}")


# -- evdev keymap dispatch (Phase 4 Task 1, docs/phase4-notes.md) -----------
#
# `_build_evdev_char_table` is pure and needs no real input device -- `evdev`
# is a hard runtime dependency (pyproject.toml), importable in any test env
# this suite runs in. `_input_loop`'s own `dev.read_loop()` consumption is
# NOT unit tested here, matching the pre-existing convention (it never was
# either, before this task -- see this file's own module docstring: real
# hardware I/O is exercised only in supervised smoke windows, not pytest).

def test_build_evdev_char_table_covers_letters_and_digits():
    import evdev

    table = app._build_evdev_char_table(evdev)
    assert table[evdev.ecodes.KEY_N] == "n"
    assert table[evdev.ecodes.KEY_Q] == "q"
    assert table[evdev.ecodes.KEY_C] == "c"
    assert table[evdev.ecodes.KEY_1] == "1"
    assert table[evdev.ecodes.KEY_0] == "0"
    assert len(table) == 36   # 26 letters + 10 digits, one entry each


def test_build_evdev_char_table_values_are_all_single_lowercase_chars():
    import evdev

    table = app._build_evdev_char_table(evdev)
    assert all(len(ch) == 1 and ch == ch.lower() for ch in table.values())


# -- action-dispatch failure is logged, not silently swallowed forever ------
# (Important, bindings review -- covered by/alongside the TUI's own Critical
# fix: `_input_loop`'s old `except ClientError: pass` was a SILENT,
# PERMANENT no-op with zero diagnostic -- unlike the TUI's exit bug, nothing
# ever surfaced this failure at all. `_dispatch_evdev_key` is the per-
# keypress body extracted from `_input_loop` so this is directly testable
# without a real evdev device.)

class _RejectingClient:
    def __init__(self, message="missing arg: name"):
        self.message = message
        self.calls: list[str] = []

    def action(self, name):
        self.calls.append(name)
        raise ClientError(self.message)


class _RecordingClient:
    def __init__(self):
        self.calls: list[str] = []

    def action(self, name):
        self.calls.append(name)


def test_dispatch_evdev_key_client_quit_returns_true():
    assert app._dispatch_evdev_key(_RecordingClient(), "q", {"q": "client.quit"}, {}) is True


def test_dispatch_evdev_key_sends_a_real_action_and_returns_false():
    client = _RecordingClient()
    assert app._dispatch_evdev_key(client, "c", {"c": "eventlog.clear"}, {}) is False
    assert client.calls == ["eventlog.clear"]


def test_dispatch_evdev_key_logs_a_rejected_action_and_returns_false(caplog):
    client = _RejectingClient("missing arg: name")
    rate_state: dict = {}
    with caplog.at_level(logging.WARNING):
        result = app._dispatch_evdev_key(client, "g", {"g": "page.goto"}, rate_state)
    assert result is False   # never treated as "quit"
    assert "missing arg" in caplog.text
    assert "g" in caplog.text


def test_dispatch_evdev_key_rate_caps_repeated_failures(caplog):
    # A stuck/repeating key firing the SAME rejected action many times a
    # second must not flood the journal -- at most one warning per
    # _KEY_ERROR_LOG_INTERVAL_S seconds, using ONE shared rate_state dict
    # across calls (mirrors how _input_loop keeps one for its whole
    # read_loop).
    client = _RejectingClient("boom")
    rate_state: dict = {}
    with caplog.at_level(logging.WARNING):
        app._dispatch_evdev_key(client, "g", {"g": "page.goto"}, rate_state)
        app._dispatch_evdev_key(client, "g", {"g": "page.goto"}, rate_state)
    assert len(client.calls) == 2                     # the action IS retried every press...
    assert caplog.text.count("action dispatch failed") == 1   # ...but only logged once


def test_dispatch_evdev_key_unmapped_key_is_a_noop():
    client = _RecordingClient()
    assert app._dispatch_evdev_key(client, "z", {}, {}) is False
    assert client.calls == []


def test_run_device_refetches_keymap_on_keymap_changed_event(tmp_path, monkeypatch):
    """Twin of clients/tui.py's own
    test_run_tui_refetches_keymap_on_keymap_changed_event: a `keymap_changed`
    event delivered through `_run_device`'s `on_event` (`_make_page_switcher`)
    must trigger a real `fetch_keymap` re-fetch (a growing `describe()` call
    count is direct, unambiguous proof), not a value cached at startup."""
    monkeypatch.setattr(app, "_read_fb_geometry", lambda: (10, 10, 20))

    describe_calls = []

    class FakeClient:
        def request(self, cmd):
            if cmd == "describe":
                describe_calls.append(cmd)
                keymap = ({"n": "page.prev"} if len(describe_calls) > 1
                         else {"n": "page.next"})
                return {"data": {"keymap": keymap}}
            return {"data": {}}

        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.eventlog", "data": EMPTY_VM})
    inbox.put({"kind": "event", "name": "keymap_changed", "data": {}})
    timer = threading.Timer(0.05, inbox.put, args=(None,))
    timer.start()

    fb_path = str(tmp_path / "fb0")
    try:
        app._run_device(FakeClient(), inbox, fb_path, True, 1000.0, "eventlog", "page.eventlog",
                        {"n": "page.next"})
    except ClientError:
        pass  # expected clean shutdown via the `None` sentinel
    finally:
        timer.cancel()

    assert len(describe_calls) >= 1, (
        "expected a fetch_keymap() re-fetch (a describe() call) after keymap_changed"
    )
