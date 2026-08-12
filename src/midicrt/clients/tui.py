"""Minimal TUI client: renders the current page, sends actions.

Page dispatch
-------------
`RENDERERS` maps a page name to its `(vm, width, height) -> list[str]`
renderer -- today only `"eventlog"` exists. On connect the client asks the
engine (via `describe`) which page is CURRENT rather than assuming
"eventlog", and subscribes to that page's topic. On a `page_changed` event
it calls `base.switch_topic()` (unsubscribe old topic, subscribe new) and
looks up the new renderer by name, falling back to `_render_unknown` for a
page name this client build doesn't recognise -- wire compat is
additive-only, so an older client can meet a newer server's extra page
without crashing.

Key dispatch (Phase 4 Task 1, docs/phase4-notes.md)
----------------------------------------------------
Key handling used to be a hardcoded module-level `_KEY_ACTIONS = {"c":
"eventlog.clear", "n": "page.next"}` dict, with `q` (quit) special-cased
separately before it was even consulted. Both are now driven entirely by
the engine's OWN keymap (`engine/keymap.py`), fetched once via `describe`
at connect (`fetch_keymap`, `clients/base.py`) and re-fetched whenever a
`keymap_changed` event arrives (the engine's `config.reload` action
re-read `keymap.toml`) -- `dispatch_key` (also `clients/base.py`, shared
with the fb client) is the one place that resolves a pressed key against
`state["keymap"]` and either quits locally (`client.quit`, the only
`client.*` pseudo-action with real meaning) or sends the named action to
the engine. An unmapped key is silently ignored, matching the prior
hardcoded behavior exactly.

Chrome (phase 3 task 3)
------------------------
The bottom terminal row is now a reverse-video transport status bar (same
treatment as the header), built from `clients/chrome.py`'s shared
`status_text()` so its wording is identical to the fb client's status
strip. This is the "page owns height-2 rows" split phase3-notes.md asks
for: `render_lines`'s own contract is UNCHANGED (it still renders its
header + body, "the page's height-1 rows"), but `run_tui` now calls it
with `term.height - 1` instead of `term.height`, reserving exactly one row
for chrome to append below it. The header stays page-owned code (its text
is page-specific -- title, count, keybinds -- unlike the status bar, which
is identical regardless of which page is showing) rather than a second
chrome extraction; only the NEW status row is chrome's to own here.
`run_tui` subscribes to `overlay.status` ALONGSIDE the current page's
topic (multi-topic subscribe -- `drain_latest`/`wait_first_snapshot`
already supported many topics at once, just never asked for more than one
before this).

Chrome, part 2 (phase 3 task 6): a second row for alerts/time-signature
------------------------------------------------------------------------
`overlay.alerts` (stuck-note warnings) and `overlay.timesig` (time-
signature estimate) share ONE additional row directly above the original
status row (`render_secondary_row`, `clients/chrome.py`'s
`secondary_status_text()` -- alerts win whenever any are active, else the
row shows the time-signature line). `run_tui` now reserves the bottom TWO
rows for chrome (`render_lines`'s own contract stays unchanged, just
invoked with `term.height - 2`) and subscribes to both new overlay topics
alongside `overlay.status`.

Chrome, part 3 (phase 3 task 9): a THIRD row, below status, for beatflash
+ loopprogress
------------------------------------------------------------------------
`overlay.beatflash` (beat-synced flash pulse) and `overlay.loopprogress`
(8-bar cyclic position bar) share a further row -- `render_beatprogress_row`,
`clients/chrome.py`'s `beatprogress_row_text()` -- positioned as the new
TRUE BOTTOM-MOST row, with the status row now one row above it. This
mirrors v1's own physical layout exactly, not an arbitrary stacking
choice: `plugins/beatflash.py`/`plugins/loopprogress.py` draw at v1's
literal bottom two screen rows, BELOW `plugins/timeclock.py`'s own row
(ported as `overlay.status`) -- see `clients/chrome.py`'s module comment
above `beatprogress_row_text` for the full row-offset evidence. `run_tui`
now reserves the bottom THREE rows for chrome (`render_lines`'s contract
again unchanged, invoked with `term.height - 3`) and subscribes to both
new overlay topics alongside the existing three.
"""
import json
import time

from midicrt.behaviors.screensaver import SCREENSAVER_PAGE
from midicrt.clients import chrome
from midicrt.clients.base import (
    KEY_HELP_TOGGLE,
    KEY_QUIT,
    ClientError,
    EngineClient,
    current_page_topic,
    dispatch_key,
    drain_latest,
    fetch_keymap_sections,
    fetch_show_fps,
    switch_topic,
    wait_first_snapshot,
)


def _fit(text: str, width: int) -> str:
    return text[:width].ljust(width)


def _tail(lines: list, body_h: int) -> list:
    """Slice the last body_h items. Guards the height<=0 case where a plain
    `lines[-0:]` slice would (surprisingly) return everything instead of []."""
    return lines[-body_h:] if body_h > 0 else []


def render_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  ({vm['count']} events)  [c]lear [n]ext page [q]uit"
    body_h = height - 1
    tail = _tail(vm["lines"], body_h)
    body = [_fit(" " + ln["text"], width) for ln in tail]
    while len(body) < body_h:
        body.insert(0, " " * width)
    return [_fit(header, width)] + body


def _render_unknown(vm: dict, width: int, height: int) -> list[str]:
    """Fallback for a page name this client build has no renderer for."""
    header = _fit("(no renderer for this page)  [q]uit", width)
    body = [" " * width] * max(0, height - 1)
    return [header] + body


def render_status_row(vm: dict, width: int) -> str:
    """TUI's presentation of the shared chrome status text (clients/chrome.py):
    fit/pad to the exact terminal width, mirroring `render_lines`'s own
    `_fit` usage. Reverse-video styling is applied by the caller (run_tui's
    render loop, same as the header) -- stays plain-text/pure/testable."""
    return _fit(chrome.status_text(vm), width)


def render_secondary_row(alerts_vm: dict, timesig_vm: dict, width: int,
                         polylimit_vm: dict | None = None, sysex_vm: dict | None = None) -> str:
    """TUI's presentation of the shared second chrome row (phase-3 task 6:
    stuck-note alerts when any are active, else the time-signature
    estimate -- see clients/chrome.py's `secondary_status_text()`). Same
    fit/pad treatment as `render_status_row`.

    Phase 9 Task 2 (stuck-linger): `secondary_status_text`'s own
    `alerts_text()` now also falls back to a lingering "STUCK CLEARED:
    ..." message (`config.stuck_hold_after`) -- shown here identically to
    an active alert, zero-touch. `clients/chrome.py::secondary_status_dim`
    exists for the fb client's DIMMED luminance treatment specifically
    (monochrome mandate: "dim" is a shading level) -- deliberately NOT
    consulted here, matching this module's own existing precedent
    (`run_tui`'s docstring: "the TUI has no dimming primitive, and
    burn-in isn't a real concern on [a terminal]").

    Phase 9 Task 2 (poly-limit chrome flash): `polylimit_vm` (OPTIONAL,
    defaults to "not flashing" -- unchanged for every pre-existing 3-arg
    call site) is forwarded straight into `secondary_status_text()`'s own
    3rd param. Unlike the DIM treatment above, the flash is plain TEXT
    (`"POLY LIMIT EXCEEDED"`) with no luminance concern at all, so it IS
    shown here identically to the fb client -- nothing about it needs the
    dimming primitive the TUI lacks.

    Phase 9 Task 5 (SysEx manager): `sysex_vm` (OPTIONAL, same convention)
    forwards straight into `secondary_status_text()`'s own 4th param --
    plain text here too, shown identically to the fb client."""
    return _fit(chrome.secondary_status_text(alerts_vm, timesig_vm, polylimit_vm, sysex_vm), width)


