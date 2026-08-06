"""fb/text.py — PSF bitmap font loading and text rendering onto a Surface.

Font asset origin: v1's default console font, copied verbatim from the Pi
system path v1 actually loads (v1 instantiates `PSFFont()` with no path
argument in `~/codex/midicrt/fb/compositor.py`, which defaults to
`fb/psf_font.py::DEFAULT_PSF`):
    /usr/share/consolefonts/Lat2-VGA8.psf.gz
Vendored into this repo at assets/Lat2-VGA8.psf.gz (md5 matches the system
file — see task-2 report) so the client has no system-font dependency.
It's a PSF1 font, mode=2 (has a Unicode table, 256 glyphs), 8x8 glyph cells
— confirmed by parsing the real header on the Pi, not merely assumed from
the filename.

PSF1/PSF2 parsing ported from v1's `~/codex/midicrt/fb/psf_font.py`
(read-only reference, not modified) — magic detection, PSF1/PSF2 header
layout, and the Unicode-table decoding for both formats. This module drops
v1's numpy-array rendering paths (`draw_*_buf`/`draw_*_buf16`) since this
Surface is a plain PIL "RGB" image, not a native RGB565 numpy buffer;
glyph painting instead ports v1's PIL `draw_char`/`draw_text` path (RGBA
glyph stamp cached per (glyph index, colour), pasted with itself as mask
so only foreground bits touch the destination — background pixels are
left untouched, matching v1's transparent-text behaviour).
"""

import gzip
import struct
from pathlib import Path

from PIL import Image

PSF1_MAGIC = 0x0436
PSF2_MAGIC = 0x864AB572

# assets/ lives at the repo root, a sibling of src/ (see this file's path:
# src/midicrt/clients/fb/text.py -> parents[4] is the repo root).
_DEFAULT_FONT_PATH = Path(__file__).resolve().parents[4] / "assets" / "Lat2-VGA8.psf.gz"

Color = tuple[int, int, int]


