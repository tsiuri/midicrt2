"""Guard test — Phase 8 Task 2 (monochrome green-luminance conversion).

The 2026-08-08 gui-phase-decisions doc's ruling #1 ("MONOCHROME MANDATE":
"the system is monochrome green, always. The only 'color' is shading/
brightness level of green") means no `clients/fb/*.py` or `clients/
chrome.py` module may ever contain a raw RGB literal that isn't a green
shade — see `clients/fb/lum.py`'s module docstring for the luminance
framework this guards, and `docs/visual-audit.md` §9c for the audit finding
that named this exact gap (v2's old `_ROLL_CHANNEL_PALETTE`, an 8-hue
rainbow cycle, vs. v1's own `_CH_BASE_RGB = [(0, 255, 80)] * 16` — v1 was
ALREADY monochrome; only v2's pianoroll renderer had drifted from it).

Rule (derived directly from v1's own constants — `~/codex/midicrt/fb/
compositor.py`'s GREEN_BRIGHT/GREEN_MID/GREEN_DIM and `fb/
compositor_renderer.py`'s dimmer roll-grid/row-tint tones, read on the Pi,
read-only reference, confirmed by direct grep — see task-2-report.md for
the full constant table):

  1. R is 0 in EVERY real v1 green tier without exception (GREEN_BRIGHT=
     (0,255,80), GREEN_MID=(0,180,50), GREEN_DIM=(0,140,45),
     _CH_BASE_RGB=(0,255,80), _ROLL_H_DOT=(0,38,15), _ROLL_H_DOT_C=
     (0,68,27), _ROLL_V_BAR_DOT=(0,86,33), _ROLL_ACTIVE_ROW_BASE_RGB=
     (0,38,12) — not one has a nonzero red component). A rainbow hue (any
     of the old `_ROLL_CHANNEL_PALETTE` entries, e.g. (255, 60, 60) red or
     (60, 200, 255) cyan) fails on this half of the rule ALONE — every one
     of those 8 entries has R > 0.
  2. B never exceeds a small fraction of G in any of those same
     constants — the widest ratio among them is `_ROLL_H_DOT_C`'s
     27/68 ≈ 0.397. This test uses 0.45 as the threshold: comfortable
     headroom above that real maximum, without being loose enough to
     admit a "monochrome cyan/white" workaround that keeps R at 0 but
     blows out B (e.g. (0, 100, 100) would still be non-green and must
     still fail).

Scans literal 3-int tuples via `ast` (not regex), so it survives
reformatting and doesn't need to special-case non-color tuples elsewhere in
these files — verified by hand (and by this test itself, which would fail
loudly) that no non-color 3-all-int-literal tuple exists anywhere in the
current file set (sizes are 2-tuples, RGBA stamps in `fb/text.py` are
4-tuples, struct-unpack targets are never tuple literals).

NO ALLOWLIST (brief's explicit instruction) — `clients/fb/lum.py`'s own
named-tier constants are scanned exactly like every other file in scope,
and pass because they already satisfy the rule; nothing is exempted by
filename, function, or line comment.
"""
import ast
from pathlib import Path

_CLIENTS_DIR = Path(__file__).resolve().parent.parent / "src" / "midicrt" / "clients"
_TARGETS = sorted((_CLIENTS_DIR / "fb").glob("*.py")) + [_CLIENTS_DIR / "chrome.py"]

_MAX_B_OVER_G = 0.45  # see module docstring for the v1-derived provenance


def _literal_int3_tuples(path: Path) -> list[tuple[int, tuple[int, int, int]]]:
    """Return `(lineno, (a, b, c))` for every literal 3-element, all-int
    tuple appearing anywhere in `path`'s source — the exact shape any raw
    RGB color constant takes in this codebase (see `Color = tuple[int, int,
    int]` in `clients/fb/surface.py`)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Tuple) and len(node.elts) == 3):
            continue
        values: list[int] = []
        for elt in node.elts:
            v = elt.value if isinstance(elt, ast.Constant) else None
            if type(v) is not int:  # `is not int` (not isinstance) also rejects bool
                values = []
                break
            values.append(v)
        if len(values) == 3:
            found.append((node.lineno, (values[0], values[1], values[2])))
    return found


def test_guard_scope_is_not_accidentally_empty():
    # Sanity: the glob actually matched files, so a future rename/move of
    # clients/fb/ can't silently turn this whole guard into a no-op.
    assert len(_TARGETS) >= 3
    assert any(p.name == "app.py" for p in _TARGETS)
    assert any(p.name == "chrome.py" for p in _TARGETS)


def _is_green_shade(r: int, g: int, b: int) -> bool:
    if r != 0:
        return False
    if g == 0:
        return b == 0
    return 0 <= b <= _MAX_B_OVER_G * g


def test_every_literal_rgb_tuple_in_fb_and_chrome_is_a_green_shade():
    violations = []
    for path in _TARGETS:
        for lineno, (r, g, b) in _literal_int3_tuples(path):
            if not _is_green_shade(r, g, b):
                violations.append(f"{path.name}:{lineno} = ({r}, {g}, {b})")
    assert not violations, (
        "non-green RGB literal(s) found in a CRT renderer module — "
        "monochrome mandate violation (2026-08-08 gui-phase-decisions doc "
        "ruling #1):\n" + "\n".join(violations)
    )
