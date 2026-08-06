import struct
from pathlib import Path

from PIL import Image

from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import PSF2_MAGIC, Font, draw_text, load_font

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


def _build_synthetic_psf2(tmp_path: Path) -> Path:
    """Write a tiny synthetic PSF2 font to `tmp_path` and return its path.

    width=12, height=2 -> bytes_per_row=ceil(12/8)=2, so each glyph row
    spans two bytes — v1's vendored default font is 8x8 (one byte per
    row) and never exercises the multi-byte-per-row path. The unicode
    table below also includes a 0xFE sequence-start marker followed by
    bytes that a naive UTF-8 decoder would misparse as a 4-byte sequence
    (consuming the terminating 0xFF and desyncing the following glyph
    mapping) if 0xFE isn't special-cased before UTF-8 decoding.
    """
    width, height = 12, 2
    bytes_per_row = (width + 7) // 8  # 2
    bytes_per_glyph = height * bytes_per_row  # 4
    header = struct.pack(
        "<IIIIIIII",
        PSF2_MAGIC,
        0,              # version
        32,             # headersize
        0x01,           # flags: has unicode table
        2,              # numglyph
        bytes_per_glyph,
        height,
        width,
    )
    # Glyph 0: bits set at (col=0,row=0), (col=11,row=0) [byte1 bit4],
    # (col=5,row=1) [byte0 bit2], (col=8,row=1) [byte1 bit7] — deliberately
    # spans both bytes of each 2-byte row.
    glyph0 = bytes([0x80, 0x10, 0x04, 0x80])
    # Glyph 1: content doesn't matter — it only needs to exist so the
    # unicode table below can map a second, distinguishable glyph index.
    glyph1 = bytes([0x10, 0x00, 0x10, 0x00])

    # 'A'(0x41) -> glyph 0, then a 0xFE sequence marker + bytes that would
    # be misdecoded as a 4-byte UTF-8 sequence (swallowing the real 0xFF
    # terminator) under the pre-fix parser, then 'B'(0x42) -> glyph 1.
    unicode_table = bytes([0x41, 0xFE, 0xCC, 0x81, 0xFF, 0x42, 0xFF])

    path = tmp_path / "synthetic.psf2"
    path.write_bytes(header + glyph0 + glyph1 + unicode_table)
    return path


def test_psf2_multibyte_glyph_rows_and_unicode_sequence_marker(tmp_path):
    font = Font(_build_synthetic_psf2(tmp_path))
    assert font.width == 12
    assert font.height == 2

    # --- multi-byte-per-row glyph bit indexing (previously raised
    # ValueError: negative shift count for col >= 8) ---
    img = font.glyph_image(0, (0, 255, 80))
    px = img.load()
    fg = (0, 255, 80, 255)
    bg = (0, 0, 0, 0)
    lit = {(0, 0), (11, 0), (5, 1), (8, 1)}
    for x in range(12):
        for y in range(2):
            assert px[x, y] == (fg if (x, y) in lit else bg)

    # --- 0xFE sequence marker must not desync `pos` in the unicode table
    # ---
    assert font.glyph_index("A") == 0
    assert font.glyph_index("B") == 1