def render_beatprogress_row(beatflash_vm: dict, loopprogress_vm: dict, width: int) -> str:
    """TUI's presentation of the shared THIRD chrome row (phase-3 task 9:
    v1's beatflash + loopprogress rows, combined onto one -- see
    `clients/chrome.py`'s `beatprogress_row_text()` for the full v1
    row-offset evidence and why they share a row). That shared function is
    already width-aware and returns exactly `width` characters; this thin
    wrapper exists purely for naming symmetry with `render_status_row`/
    `render_secondary_row` above."""
    return chrome.beatprogress_row_text(beatflash_vm, loopprogress_vm, width)


# -- voices page (phase-3 task 4) --------------------------------------------
#
# Bar scale: 8 segments matches v1's zvoicemonitor.py per-channel poly-limit
# default (POLY_LIMIT_CH) -- a fixed visual scale only, not an enforced
# limit (no limit/warning behavior is ported here -- see
# analyzers/voices.py's module docstring), so 8+ simultaneously held voices
# on one channel just shows a full bar rather than clipping or flagging.
# Deliberately NOT shared with the fb renderer's own bar math below (unlike
# clients/chrome.py's status text, which both clients must render
# word-for-word identically): a per-page body widget isn't the page-agnostic
# chrome status bar phase3-notes.md's sharing contract is about.
_VOICES_BAR_SEGMENTS = 8
_VOICES_NAME_WIDTH = 12


def _voices_bar(active: int, segments: int = _VOICES_BAR_SEGMENTS) -> str:
    filled = min(max(active, 0), segments)
    return "▓" * filled + "░" * (segments - filled)


def _voices_row_text(row: dict) -> str:
    bar = _voices_bar(row["active"])
    name = f"{row['name']:<{_VOICES_NAME_WIDTH}.{_VOICES_NAME_WIDTH}}"
    return f"{row['ch']:02d} {name} {bar} {row['active']:>2d}/{row['peak']:<2d}"


def render_voices_lines(vm: dict, width: int, height: int) -> list[str]:
    """16 fixed channel rows (not a scrolling tail like `render_lines`'s
    eventlog): channels 1..16 always render top-down in that order, padded
    with blank rows at the BOTTOM if `height` leaves more room than 16 rows,
    and simply cut off after the Nth row (channels stay in order, extras at
    the bottom are dropped) if there's less."""
    header = f"{vm['title']}  (poly {vm['total']}/{vm['total_peak']})  [n]ext page [q]uit"
    body_h = height - 1
    rows = [_fit(_voices_row_text(r), width) for r in vm["rows"]]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [_fit(header, width)] + body


# -- harmony page (phase-3 task 5) -------------------------------------------
#
# Layout mirrors v1's Notes-page harmony section (docs/evidence-phase2-
# smoke/after.png): "Chord:"/"Scale:" each get a "Last/2nd/3rd/4th" label
# row + a values row (v1's `_render_slots`), then "Inside:"/"Outside:"
# (v2's adapted list-shaped fields, see pages/harmony.py's v1-field
# mapping docstring), "Chord conf:"/"Scale conf:" (v1's confidence+
# missing-tones line, only ever populated for the CURRENT/index-0
# candidate), "Key:", a tension bar (v1's exact block characters, "█"
# filled / "░" empty -- distinct from `_voices_bar`'s own "▓"/"░" choice
# above; per that function's own comment, per-page body widgets are NOT
# required to share a look, only the page-agnostic chrome status bar is),
# "Harm.rhy:", and "Motif:". Fixed 12 body rows (unlike eventlog's
# scrolling tail or voices' 16 channel rows) -- padded/cut exactly like
# `render_voices_lines` when `height` doesn't match.
_HARMONY_LABELS = ("Last", "2nd", "3rd", "4th")
_HARMONY_TENSION_BAR_SEGMENTS = 20   # matches v1's zharmony.py notes.py `bar_max = 20`


def _harmony_header_text(vm: dict) -> str:
    return f"{vm['title']}  (key: {vm['key'] or '?'})  [n]ext page [q]uit"


