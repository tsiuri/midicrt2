from midicrt.clients.fb.surface import Surface

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Expected RGB565 packing, little-endian uint16 (matches v1's
# fb/compositor.py _rgb565(): ((r&0xF8)<<8) | ((g&0xFC)<<3) | (b>>3)).
RED_565_LE = b"\x00\xf8"     # 0xF800
GREEN_565_LE = b"\xe0\x07"   # 0x07E0
BLUE_565_LE = b"\x1f\x00"    # 0x001F
WHITE_565_LE = b"\xff\xff"   # 0xFFFF


def test_surface_creates_correctly_sized_rgb_image():
    surf = Surface(10, 6)
    assert surf.width == 10
    assert surf.height == 6
    assert surf.image.size == (10, 6)
    assert surf.image.mode == "RGB"


def test_clear_fills_entire_surface():
    surf = Surface(8, 4)
    surf.clear(GREEN)
    px = surf.image.load()
    for x in range(8):
        for y in range(4):
            assert px[x, y] == GREEN


def test_rect_draws_filled_rectangle_over_background():
    surf = Surface(10, 10)
    surf.clear(BLACK)
    surf.rect(2, 3, 4, 2, RED)  # covers x in [2,5], y in [3,4]
    px = surf.image.load()
    for x in range(2, 6):
        for y in range(3, 5):
            assert px[x, y] == RED
    # spot-check outside the rect stays background
    assert px[0, 0] == BLACK
    assert px[6, 3] == BLACK
    assert px[2, 5] == BLACK


def test_rect_clips_to_surface_bounds():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    surf.rect(-2, -2, 4, 4, WHITE)  # only bottom-right 2x2 corner is on-surface
    px = surf.image.load()
    assert px[0, 0] == WHITE
    assert px[1, 1] == WHITE
    assert px[2, 2] == BLACK


def test_to_rgb565_length_matches_width_height():
    surf = Surface(5, 3)
    data = surf.to_rgb565()
    assert len(data) == 5 * 3 * 2


def test_to_rgb565_known_colors_at_exact_offsets():
    surf = Surface(4, 1)
    px = surf.image.load()
    px[0, 0] = RED
    px[1, 0] = GREEN
    px[2, 0] = BLUE
    px[3, 0] = WHITE
    data = surf.to_rgb565()
    assert len(data) == 8
    assert data[0:2] == RED_565_LE
    assert data[2:4] == GREEN_565_LE
    assert data[4:6] == BLUE_565_LE
    assert data[6:8] == WHITE_565_LE


def test_write_fb_no_stride_writes_tightly_packed_rows(tmp_path):
    surf = Surface(4, 1)
    px = surf.image.load()
    px[0, 0] = RED
    px[1, 0] = GREEN
    px[2, 0] = BLUE
    px[3, 0] = WHITE
    out_path = tmp_path / "fb_no_stride.bin"
    surf.write_fb(str(out_path))
    written = out_path.read_bytes()
    assert written == surf.to_rgb565()
    assert len(written) == 4 * 1 * 2


def test_write_fb_pads_rows_to_device_stride(tmp_path):
    # w=4 -> row_bytes = 4*2 = 8; stride=16 -> 8 bytes of padding per row.
    surf = Surface(4, 2)
    px = surf.image.load()
    for x in range(4):
        px[x, 0] = RED
        px[x, 1] = BLUE
    out_path = tmp_path / "fb_strided.bin"
    surf.write_fb(str(out_path), stride=16)
    written = out_path.read_bytes()
    assert len(written) == 16 * 2  # stride * height

    row0 = written[0:16]
    row1 = written[16:32]
    assert row0[0:8] == RED_565_LE * 4
    assert row0[8:16] == b"\x00" * 8
    assert row1[0:8] == BLUE_565_LE * 4
    assert row1[8:16] == b"\x00" * 8


def test_write_fb_overwrites_existing_file_in_place(tmp_path):
    # Simulates the /dev/fb0 case: the target already exists (a character
    # device can't be truncated) — writing must overwrite the leading
    # bytes in place without recreating/shrinking the file.
    out_path = tmp_path / "fb_existing.bin"
    out_path.write_bytes(b"\xff" * 100)
    surf = Surface(4, 1)
    surf.clear(BLACK)
    surf.write_fb(str(out_path))
    written = out_path.read_bytes()
    packed = surf.to_rgb565()
    assert written[: len(packed)] == packed
    assert len(written) == 100  # untouched tail proves no truncation happened


def test_write_fb_raises_when_stride_narrower_than_row(tmp_path):
    surf = Surface(4, 1)  # row_bytes = 8
    out_path = tmp_path / "fb_bad_stride.bin"
    try:
        surf.write_fb(str(out_path), stride=4)
    except ValueError as exc:
        assert "4" in str(exc) and "8" in str(exc)
    else:
        raise AssertionError("expected ValueError for stride < row_bytes")


def test_save_png_roundtrip(tmp_path):
    from PIL import Image

    surf = Surface(6, 4)
    surf.clear(RED)
    out_path = tmp_path / "surf.png"
    surf.save_png(str(out_path))
    reloaded = Image.open(out_path)
    assert reloaded.size == (6, 4)
    assert reloaded.convert("RGB").getpixel((0, 0)) == RED
