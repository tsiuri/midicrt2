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
    line_h = font.height + app.LINE_GAP
    size = (200, header_h + line_h)  # room for exactly one body line
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
    line_h = font.height + app.LINE_GAP
    size = (200, header_h + 2 * line_h)  # room for exactly two body lines
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


def test_render_frame_accent_color_is_brighter_than_normal():
    assert app.ACCENT_FG != app.NORMAL_FG
    assert sum(app.ACCENT_FG) > sum(app.NORMAL_FG)


def test_render_frame_golden_matches_frozen_fixture():
    assert GOLDEN_FRAME.exists(), (
        "golden fixture missing -- see freeze procedure in task-3-report.md"
    )
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    app.render_frame(VM, surf)

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
