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


# -- new primitives (Phase 3 Task 2): hline / vline / box / fill_column ----


def test_hline_draws_horizontal_run():
    surf = Surface(6, 4)
    surf.clear(BLACK)
    surf.hline(1, 2, 3, RED)  # x in [1,3], y=2
    px = surf.image.load()
    for x in range(1, 4):
        assert px[x, 2] == RED
    assert px[0, 2] == BLACK
    assert px[4, 2] == BLACK
    assert px[1, 1] == BLACK
    assert px[1, 3] == BLACK


def test_hline_clips_to_surface_bounds():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    surf.hline(-2, 1, 4, WHITE)  # only x in [0,1] on-surface
    px = surf.image.load()
    assert px[0, 1] == WHITE
    assert px[1, 1] == WHITE
    assert px[2, 1] == BLACK


def test_vline_draws_vertical_run():
    surf = Surface(4, 6)
    surf.clear(BLACK)
    surf.vline(2, 1, 3, RED)  # y in [1,3], x=2
    px = surf.image.load()
    for y in range(1, 4):
        assert px[2, y] == RED
    assert px[2, 0] == BLACK
    assert px[2, 4] == BLACK
    assert px[1, 1] == BLACK
    assert px[3, 1] == BLACK


def test_vline_clips_to_surface_bounds():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    surf.vline(1, -2, 4, WHITE)  # only y in [0,1] on-surface
    px = surf.image.load()
    assert px[1, 0] == WHITE
    assert px[1, 1] == WHITE
    assert px[1, 2] == BLACK


def test_box_draws_outline_leaving_interior_untouched():
    surf = Surface(6, 5)
    surf.clear(BLACK)
    surf.box(1, 1, 4, 3, RED)  # covers x in [1,4], y in [1,3]
    px = surf.image.load()
    for x in range(1, 5):
        assert px[x, 1] == RED
        assert px[x, 3] == RED
    for y in range(1, 4):
        assert px[1, y] == RED
        assert px[4, y] == RED
    # interior stays background
    assert px[2, 2] == BLACK
    assert px[3, 2] == BLACK
    # outside the box stays background
    assert px[0, 0] == BLACK
    assert px[5, 4] == BLACK


def test_box_clips_to_surface_bounds():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    # box spans x in [-2,1], y in [-2,1]; only its bottom edge (y=1) and
    # right edge (x=1) fall on-surface -- the top/left edges are fully
    # clipped away, so (0,0) is genuine box INTERIOR (2 in from each of
    # those off-canvas edges) and must stay background, not filled.
    surf.box(-2, -2, 4, 4, WHITE)
    px = surf.image.load()
    assert px[0, 0] == BLACK
    assert px[0, 1] == WHITE  # bottom edge
    assert px[1, 0] == WHITE  # right edge
    assert px[1, 1] == WHITE  # corner where bottom and right edges meet


def test_box_non_positive_size_draws_nothing():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    surf.box(1, 1, 0, 3, RED)
    surf.box(1, 1, 3, -1, RED)
    px = surf.image.load()
    for x in range(4):
        for y in range(4):
            assert px[x, y] == BLACK


def test_fill_column_grows_upward_from_bottom_row():
    surf = Surface(4, 6)
    surf.clear(BLACK)
    surf.fill_column(1, 5, 3, RED)  # bottom-aligned at row 5, height 3 -> rows 3,4,5
    px = surf.image.load()
    for y in (3, 4, 5):
        assert px[1, y] == RED
    assert px[1, 2] == BLACK
    assert px[0, 5] == BLACK
    assert px[2, 5] == BLACK


def test_fill_column_width_spans_multiple_columns():
    surf = Surface(6, 4)
    surf.clear(BLACK)
    surf.fill_column(1, 3, 2, RED, width=3)  # x in [1,3], y in [2,3]
    px = surf.image.load()
    for x in range(1, 4):
        for y in (2, 3):
            assert px[x, y] == RED
    assert px[0, 3] == BLACK
    assert px[4, 3] == BLACK


