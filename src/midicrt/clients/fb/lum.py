"""fb/lum.py — the monochrome green-luminance framework (Phase 8 Task 2).

The 2026-08-08 gui-phase-decisions doc's ruling #1 ("MONOCHROME MANDATE"):
"the system is monochrome green, always. The only 'color' is shading/
brightness level of green." `docs/visual-audit.md` (Phase 8 Task 1's v1
code audit) traced this to a single concrete gap: v1's real CRT compositor
was ALREADY monochrome everywhere — `fb/compositor.py`'s own
`GREEN_BRIGHT`/`GREEN_MID`/`GREEN_DIM` and `fb/compositor_renderer.py`'s
`_CH_BASE_RGB = [(0, 255, 80)] * 16` (identical for all 16 channels) — only
v2's pianoroll renderer had drifted into a from-scratch 8-hue
`_ROLL_CHANNEL_PALETTE` rainbow cycle. This module is the shared "pick a
green shade" primitive every `clients/fb/*.py` renderer (and, in principle,
`clients/chrome.py`, though that module is pure text and has never needed
a Color at all) now routes through, so a colored pixel can never creep back
in unnoticed — see `tests/test_fb_monochrome_guard.py` for the repo-wide
scan that fails the build the moment one does.

v1's exact constants (`~/codex/midicrt/fb/compositor.py` /
`fb/compositor_renderer.py` on the Pi, read-only reference, confirmed via
direct grep — see task-2-report.md for the full transcript):

    GREEN_BRIGHT   = _rgb565(0, 255, 80)   # fb/compositor.py:46
    GREEN_MID      = _rgb565(0, 180, 50)   # fb/compositor.py:47
    GREEN_DIM      = _rgb565(0, 140, 45)   # fb/compositor.py:48
    _CH_BASE_RGB   = [(0, 255, 80)] * 16   # compositor_renderer.py:45 —
                                            # confirms v1's pianoroll was
                                            # ALREADY one hue, every channel
    _VEL_BRIGHTNESS_FLOOR = 0.5            # compositor_renderer.py:78 —
                                            # velocity brightness floors at
                                            # 50%, linear to 100% at vel=127
    _ROLL_H_DOT    = _rgb565(0, 38, 15)    # compositor_renderer.py:81 —
                                            # "faint between-note dotted
                                            # rows" (v1's own comment)

`LUM_FAINT` below is `_ROLL_H_DOT` — v1 never names a fourth "GREEN_FAINT"
tier of its own, but this is the dimmest green it ever actually paints
on-screen (per its own "faint" comment), which is exactly the missing
4th rung the task brief's `LUM_BRIGHT/MID/DIM/FAINT` naming asks for.

Two distinct, deliberately NOT-unified mechanisms (a design decision worth
stating plainly — see task-2-report.md's self-review for the full
reasoning):

  1. Four NAMED TIERS (`LUM_BRIGHT`/`LUM_MID`/`LUM_DIM`/`LUM_FAINT`) —
     byte-for-byte copies of v1's own independently-authored constants,
     used for fixed chrome/text roles (header bars, body text, secondary
     text). v1 itself never derives GREEN_MID/GREEN_DIM from GREEN_BRIGHT
     by any formula — they're three separately chosen RGB triples whose
     B:G ratios don't actually agree with each other (0.314 / 0.278 /
     0.321) — so forcing them onto one continuous curve would be inventing
     a precision v1's own source doesn't have. These are looked-up
     directly, not computed.
  2. `lum(level)` — the CONTINUOUS ramp v1 genuinely does use, ported from
     its "the monochrome-conversion blueprint" mechanism (audit §9c):
     `_velocity_scale()` scales `_CH_BASE_RGB`'s (i.e. `GREEN_BRIGHT`'s)
     own R/G/B components by one linear brightness fraction — "green
     channel scaled, [with GREEN_BRIGHT's own already-small] matched R/B
     fraction" preserved throughout, never re-hued. `lum(0.0)` is exactly
     `LUM_OFF` (black); `lum(1.0)` is exactly `LUM_BRIGHT`. This is used
     wherever a renderer needs an actual continuous 0..1 brightness (the
     pianoroll's velocity ramp, the img2txtviz cell field) — the "interpolate
     between [named tiers]" the task brief also asks for is satisfied at
     lum()'s two endpoints (0.0 and 1.0 land exactly on LUM_OFF/LUM_BRIGHT)
     while everything strictly between is a smooth single-hue fade, not a
     multi-tier blend — the same shape v1's own LUT actually has.
"""

