"""fb/surface.py — RGB pixel surface + RGB565 framebuffer packing.

Wraps a PIL "RGB" Image as the drawing target for the CRT fb client.
Drawing (`clear`/`rect`/`hline`/`vline`/`box`/`fill_column`/`dotted_hline`/
`dotted_vline`) happens in ordinary 8-bit RGB; `to_rgb565()` packs the
buffer into the wire/device format on demand. `write_fb()` and
`write_to_mmap()` write that packed buffer to a framebuffer device node
(or, in tests, a plain file standing in for one) — see their docstrings
for when to use which.

Convention: renderers (`clients/fb/app.py::render_frame` and friends) must
draw only through `Surface`'s methods below — never reach into
`surface.image` directly. That keeps every renderer portable to a future
non-PIL backend and keeps `to_rgb565()` the single place pixel format is
decided.

Target hardware (Pi CRT framebuffer, confirmed against the real device —
see task-2 report): 800x475, stride 1600 bytes/row (== width*2, so no
per-row padding on this hardware), bits_per_pixel 16, RGB565.

RGB565 packing formula ported from v1's `fb/compositor.py::_rgb565()`
(`~/codex/midicrt/fb/compositor.py` on the Pi, read-only reference):
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
Each pixel is written little-endian (low byte first), matching v1's
`np.ndarray(..., dtype="<u2", buffer=mmap(...))` framebuffer view.

`to_rgb565()` packing implementation (Phase 3 Task 2): benchmarked the
original pure-Python per-pixel `array("H", generator)` pack at 800x475 on
the Pi (~1240ms/frame avg over 30 runs — see task-2 report) against a
30fps budget (33ms/frame) and against this task's <40ms target. PIL's
"raw" encoder has no packer for any 16bpp rawmode when encoding FROM an
"RGB" image in this Pillow version (12.3.0 on the Pi) — confirmed by
probing every "BGR;16"/"RGB;16"/"BGR;15"/etc. variant, all of which raise
"No packer found from RGB to ..." (PIL DOES support decoding those
rawmodes via `Image.frombytes`, just not encoding via `Image.tobytes`).
Per the task brief's sanctioned fallback, this module now packs with
numpy (v1's own approach: vectorised `(r&0xF8)<<8 | (g&0xFC)<<3 | b>>3`
over a `(H,W,3)` uint8 array, cast to little-endian `<u2`) — ~26ms/frame
avg over 30 runs on the Pi, an ~47x speedup, comfortably under the 40ms
target. `numpy` is accordingly a new runtime dependency (see
pyproject.toml) — the only implementation swap this module needed.

This module does not write to /dev/fb0 during Phase 2 Task 2 — v1 owns the
real CRT until Task 4's supervised smoke test exercises `write_fb()` for
real. All tests here use in-memory surfaces and plain files.
"""

import mmap
import os

import numpy as np
from PIL import Image, ImageDraw

Color = tuple[int, int, int]


