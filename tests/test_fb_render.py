"""Tests for the fb client app: `render_frame` (pure) + the `--out` headless
end-to-end path (the acceptance path for this task -- real device writes are
coded but NOT exercised here; v1 owns /dev/fb0 until Task 4's supervised
smoke window).
"""
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

from midicrt.clients.fb import app
from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import draw_text, load_font
from midicrt.clients.tui import _tail as tui_tail

VENVPY = sys.executable
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_FRAME = FIXTURES / "fb_frame_golden.png"
GOLDEN_EMPTY = FIXTURES / "fb_frame_empty_golden.png"

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
GOLDEN_SURFACE_SIZE = (220, 60)

# The golden-frame fixture now also exercises the chrome status strip
# (phase-3 task 3: "golden updates for both renderers -- chrome now
# present") -- a fixed, representative overlay.status view-model so the
# frozen PNG shows real BPM/BAR/BEAT/running/source content, not just the
# all-defaults idle state (that case is covered separately by the
# `--out`-mode golden against a real `--no-midi` daemon, which never sees
# a start/clock event and so IS the all-defaults case).
GOLDEN_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}


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
    strip_h = app._status_strip_height(font)
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
    strip_h = app._status_strip_height(font)
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
    font = load_font()
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    surf.clear(app.BG)
    app._draw_status(surf, GOLDEN_STATUS_VM, font)
    px = surf.image.load()
    strip_h = app._status_strip_height(font)
    y = surf.height - strip_h
    # Fill matches the header's reverse-video bar colour.
    assert px[surf.width - 1, y] == app.HEADER_BG
    assert px[surf.width - 1, surf.height - 1] == app.HEADER_BG
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
    y = surf_b.height - strip_h
    surf_b.rect(0, y, surf_b.width, strip_h, app.HEADER_BG)
    draw_text(surf_b, app.LEFT_MARGIN, y + app.STATUS_PAD,
              chrome.status_text(GOLDEN_STATUS_VM), app.BG, font)

    assert surf_a.image.tobytes() == surf_b.image.tobytes()


def test_render_frame_accent_color_is_brighter_than_normal():
    assert app.ACCENT_FG != app.NORMAL_FG
    assert sum(app.ACCENT_FG) > sum(app.NORMAL_FG)


def test_render_frame_golden_matches_frozen_fixture():
    # Phase-3 task 3: the golden now composes BOTH renderers, page body
    # (render_frame) + chrome status strip (_draw_status), the same way the
    # real run loops do -- "golden updates for both renderers, chrome now
    # present" (task-3 brief).
    assert GOLDEN_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-3-report.md"
    )
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(VM, surf)
    app._draw_status(surf, GOLDEN_STATUS_VM, load_font())

    golden = Image.open(GOLDEN_FRAME).convert("RGB")
    assert golden.size == GOLDEN_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()


def _start_daemon(sock):
    p = subprocess.Popen(
        [VENVPY, "-m", "midicrt.daemon", "--socket", sock, "--no-midi"],
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
    daemon = _start_daemon(sock)
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
VOICES_SURFACE_SIZE = (240, 216)   # header(12) + 16 rows*12 + status strip(12)

VOICES_ROWS = [
    {"ch": i, "name": f"Instr{i}", "active": 0, "peak": 0, "notes": []}
    for i in range(1, 17)
]
VOICES_ROWS[0] = {"ch": 1, "name": "Kawai XD5", "active": 3, "peak": 8, "notes": [60, 64, 67]}
VOICES_ROWS[2] = {"ch": 3, "name": "BassStaRack", "active": 8, "peak": 12, "notes": list(range(30, 38))}
VOICES_VM = {"title": "VOICES", "total": 11, "total_peak": 20, "rows": VOICES_ROWS}

VOICES_STATUS_VM = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}


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
    usable_h = surf.height - app._status_strip_height(font) - header_h
    row_h = usable_h // len(VOICES_ROWS)
    bar_x = app.LEFT_MARGIN + app.NAME_COL_CHARS * font.width + app.BAR_GAP
    for i in range(len(VOICES_ROWS)):
        row_y = header_h + i * row_h
        bar_y = row_y + app.ROW_PAD
        # top-left corner of the box outline for this row's meter.
        assert px[bar_x, bar_y] == app.NORMAL_FG


def test_render_voices_frame_live_fill_reflects_active_count():
    # Channel 3 (index 2) is maxed at BAR_MAX active voices -> the topmost
    # interior row of its meter box must be filled; an idle channel's must
    # stay background.
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    px = surf.image.load()
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    usable_h = surf.height - app._status_strip_height(font) - header_h
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
    usable_h = surf.height - app._status_strip_height(font) - header_h
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
        "golden fixture missing -- see freeze procedure in task-4-report.md"
    )
    surf = Surface(*VOICES_SURFACE_SIZE)
    app.render_voices_frame(VOICES_VM, surf)
    app._draw_status(surf, VOICES_STATUS_VM, load_font())

    golden = Image.open(GOLDEN_VOICES_FRAME).convert("RGB")
    assert golden.size == VOICES_SURFACE_SIZE
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
    daemon = _start_daemon(sock)
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