def test_fill_column_clips_like_rect():
    surf = Surface(4, 4)
    surf.clear(BLACK)
    surf.fill_column(1, 10, 3, WHITE)  # bar entirely below the surface -> nothing drawn
    px = surf.image.load()
    for x in range(4):
        for y in range(4):
            assert px[x, y] == BLACK
    surf.fill_column(1, 1, 5, WHITE)  # bar's top clips off the top edge
    px = surf.image.load()
    assert px[1, 0] == WHITE
    assert px[1, 1] == WHITE
    assert px[1, 2] == BLACK


# -- mmap device-loop primitives (Phase 3 Task 2) --------------------------


def test_write_to_mmap_matches_write_fb_output(tmp_path):
    from midicrt.clients.fb.surface import open_fb_mmap

    surf = Surface(4, 1)
    px = surf.image.load()
    px[0, 0] = RED
    px[1, 0] = GREEN
    px[2, 0] = BLUE
    px[3, 0] = WHITE

    size = surf.width * 2 * surf.height
    path = tmp_path / "fb_mmap.bin"
    f, mm = open_fb_mmap(str(path), size)
    try:
        surf.write_to_mmap(mm)
    finally:
        mm.close()
        f.close()
    assert path.read_bytes() == surf.to_rgb565()


def test_write_to_mmap_pads_rows_to_stride(tmp_path):
    from midicrt.clients.fb.surface import open_fb_mmap

    surf = Surface(4, 2)
    px = surf.image.load()
    for x in range(4):
        px[x, 0] = RED
        px[x, 1] = BLUE
    stride = 16
    size = stride * surf.height
    path = tmp_path / "fb_mmap_strided.bin"
    f, mm = open_fb_mmap(str(path), size)
    try:
        surf.write_to_mmap(mm, stride=stride)
    finally:
        mm.close()
        f.close()
    written = path.read_bytes()
    assert len(written) == size
    row0 = written[0:16]
    row1 = written[16:32]
    assert row0[0:8] == RED_565_LE * 4
    assert row0[8:16] == b"\x00" * 8
    assert row1[0:8] == BLUE_565_LE * 4
    assert row1[8:16] == b"\x00" * 8


def test_open_fb_mmap_reuses_across_multiple_writes(tmp_path):
    # The whole point of the device loop's mmap path: open the map ONCE and
    # write many frames into it, unlike write_fb which reopens every call.
    from midicrt.clients.fb.surface import open_fb_mmap

    surf = Surface(2, 1)
    size = surf.width * 2 * surf.height
    path = tmp_path / "fb_mmap_reuse.bin"
    f, mm = open_fb_mmap(str(path), size)
    try:
        surf.clear(RED)
        surf.write_to_mmap(mm)
        assert path.read_bytes() == surf.to_rgb565()

        surf.clear(BLUE)
        surf.write_to_mmap(mm)
        assert path.read_bytes() == surf.to_rgb565()
    finally:
        mm.close()
        f.close()


def test_open_fb_mmap_sizes_a_fresh_file(tmp_path):
    from midicrt.clients.fb.surface import open_fb_mmap

    path = tmp_path / "fresh_fb.bin"
    assert not path.exists()
    size = 32
    f, mm = open_fb_mmap(str(path), size)
    try:
        assert len(mm) == size
        assert path.stat().st_size == size
    finally:
        mm.close()
        f.close()


def test_open_fb_mmap_resizes_pre_existing_file(tmp_path):
    from midicrt.clients.fb.surface import open_fb_mmap

    path = tmp_path / "existing_fb.bin"
    path.write_bytes(b"\xaa" * 5)  # wrong size, simulating a stale fixture
    size = 16
    f, mm = open_fb_mmap(str(path), size)
    try:
        assert len(mm) == size
        assert path.stat().st_size == size
    finally:
        mm.close()
        f.close()