class Surface:
    """A drawable RGB pixel surface that can be packed to RGB565 bytes."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height), (0, 0, 0))

    def clear(self, color: Color) -> None:
        """Fill the entire surface with a solid RGB colour."""
        self.image = Image.new("RGB", (self.width, self.height), color)

    def rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        """Draw a filled rectangle at pixel (x, y), size (w, h).

        Clips silently to the surface bounds; a rectangle entirely off
        the surface (or with non-positive width/height) draws nothing.
        """
        if w <= 0 or h <= 0:
            return
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w) - 1
        y1 = min(self.height, y + h) - 1
        if x1 < x0 or y1 < y0:
            return
        ImageDraw.Draw(self.image).rectangle([x0, y0, x1, y1], fill=color)

    def hline(self, x: int, y: int, w: int, color: Color) -> None:
        """Draw a horizontal 1px-tall run, `w` px wide, starting at (x, y).

        Equivalent to `rect(x, y, w, 1, color)` — same clipping rules.
        """
        self.rect(x, y, w, 1, color)

    def vline(self, x: int, y: int, h: int, color: Color) -> None:
        """Draw a vertical 1px-wide run, `h` px tall, starting at (x, y).

        Equivalent to `rect(x, y, 1, h, color)` — same clipping rules.
        """
        self.rect(x, y, 1, h, color)

    def box(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        """Draw a 1px rectangle OUTLINE at (x, y), size (w, h) — the
        interior is left untouched (unlike `rect`, which fills it).

        Built from four `hline`/`vline` calls (one per edge), so it
        inherits their clipping and non-positive-size no-op behaviour for
        free. A box 1px wide or tall degenerates to a filled line/rect —
        the four edges of a 1px-thick box necessarily cover its whole
        area, so that's the correct outline for that size, not a bug.
        """
        if w <= 0 or h <= 0:
            return
        self.hline(x, y, w, color)           # top
        self.hline(x, y + h - 1, w, color)   # bottom
        self.vline(x, y, h, color)           # left
        self.vline(x + w - 1, y, h, color)   # right

    def dotted_hline(self, x: int, y: int, w: int, color: Color, stride: int = 2, phase: int = 0) -> None:
        """Draw a horizontal DOTTED run: only every `stride`-th pixel of the
        `w`-px span starting at `x` is lit (at `x + phase`, `x + phase +
        stride`, ...) — the pixels in between are left untouched, not
        painted background, so this reads as a faint backdrop grid a
        renderer draws UNDER other content, not a solid line with holes
        punched in it (the pianoroll paper-grid's dotted pitch-row/bar/beat
        guides — Phase 8 Task 3 — are this primitive's only caller so far).

        `phase` (mod `stride`) lets a caller offset the pattern per row/line
        — v1's own alternating-phase dotted rows (`row_idx & 1`) is exactly
        this. Clips silently to the surface bounds; a row entirely off the
        surface, or a non-positive `w`/`stride`, draws nothing.
        """
        if w <= 0 or stride <= 0 or not (0 <= y < self.height):
            return
        draw = ImageDraw.Draw(self.image)
        pts = [(px, y) for px in range(x + (phase % stride), x + w, stride) if 0 <= px < self.width]
        if pts:
            draw.point(pts, fill=color)

    def dotted_vline(self, x: int, y: int, h: int, color: Color, stride: int = 2, phase: int = 0) -> None:
        """Draw a vertical DOTTED run — the column analogue of
        `dotted_hline` above; same stride/phase/clipping semantics, own
        pixel untouched-between-dots convention.
        """
        if h <= 0 or stride <= 0 or not (0 <= x < self.width):
            return
        draw = ImageDraw.Draw(self.image)
        pts = [(x, py) for py in range(y + (phase % stride), y + h, stride) if 0 <= py < self.height]
        if pts:
            draw.point(pts, fill=color)

    def fill_column(self, x: int, y_bottom: int, h: int, color: Color, width: int = 1) -> None:
        """Draw a vertical bar `width` px wide and `h` px tall, bottom-
        aligned at row `y_bottom` (inclusive) and growing upward — the
        shape a spectrum-analyzer bar needs.

        Delegates straight to `rect`, so it clips exactly like `rect`
        (including a bar that's entirely off-surface drawing nothing).
        """
        self.rect(x, y_bottom - h + 1, width, h, color)

    def to_rgb565(self) -> bytes:
        """Pack the RGB image into little-endian RGB565 bytes, row-major.

        Output length is always width * height * 2 bytes, tightly packed
        (no row padding — that's `write_fb()`/`write_to_mmap()`'s job when
        a device stride requires it).

        Implementation: vectorised numpy pack — see the module docstring
        for the benchmark that motivated this over a pure-Python per-pixel
        loop, and why PIL's own encoders can't do this conversion.
        """
        arr = np.asarray(self.image, dtype=np.uint16)  # (height, width, 3): R,G,B
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        packed = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return packed.astype("<u2").tobytes()

    def _pack_strided(self, data: bytes, stride: int | None) -> bytes:
        """Pad `data` (tightly-packed RGB565 rows) to `stride` bytes/row.

        `stride` is the device's line_length in bytes (e.g. read from
        `/sys/class/graphics/fb0/stride`). When it's wider than the tight
        row size (width*2), each row is padded with zero bytes to match —
        this is how Linux fbdev reports a line_length wider than the
        visible width on some GPUs/alignments. `stride=None` or
        `stride == width*2` returns `data` unchanged (no padding).

        Shared by `write_fb()` and `write_to_mmap()` so both device-write
        paths agree on padding without duplicating the logic.
        """
        row_bytes = self.width * 2
        if stride is None or stride == row_bytes:
            return data
        if stride < row_bytes:
            raise ValueError(f"stride {stride} is smaller than row size {row_bytes}")
        pad = bytes(stride - row_bytes)
        rows = (data[i:i + row_bytes] + pad for i in range(0, len(data), row_bytes))
        return b"".join(rows)

    def write_fb(self, path: str, stride: int | None = None) -> None:
        """Write the packed RGB565 buffer to a framebuffer device (or file),
        opening (and closing) `path` fresh on every call.

        Use this for tests/one-shots (e.g. `--out` mode's single frame).
        The device render loop instead opens once and reuses the mapping
        via `open_fb_mmap()` + `write_to_mmap()` below — reopening a
        character device every frame is needless syscall overhead at
        30fps.

        Opens in "r+b" if the target already exists (the /dev/fb0 case —
        a character device can't be truncated/recreated) and "wb"
        otherwise (creates a fresh file, e.g. a test fixture path).
        """
        payload = self._pack_strided(self.to_rgb565(), stride)
        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode, buffering=0) as f:
            f.write(payload)

    def write_to_mmap(self, mm: mmap.mmap, stride: int | None = None) -> None:
        """Write the packed RGB565 buffer into an already-open mmap.

        This is the device render loop's per-frame fast path: open the
        framebuffer once with `open_fb_mmap()`, then call this every frame
        instead of `write_fb()` reopening the device each time. Same
        stride-padding semantics as `write_fb()` (see `_pack_strided`).
        """
        payload = self._pack_strided(self.to_rgb565(), stride)
        mm[:len(payload)] = payload

    def save_png(self, path: str) -> None:
        """Save the surface as a PNG (for golden fixtures / debugging)."""
        self.image.save(path, format="PNG")


def open_fb_mmap(path: str, size: int) -> tuple[object, mmap.mmap]:
    """Open `path` and mmap the first `size` bytes of it — the device
    loop's "open once per run" half of the mmap fast path (pair with
    `Surface.write_to_mmap()` for the per-frame write).

    For a regular file (the test-fixture / one-shot case), the file is
    created if missing and resized to exactly `size` bytes via
    `truncate()` — mmap requires the backing file to already be at least
    that long. For a character device (the real `/dev/fb0` case) the
    kernel already presents it at its full framebuffer size and
    `truncate()` isn't supported there, so that failure is swallowed —
    the device is trusted to already be the right size.

    Returns `(file, mmap)`; the caller owns both and must close them
    (mmap first, then file) when done — see the docstring of
    `Surface.write_to_mmap()`'s callers for the expected lifecycle.
    """
    # mmap's default ACCESS_DEFAULT maps PROT_READ|PROT_WRITE, which needs
    # the fd opened for read+write -- "wb" alone is write-only (O_WRONLY)
    # and mmap() rejects it with EACCES, so the fresh-file branch needs
    # "w+b" here even though write_fb()'s plain-write path is happy with
    # "wb" (it never mmaps its fd).
    mode = "r+b" if os.path.exists(path) else "w+b"
    f = open(path, mode, buffering=0)  # noqa: SIM115 -- caller owns the handle's lifetime, not us
    try:
        f.truncate(size)
    except OSError:
        pass  # character devices (e.g. /dev/fb0) reject truncate; already sized by the kernel
    mm = mmap.mmap(f.fileno(), size)
    return f, mm
