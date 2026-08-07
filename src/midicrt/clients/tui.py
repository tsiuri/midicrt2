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

`n` sends `page.next`; `c` still sends `eventlog.clear` (a no-op action on
a page other than eventlog would be rejected by the server as "unknown
action" only if the page itself removed it -- eventlog.clear is global for
now, unchanged from phase 2).

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

from midicrt.behaviors.screensaver import SCREENSAVER_PAGE
from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    drain_latest,
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


def render_secondary_row(alerts_vm: dict, timesig_vm: dict, width: int) -> str:
    """TUI's presentation of the shared second chrome row (phase-3 task 6:
    stuck-note alerts when any are active, else the time-signature
    estimate -- see clients/chrome.py's `secondary_status_text()`). Same
    fit/pad treatment as `render_status_row`."""
    return _fit(chrome.secondary_status_text(alerts_vm, timesig_vm), width)


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


# -- tuner page (phase-3 task 6) ----------------------------------------------
#
# Layout mirrors v1's `pages/tuner.py::draw()` text rows: a status line
# ("Input:.../Dev:.../SR:..." in v1 -- adapted here to "Listening... Conf:
# .../Level:... dB" since this page has no live audio-device readout of its
# own to report yet, see pages/tuner.py's/analyzers/tuner.py's module
# docstrings), then either "Note:.../Pitch:.../Cents:.../Conf:.../Level:..."
# + a "Tuning: <meter>" row (signal locked) or a second blank row (idle --
# the state this page shows in production today, see those same
# docstrings). `tuning_meter` is reused from analyzers/tuner.py, not
# duplicated, same "shared pure function" convention as
# `analyzers.theory.NOTE_NAMES` in the harmony renderers.
def _tuner_header_text(vm: dict) -> str:
    return f"{vm['title']}  [n]ext page [q]uit"


def _tuner_body_lines(vm: dict) -> list[str]:
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


# -- pianoroll page (phase-3 task 7) -----------------------------------------
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


RENDERERS = {"eventlog": render_lines, "voices": render_voices_lines,
             "harmony": render_harmony_lines, "tuner": render_tuner_lines,
             "pianoroll": render_pianoroll_lines, "spectrum": render_spectrum_lines,
             "screensaver": render_screensaver_lines}

_SUBSCRIBE_RATE = 10.0
_KEY_ACTIONS = {"c": "eventlog.clear", "n": "page.next"}


def run_tui(socket_path: str) -> int:
    import blessed

    client = EngineClient(socket_path)
    overlay_topics = [chrome.OVERLAY_STATUS_TOPIC, chrome.OVERLAY_ALERTS_TOPIC,
                       chrome.OVERLAY_TIMESIG_TOPIC, chrome.OVERLAY_BEATFLASH_TOPIC,
                       chrome.OVERLAY_LOOPPROGRESS_TOPIC]
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic, *overlay_topics], max_rate=_SUBSCRIBE_RATE)
    except ClientError as exc:
        print(f"midicrt tui: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    state = {"page": page, "topic": topic, "status_vm": dict(chrome.DEFAULT_STATUS_VM),
             "alerts_vm": dict(chrome.DEFAULT_ALERTS_VM), "timesig_vm": dict(chrome.DEFAULT_TIMESIG_VM),
             "beatflash_vm": dict(chrome.DEFAULT_BEATFLASH_VM),
             "loopprogress_vm": dict(chrome.DEFAULT_LOOPPROGRESS_VM)}

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, _SUBSCRIBE_RATE)
            state["page"], state["topic"] = new_page, new_topic

    try:
        # `on_event` is wired in HERE, before the startup wait, not just for
        # the main loop below: the input thread analogue in fb/app.py can
        # fire `page.next` while this blocks for the first snapshot (and
        # even here, another connected client could dispatch page.next
        # first) -- without on_event, that page_changed would be silently
        # dropped and the client would stay on the stale topic forever.
        vm = wait_first_snapshot(inbox, lambda: state["topic"], on_event)
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
                    vm, dirty = drained[state["topic"]], True
                if chrome.OVERLAY_STATUS_TOPIC in drained:
                    state["status_vm"] = drained[chrome.OVERLAY_STATUS_TOPIC]
                    dirty = True
                if chrome.OVERLAY_ALERTS_TOPIC in drained:
                    state["alerts_vm"] = drained[chrome.OVERLAY_ALERTS_TOPIC]
                    dirty = True
                if chrome.OVERLAY_TIMESIG_TOPIC in drained:
                    state["timesig_vm"] = drained[chrome.OVERLAY_TIMESIG_TOPIC]
                    dirty = True
                if chrome.OVERLAY_BEATFLASH_TOPIC in drained:
                    state["beatflash_vm"] = drained[chrome.OVERLAY_BEATFLASH_TOPIC]
                    dirty = True
                if chrome.OVERLAY_LOOPPROGRESS_TOPIC in drained:
                    state["loopprogress_vm"] = drained[chrome.OVERLAY_LOOPPROGRESS_TOPIC]
                    dirty = True
                if dirty:
                    # Chrome reserves the LAST THREE rows (phase-3 task 9
                    # adds the beatflash/loopprogress row BELOW the
                    # original status row, matching v1's own bottom-to-top
                    # physical layout -- see clients/chrome.py's module
                    # comment above `beatprogress_row_text` for the row-
                    # offset evidence); the page renders header + body into
                    # the remaining `height - 3` rows.
                    renderer = RENDERERS.get(state["page"], _render_unknown)
                    page_lines = renderer(vm, term.width, term.height - 3)
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
                        status_line = render_status_row(state["status_vm"], term.width)
                        secondary_line = render_secondary_row(
                            state["alerts_vm"], state["timesig_vm"], term.width)
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
                    dirty = False
                key = term.inkey(timeout=0.05)
                if key == "q":
                    return 0
                name = _KEY_ACTIONS.get(str(key))
                if name:
                    try:
                        client.action(name)
                    except ClientError:
                        lost = True
                        return 1
    finally:
        client.close()
        if lost:
            print("midicrt tui: engine connection lost")


def main_debug(socket_path="/tmp/midicrt-dev.sock") -> None:  # manual smoke helper
    print(json.dumps({"socket": socket_path}))
