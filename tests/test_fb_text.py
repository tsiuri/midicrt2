from pathlib import Path

from PIL import Image

from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import draw_text, load_font

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PATH = FIXTURES / "fb_text_golden.png"

BLACK = (0, 0, 0)
FG = (0, 255, 80)  # CRT-green foreground, matches v1's GREEN_BRIGHT

# The golden fixture renders this exact string at this exact position/color
# on this exact surface size — see the freeze procedure in text.py's
# module docstring and the task-2 report.
GOLDEN_TEXT = "Hi 2!"
GOLDEN_X, GOLDEN_Y = 4, 4
GOLDEN_SURFACE_SIZE = (48, 16)


def test_load_font_reports_8x8_glyph_cell():
    font = load_font()
    assert font.width == 8
    assert font.height == 8


def test_load_font_returns_cached_default_instance():
    a = load_font()
    b = load_font()
    assert a is b


def test_draw_text_returns_pixel_width_drawn():
    surf = Surface(64, 16)
    width = draw_text(surf, 0, 0, "AB", FG)
    font = load_font()
    assert width == 2 * font.width


def test_draw_text_empty_string_draws_nothing_and_returns_zero():
    surf = Surface(16, 16)
    surf.clear(BLACK)
    width = draw_text(surf, 0, 0, "", FG)
    assert width == 0
    px = surf.image.load()
    assert px[0, 0] == BLACK


def test_draw_text_leaves_background_untouched_around_glyph():
    surf = Surface(16, 16)
    surf.clear(BLACK)
    draw_text(surf, 0, 0, "A", FG)
    px = surf.image.load()
    # Corner of the glyph cell should still be background for glyph 'A'
    # (top-left pixel of an 8x8 VGA 'A' glyph is unset).
    assert px[0, 0] == BLACK
    # Something in the cell must have been painted the foreground color.
    assert any(
        px[x, y] == FG for x in range(8) for y in range(8)
    )


def test_draw_text_space_paints_nothing():
    surf = Surface(16, 16)
    surf.clear(BLACK)
    draw_text(surf, 0, 0, " ", FG)
    px = surf.image.load()
    for x in range(8):
        for y in range(8):
            assert px[x, y] == BLACK


def test_draw_text_golden_matches_frozen_fixture():
    assert GOLDEN_PATH.exists(), (
        "golden fixture missing — see freeze procedure in "
        "midicrt/clients/fb/text.py's module docstring"
    )
    surf = Surface(*GOLDEN_SURFACE_SIZE)
    surf.clear(BLACK)
    draw_text(surf, GOLDEN_X, GOLDEN_Y, GOLDEN_TEXT, FG)

    golden = Image.open(GOLDEN_PATH).convert("RGB")
    assert golden.size == GOLDEN_SURFACE_SIZE
    assert surf.image.tobytes() == golden.tobytes()