def _harmony_slot_lines(prefix: str, items: list[dict], width: int) -> tuple[str, str]:
    slot_w = max(8, (width - len(prefix) - 1) // 4)
    label_row = prefix + " " + "".join(lbl.ljust(slot_w) for lbl in _HARMONY_LABELS)
    values = []
    for i in range(4):
        name = items[i]["name"] if i < len(items) else None
        values.append((name or "--")[:slot_w].ljust(slot_w))
    value_row = prefix + " " + "".join(values)
    return label_row, value_row


def _harmony_list_line(prefix: str, names: list[str]) -> str:
    return f"{prefix} {' '.join(names) if names else '-'}"


def _harmony_conf_missing_line(prefix: str, items: list[dict]) -> str:
    if items and items[0]["conf"] is not None:
        missing = " ".join(items[0]["missing"]) or "-"
        return f"{prefix} {items[0]['conf']:0.2f}  missing: {missing}"
    return f"{prefix} --  missing: -"


def _harmony_key_line(vm: dict) -> str:
    line = f"Key: {vm['key'] or '?'}"
    if vm.get("key_alternatives"):
        line += f"  (alts: {', '.join(vm['key_alternatives'])})"
    return line


def _harmony_tension_line(vm: dict) -> str:
    filled = round(vm["tension"] * _HARMONY_TENSION_BAR_SEGMENTS)
    bar = "█" * filled + "░" * (_HARMONY_TENSION_BAR_SEGMENTS - filled)
    worst = vm.get("tension_worst_interval") or ""
    worst_str = f"  [{worst}]" if worst else ""
    return f"Tension: {bar}  {vm['tension']:.2f}  {vm.get('tension_label', '')}{worst_str}"


def _harmony_rhythm_line(vm: dict) -> str:
    hr = vm["harmonic_rhythm"]
    if hr and hr.get("changes_per_bar") is not None:
        return f"Harm.rhy: {hr['changes_per_bar']:.1f} ch/bar  {hr['label']}"
    return "Harm.rhy: --"


def _harmony_motif_line(vm: dict) -> str:
    motif = vm["motif"]
    if motif and motif.get("found"):
        return f"Motif: {motif['pattern']}  [x{motif['count']}]"
    return "Motif: --"


def _harmony_body_lines(vm: dict, width: int) -> list[str]:
    chord_hdr, chord_vals = _harmony_slot_lines("Chord:", vm["chords"], width)
    scale_hdr, scale_vals = _harmony_slot_lines("Scale:", vm["scales"], width)
    return [
        chord_hdr,
        chord_vals,
        scale_hdr,
        scale_vals,
        _harmony_list_line("Inside:", vm["inside"]),
        _harmony_list_line("Outside:", vm["outside"]),
        _harmony_conf_missing_line("Chord conf:", vm["chords"]),
        _harmony_conf_missing_line("Scale conf:", vm["scales"]),
        _harmony_key_line(vm),
        _harmony_tension_line(vm),
        _harmony_rhythm_line(vm),
        _harmony_motif_line(vm),
    ]


def render_harmony_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_harmony_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _harmony_body_lines(vm, width)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


# -- tuner page (phase-3 task 6; live-wired + status line Phase 9 Task 3) ----
#
# Layout mirrors v1's `pages/tuner.py::draw()` text rows: v1's own status
# line ("Input:OK/… Dev:<device> SR:<sr>", `pages/tuner.py:172-174`, its
# own separate body row in every state) is ported into the HEADER here
# instead (fix round, review finding 2) -- following `_spectrum_header_
# text`'s own already-established v2 convention (device-in-header,
# conditional on `available`) rather than v1's literal separate-row/
# three-field layout, keeping both audio pages visually consistent. Body
# rows: "no audio input" (no device open, mirrors `render_spectrum_lines`'
# identical `available` gate) / "Listening... Conf:.../Level:... dB" (device
# open, no pitch locked) / "Note:.../Pitch:.../Cents:.../Conf:.../Level:..."
# + a "Tuning: <meter>" row (signal locked). `tuning_meter` is reused from
# analyzers/tuner.py, not duplicated, same "shared pure function"
# convention as `analyzers.theory.NOTE_NAMES` in the harmony renderers.
def _tuner_header_text(vm: dict) -> str:
    if not vm.get("available", True):
        return f"{vm['title']}  [n]ext page [q]uit"
    device = vm.get("device") or "default"
    return f"{vm['title']}  (device: {device})  [n]ext page [q]uit"


def _tuner_body_lines(vm: dict) -> list[str]:
    # Phase 9 Task 3: "no audio input" -- the SAME text `render_spectrum_
    # lines` already uses for its own identical `available` gate --
    # distinct from "audio present, no pitch locked" (has_signal=False
    # with available still True, below). Defaults `available` to True so
    # older VMs with no such key (this module's own pre-Task-3 fixtures)
    # keep rendering the "Listening..."/locked states unchanged.
    if not vm.get("available", True):
        return ["no audio input", ""]
    if not vm.get("has_signal"):
        return [
            f"Listening...  Conf:{vm['confidence']:.2f}  Level:{vm['db']:5.1f} dB",
            "",
        ]
    from midicrt.analyzers.tuner import tuning_meter

    meter = tuning_meter(vm["cents"], 40)
    line1 = (f"Note:{vm['note']:<4}  Pitch:{vm['hz']:7.2f} Hz  Cents:{vm['cents']:+6.1f}  "
             f"Conf:{vm['confidence']:.2f}  Level:{vm['db']:5.1f} dB")
    return [line1, "Tuning: " + meter]


def render_tuner_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_tuner_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _tuner_body_lines(vm)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


# -- pianoroll page (phase-3 task 7; paper-grid: phase-8 task 3 SKIPPED here) -
#
# Phase 8 Task 3 ("paper" grid) intentionally does NOT touch this renderer:
# `docs/visual-audit.md` §9b confirms v1's own TEXT renderer never had a
# background grid either (dotted guides are exclusively a §9c fb-compositor
# feature) -- v2's TUI was already at honest parity, and `vm["grid"]` (the
# new VM key that task adds) is simply left unread here, same as any other
# renderer-specific field this one doesn't need.
#
# Layout: header row (title + projection mode/zoom + fixed pitch range),
# then a QUANTIZED note grid body -- the ENGINE already produced normalized
# [0,1] x0/x1/y/vel coordinates (pages/pianoroll.py's module docstring),
# this renderer's only job is bucketing them onto `width` columns x
# `body_h` rows (docs/phase3-notes.md's "renderers only quantize" rule).
#
# Unlike v1's fixed one-row-per-semitone grid (`pitch_high` computed FROM
# the terminal's own available rows, `ui/renderers/text/renderer.py`'s
# PianoRollWidget branch), the engine's `range.lo`/`range.hi` here is a
# FIXED span independent of any client's terminal size -- so a single row
# may bucket more than one semitone when `height` is short, or sit blank
# when it's tall. A per-row note-name label (v1's `f"{note_name:>7} │"`
# prefix) would often be misleading under that many-semitones-per-row
# bucketing, so this renderer drops it; the fixed range is shown once in
# the header instead ("range 36-83").
#
# Glyph choice ("charred", ported byte-for-byte from v1's actual DEFAULT
# experience -- `ui/renderers/text/renderer.py::TextRenderer._velocity_
# char`'s thresholds: >=96/127 "█", >=48/127 "▓", >0 "▒", else " ") --
# deliberately NOT v1's alternate per-channel "dense" PIXEL_STYLE (channel-
# cycled shade/ANSI-color glyphs, `ui/renderers/pixel.py::_pixel_char`).
# Every other v2 TUI renderer returns plain, unstyled text with an exact
# `len(line) == width` per row -- styling, where it exists at all
# (eventlog's bold "accent" lines), is applied OUTSIDE the renderer by
# `run_tui`'s own loop off a separate "style" field, never baked into the
# returned string itself. Embedding raw ANSI color escapes per-glyph here
# would be the first renderer in this codebase to vary a line's true
# rendered width from its Python `len()`. Channel distinction stays
# available via each note's own `ch` field for a renderer that wants it --
# the fb pixel renderer below uses it for real per-channel color, which a
# monochrome-by-default terminal can't do -- this one reproduces v1's
# actual default (`PIXEL_STYLE="text"`), not its optional "dense" variant.
_ROLL_VEL_HIGH = 96 / 127
_ROLL_VEL_MID = 48 / 127


def _roll_glyph(vel: float) -> str:
    if vel >= _ROLL_VEL_HIGH:
        return "█"
    if vel >= _ROLL_VEL_MID:
        return "▓"
    if vel > 0:
        return "▒"
    return " "


def _pianoroll_header_text(vm: dict) -> str:
    w = vm["window"]
    rng = vm["range"]
    return (f"{vm['title']}  ({w['mode']} zoom {w['zoom']:.2f}, "
            f"range {rng['lo']}-{rng['hi']})  [n]ext page [q]uit")


def _pianoroll_grid(vm: dict, width: int, body_h: int) -> list[list[float]]:
    """Quantize `vm['notes']` onto a `body_h` x `width` grid of velocities
    (0.0 = empty cell), picking the HIGHEST-velocity note covering each
    cell when two overlap -- matches v1's own `_best_visible_columns`'s
    identical tie-break for overlapping notes."""
    grid = [[0.0] * width for _ in range(max(0, body_h))]
    if width <= 0 or body_h <= 0:
        return grid
    for note in vm["notes"]:
        row = min(body_h - 1, max(0, int(note["y"] * body_h)))
        c0 = min(width - 1, max(0, int(note["x0"] * width)))
        c1 = min(width - 1, max(0, int(note["x1"] * width)))
        c1 = max(c1, c0)
        for c in range(c0, c1 + 1):
            grid[row][c] = max(grid[row][c], note["vel"])
    return grid


def render_pianoroll_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_pianoroll_header_text(vm), width)
    body_h = height - 1
    if body_h <= 0:
        return [header]
    grid = _pianoroll_grid(vm, width, body_h)
    body = [_fit("".join(_roll_glyph(v) for v in row), width) for row in grid]
    return [header] + body


# -- spectrum page (phase-3 task 8) -------------------------------------------
#
# Block-character bars, per v1's own `_bar_rows` (pages/audiospectrum.py on
# the Pi, read-only reference): each display column averages a slice of
# `vm["bins"]` (v1's exact column-averaging math, `n = min(width,
# len(bins))` columns used -- a terminal WIDER than the bin count leaves
# the extra trailing columns blank rather than upsampling, matching v1
# exactly), filled bottom-up with solid "█" for `round(val*height)` rows.
# `peak_hold` (v2 addition -- v1 has NO peak-hold at all, see analyzers/
# spectrum.py's module docstring) overlays one "-" tick per column at its
# own (separately column-averaged) row, only drawn over an otherwise-blank
# cell so it never hides a live "█" bar-top.
_SPECTRUM_BAR_GLYPH = "█"
_SPECTRUM_PEAK_GLYPH = "-"


def _spectrum_columns(values: list[float], n: int) -> list[float]:
    """v1's `_bar_rows` column-averaging step, standalone: reduce `values`
    (length >= n) down to exactly `n` columns, each the average of its
    slice (or the single nearest value when `n == len(values)`)."""
    if n <= 0 or not values:
        return []
    step = len(values) / n
    cols = []
    for i in range(n):
        j0, j1 = int(i * step), int((i + 1) * step)
        cols.append(values[j0] if j1 <= j0 else sum(values[j0:j1]) / (j1 - j0))
    return cols