class Font:
    """A loaded PSF1/PSF2 bitmap font, glyph-image-cached per colour."""

    def __init__(self, path: str | Path = _DEFAULT_FONT_PATH) -> None:
        data = self._read(path)
        self.width = 0
        self.height = 0
        self._glyphs: list[bytes] = []
        self._unicode_map: dict[int, int] = {}  # codepoint -> glyph index
        self._glyph_cache: dict[tuple[int, Color], Image.Image] = {}

        magic16 = struct.unpack_from("<H", data, 0)[0]
        magic32 = struct.unpack_from("<I", data, 0)[0]
        if magic16 == PSF1_MAGIC:
            self._load_psf1(data)
        elif magic32 == PSF2_MAGIC:
            self._load_psf2(data)
        else:
            raise ValueError(f"unrecognised PSF magic: 0x{magic16:04x}")

    # ------------------------------------------------------------------
    # Loaders (ported from v1's fb/psf_font.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _read(path: str | Path) -> bytes:
        p = Path(path)
        raw = p.read_bytes()
        if str(path).endswith(".gz"):
            raw = gzip.decompress(raw)
        return raw

    def _load_psf1(self, data: bytes) -> None:
        mode = data[2]
        charsize = data[3]  # bytes per glyph == height; width is always 8
        self.width = 8
        self.height = charsize

        n_glyphs = 512 if (mode & 0x01) else 256
        glyph_end = 4 + n_glyphs * charsize
        raw = data[4:glyph_end]
        for i in range(n_glyphs):
            self._glyphs.append(raw[i * charsize:(i + 1) * charsize])

        has_unicode = bool(mode & 0x02)
        if has_unicode:
            self._parse_psf1_unicode(data[glyph_end:], n_glyphs)
        else:
            for cp in range(n_glyphs):
                self._unicode_map[cp] = cp

    def _parse_psf1_unicode(self, table: bytes, n_glyphs: int) -> None:
        pos = 0
        for glyph_idx in range(n_glyphs):
            while pos + 1 < len(table):
                cp = struct.unpack_from("<H", table, pos)[0]
                pos += 2
                if cp == 0xFFFF:
                    break
                if cp != 0xFFFE:  # 0xFFFE = start-of-sequence marker
                    self._unicode_map[cp] = glyph_idx

    def _load_psf2(self, data: bytes) -> None:
        (_magic, _version, hdr_size, flags,
         n_glyphs, bytes_per_glyph,
         self.height, self.width) = struct.unpack_from("<IIIIIIII", data, 0)

        raw = data[hdr_size:hdr_size + n_glyphs * bytes_per_glyph]
        for i in range(n_glyphs):
            self._glyphs.append(raw[i * bytes_per_glyph:(i + 1) * bytes_per_glyph])

        has_unicode = bool(flags & 0x01)
        if has_unicode:
            self._parse_psf2_unicode(data[hdr_size + n_glyphs * bytes_per_glyph:], n_glyphs)
        else:
            for cp in range(n_glyphs):
                self._unicode_map[cp] = cp

    def _parse_psf2_unicode(self, table: bytes, n_glyphs: int) -> None:
        pos = 0
        glyph_idx = 0
        while pos < len(table) and glyph_idx < n_glyphs:
            b = table[pos]
            if b == 0xFF:
                glyph_idx += 1
                pos += 1
                continue
            if b < 0x80:
                cp = b
                pos += 1
            elif b < 0xE0:
                cp = ((b & 0x1F) << 6) | (table[pos + 1] & 0x3F)
                pos += 2
            elif b < 0xF0:
                cp = ((b & 0x0F) << 12) | ((table[pos + 1] & 0x3F) << 6) | (table[pos + 2] & 0x3F)
                pos += 3
            else:
                cp = (((b & 0x07) << 18) | ((table[pos + 1] & 0x3F) << 12)
                      | ((table[pos + 2] & 0x3F) << 6) | (table[pos + 3] & 0x3F))
                pos += 4
            if cp != 0xFFFE:
                self._unicode_map[cp] = glyph_idx

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def glyph_index(self, char: str) -> int | None:
        """Resolve a character to a glyph index, falling back to '?'."""
        cp = ord(char)
        idx = self._unicode_map.get(cp)
        if idx is None:
            idx = self._unicode_map.get(0x3F)  # '?' fallback
        if idx is None or idx >= len(self._glyphs):
            return None
        return idx

    def glyph_image(self, idx: int, fg: Color) -> Image.Image:
        """Return a cached RGBA stamp for glyph `idx` in colour `fg`.

        Foreground pixels get full alpha, background pixels alpha 0, so
        `Surface.image.paste(stamp, (x, y), mask=stamp)` only overwrites
        foreground bits — background pixels are left untouched.
        """
        key = (idx, fg)
        img = self._glyph_cache.get(key)
        if img is None:
            img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            px = img.load()
            r, g, b = fg
            for row, byte in enumerate(self._glyphs[idx][:self.height]):
                for col in range(self.width):
                    if (byte >> (7 - col)) & 1:
                        px[col, row] = (r, g, b, 255)
            self._glyph_cache[key] = img
        return img


_default_font: Font | None = None


def load_font(path: str | Path | None = None) -> Font:
    """Load a PSF font. `path=None` loads and caches the vendored default
    (assets/Lat2-VGA8.psf.gz); an explicit path always loads fresh
    (uncached) — mainly useful for tests exercising the parser against a
    different PSF file.
    """
    global _default_font
    if path is None:
        if _default_font is None:
            _default_font = Font(_DEFAULT_FONT_PATH)
        return _default_font
    return Font(path)


def draw_text(surface, x: int, y: int, text: str, color: Color, font: Font | None = None) -> int:
    """Draw `text` onto `surface.image` at pixel (x, y) in `color`.

    Space characters paint nothing (background shows through, matching
    v1's transparent-text behaviour). Returns the pixel width drawn
    (`len(text) * font.width`), regardless of clipping/unknown glyphs.
    """
    if font is None:
        font = load_font()
    cx = x
    for char in text:
        if char != " ":
            idx = font.glyph_index(char)
            if idx is not None:
                stamp = font.glyph_image(idx, color)
                surface.image.paste(stamp, (cx, y), mask=stamp)
        cx += font.width
    return cx - x
