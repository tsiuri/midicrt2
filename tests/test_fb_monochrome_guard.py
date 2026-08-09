"""Guard test — Phase 8 Task 2 (monochrome green-luminance conversion),
hardened in the fix round after review.

The 2026-08-08 gui-phase-decisions doc's ruling #1 ("MONOCHROME MANDATE":
"the system is monochrome green, always. The only 'color' is shading/
brightness level of green") means no `clients/fb/*.py` or `clients/
chrome.py` module may ever paint a pixel that isn't a green shade — see
`clients/fb/lum.py`'s module docstring for the luminance framework this
guards, and `docs/visual-audit.md` §9c for the audit finding that named
this exact gap (v2's old `_ROLL_CHANNEL_PALETTE`, an 8-hue rainbow cycle,
vs. v1's own `_CH_BASE_RGB = [(0, 255, 80)] * 16` — v1 was ALREADY
monochrome; only v2's pianoroll renderer had drifted from it).

**Two deliberately separate layers, not one — this file's original
single-layer version (a pure `ast` scan of literal RGB tuples) was proven
evadable in review**: any color built from a variable, a function call, an
arithmetic expression, or a `list` instead of a `tuple` sails straight
through a literal-syntax scan untouched, since none of those are literal
`ast.Tuple`/`ast.List` nodes made of `ast.Constant` ints. Neither layer
alone is a complete guard; together they are:

  1. **Source-level (`test_every_literal_rgb_or_rgb_list_...`)**: an `ast`
     scan (not regex, so it survives reformatting) over every literal
     3-int `tuple` OR `list` in `clients/fb/*.py` + `clients/chrome.py`.
     Cheap, fast, and catches the most common accidental mistake (typing
     a raw color tuple/list straight into source) with a precise
     file:line pointer — but it is a SYNTAX check, not a behavior check,
     and is blind to anything constructed at runtime.
  2. **Output-level (`test_every_rendered_pixel_...`)**: renders every
     registered page (`clients/fb/app.py::RENDERERS`, using the same VM
     fixtures `tests/test_fb_render.py`'s own golden tests already use —
     imported, never re-typed) plus the three chrome strips into a real
     `Surface`, then inspects every ACTUAL pixel that ends up in the
     packed image. This is the layer that closes the evasion gap: it
     doesn't care how a color was constructed in source, only what number
     actually landed on the screen. `test_pixel_guard_catches_an_injected_
     colored_literal_the_ast_scan_would_miss` (TDD, see its own docstring)
     proves this empirically rather than just asserting it.

Rule (derived directly from v1's own constants — `~/codex/midicrt/fb/
compositor.py`'s GREEN_BRIGHT/GREEN_MID/GREEN_DIM and `fb/
compositor_renderer.py`'s dimmer roll-grid/row-tint tones, read on the Pi,
read-only reference, confirmed by direct grep — see task-2-report.md for
the full constant table): R is 0 in EVERY real v1 green tier without
exception, and B never exceeds a small fraction of G (widest real ratio
among v1's own constants: `_ROLL_H_DOT_C`'s 27/68 ≈ 0.397). Both layers
share the exact same `_is_green_shade` predicate below, so "green" means
one thing in this file, not two slightly-different rules that could drift
apart.

NO ALLOWLIST (brief's explicit instruction, both rounds) — `clients/fb/
lum.py`'s own tier constants are scanned exactly like every other file
(layer 1) and rendered exactly like every other page's chrome (layer 2's
`_draw_status`/`_draw_secondary`/`_draw_beatprogress` calls import their
colors from `lum.py`); nothing is exempted by filename, function, or line
comment.
"""
import ast
from pathlib import Path

import test_fb_render as fbr  # sibling test module -- reuse its VM/size fixtures, never re-type them

from midicrt.clients.fb import app
from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import load_font

_CLIENTS_DIR = Path(__file__).resolve().parent.parent / "src" / "midicrt" / "clients"
_TARGETS = sorted((_CLIENTS_DIR / "fb").glob("*.py")) + [_CLIENTS_DIR / "chrome.py"]

_MAX_B_OVER_G = 0.45  # see module docstring for the v1-derived provenance


def _is_green_shade(r: int, g: int, b: int) -> bool:
    if r != 0:
        return False
    if g == 0:
        return b == 0
    return 0 <= b <= _MAX_B_OVER_G * g


# -- layer 1: source-level ast scan ------------------------------------------