def _spectrum_bar_rows(height: int, width: int, levels: list[float],
                        peaks: list[float] | None = None) -> list[str]:
    """Pure port of v1's `_bar_rows`, extended with an optional peak-hold
    tick overlay (v2 addition -- see module comment above)."""
    if height <= 0 or width <= 0 or not levels:
        return [" " * width for _ in range(max(0, height))]
    n = min(width, len(levels))
    rows = [[" "] * width for _ in range(height)]
    for i, val in enumerate(_spectrum_columns(levels, n)):
        h = min(height, round(val * height))
        for r in range(height - 1, height - h - 1, -1):
            rows[r][i] = _SPECTRUM_BAR_GLYPH
    if peaks:
        for i, val in enumerate(_spectrum_columns(peaks, n)):
            r = height - 1 - min(height - 1, round(val * height))
            if rows[r][i] == " ":
                rows[r][i] = _SPECTRUM_PEAK_GLYPH
    return ["".join(row) for row in rows]


def _spectrum_header_text(vm: dict) -> str:
    if not vm.get("available"):
        return f"{vm['title']}  [n]ext page [q]uit"
    device = vm.get("device") or "default"
    return f"{vm['title']}  (device: {device})  [n]ext page [q]uit"


def render_spectrum_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_spectrum_header_text(vm), width)
    body_h = height - 1
    if body_h <= 0:
        return [header]
    if not vm.get("available"):
        body = [_fit("no audio input", width)] + [" " * width] * (body_h - 1)
        return [header, *body[:body_h]]
    rows = _spectrum_bar_rows(body_h, width, vm["bins"], vm.get("peak_hold"))
    return [header] + [_fit(r, width) for r in rows]


# -- screensaver page (phase-3 task 9) ---------------------------------------
#
# All-blank rows, no header text at all -- see pages/screensaver.py's module
# docstring for the v1 comparison (v1 zeroes the ENTIRE real framebuffer,
# bypassing every plugin including chrome). `run_tui`'s render loop (below)
# special-cases `state["page"] == SCREENSAVER_PAGE` at its one chrome-
# painting call site to skip `term.reverse()` on this renderer's blank
# header AND skip painting the three chrome rows below it (task-9 review
# fix -- a prior cut left them lit, a real CRT burn-in regression).
# `SCREENSAVER_PAGE` is imported from `behaviors.screensaver` (2nd review
# pass, Minor fix) rather than hardcoded here a second time.


def render_screensaver_lines(vm: dict, width: int, height: int) -> list[str]:
    return [" " * width for _ in range(max(0, height))]


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# The engine already emits a FIXED `GRID_ROWS` x `GRID_COLS` grid of final
# [0,1] brightness values (analyzers/img2txtviz.py's own module docstring,
# citing the design spec's §5 "renderers... quantize normalized view-models
# to the raster" rule) -- this renderer's only job is a nearest-neighbor
# UPSAMPLE from that small grid onto `width` x `body_h` terminal cells (the
# reverse direction of
# `_pianoroll_grid`'s continuous-to-bucket quantization above, same "the
# engine emits one normalized shape, every client quantizes it to its own
# raster independently" spirit), then an index into `vm["charset"]`
# (already darkest-to-brightest, `invert` already applied engine-side) to
# pick each cell's glyph.
def _img2txtviz_header_text(vm: dict) -> str:
    flags = "INV " if vm.get("invert") else ""
    return (f"{vm['title']}  notes:{vm['active_notes']:02d}  "
            f"energy:{vm['energy']:.2f}  splash:{vm['vel_splash']:.2f}  {flags}"
            f"[n]ext page [q]uit")