from midicrt.clients.fb.surface import Color

# -- Named tiers: exact copies of v1's own constants ------------------------
LUM_OFF: Color = (0, 0, 0)
LUM_FAINT: Color = (0, 38, 15)     # v1 _ROLL_H_DOT ("faint... dotted rows")
LUM_DIM: Color = (0, 140, 45)      # v1 GREEN_DIM
LUM_MID: Color = (0, 180, 50)      # v1 GREEN_MID
LUM_BRIGHT: Color = (0, 255, 80)   # v1 GREEN_BRIGHT / _CH_BASE_RGB

# -- Paper-grid tiers (Phase 8 Task 3): the two v1 roll-guide constants
# task-2-report.md §2/§7 explicitly deferred adding ("not adopted... grid
# guides are a separate, later task per the brief") -- byte-for-byte copies,
# same "looked up directly, never computed" convention as the four above.
LUM_FAINT_C: Color = (0, 68, 27)     # v1 _ROLL_H_DOT_C ("slightly brighter on C rows")
LUM_BAR_GUIDE: Color = (0, 86, 33)   # v1 _ROLL_V_BAR_DOT ("dotted bar-tracking verticals")

# Symbolic lookup for the tiers — lets a per-page ramp table (below)
# name a fixed role by string instead of importing+repeating the tuple, and
# gives the guard test / a future debug tool one place to enumerate "every
# tier v1 actually uses" without re-deriving the list.
TIERS: dict[str, Color] = {
    "off": LUM_OFF,
    "faint": LUM_FAINT,
    "faint_c": LUM_FAINT_C,
    "dim": LUM_DIM,
    "mid": LUM_MID,
    "bright": LUM_BRIGHT,
    "bar_guide": LUM_BAR_GUIDE,
}


def lum(level: float) -> Color:
    """Scale `LUM_BRIGHT` linearly by `level` (0..1, clamped) — v1's own
    continuous-brightness mechanism (see module docstring's `_velocity_scale`
    citation): every component (including the small, already-baked-in blue
    fraction) scales together, so the hue never shifts, only its intensity.

    `lum(0.0) == LUM_OFF` and `lum(1.0) == LUM_BRIGHT` exactly (integer
    arithmetic at the endpoints, no rounding drift); everything between is
    `round(component * level)`.
    """
    level = max(0.0, min(1.0, level))
    r, g, b = LUM_BRIGHT
    return (round(r * level), round(g * level), round(b * level))


# -- Per-page ramp tables, AS DATA ------------------------------------------
#
# Semantic role -> `lum()` level (0..1), declared once per page instead of
# scattered as inline arithmetic through each renderer — keeps every
# renderer's brightness policy auditable/tunable from one place (task
# brief: "per-page ramp tables AS DATA"). Pages whose only color needs are
# the four fixed named tiers above (the header bar, body text, accent
# lines — the overwhelming majority of `clients/fb/app.py`'s renderers,
# already monochrome before this task and unchanged by it) have no entry
# here; a ramp table only exists where a renderer needs an actual
# continuous 0..1 brightness value fed through `lum()`.
RAMPS: dict[str, dict[str, float]] = {
    # Pianoroll note brightness (docs/visual-audit.md §9c, "the
    # monochrome-conversion blueprint"): v1's exact `_VEL_BRIGHTNESS_FLOOR`
    # — velocity 0 never goes fully dark, it floors at 50% of LUM_BRIGHT;
    # velocity 127 (vel=1.0 in v2's already-normalized 0..1 VM field)
    # reaches 100% (`lum(1.0)` == `LUM_BRIGHT` exactly).
    "pianoroll": {"velocity_floor": 0.5, "velocity_ceiling": 1.0},
    # img2txtviz's procedural field is already emitted engine-side as a
    # final [0, 1] brightness value per cell (see analyzers/img2txtviz.py's
    # own module docstring) — this ramp is the identity mapping, declared
    # here (rather than assumed inline) so the contract "0.0 -> black,
    # 1.0 -> full LUM_BRIGHT" is visible as data, not just as a comment.
    "img2txtviz": {"cell_min": 0.0, "cell_max": 1.0},
}