def _literal_int3_sequences(path: Path) -> list[tuple[int, tuple[int, int, int]]]:
    """Return `(lineno, (a, b, c))` for every literal 3-element, all-int
    `tuple` OR `list` appearing anywhere in `path`'s source. `list` is
    included (fix round, item 3a) because nothing in this codebase's own
    `Color = tuple[int, int, int]` type alias (`clients/fb/surface.py`) is
    enforced at runtime — a `[255, 60, 60]` literal handed to `Surface.rect`
    would work identically to a tuple and previously slipped straight past
    this scan, which only ever walked `ast.Tuple` nodes."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 3):
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


def test_every_literal_rgb_tuple_or_list_in_fb_and_chrome_is_a_green_shade():
    violations = []
    for path in _TARGETS:
        for lineno, (r, g, b) in _literal_int3_sequences(path):
            if not _is_green_shade(r, g, b):
                violations.append(f"{path.name}:{lineno} = ({r}, {g}, {b})")
    assert not violations, (
        "non-green RGB literal(s) found in a CRT renderer module — "
        "monochrome mandate violation (2026-08-08 gui-phase-decisions doc "
        "ruling #1):\n" + "\n".join(violations)
    )


# -- layer 2: output-level pixel scan ----------------------------------------
#
# One (size, draw-callable) case per page registered in `app.RENDERERS`,
# reusing the EXACT VM fixtures `tests/test_fb_render.py`'s own
# `test_*_golden_matches_frozen_fixture` tests already build (imported via
# the `fbr` alias above) -- same "one source of truth for what a page looks
# like" precedent the golden-freeze process itself follows. `_PAGE_CASES`'
# keys are asserted to exactly match `app.RENDERERS`' keys below
# (`test_page_cases_cover_every_registered_renderer`) so a future page added
# to `RENDERERS` without a matching entry here fails loudly instead of
# silently going unguarded.
_FONT = load_font()

# Phase 8 Task 4 review fix (Minor #2): fbr.PIANOROLL_VM alone has neither
# "row_tint" nor "overlap_flash" populated (both default to `[]` via
# render_pianoroll_frame's own `.get(key, [])` back-compat), so the
# pianoroll case below was exercising `_draw_pianoroll_row_tint`/
# `_draw_pianoroll_overlap_flash` only in their early-return branches —
# the output-level pixel layer never actually painted a tinted row or a
# flash region, only construction/by-reasoning ("lum()/_roll_note_color
# are the only color sources, both already green") covered those two
# paths. This variant adds one of each, reusing the same guide/note
# geometry `tests/test_fb_render.py`'s own row-tint/overlap-flash tests
# already probe (guide row 1 for the tint; note[1]'s row for the flash),
# so BOTH new draw functions actually run their real fill/rect calls here.
_PIANOROLL_VM_WITH_TINT_AND_FLASH = {
    **fbr.PIANOROLL_VM,
    "row_tint": [{"y": fbr.PIANOROLL_GRID["pitch_guide_ys"][1]["y"], "intensity": 0.6}],
    "overlap_flash": [{"y": fbr.PIANOROLL_NOTES[1]["y"], "x0": 0.45, "x1": 0.55,
                        "ch": 5, "vel": 0.9}],
}

_PAGE_CASES: dict[str, tuple[tuple[int, int], object]] = {
    "eventlog": (fbr.GOLDEN_SURFACE_SIZE, lambda s: app.render_frame(fbr.VM, s)),
    "voices": (fbr.VOICES_SURFACE_SIZE, lambda s: app.render_voices_frame(fbr.VOICES_VM, s)),
    "harmony": (fbr.HARMONY_SURFACE_SIZE, lambda s: app.render_harmony_frame(fbr.HARMONY_VM, s)),
    "tuner": (fbr.TUNER_SURFACE_SIZE, lambda s: app.render_tuner_frame(fbr.TUNER_LOCKED_VM, s)),
    "pianoroll": (fbr.PIANOROLL_SURFACE_SIZE,
                  lambda s: app.render_pianoroll_frame(_PIANOROLL_VM_WITH_TINT_AND_FLASH, s)),
    "spectrum": (fbr.SPECTRUM_SURFACE_SIZE, lambda s: app.render_spectrum_frame(fbr.SPECTRUM_VM, s)),
    "screensaver": ((64, 64), lambda s: app.render_screensaver_frame({"title": "SCREENSAVER"}, s)),
    "img2txtviz": (fbr.IMG2TXTVIZ_SURFACE_SIZE,
                   lambda s: app.render_img2txtviz_frame(fbr.IMG2TXTVIZ_VM, s)),
    "config": (fbr.CONFIG_SURFACE_SIZE, lambda s: app.render_config_frame(fbr.CONFIG_VM, s)),
    "help": (fbr.HELP_SURFACE_SIZE, lambda s: app.render_help_frame(fbr.HELP_VM, s)),
    "progchanges": (fbr.PROGCHANGES_SURFACE_SIZE,
                    lambda s: app.render_progchanges_frame(fbr.PROGCHANGES_VM, s)),
    "ccmonitor": (fbr.CCMONITOR_SURFACE_SIZE,
                  lambda s: app.render_ccmonitor_frame(fbr.CCMONITOR_VM, s)),
    "ccdashboard": (fbr.CCDASHBOARD_SURFACE_SIZE,
                    lambda s: app.render_ccdashboard_frame(fbr.CCDASHBOARD_VM, s)),
    "chordkey": (fbr.CHORDKEY_SURFACE_SIZE, lambda s: app.render_chordkey_frame(fbr.CHORDKEY_VM, s)),
    "sendnotes": (fbr.SENDNOTES_SURFACE_SIZE,
                  lambda s: app.render_sendnotes_frame(fbr.SENDNOTES_VM, s)),
}


def _draw_all_chrome_strips(surface: Surface) -> None:
    """Composes all three reserved chrome strips (reverse-video header
    fill + status/secondary/beatprogress text) on top of whatever page body
    is already drawn -- reuses the exact golden-frame VMs
    (`tests/test_fb_render.py`'s `GOLDEN_*_VM`) so this exercises the same
    real code path `_paint_frame` does on every non-screensaver page."""
    app._draw_secondary(surface, fbr.GOLDEN_ALERTS_VM, fbr.GOLDEN_TIMESIG_VM, _FONT)
    app._draw_status(surface, fbr.GOLDEN_STATUS_VM, _FONT)
    app._draw_beatprogress(surface, fbr.GOLDEN_BEATFLASH_VM, fbr.GOLDEN_LOOPPROGRESS_VM, _FONT)


def test_page_cases_cover_every_registered_renderer():
    assert set(_PAGE_CASES) == set(app.RENDERERS) | {"screensaver"}


def _find_pixel_violations(surface: Surface) -> list[str]:
    # `get_flattened_data()`, not the deprecated `getdata()` -- matches the
    # convention `tests/test_fb_render.py`'s own screensaver-guard tests
    # already use for a full-surface pixel scan.
    violations = []
    for r, g, b in surface.image.get_flattened_data():
        if not _is_green_shade(r, g, b):
            violations.append(f"({r}, {g}, {b})")
            if len(violations) >= 5:  # enough to prove it, no need to enumerate every pixel
                break
    return violations


def test_every_rendered_pixel_across_every_page_is_a_green_shade():
    all_violations = {}
    for name, (size, draw) in _PAGE_CASES.items():
        surf = Surface(*size)
        draw(surf)
        violations = _find_pixel_violations(surf)
        if violations:
            all_violations[name] = violations
    assert not all_violations, (
        "non-green pixel(s) found in a rendered page — monochrome mandate "
        f"violation at the OUTPUT level (not just source): {all_violations}"
    )


def test_every_rendered_pixel_including_chrome_strips_is_a_green_shade():
    # One full composition (page body + all three chrome strips), same
    # convention `_paint_frame` uses for every non-screensaver page --
    # covers `_draw_status`/`_draw_secondary`/`_draw_beatprogress`'s own
    # color usage, which the page-only cases above never touch.
    surf = Surface(*fbr.GOLDEN_SURFACE_SIZE)
    app.render_frame(fbr.VM, surf)
    _draw_all_chrome_strips(surf)
    violations = _find_pixel_violations(surf)
    assert not violations, (
        f"non-green pixel(s) found in composed chrome strips: {violations}"
    )


def test_pixel_guard_catches_an_injected_colored_literal_the_ast_scan_would_miss():
    """TDD proof (fix round, item 3b) that the pixel-level layer is not
    redundant with the ast-scan layer: builds a color the EXACT way a
    clever evasion would (a runtime computation, not a literal), paints it
    onto a Surface with the real `render_frame`/`Surface.rect` machinery,
    and asserts the pixel scan (reusing this file's own
    `_find_pixel_violations`) catches it — proving layer 2 inspects actual
    output, not source syntax. This function's own "colored literal" is
    deliberately built via a `tuple(int(c) for c in [...])` generator over
    a list of STRING constants, not an `ast.Tuple`/`ast.List` of int
    constants, specifically so the layer-1 ast scan run against THIS TEST
    FILE would not itself flag it (`_literal_int3_sequences` requires every
    element to already be an `int` constant; a `str` constant fed through
    a runtime `int()` call is invisible to it) -- the point is to prove
    layer 2 catches what layer 1 structurally cannot, not to re-prove
    layer 1 on a literal it already covers."""
    evil_red = tuple(int(c) for c in ["255", "60", "60"])  # str constants, not int -- evades layer 1
    assert not _is_green_shade(*evil_red)  # sanity: this really is a violation

    surf = Surface(*fbr.GOLDEN_SURFACE_SIZE)
    app.render_frame(fbr.VM, surf)
    surf.rect(0, 0, 4, 4, evil_red)  # simulates a colored pixel reaching real output
    violations = _find_pixel_violations(surf)
    assert violations, "pixel guard failed to catch an injected non-green pixel"