def _img2txtviz_grid_lines(vm: dict, width: int, body_h: int) -> list[str]:
    grid = vm["grid"]
    charset = vm["charset"]
    gh = len(grid)
    gw = len(grid[0]) if gh else 0
    if gh == 0 or gw == 0 or body_h <= 0 or width <= 0 or not charset:
        return [" " * width for _ in range(max(0, body_h))]
    n = len(charset)
    lines = []
    for ty in range(body_h):
        gy = min(gh - 1, ty * gh // body_h)
        row = grid[gy]
        chars = []
        for tx in range(width):
            gx = min(gw - 1, tx * gw // width)
            idx = min(n - 1, max(0, int(row[gx] * n)))
            chars.append(charset[idx])
        lines.append("".join(chars))
    return lines


def render_img2txtviz_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_img2txtviz_header_text(vm), width)
    body_h = height - 1
    if body_h <= 0:
        return [header]
    return [header] + _img2txtviz_grid_lines(vm, width, body_h)


# -- config page (phase-3 task 10) -------------------------------------------
#
# A plain "label: value" dump -- see pages/configview.py's module docstring
# for why this is a fixed flat list rather than v1's recursive JSON
# tree/editor. Same pad/cut-to-height convention as
# `render_harmony_lines`/`render_voices_lines`.
def _config_header_text(vm: dict) -> str:
    return f"{vm['title']}  [n]ext page [q]uit"


def _config_body_lines(vm: dict) -> list[str]:
    lines = ["-- Config --"]
    lines += [f"{r['label']}: {r['value']}" for r in vm["config_rows"]]
    lines.append("")
    lines.append("-- Engine --")
    lines += [f"{r['label']}: {r['value']}" for r in vm["engine_rows"]]
    return lines


def render_config_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_config_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _config_body_lines(vm)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


def screensaver_row_texts(header_line: str, body_lines: list[str], width: int) -> list[str]:
    """The FULL blank-frame content for the screensaver page (task-9
    review fix): `header_line`/`body_lines` (already all-blank, from
    `render_screensaver_lines`) plus THREE further blank rows standing in
    for the secondary/status/beatprogress chrome rows `run_tui` would
    otherwise paint in reverse video. Pulled out as a pure function --
    unlike the reverse-video assembly around it, which needs a real
    `blessed.Terminal` for its escape codes -- purely so the "screensaver
    means EVERY row goes blank, no chrome exempted" contract is directly
    unit-testable. `run_tui` positions these plain (no `term.reverse()`)."""
    blank_row = " " * width
    return [header_line, *body_lines, blank_row, blank_row, blank_row]


# -- help page (phase-3 task 12, gap ports; keymap section: Phase 5 Task 3) ---
#
# Same "-- Section --" + "label: value" dump convention as
# `render_config_lines` (pages/help.py's view_model is deliberately the same
# `{page_rows, action_rows, keymap_rows}`-shaped list-of-dicts). See
# pages/help.py's own module docstring for why this describe-data reference
# IS the v1 Help page's parity port, not a literal keybinding-list
# transcription.
def _help_header_text(vm: dict) -> str:
    return f"{vm['title']}  [n]ext page [q]uit"


def _help_body_lines(vm: dict) -> list[str]:
    lines = ["-- Pages --"]
    lines += [f"{r['label']}: {r['value']}" for r in vm["page_rows"]]
    lines.append("")
    lines.append("-- Actions --")
    lines += [f"{r['label']}: {r['value']}" for r in vm["action_rows"]]
    # Phase 5 Task 3 (docs/phase5-notes.md cheap-wins bundle: "help page
    # renders live keymap"): a THIRD section, appended AFTER Actions --
    # `.get(...)`, not `vm["keymap_rows"]`, so an older/test-double vm
    # dict missing this key entirely (this module's own pre-Task-3 test
    # fixtures, still valid input shapes) renders exactly as before,
    # rather than raising a `KeyError`.
    keymap_rows = vm.get("keymap_rows", [])
    if keymap_rows:
        lines.append("")
        lines.append("-- Keymap --")
        lines += [f"{r['label']}: {r['value']}" for r in keymap_rows]
    return lines


def render_help_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_help_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _help_body_lines(vm)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


# -- program changes page (phase-3 task 12, gap ports) ------------------------
#
# Byte-for-byte the same layout as `render_lines` (eventlog's own renderer):
# scrolling tail of text lines, since pages/progchanges.py deliberately
# reuses eventlog's own `{title, count, lines}` VM shape (see that module's
# docstring).
def render_progchanges_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  ({vm['count']} events)  [n]ext page [q]uit"
    body_h = height - 1
    tail = _tail(vm["lines"], body_h)
    body = [_fit(" " + ln["text"], width) for ln in tail]
    while len(body) < body_h:
        body.insert(0, " " * width)
    return [_fit(header, width)] + body


# -- CC monitor page (phase-3 task 12, gap ports) ------------------------------
#
# 16 fixed channel rows (same "top-down in order, pad/cut to height"
# convention as `render_voices_lines`) -- each row's text is the channel's
# recent-CC window, plain text since a controller number has no natural
# bar scale (see fb/app.py's own `render_ccmonitor_frame` comment).
def _ccmonitor_row_text(ch_vm: dict) -> str:
    cells = " ".join(f"CC{e['cc']:02d}:{e['value']:03d}" for e in ch_vm["recent"])
    return f"{ch_vm['ch']:02d}  {cells}"


def render_ccmonitor_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  [n]ext page [q]uit"
    body_h = height - 1
    rows = [_fit(_ccmonitor_row_text(r), width) for r in vm["channels"]]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [_fit(header, width)] + body


# -- CC dashboard page (phase-3 task 12, gap ports) -----------------------------
#
# One row per tracked (channel, cc) pair -- a "Ch01 CC074:100" label, a
# block-character bar proportional to the value (v1's `ccgraph.py` own
# "█" fill), and a "LIVE"/"N.Ns ago" freshness label -- matching v1's
# layout, same pad/cut-to-height convention as the other fixed-list pages.
_CCDASH_BAR_SEGMENTS = 20


def _ccdashboard_bar(value: int, segments: int = _CCDASH_BAR_SEGMENTS) -> str:
    filled = round(segments * min(max(value, 0), 127) / 127)
    return "█" * filled + "░" * (segments - filled)


def _ccdashboard_age_text(entry: dict) -> str:
    return "LIVE" if entry["fresh"] else f"{entry['age_s']:.1f}s"


def _ccdashboard_row_text(entry: dict) -> str:
    label = f"Ch{entry['ch']:02d} CC{entry['cc']:03d}:{entry['value']:03d}"
    bar = _ccdashboard_bar(entry["value"])
    age = _ccdashboard_age_text(entry)
    return f"{label}  {bar}  {age:>6}"


def render_ccdashboard_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  [n]ext page [q]uit"
    body_h = height - 1
    rows = [_fit(_ccdashboard_row_text(e), width) for e in vm["entries"]]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [_fit(header, width)] + body


# -- chord+key page (phase-3 task 12, gap ports) -------------------------------
#
# Same text-row layout as fb/app.py's `render_chordkey_frame` -- see that
# module and pages/chordkey.py's own docstrings for the full v1
# (`pages/chordkey.py`) field mapping.
def _chordkey_header_text(vm: dict) -> str:
    return f"{vm['title']}  [n]ext page [q]uit"


def _chordkey_key_lines(key: dict) -> tuple[str, str]:
    if not key["label"]:
        top = key["top"]
        line1 = f"Key: ?  top:{top['label']} {top['pct']}%" if top else "Key: ?"
    else:
        tag = "~" if key["ambiguous"] else "="
        line1 = f"Key{tag} {key['label']}  {key['pct']}% (thr {key['threshold_pct']}%)"
    if key["alternatives"]:
        alt_txt = " | ".join(f"{a['label']} {a['pct']}%" for a in key["alternatives"][:2])
        line2 = f"alts: {alt_txt}"
    elif key["ambiguous"]:
        line2 = "alts: near-threshold / ambiguous"
    else:
        line2 = "alts: -"
    return line1, line2


def _chordkey_body_lines(vm: dict) -> list[str]:
    lines = [f"Recent PCs: {' '.join(vm['recent_pcs']) or '(none)'}", "Chord candidates:"]
    if not vm["chords"]:
        lines.extend(["(no chord match yet)", "", ""])
    else:
        for i, c in enumerate(vm["chords"]):
            miss = " ".join(c["missing"]) or "-"
            lines.append(f"{i + 1}) {c['label']}  {c['pct']:3d}%  missing:{miss}")
        for _ in range(len(vm["chords"]), 3):
            lines.append("")
    lines.append("")
    lines.append("Stabilized key:")
    k1, k2 = _chordkey_key_lines(vm["key"])
    lines.extend([k1, k2])
    lines.append(f"Function: {vm['function'] or '?'}")
    return lines


def render_chordkey_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_chordkey_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _chordkey_body_lines(vm)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


# -- send notes page (phase-3 task 12, gap ports) -------------------------------
#
# Same status-line + keymap-hint layout as fb/app.py's
# `render_sendnotes_frame` -- see pages/sendnotes.py's own module docstring
# for the full v1 (`pages/sendnotes.py`) field mapping.
def _sendnotes_header_text(vm: dict) -> str:
    return f"{vm['title']}  [n]ext page [q]uit"


def _sendnotes_status_text(vm: dict) -> str:
    dev = vm["device"] or "(not open)"
    return (f"Dev: {dev}  Ch:{vm['channel']:02d}  Oct:{vm['octave']:+d}  "
            f"Vel:{vm['velocity']:03d}  Gate:{vm['gate_ms']}ms  Active:{vm['active']}")


def _sendnotes_body_lines(vm: dict) -> list[str]:
    return [
        _sendnotes_status_text(vm),
        "",
        "Keys: z s x d c v g b h n j m (, l . ; /) -- white/black keys",
        "[,] ch-+/  [-]/[+] oct  [-]/[=] vel  g/h gate",
    ]


def render_sendnotes_lines(vm: dict, width: int, height: int) -> list[str]:
    header = _fit(_sendnotes_header_text(vm), width)
    body_h = height - 1
    rows = [_fit(ln, width) for ln in _sendnotes_body_lines(vm)]
    body = rows[:body_h] if body_h > 0 else []
    while len(body) < body_h:
        body.append(" " * width)
    return [header] + body


RENDERERS = {"eventlog": render_lines, "voices": render_voices_lines,
             "harmony": render_harmony_lines, "tuner": render_tuner_lines,
             "pianoroll": render_pianoroll_lines, "spectrum": render_spectrum_lines,
             "screensaver": render_screensaver_lines,
             "img2txtviz": render_img2txtviz_lines, "config": render_config_lines,
             "help": render_help_lines, "progchanges": render_progchanges_lines,
             "ccmonitor": render_ccmonitor_lines, "ccdashboard": render_ccdashboard_lines,
             "chordkey": render_chordkey_lines, "sendnotes": render_sendnotes_lines}

_SUBSCRIBE_RATE = 10.0
# How long a rejected-action message stays on the status row (bindings
# review fix, below) -- long enough for a human watching the CRT to
# actually read it, short enough that it doesn't linger forever over
# unrelated later status updates.
_ERROR_DISPLAY_S = 3.0


def _normalize_key(key) -> str:
    """Normalize a `blessed.Terminal.inkey()` result to the string
    `clients/base.py::dispatch_key` looks up in the keymap. Phase 10 Task A
    (docs/demo-feedback-2026-08-12.md item 11): a SEQUENCE keypress
    (arrows, and any future special key) resolves to blessed's own
    symbolic `key.name` ("KEY_UP"/"KEY_DOWN"/...) -- `str(key)` on a
    sequence is instead the raw multi-byte escape code, which never
    matches anything in `state["keymap"]` (this is WHY arrow keys
    previously did nothing at all here, matching the demo feedback's own
    "are arrow keycodes even bound... TUI?" question). An ordinary
    printable keypress, or the blank/no-keypress timeout case, is
    unaffected -- `is_sequence` is False for both, `str(key)` unchanged
    from before this function existed.

    Extracted as its own pure function -- same "pull the per-keypress
    body out for direct testability" precedent `clients/fb/app.py::
    _dispatch_evdev_key` already set for the fb client -- takes anything
    duck-typing blessed's `Keystroke` (`.is_sequence`, `.name`, `str()`),
    so a test can pass a trivial stand-in with no real blessed Terminal/
    tty involved."""
    return (key.name or "") if key.is_sequence else str(key)


def _handle_key_press(client, key: str, state: dict) -> bool:
    """Resolve `key` against `state["keymap"]` and dispatch it -- the ONE
    place `run_tui`'s main loop must distinguish an action-dispatch
    failure from a lost connection (bindings review, live-reproduced
    Critical finding). `dispatch_key` (`clients/base.py`) raises
    `ClientError` for BOTH a rejected action (bad/missing args, unknown
    action -- e.g. a keymap.toml entry that somehow slipped past
    `engine/keymap.py::filter_known_actions`'s own args-schema check, or a
    roster mismatch after a `config.reload`) AND a genuinely dead
    connection, with no reliable way to tell them apart by TYPE or by
    parsing the message text. This function therefore treats EVERY
    `ClientError` caught here as "an action failed, not fatal" -- recorded
    into `state["last_error"]`/`state["last_error_until"]` as a transient
    status-line message (`_active_error_text` renders it) -- and always
    returns `False` (never signals quit) in that case. A genuine
    disconnect is still caught, just not HERE: it's detected
    AUTHORITATIVELY via the reader thread's own EOF/None sentinel,
    surfacing through `drain_latest`'s/`wait_first_snapshot`'s OWN
    `ClientError` in `run_tui`'s main loop below -- unaffected by this
    function ever swallowing one. Returns `True` only for the
    `client.quit` pseudo-action.

    Phase 8 Task 6 (help overlay, docs/gui-phase-decisions-2026-08-08.md
    keymap revamp): `state["help_overlay"]` is the sticky bool this
    function owns (mirrors `state["learn_armed"]`'s own sticky-flag
    precedent right below in this module). Two rules, checked in this
    order (module docstring: "ANY key dismisses"):
      1. If the overlay IS currently showing, THIS key dismisses it and
         is SWALLOWED -- never reaches `dispatch_key`/the engine.
      2. Otherwise, `KEY_HELP_TOGGLE` (`client.help_toggle`) sets the flag
         and returns `False` (never quits)."""
    if state.get("help_overlay"):
        state["help_overlay"] = False
        return False
    try:
        outcome = dispatch_key(client, key, state["keymap"])
    except ClientError as exc:
        state["last_error"] = str(exc)
        state["last_error_until"] = time.time() + _ERROR_DISPLAY_S
        return False
    if outcome == KEY_HELP_TOGGLE:
        state["help_overlay"] = True
        return False
    return outcome == KEY_QUIT


def _active_error_text(state: dict) -> str | None:
    """The transient status-line error message recorded by
    `_handle_key_press`, if still within its `_ERROR_DISPLAY_S`-second
    display window -- `None` once expired (or if none was ever recorded),
    so `run_tui`'s repaint falls back to the normal status text with no
    explicit clearing step needed."""
    if state.get("last_error") and time.time() < state.get("last_error_until", 0):
        return state["last_error"]
    return None


# -- DAW-style MIDI learn status-line flash (Phase 4 Task 3,
# docs/phase4-notes.md) ------------------------------------------------------
#
# `learn_bound`/`learn_cancelled` reuse the SAME transient-message mechanism
# as `_handle_key_press`'s own `last_error`/`_active_error_text` right above
# (fixed `_ERROR_DISPLAY_S`-second display window, overwrites the status
# line while active) -- via a PARALLEL state slot (`learn_msg`/`learn_msg_
# until`), not the literal same one: a learn outcome isn't an error (bound
# successfully is good news), and the two must be able to coexist without
# one clobbering the other's expiry. `learn_armed` itself is a separate,
# STICKY flag (`state["learn_armed"]`, plain bool) -- there's no fixed
# duration for "how long a human takes to hit a key/knob after arming", so
# it stays true for as long as the arm is outstanding, cleared the moment
# either outcome event arrives.

_LEARN_EVENT_NAMES = {"learn_armed", "learn_bound", "learn_cancelled"}


def _set_learn_message(state: dict, text: str) -> None:
    state["learn_msg"] = text
    state["learn_msg_until"] = time.time() + _ERROR_DISPLAY_S


def _active_learn_message(state: dict) -> str | None:
    if state.get("learn_msg") and time.time() < state.get("learn_msg_until", 0):
        return state["learn_msg"]
    return None


def _apply_learn_event(state: dict, msg: dict) -> None:
    """Update `state` for a `learn_armed`/`learn_bound`/`learn_cancelled`
    event -- called from `run_tui`'s own `on_event` closure for exactly
    these three event names (`_LEARN_EVENT_NAMES`). `learn_armed` sets the
    sticky flag with no transient message of its own (the "waiting for
    MIDI..." text is a STATIC status while armed, not a flash -- rendered
    directly off `state["learn_armed"]` at the call site, see `run_tui`'s
    main loop); either outcome clears that flag and records a transient
    confirmation via `_set_learn_message`."""
    name = msg.get("name")
    if name == "learn_armed":
        state["learn_armed"] = True
        return
    state["learn_armed"] = False
    if name == "learn_bound":
        binding_id = msg.get("data", {}).get("binding", {}).get("id", "?")
        _set_learn_message(state, f"LEARN: bound to {binding_id}")
    elif name == "learn_cancelled":
        reason = msg.get("data", {}).get("reason", "?")
        _set_learn_message(state, f"LEARN: cancelled ({reason})")


# -- help overlay (Phase 8 Task 6) -------------------------------------------
#
# TUI equivalent of `clients/fb/app.py::_draw_help_overlay` -- an
# alternate-screen-style boxed panel (module docstring's own "TUI:
# alternate-screen box" framing) rather than a dim-backdrop pixel rect
# (the TUI has no dimming primitive, and burn-in isn't a real concern on
# a terminal emulator anyway -- see chrome.py's own indicator docstring
# for why the TUI skips the fb-specific burn-in machinery entirely). Reuses
# `chrome.overlay_lines()` for CONTENT (global + current-page sections),
# same as the fb renderer -- only the ASCII border framing here is TUI-
# specific.


def render_help_overlay_box(keymap_global: dict, keymap_page: dict, page_name: str,
                            width: int, height: int,
                            roster: list[str] | None = None) -> tuple[int, int, list[str]]:
    """Returns `(box_x, box_y, rows)`: the box's top-left terminal
    position and its own full row strings (border included) -- sized to
    the actual content, centered, clamped to `width`x`height`. `run_tui`
    blits each row via `term.move_xy(box_x, box_y + i) + row`, writing
    ONLY the box's own columns so the rest of whatever frame was already
    printed underneath is left completely undisturbed outside the box's
    own rectangle (module docstring: "not a page switch" -- the page
    keeps rendering beneath; here that means literally: this function
    never touches the terminal itself, and the caller only overwrites the
    exact cells the box occupies). `roster` (optional, the live page
    cycle order) resolves `page.jump` entries to their target page name --
    see `clients/chrome.py::overlay_lines`'s own docstring."""
    lines = chrome.overlay_lines(keymap_global, keymap_page, page_name, roster)
    if not lines:
        lines = ["(no bindings)"]
    inner_w = max(1, min(max(0, width - 4), max(len(line) for line in lines)))
    box_w = inner_w + 4
    box_h = min(height, len(lines) + 2)
    box_x = max(0, (width - box_w) // 2)
    box_y = max(0, (height - box_h) // 2)
    rows = ["+" + "-" * (box_w - 2) + "+"]
    for i in range(box_h - 2):
        content = lines[i][:inner_w] if i < len(lines) else ""
        rows.append("| " + content.ljust(inner_w) + " |")
    rows.append("+" + "-" * (box_w - 2) + "+")
    return box_x, box_y, rows


def run_tui(socket_path: str) -> int:
    import blessed

    client = EngineClient(socket_path)
    overlay_topics = [chrome.OVERLAY_STATUS_TOPIC, chrome.OVERLAY_ALERTS_TOPIC,
                       chrome.OVERLAY_TIMESIG_TOPIC, chrome.OVERLAY_BEATFLASH_TOPIC,
                       chrome.OVERLAY_LOOPPROGRESS_TOPIC, chrome.OVERLAY_POLYLIMIT_TOPIC,
                       chrome.OVERLAY_SYSEX_TOPIC]
    try:
        client.connect()
        page, topic = current_page_topic(client)
        keymap_bundle = fetch_keymap_sections(client)
        # Phase 10 Task A (docs/demo-feedback-2026-08-12.md item 4): fetched
        # ONCE here, same boot-time-only contract as keymap_bundle's own
        # "hints_enabled" -- see clients/base.py::fetch_show_fps's own
        # docstring for why this is a separate describe() field, not folded
        # into keymap_bundle.
        show_fps = fetch_show_fps(client)
        client.subscribe([topic, *overlay_topics], max_rate=_SUBSCRIBE_RATE)
    except ClientError as exc:
        print(f"midicrt tui: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    state = {"page": page, "topic": topic, "keymap": keymap_bundle["effective"],
             "keymap_global": keymap_bundle["global"], "keymap_page": keymap_bundle["page"],
             "keymap_hints_enabled": keymap_bundle.get("hints_enabled", True),
             # Page CYCLE order, fetched once -- fixed for the daemon's
             # whole life (see clients/fb/app.py::_run_device's identical
             # comment); resolves page.jump entries in the help overlay.
             "roster": list(keymap_bundle.get("roster", [])),
             "help_overlay": False,
             "status_vm": dict(chrome.DEFAULT_STATUS_VM),
             "alerts_vm": dict(chrome.DEFAULT_ALERTS_VM), "timesig_vm": dict(chrome.DEFAULT_TIMESIG_VM),
             "beatflash_vm": dict(chrome.DEFAULT_BEATFLASH_VM),
             "loopprogress_vm": dict(chrome.DEFAULT_LOOPPROGRESS_VM),
             "polylimit_vm": dict(chrome.DEFAULT_POLYLIMIT_VM),
             "sysex_vm": dict(chrome.DEFAULT_SYSEX_VM),
             "learn_armed": False,
             # Phase 10 Task A: "show_fps" is the boot-time config gate;
             # "_fps_last_t" tracks the wall-clock time of the last ACTUAL
             # repaint (not every loop iteration -- this client only
             # repaints when `dirty`, see the render loop below) so the
             # measured figure is the true draw cadence, matching v1's own
             # `_frame_dt`-per-redraw intent (`~/codex/midicrt/midicrt.py:
             # 1093-1094`) rather than a busier "how often did we poll"
             # number this loop's own `term.inkey(timeout=0.05)` pacing
             # would otherwise produce.
             "show_fps": show_fps, "_fps_last_t": None}

    def _render_vm(page: str, vm: dict) -> dict:
        """Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9):
        TUI's own consumer of `chrome.extrapolate_pianoroll_vm` -- see
        `clients/fb/app.py::_run_device`'s identical `_render_vm` helper
        for the full rationale (shared logic, same defensive-against-an-
        older-server contract); this is the TUI's copy only because each
        client owns its own render-loop `state` closure, not because the
        behavior differs at all."""
        if page != "pianoroll":
            return vm
        window = vm.get("window")
        origin_ts = window.get("origin_ts") if isinstance(window, dict) else None
        elapsed_s = time.time() - origin_ts if isinstance(origin_ts, (int, float)) else 0.0
        return chrome.extrapolate_pianoroll_vm(vm, elapsed_s)

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, _SUBSCRIBE_RATE)
            state["page"], state["topic"] = new_page, new_topic
        elif msg.get("kind") == "event" and msg.get("name") == "keymap_changed":
            # Phase 4 Task 1: the engine's `config.reload` action (Phase 8
            # Task 6: also every page transition, see `Engine.
            # _set_current_page`'s own docstring) -- re-fetch the full
            # bundle via `describe` (rather than trusting this event's own
            # payload) so this client's dispatch/indicator/overlay always
            # match what a fresh `describe` would report right now, same
            # "ask, don't assume" precedent `page_changed`'s own handler
            # above already sets for page/topic state.
            bundle = fetch_keymap_sections(client)
            state["keymap"] = bundle["effective"]
            state["keymap_global"] = bundle["global"]
            state["keymap_page"] = bundle["page"]
            state["keymap_hints_enabled"] = bundle.get("hints_enabled", True)
        elif msg.get("kind") == "event" and msg.get("name") in _LEARN_EVENT_NAMES:
            # Phase 4 Task 3 (DAW-style MIDI learn, docs/phase4-notes.md):
            # see `_apply_learn_event`'s own docstring for the sticky-flag/
            # transient-flash split.
            _apply_learn_event(state, msg)

    try:
        # `on_event` is wired in HERE, before the startup wait, not just for
        # the main loop below: the input thread analogue in fb/app.py can
        # fire `page.next` while this blocks for the first snapshot (and
        # even here, another connected client could dispatch page.next
        # first) -- without on_event, that page_changed would be silently
        # dropped and the client would stay on the stale topic forever.
        vm = wait_first_snapshot(inbox, lambda: state["topic"], on_event)
        vm_topic = state["topic"]
    except ClientError:
        print("midicrt tui: engine connection lost")
        client.close()
        return 1

    term = blessed.Terminal()
    lost = False
    try:
        with term.fullscreen(), term.cbreak(), term.hidden_cursor():
            dirty = True
            while True:
                try:
                    # A callable, not a frozen `{state["topic"]}` snapshot:
                    # `on_event` (invoked from inside this very call) can
                    # switch `state["topic"]` mid-drain, and a same-batch
                    # snapshot for the NEW topic must still be recognised.
                    # The three overlay topics are FIXED members -- never
                    # switched, so they need no such closure trick.
                    drained = drain_latest(
                        inbox, lambda: {state["topic"], *overlay_topics},
                        on_event=on_event)
                except ClientError:
                    lost = True
                    return 1
                if state["topic"] in drained:
                    vm, dirty, vm_topic = drained[state["topic"]], True, state["topic"]
                if chrome.OVERLAY_STATUS_TOPIC in drained:
                    state["status_vm"] = drained[chrome.OVERLAY_STATUS_TOPIC]
                    dirty = True
                if chrome.OVERLAY_ALERTS_TOPIC in drained:
                    state["alerts_vm"] = drained[chrome.OVERLAY_ALERTS_TOPIC]
                    dirty = True
                if chrome.OVERLAY_TIMESIG_TOPIC in drained:
                    state["timesig_vm"] = drained[chrome.OVERLAY_TIMESIG_TOPIC]
                    dirty = True
                if chrome.OVERLAY_POLYLIMIT_TOPIC in drained:
                    state["polylimit_vm"] = drained[chrome.OVERLAY_POLYLIMIT_TOPIC]
                    dirty = True
                if chrome.OVERLAY_SYSEX_TOPIC in drained:
                    state["sysex_vm"] = drained[chrome.OVERLAY_SYSEX_TOPIC]
                    dirty = True
                if chrome.OVERLAY_BEATFLASH_TOPIC in drained:
                    state["beatflash_vm"] = drained[chrome.OVERLAY_BEATFLASH_TOPIC]
                    dirty = True
                if chrome.OVERLAY_LOOPPROGRESS_TOPIC in drained:
                    state["loopprogress_vm"] = drained[chrome.OVERLAY_LOOPPROGRESS_TOPIC]
                    dirty = True
                # Same fix as fb/app.py::_run_device (phase-3 task 11,
                # found live in the supervised CRT smoke): a page_changed
                # event can flip state["page"]/state["topic"] before that
                # new topic's own first snapshot arrives (delivery can lag
                # "up to 1/max_rate", docs/phase2-notes.md). An unrelated
                # overlay-only update must not repaint the body against
                # `vm` while it still belongs to the OLD topic -- `render_
                # lines`' eventlog renderer crashes on `vm['count']` the
                # same way `render_frame` did. `dirty` deliberately stays
                # True when skipped, so the render fires as soon as
                # vm_topic catches up.
                #
                # Phase 10 Task A (docs/demo-feedback-2026-08-12.md items
                # 3+9): pianoroll forces `dirty` every loop iteration while
                # it's the current page -- mirrors clients/fb/app.py::
                # _run_device's identical "pianoroll_live" gate (see that
                # comment for the full rationale: nothing in `vm` itself
                # changes between real snapshots, only the wall clock does,
                # so a change-gated repaint can never show the extrapolated
                # motion `_render_vm` below computes). Every other page is
                # unchanged.
                if state["page"] == "pianoroll" and vm_topic == state["topic"]:
                    dirty = True
                if dirty and vm_topic == state["topic"]:
                    # Chrome reserves the LAST THREE rows (phase-3 task 9
                    # adds the beatflash/loopprogress row BELOW the
                    # original status row, matching v1's own bottom-to-top
                    # physical layout -- see clients/chrome.py's module
                    # comment above `beatprogress_row_text` for the row-
                    # offset evidence); the page renders header + body into
                    # the remaining `height - 3` rows.
                    renderer = RENDERERS.get(state["page"], _render_unknown)
                    page_lines = renderer(_render_vm(state["page"], vm), term.width,
                                          term.height - 3)
                    header_line, body_lines = page_lines[0], page_lines[1:]
                    if state["page"] == SCREENSAVER_PAGE:
                        # Important fix (task-9 review): a TRUE full blank,
                        # no reverse video anywhere -- matching v1's raw fb
                        # zeroing, which bypasses chrome entirely (leaving
                        # three brightly-lit reverse-video bars burning at
                        # the bottom would defeat the whole burn-in-
                        # avoidance purpose). See fb/app.py's `_paint_frame`
                        # for this fix's FB-side twin.
                        rows = screensaver_row_texts(header_line, body_lines, term.width)
                        out = [term.home + rows[0]]
                        for i, line in enumerate(rows[1:]):
                            out.append(term.move_xy(0, i + 1) + line)
                        print("".join(out), end="", flush=True)
                    else:
                        # Phase 8 Task 6 (on-screen keymap indicator): the
                        # CURRENT page's own compact key hints, appended
                        # right-aligned into the header row's spare width --
                        # see clients/chrome.py::header_with_hint's own
                        # docstring for why this is the SAME function fb's
                        # header-row integration uses, just fed a static
                        # (non-scrolling) left string instead of a marquee
                        # slice: the TUI has no burn-in concern of its own
                        # (chrome.py's indicator-section docstring), so a
                        # static hint here is not a gap versus fb, just a
                        # simpler equivalent for a renderer that was never
                        # going to scroll its header to begin with.
                        hint_text = (chrome.page_keymap_hint_text(state["keymap_page"])
                                    if state["keymap_hints_enabled"] else "")
                        header_line = chrome.header_with_hint(header_line, hint_text, term.width)
                        status_line = render_status_row(state["status_vm"], term.width)
                        error_text = _active_error_text(state)
                        learn_text = _active_learn_message(state)
                        if error_text:
                            # Bindings review fix: a rejected key-dispatch
                            # (see `_handle_key_press`) shows here instead
                            # of the normal transport status, transiently
                            # -- overwrites, never appends, so it can never
                            # push the line past `term.width`.
                            status_line = _fit(f"ERROR: {error_text}", term.width)
                        elif learn_text:
                            # Phase 4 Task 3: a learn OUTCOME (`learn_bound`/
                            # `learn_cancelled`) flashes here transiently,
                            # same mechanism/priority slot as a rejected key
                            # dispatch right above (an error mid-learn still
                            # wins, though nothing in this task ever produces
                            # both at once).
                            status_line = _fit(learn_text, term.width)
                        elif state.get("learn_armed"):
                            # Sticky (not transient) for as long as the arm
                            # is outstanding -- see _apply_learn_event's own
                            # docstring for why this has no display window.
                            status_line = _fit("LEARN: waiting for MIDI...", term.width)
                        if state["show_fps"]:
                            # Phase 10 Task A (docs/demo-feedback-2026-08-12.
                            # md item 4): right-aligned into the status row's
                            # spare width via the SAME merge `header_with_
                            # hint` uses for the header's own keymap hint --
                            # applied LAST, after the error/learn overrides
                            # above, so the readout is always present
                            # whenever show_fps is on, regardless of which
                            # branch set status_line. Measured across actual
                            # repaints only (see state["_fps_last_t"]'s own
                            # comment at this state dict's construction).
                            now = time.monotonic()
                            last_t = state["_fps_last_t"]
                            frame_dt = (now - last_t) if last_t is not None else None
                            state["_fps_last_t"] = now
                            fps = 1.0 / frame_dt if frame_dt and frame_dt > 0 else None
                            status_line = chrome.header_with_hint(
                                status_line, chrome.format_fps(fps), term.width)
                        secondary_line = render_secondary_row(
                            state["alerts_vm"], state["timesig_vm"], term.width,
                            polylimit_vm=state["polylimit_vm"], sysex_vm=state["sysex_vm"])
                        beatprogress_line = render_beatprogress_row(
                            state["beatflash_vm"], state["loopprogress_vm"], term.width)
                        # Accent (bold) highlighting reaches back into the
                        # vm's own "lines"/"style" shape -- eventlog-
                        # specific, but `.get()` everywhere below keeps a
                        # page whose vm lacks that shape from crashing the
                        # loop (it just renders un-bolded). Factoring this
                        # into the per-page renderer contract is future
                        # chrome-factoring work, not task 3.
                        shown = _tail(vm.get("lines", []), len(body_lines))
                        out = [term.home + term.reverse(header_line) + term.normal]
                        for i, line in enumerate(body_lines):
                            pad = len(body_lines) - len(shown)
                            is_accent = i >= pad and shown[i - pad].get("style") == "accent"
                            styled = term.bold(line) if is_accent else line
                            out.append(term.move_xy(0, i + 1) + styled)
                        out.append(term.move_xy(0, term.height - 3)
                                   + term.reverse(secondary_line) + term.normal)
                        out.append(term.move_xy(0, term.height - 2)
                                   + term.reverse(status_line) + term.normal)
                        out.append(term.move_xy(0, term.height - 1)
                                   + term.reverse(beatprogress_line) + term.normal)
                        print("".join(out), end="", flush=True)
                    if state.get("help_overlay"):
                        # Phase 8 Task 6: drawn LAST, blitting only its own
                        # cells over whatever frame this same repaint just
                        # printed (screensaver included -- toggling help
                        # while screensaving is allowed, same as fb) --
                        # see render_help_overlay_box's own docstring.
                        box_x, box_y, box_rows = render_help_overlay_box(
                            state["keymap_global"], state["keymap_page"], state["page"],
                            term.width, term.height, state["roster"])
                        overlay_out = [term.move_xy(box_x, box_y + i) + row
                                      for i, row in enumerate(box_rows)]
                        print("".join(overlay_out), end="", flush=True)
                    dirty = False
                key = term.inkey(timeout=0.05)
                overlay_before = state.get("help_overlay", False)
                if _handle_key_press(client, _normalize_key(key), state):
                    return 0
                if state.get("help_overlay", False) != overlay_before:
                    # Phase 8 Task 6: the overlay's on/off state is flipped
                    # entirely inside `_handle_key_press` (no vm/topic
                    # change involved at all -- module docstring: "not a
                    # page switch") -- without this, toggling help would
                    # never trigger a repaint until some UNRELATED update
                    # happened to coincide with it.
                    dirty = True
                if _active_error_text(state) or _active_learn_message(state) or state.get(
                        "learn_armed"):
                    # Keep repainting while the transient error/learn
                    # message is still within its display window, or while
                    # a learn slot is armed, even with no other state
                    # change (see _handle_key_press/_active_error_text and
                    # _apply_learn_event/_active_learn_message above).
                    dirty = True
    finally:
        client.close()
        if lost:
            print("midicrt tui: engine connection lost")


def main_debug(socket_path="/tmp/midicrt-dev.sock") -> None:  # manual smoke helper
    print(json.dumps({"socket": socket_path}))
