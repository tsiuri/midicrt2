"""fb/surface.py — RGB pixel surface + RGB565 framebuffer packing.

Wraps a PIL "RGB" Image as the drawing target for the CRT fb client.
Drawing (`clear`/`rect`) happens in ordinary 8-bit RGB; `to_rgb565()` packs
the buffer into the wire/device format on demand and `write_fb()` writes
that packed buffer to a framebuffer device node (or, in tests, a plain
file standing in for one).

Target hardware (Pi CRT framebuffer, confirmed against the real device —
see task-2 report): 800x475, stride 1600 bytes/row (== width*2, so no
per-row padding on this hardware), bits_per_pixel 16, RGB565.

RGB565 packing formula ported from v1's `fb/compositor.py::_rgb565()`
(`~/codex/midicrt/fb/compositor.py` on the Pi, read-only reference):
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
Each pixel is written little-endian (low byte first), matching v1's
`np.ndarray(..., dtype="<u2", buffer=mmap(...))` framebuffer view.

This module does not write to /dev/fb0 during Phase 2 Task 2 — v1 owns the
real CRT until Task 4's supervised smoke test exercises `write_fb()` for
real. All tests here use in-memory surfaces and plain files.
"""

import os
import sys
from array import array

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

    def to_rgb565(self) -> bytes:
        """Pack the RGB image into little-endian RGB565 bytes, row-major.

        Output length is always width * height * 2 bytes, tightly packed
        (no row padding — that's `write_fb()`'s job when a device stride
        requires it).
        """
        rgb = self.image.tobytes()  # row-major R,G,B,R,G,B,... (no padding)
        values = array(
            "H",
            (
                ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
                for i in range(0, len(rgb), 3)
            ),
        )
        if sys.byteorder == "big":
            # Not the case on any target hardware here (Pi is little-endian),
            # but keep the on-wire format little-endian regardless of host.
            values.byteswap()
        return values.tobytes()

    def write_fb(self, path: str, stride: int | None = None) -> None:
        """Write the packed RGB565 buffer to a framebuffer device (or file).

        `stride` is the device's line_length in bytes (e.g. read from
        `/sys/class/graphics/fb0/stride`). When it's wider than the tight
        row size (width*2), each row is padded with zero bytes to match —
        this is how Linux fbdev reports a line_length wider than the
        visible width on some GPUs/alignments. `stride=None` (default) or
        `stride == width*2` writes tightly-packed rows with no padding.

        Opens in "r+b" if the target already exists (the /dev/fb0 case —
        a character device can't be truncated/recreated) and "wb"
        otherwise (creates a fresh file, e.g. a test fixture path).
        """
        row_bytes = self.width * 2
        data = self.to_rgb565()

        if stride is None or stride == row_bytes:
            payload = data
        else:
            if stride < row_bytes:
                raise ValueError(f"stride {stride} is smaller than row size {row_bytes}")
            pad = bytes(stride - row_bytes)
            rows = (data[i:i + row_bytes] + pad for i in range(0, len(data), row_bytes))
            payload = b"".join(rows)

        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode, buffering=0) as f:
            f.write(payload)

    def save_png(self, path: str) -> None:
        """Save the surface as a PNG (for golden fixtures / debugging)."""
        self.image.save(path, format="PNG")
