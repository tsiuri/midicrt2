"""Chrome: the shared, renderer-agnostic status-bar logic both clients wrap.

This is the "chrome/page-body layering" phase3-notes.md calls for: a page
renders its own body (and, for now, its own header -- see tui.py's/
fb/app.py's docstrings for why header extraction is future work, not this
task), while a THIRD thing -- the transport status bar -- is chrome: it is
the same on every page, driven by `overlay.status` instead of whatever
page topic is current, and both clients render an IDENTICAL line of text
for it (TUI fits it into a terminal row; fb draws it into a pixel strip --
see clients/tui.py's `render_status_row` and clients/fb/app.py's
`_draw_status`). Putting the text-building logic here once is what
"mirrors it" (task-3 brief) means in practice: change the wording here and
both clients change together, they can never drift.

Pure text in, text out -- no Surface, no blessed.Terminal, no font, so this
module is trivially unit-testable and has zero rendering dependencies.
`analyzers.marquee` is the one exception (Phase 9 Task 0, below) -- it is
itself just as pure/dependency-free (see its own module docstring), so
importing its `PAGE_IDS`/`PAGE_TITLES` tables here doesn't compromise that
property, only reuses it.
"""

import math

from midicrt.analyzers.marquee import PAGE_IDS as _V1_PAGE_IDS
from midicrt.analyzers.marquee import PAGE_TITLES as _V1_PAGE_TITLES

OVERLAY_STATUS_TOPIC = "overlay.status"

# What a client should show before the first overlay.status snapshot has
# arrived (a few hundred ms after subscribing at most -- see
# ProtocolServer._push_loop) -- matches TransportAnalyzer's own initial
# view_model() exactly, so there is no visible "flash" once the real one
# lands.
DEFAULT_STATUS_VM = {"bpm": None, "bar": 0, "beat": 1, "running": False, "source": None,
                     "rec": False}

# Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md): a plain
# `.get("rec")` (not a required key) below means a caller passing an OLDER
# vm dict shape with no "rec" key at all (any pre-phase-5 test literal, or
# a pre-phase-5 recorded fixture) renders IDENTICALLY to before -- adding
# this marker required zero golden-string updates anywhere in
# test_tui_render.py/test_fb_render.py (verified: both already build their
# `vm` dicts fresh per test and only ever substring-check `status_text()`'s
# output, never a hardcoded exact status line).
REC_MARKER = "● REC  "


def format_bpm(bpm: float | None) -> str:
    """`None` -> "—" (no clock observed yet, or transport never started);
    otherwise one decimal place, per the task-3 brief's VM contract."""
    return "—" if bpm is None else f"{bpm:.1f}"


def format_fps(fps: float | None) -> str:
    """Phase 10 Task A (docs/demo-feedback-2026-08-12.md item 4): the
    `show_fps`-gated footer readout. `fps` is a CLIENT-LOCAL measurement
    (each client's own frame-to-frame wall-clock delta -- see clients/
    tui.py's/clients/fb/app.py's own render-loop docstrings), never a
    server-reported value, so this is pure text formatting with no VM
    contract of its own to document, unlike `format_bpm` above.

    `None` -> `"fps:--"` (no frame delta yet -- the very first frame of a
    run, or a headless single-shot `--out` capture with nothing to measure
    a delta against) -- same "no data yet" shape as `format_bpm`'s em-dash,
    just v1's own literal text instead of a dash (`~/codex/midicrt/
    midicrt.py:993`, `fps_status = f"fps:{1.0/_frame_dt:.1f}" if
    _frame_dt > 0 else "fps:--"` -- this ports that exact "fps:X.X"/
    "fps:--" formatting byte-for-byte, the one piece of v1's own fps
    readout this task's new footer element reuses; PLACEMENT is new
    design, per the task brief, not a v1 port)."""
    return "fps:--" if fps is None else f"fps:{fps:.1f}"


OVERLAY_ALERTS_TOPIC = "overlay.alerts"
OVERLAY_TIMESIG_TOPIC = "overlay.timesig"

DEFAULT_ALERTS_VM = {"alerts": [], "cleared": []}
DEFAULT_TIMESIG_VM = {"labels": [], "confidence": 0.0, "events": 0,
                       "events_window": 0, "events_total": 0, "pending": None}

# Stuck-note alerts (phase-3 task 6) and the time-signature estimate share
# ONE second chrome row instead of each getting their own -- v1 itself only
# ever reserves TWO bottom rows total (`plugins/timeclock.py`'s chrome bar
# + `plugins/zstucknotes.py`'s warning line; see analyzers/stucknotes.py's/
# analyzers/timesig.py's own module docstrings for why timesig has no v1
# chrome row of its own to mirror -- its only v1 home is a page v2 never
# ported). Alerts are rare and urgent -- v1's own zstucknotes.py PANICs and
# reverse-videos on "crit" -- so they take priority on the shared row
# whenever any are active; the row falls back to the (routine, far more
# often shown) time-signature line otherwise. This keeps v2's total chrome
# footprint at the same two rows v1 actually uses, rather than growing to
# three.
MAX_ALERT_LIST = 3   # matches v1's zstucknotes.py MAX_LIST


def _fmt_alert_parts(entries: list[dict]) -> str:
    """Shared `"CH03 n060  2.3s | ..."` formatter for both the active
    `alerts` list and the lingering `cleared` snapshot below -- same
    `MAX_ALERT_LIST`/"+N more" truncation either way, matching v1's own
    `zstucknotes.py` (both its live `STUCK WARN/CRIT:` line and its
    lingering `STUCK CLEARED:` message build the SAME per-entry token from
    the SAME `(age, ch, note)` shape)."""
    parts = [f"CH{a['ch']:02d} n{a['note']:03d} {a['held_s']:4.1f}s" for a in entries[:MAX_ALERT_LIST]]
    extra = len(entries) - MAX_ALERT_LIST
    if extra > 0:
        parts.append(f"+{extra} more")
    return " | ".join(parts)


def _live_alert_text(vm: dict) -> str:
    """The LIVE half of `alerts_text()` (a currently-active `STUCK WARN/
    CRIT: ...` alert), factored out so `secondary_status_text`/
    `secondary_status_dim` (Phase 9 close-out, controller ruling: chrome
    re-rank) can check it INDEPENDENTLY of the lingering-cleared half at
    its own, higher-priority rung -- see `secondary_status_text`'s own
    docstring for the full chain. `""` when no live alert is active."""
    alerts = vm.get("alerts") or []
    if not alerts:
        return ""
    level = "CRIT" if any(a["level"] == "crit" for a in alerts) else "WARN"
    return f"STUCK {level}: " + _fmt_alert_parts(alerts)


def _lingering_cleared_text(vm: dict) -> str:
    """The LINGERING half of `alerts_text()` (a DIMMED, historical "STUCK
    CLEARED: ..." message, `config.stuck_hold_after` -- v1's `HOLD_AFTER`)
    -- see `_live_alert_text`'s own docstring for why this is a separate
    function now. `""` when nothing is lingering."""
    cleared = vm.get("cleared") or []
    if not cleared:
        return ""
    return "STUCK CLEARED: " + _fmt_alert_parts(cleared)


def alerts_text(vm: dict) -> str:
    """Build v1's "STUCK WARN/CRIT: ..." banner line from an
    `overlay.alerts` view-model, falling back to a DIMMED (see
    `secondary_status_dim` below) "STUCK CLEARED: ..." lingering message
    (Phase 9 Task 2, `config.stuck_hold_after` -- v1's `HOLD_AFTER`) built
    from the SAME view-model's `cleared` field, and finally `""` (blank
    row) once neither is present. Note numbers are shown raw (`n060`), not
    v1's octave-letter name (`_fmt_note`, e.g. note 60 -> "C6(060)") -- see
    analyzers/stucknotes.py's module docstring for why that convention
    isn't reproduced.

    This function's OWN contract is unchanged by the Phase 9 close-out
    chrome re-rank (still combined live-then-lingering, live always wins
    when both are present) -- it is a thin wrapper around `_live_alert_
    text`/`_lingering_cleared_text` now, kept as its own public, tested
    entry point for any caller that only cares about "what would the
    alerts widget alone show," not the FULL shared-row priority chain
    `secondary_status_text` builds (which needs the two halves at
    DIFFERENT rungs, interleaved with poly-limit/sysex -- see that
    function's own docstring)."""
    live = _live_alert_text(vm)
    if live:
        return live
    return _lingering_cleared_text(vm)


def secondary_status_dim(alerts_vm: dict, polylimit_vm: dict | None = None,
                         sysex_vm: dict | None = None) -> bool:
    """True exactly when the second chrome row is about to show the
    DIMMED, lingering "STUCK CLEARED" message (Phase 9 Task 2) -- the
    renderer's cue to paint a LOWER luminance ramp tier (monochrome
    mandate: "dim" is a shading level, never a separate hue or a blink)
    instead of the normal bright reverse-video fill. False whenever
    anything HIGHER-priority than the lingering message is what's
    actually being shown instead -- a live alert, a poly-limit flash, or
    active sysex-status text (all full brightness, urgent/confirmatory)
    -- or the row has fallen all the way through to the ordinary time-
    signature line (also full brightness -- v1 never dims that line
    either).

    Phase 9 close-out (controller ruling, chrome re-rank): `polylimit_vm`/
    `sysex_vm` are NEW, OPTIONAL params (default `None`/"not active", so
    every pre-existing 1-arg call site keeps working unchanged) --
    REQUIRED for this function to stay consistent with `secondary_status_
    text`'s own reordered priority chain (live alerts > poly flash >
    sysex status > lingering-cleared > timesig). Before this fix, this
    function computed dim-ness from `alerts_vm` ALONE, independently of
    whether `secondary_status_text` was ACTUALLY about to render the
    lingering message at all -- a real bug once poly-limit/sysex could
    both outrank it: the row would render poly-limit's or sysex's own
    full-brightness text while this function told the caller to dim the
    background under it, a visible mismatch. Callers that don't (or
    can't) supply the two new args keep the OLD, narrower "no live alert"
    behavior, which is only WRONG in the exact scenario this fix closes
    (a simultaneous poly-flash/sysex-active condition) -- see `clients/
    fb/app.py::_draw_secondary`'s own call site, which DOES supply both."""
    if _live_alert_text(alerts_vm):
        return False
    if polylimit_vm and polylimit_vm.get("flashing"):
        return False
    if sysex_vm and sysex_vm.get("active") and sysex_vm.get("text"):
        return False
    return bool(_lingering_cleared_text(alerts_vm))


def timesig_text(vm: dict) -> str:
    """Build v1's "Time Signature: ..." line (`pages/transport.py`'s
    `_timesig_line()`) from an `overlay.timesig` view-model."""
    labels = vm.get("labels") or []
    if not labels:
        return "Time Signature: (no lock)"
    conf = vm.get("confidence", 0.0)
    events = vm.get("events", 0)
    win = vm.get("events_window", events)
    total = vm.get("events_total", events)
    pending = vm.get("pending")
    ts = labels[0] if len(labels) == 1 else " / ".join(labels)
    text = f"Time Signature: {ts}  conf:{conf:0.2f}  events:{win}/{total}"
    if pending:
        pend = " / ".join(pending) if isinstance(pending, (list, tuple)) else str(pending)
        text += f"  -> {pend}"
    return text


OVERLAY_BEATFLASH_TOPIC = "overlay.beatflash"
OVERLAY_LOOPPROGRESS_TOPIC = "overlay.loopprogress"

DEFAULT_BEATFLASH_VM = {"intensity": 0.0, "is_bar": False}
DEFAULT_LOOPPROGRESS_VM = {"fraction": 0.0, "running": False}

# v1 actually reserves FOUR bottom rows, not the two the task-3/task-6
# chrome rows above account for: `plugins/beatflash.py` draws at the
# screen's literal bottom-most row (y = SCREEN_ROWS - 1), and
# `plugins/loopprogress.py` at the row directly above it (Y_POS_OFFSET=2)
# -- both BELOW `plugins/timeclock.py`'s own row (Y_POS_OFFSET=3, ported as
# `overlay.status` above) and `plugins/zstucknotes.py`'s (Y_POS_OFFSET=4,
# ported as the alerts/timesig row above). This third v2 chrome row hosts
# those remaining two v1 widgets TOGETHER (one row, not two): v1's
# loopprogress.py itself only fills the LEFT portion of its row with
# unrelated scheduler/sysex diagnostic text that has no v2 analog (see
# analyzers/loopprogress.py's module docstring -- not ported), which
# leaves that space free for beatflash's own tiny (2-char) indicator
# instead of needing a whole extra reserved row of its own. Combining
# follows the exact same "v1 reserves N bottom rows; group cheap/glanceable
# concerns onto shared rows" precedent `secondary_status_text` above
# already established for alerts+timesig.
_BEATFLASH_LEVELS = (
    (1.0, "██"),    # only a bar flash (BAR_PEAK > BEAT_PEAK) ever reaches this
    (0.66, "▓▓"),
    (0.33, "▒▒"),
    (0.0, "░░"),
)

LOOPPROGRESS_BAR_WIDTH = 8   # matches v1's BAR_WIDTH exactly


def beatflash_glyph(vm: dict) -> str:
    """Build v1's 2-char beat-flash block from an `overlay.beatflash`
    view-model, ramped through shading levels as `intensity` decays (v1
    itself is a hard on/off toggle -- see analyzers/beatflash.py's module
    docstring for why v2 shows a graduated fade instead) -- two blank
    spaces once fully decayed (or before the first beat), matching v1's
    idle appearance."""
    intensity = vm.get("intensity", 0.0)
    for threshold, glyph in _BEATFLASH_LEVELS:
        if intensity > threshold:
            return glyph
    return "  "


def loopprogress_bar(vm: dict) -> str:
    """Build v1's `[        ]`/`[   *    ]` 8-cell bracketed bar from an
    `overlay.loopprogress` view-model. Blank (no `*`) while `running` is
    False, matching v1's own `if running: bar_chars[pos] = "*"` gating --
    the bar position freezes wherever it stopped, simply hidden until the
    transport runs again."""
    cells = [" "] * LOOPPROGRESS_BAR_WIDTH
    if vm.get("running"):
        pos = min(LOOPPROGRESS_BAR_WIDTH - 1, int(vm.get("fraction", 0.0) * LOOPPROGRESS_BAR_WIDTH))
        cells[max(0, pos)] = "*"
    return "[" + "".join(cells) + "]"


def beatprogress_row_text(beatflash_vm: dict, loopprogress_vm: dict, width: int) -> str:
    """Compose the shared third chrome row: `beatflash_glyph()` pinned to
    column 0 (mirroring v1's own `x=0` placement) and `loopprogress_bar()`
    centered across `width` (mirroring v1's own `xmid - len(visual)//2`
    centering) -- both built ONCE here so the fb and TUI clients render
    byte-identical text, same "mirrors it" contract as `status_text`/
    `secondary_status_text` above. Unlike those two, this function takes
    `width` explicitly: centering a moving bar is inherently a layout
    computation, and keeping it in ONE place (rather than each client
    re-deriving the same centering formula) is what actually prevents the
    two clients from drifting here, not a width-agnostic pure string.
    Always exactly `width` characters (space-padded/truncated), matching
    every other chrome row's `_fit()`-at-the-client convention -- done here
    instead since this function is already width-aware."""
    if width <= 0:
        return ""
    glyph = beatflash_glyph(beatflash_vm)
    bar = loopprogress_bar(loopprogress_vm)
    row = [" "] * width
    for i, ch in enumerate(glyph):
        if i < width:
            row[i] = ch
    start = max(len(glyph) + 1, (width - len(bar)) // 2)
    for i, ch in enumerate(bar):
        pos = start + i
        if 0 <= pos < width:
            row[pos] = ch
    return "".join(row)


OVERLAY_POLYLIMIT_TOPIC = "overlay.polylimit"
DEFAULT_POLYLIMIT_VM = {"flashing": False}

POLYLIMIT_FLASH_TEXT = "POLY LIMIT EXCEEDED"


OVERLAY_SYSEX_TOPIC = "overlay.sysex"
DEFAULT_SYSEX_VM = {"text": "", "active": False}


def secondary_status_text(alerts_vm: dict, timesig_vm: dict, polylimit_vm: dict | None = None,
                          sysex_vm: dict | None = None) -> str:
    """The shared second chrome row's text -- priority chain (most urgent
    wins, routine timesig only shows when NOTHING more urgent is active):

    1. `_live_alert_text()` (a CURRENTLY-ACTIVE stuck alert, WARN or CRIT)
       -- v1's own `zstucknotes.py` CRIT reverse-video urgency, this row's
       original tenant. Rung 1, unconditionally, no exceptions.
    2. The poly-limit chrome flash (Phase 9 Task 2, `polylimit_vm[
       "flashing"]` -- disclosed v2-native addition, v1's
       `zvoicemonitor.py` has no chrome home of its own, see
       `analyzers/voices.py`'s module docstring) -- also an urgent,
       device-malfunction-adjacent condition, same tier as a live alert in
       spirit (both mean "something is wrong RIGHT NOW"), but a live alert
       wins when both are simultaneously true (a stuck note is the more
       actionable of the two).
    3. The sysex-status text (Phase 9 Task 5, `sysex_vm["active"]` --
       v1 parity item, `plugins/loopprogress.py`'s own left-of-bar
       scheduler/sysex status string, ported now that `engine/
       sysex_store.py` finally gives it a v2 data source; see
       `engine/core.py::_SysexStatusOverlay`'s own docstring and
       docs/task-5-report.md's v1-extraction section). Deliberately
       ranked BELOW live-alert/poly-limit and ABOVE the lingering-cleared
       message AND timesig: this is a disclosed, v2-native ROW-PLACEMENT
       decision, not a literal replay of v1's own layout -- v1 showed this
       text on a COMPLETELY DIFFERENT row (the loopprogress bar's row, not
       the stuck-notes row this function builds), with nothing else ever
       competing for that space. v2 already established the precedent of
       consolidating cheap/glanceable, non-alert diagnostic text onto this
       SAME shared row rather than reserving a new one per widget
       (`timesig_text` itself has "no v1 chrome row of its own" and shares
       this exact row for the identical reason, see the module-level
       comment above `alerts_text`) -- sysex-status joins for the same
       reason: it is informational/confirmatory ("your remote-control
       command fired and here's what it did"), never as urgent as a stuck
       note or an exceeded polyphony limit, but still more timely than
       either the lingering-cleared relic below it or the always-present
       routine timesig baseline. Review fix (controller ruling, re-
       checking the v1 extraction under review): `sysex_vm["text"]` is
       fed EXCLUSIVELY by the pre-existing midicrt CMD-dispatch
       subsystem's own outcomes (`engine/sysex.py::build_status_text`,
       `Engine._handle_sysex`) -- NOT by generic incoming sysex traffic,
       and NOT by the browser-facing sysex MANAGER's own save/play/delete
       actions (`engine/sysex_store.py`'s ring/library CRUD) either. v1's
       real `sysex_status` only ever lit up for an actual midicrt remote-
       control command outcome (`plugins/sysex.py:55-57`, called only
       from `_dispatch()`'s own branches) -- a real Cirklon rig's other,
       non-midicrt sysex chatter never touched it, and this row must not
       either, or real foreign-device traffic would light this row
       constantly (noise v1 deliberately never had). See engine/
       sysex_store.py's own module docstring for the full v1-evidence
       writeup.
    4. `_lingering_cleared_text()` (a DIMMED, historical "STUCK CLEARED:
       ..." message -- see `secondary_status_dim`) -- Phase 9 close-out,
       CONTROLLER RULING: this used to be bundled into rung 1 (`alerts_
       text()` returned live-or-lingering as ONE combined value, so a
       stale, already-resolved, dimmed relic could outrank a LIVE
       poly-limit flash or an active sysex confirmation -- both more
       urgent, more actionable, more RIGHT-NOW facts than "a note used to
       be stuck"). Moved down here, its own rung, BELOW poly-limit and
       sysex: a dimmed historical relic must never mask live urgent info.
       Still ranked ABOVE the routine timesig fallback -- a recently-
       cleared stuck note is more noteworthy than the routine baseline,
       just not as noteworthy as anything actually live.
    5. `timesig_text()` -- the routine fallback, wins only when nothing
       above is active.

    `polylimit_vm`/`sysex_vm` are both OPTIONAL (default `None`/"not
    active") so every pre-existing 2- and 3-arg call site keeps working
    unchanged. `secondary_status_dim()` (above) mirrors this EXACT same
    priority chain so the renderer's "should this row be dimmed" decision
    can never disagree with what text this function actually returns --
    see that function's own docstring for the bug this fix also closes."""
    live = _live_alert_text(alerts_vm)
    if live:
        return live
    if polylimit_vm and polylimit_vm.get("flashing"):
        return POLYLIMIT_FLASH_TEXT
    if sysex_vm and sysex_vm.get("active") and sysex_vm.get("text"):
        return sysex_vm["text"]
    lingering = _lingering_cleared_text(alerts_vm)
    if lingering:
        return lingering
    return timesig_text(timesig_vm)


OVERLAY_MARQUEE_TOPIC = "overlay.marquee"

DEFAULT_MARQUEE_VM = {"text": "", "doubled": "        ", "offset": 0}   # GAP*2, empty roster


def scroll_window(text: str, doubled: str, offset: int, width: int) -> str:
    """The shared v1 scrolling-marquee slice mechanic (`midicrt.py`'s header
    marquee AND autoconnect-log rows both do this, see
    `analyzers/marquee.py`'s module docstring for the full citation): show
    `text` STATIC when it already fits within `width` characters (v1's `if
    len(page_titles) <= SCREEN_COLS: draw_line(...)` / `if len(msg) <=
    window: win_text = msg.ljust(window)`), else slice `width` characters
    out of the caller's PRE-DOUBLED string starting at `offset`.

    No wraparound handling is needed: whenever execution reaches the
    slicing branch, `len(text) > width` is true BY CONSTRUCTION (that's the
    only way to get here), and a correctly-built `doubled` is always `2 *
    (len(text) + len(gap))` long for some non-empty `gap` -- so `offset +
    width < len(text) + len(gap) + width < 2 * (len(text) + len(gap)) ==
    len(doubled)` always holds (`offset` itself is `< len(text) +
    len(gap)`, the modulo `analyzers/marquee.py::MarqueeAnalyzer` already
    applies). Matches v1's own implicit assumption -- it never defends
    against this case either, because it can't actually occur.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if not doubled:
        return ""
    off = offset % len(doubled)
    return doubled[off:off + width]


def marquee_window_text(vm: dict, width: int) -> str:
    """v1's exact header marquee (`midicrt.py` row 0) width-aware slice of
    an `overlay.marquee` view-model -- see `scroll_window()`'s own
    docstring for the shared mechanic and `analyzers/marquee.py`'s module
    docstring for the full v1 citation (line numbers, HEADER_SCROLL_SPEED,
    the doubled-string trick). Screen width is deliberately NOT known to
    the engine-side `MarqueeAnalyzer` (fb and TUI have different character
    budgets for the identical roster text) -- "does this need to scroll at
    all" is decided HERE, per-renderer, from the SAME underlying vm/offset,
    mirroring `beatprogress_row_text(vm, width)`'s own width-aware
    precedent above."""
    return scroll_window(vm.get("text", ""), vm.get("doubled", ""), vm.get("offset", 0), width)


# -- on-screen keymap indicator (Phase 8 Task 6, docs/gui-phase-decisions-
# 2026-08-08.md keymap revamp: "on-screen indicators of current keymap...
# [should be] developed") ---------------------------------------------------
#
# A compact CHROME element, not a whole new reserved strip: it shares the
# HEADER row's own remaining width, to the right of the marquee text --
# see `clients/fb/app.py::_header_text`'s own docstring for the layout
# integration and why this keeps every EXISTING page's body-height math
# (and every existing golden fixture) untouched. The TUI client (which has
# no marquee/burn-in concern at all, `run_tui`'s own docstring) shows the
# SAME compact text statically appended to its header row instead.
#
# Burn-in rule (docs/gui-phase-decisions-2026-08-08.md ruling #3: "ALL of
# v1's animations are valuable... many exist for CRT BURN-IN PREVENTION"):
# `page_keymap_hint_text` builds PLAIN text with no motion of its own --
# motion comes from `header_with_hint` reusing the SAME `overlay.marquee`
# scroll offset the page-title marquee already ticks on (via `scroll_
# window`, byte-identical engage/slice mechanic), rather than inventing a
# second independent clock. A hint line short enough to fit its reserved
# width never needs to scroll at all (matches the marquee's OWN "static
# when it fits" rule) -- on the fb client specifically, that's fine
# burn-in-wise ONLY because it sits inside the header's reverse-video bar,
# which is already a solid-fill chrome element with its OWN separate
# anti-burn-in mitigation (v1's header row precedent, `analyzers/
# marquee.py`'s whole reason for existing) -- this hint text riding along
# in the same bar doesn't add a new static-bright hazard.


def _short_action_label(action: str) -> str:
    """`"pianoroll.channel_toggle"` -> `"channel_toggle"`, `"page.jump"` ->
    `"jump"` -- the hint is a compact discovery aid (what does this key
    roughly do), not a full action-name dump; stripping the namespace
    prefix (the part before the LAST dot) keeps entries short enough that
    a handful actually fit a header's spare width. A bare name with no dot
    (shouldn't occur for any REAL action, but keymap.toml can be hand-
    edited) passes through unchanged rather than raising."""
    return action.rsplit(".", 1)[-1]


def _entry_action_name(entry) -> str:
    """A keymap entry is either a plain string action name or an args-table
    `{"action": ..., "args": {...}}` (engine/keymap.py's schema v2) -- this
    extracts just the action name either way, for display purposes only
    (the hint never shows baked args -- see `page_keymap_hint_text`'s own
    docstring for why)."""
    return entry if isinstance(entry, str) else str(entry.get("action", "?"))


def page_keymap_hint_text(page_keymap: dict) -> str:
    """Compact `"key:label  key:label  ..."` hint string for ONE page's own
    `[keys.<page>]` section (`Engine.keymap_page`, current-page-scoped --
    NOT the global table, which the help OVERLAY covers separately, see
    `overlay_lines` below) -- sorted by key for a stable, scannable order.
    `""` for a page with no page-specific bindings at all (most pages,
    today -- only pianoroll/img2txtviz/sendnotes/spectrum have any, see
    `engine/keymap.py::DEFAULT_PAGE_KEYMAPS`), which both clients treat as
    "nothing to show" -- no placeholder text, no reserved width wasted."""
    if not page_keymap:
        return ""
    parts = [f"{key}:{_short_action_label(_entry_action_name(entry))}"
             for key, entry in sorted(page_keymap.items())]
    return "  ".join(parts)


_HINT_GAP = "    "   # same 4-space seam convention as analyzers/marquee.py's own GAP


def page_keymap_hint_window_text(page_keymap: dict, offset: int, width: int) -> str:
    """Width-aware slice of `page_keymap_hint_text()`, scrolling via the
    SAME `scroll_window()` mechanic (and, at the fb call site, the SAME
    `overlay.marquee` offset) the header marquee itself uses -- burn-in
    rule (docs/gui-phase-decisions-2026-08-08.md ruling #3): a hint list
    too long to fit its reserved width must not sit static-bright at a
    fixed screen position indefinitely any more than the marquee's own
    roster text would. Static (no scroll) when it already fits `width`,
    exactly mirroring `marquee_window_text`'s own "static when it fits"
    engage condition -- the common case (most pages' hint lists are a
    handful of short entries)."""
    text = page_keymap_hint_text(page_keymap)
    if not text or width <= 0:
        return ""
    if len(text) <= width:
        return text
    doubled = (text + _HINT_GAP) * 2
    return scroll_window(text, doubled, offset, width)


def header_with_hint(marquee_slice: str, hint_text: str, width: int) -> str:
    """Compose the final header-row string: `marquee_slice` (already
    sized/scrolled for whatever width the CALLER reserved for it) on the
    left, `hint_text` right-aligned within the remaining `width` columns,
    separated by at least one blank column. `hint_text` empty (no page-
    specific keys, or the indicator disabled -- see `clients/fb/app.py`'s
    own `keymap_hints_enabled` gating at the call site) returns
    `marquee_slice` completely unchanged -- BYTE-IDENTICAL to every
    existing header call site/golden fixture that never reserved any hint
    space in the first place, since `_header_char_capacity`'s caller only
    shrinks the marquee's own budget when there's real hint text to show
    (see that call site's own docstring)."""
    if not hint_text or width <= 0:
        return marquee_slice
    if len(hint_text) >= width:
        hint_text = hint_text[:width]
    row = list(marquee_slice[:width].ljust(width))
    start = width - len(hint_text)
    for i, ch in enumerate(hint_text):
        row[start + i] = ch
    return "".join(row)


# -- help OVERLAY (Phase 8 Task 6): togglable, dim panel over the current
# page ------------------------------------------------------------------
#
# `client.help_toggle` (engine/keymap.py) is a pure CLIENT-LOCAL pseudo-
# action -- the overlay never reaches the engine at all (no page switch,
# no dirty topic, the underlying page's own subscription/render keeps
# ticking normally underneath, matching the brief's "not a page switch"
# requirement literally: there is nothing server-side to switch). Both
# clients render it the SAME way structurally: a GLOBAL section (`Engine.
# keymap_global`) followed by a CURRENT-PAGE section (`Engine.keymap_
# page`) -- see `overlay_lines` below for the shared line-building this
# module gives both renderers so the two can never drift on WHAT the
# overlay lists, only on how each draws a dim backdrop/box around it.


def _entry_display_label(entry, roster: list[str] | None, key: str | None = None) -> str:
    """The overlay row's label for one entry -- `_entry_action_name`'s bare
    action name, EXCEPT for two args-table shapes that get a friendlier,
    target-revealing label instead:

    - a `page.jump` entry, when `roster` (the live page cycle order) is
      available: resolves to the actual TARGET PAGE NAME (`"-> voices"`)
      instead of the literal string "page.jump" repeated identically for
      every roster-positional jump binding (live-verification finding,
      task-6-report.md's own self-review: the overlay's whole purpose is
      "what does this key actually do" -- "page.jump" answers that for a
      human far worse than "jump to voices" does). `roster=None` (the
      default) falls back to the bare action name unchanged. An
      out-of-range/malformed `position` (shouldn't occur for a build's own
      DEFAULT_KEYMAP, but a hand-edited keymap.toml could bind `page.jump`
      to a position past a SMALLER roster) falls back to
      `"{key} (unassigned)"` when `key` is provided, otherwise the bare
      action name.
    - a `page.goto` entry (Phase 9 Task 0, docs/gui-phase-decisions-
      2026-08-08.md digit-nav reconciliation): resolves to `"-> {v1
      ID}:{TITLE}"` (`"-> 8:PIANOROLL"`) when its `name` has a v1 page ID
      (`analyzers.marquee.PAGE_IDS`) -- the SAME `id:TITLE` text the
      marquee's own scrolling header uses for that page
      (`analyzers/marquee.py::_marquee_text`), so a digit key's overlay
      row and the marquee's `[id:TITLE]` entry read as the literal same
      vocabulary (the whole point of mapping digits to v1 IDs in the first
      place). Unlike `page.jump` above, this does NOT need `roster` at
      all -- the v1 ID comes from the STATIC `PAGE_IDS` table, not the
      live cycle order, so it resolves identically whether or not the
      named page is actually reachable on this build (a `PAGE_IDS` entry
      excluded by a narrowed/custom `config.pages` roster -- the STOCK
      default roster currently includes every `PAGE_IDS`-mapped page, so
      this is only reachable via a hand-edited roster today; stale-claim
      fix, Phase 9 close-out: this used to cite "tuner", v1 ID 10, as the
      concrete example, but Task 3 (96b1c12, "feat: live tuner") added it
      to the default roster -- see `engine/core.py::Engine._page_goto`'s
      own graceful-no-op docstring for what actually happens if a digit
      key naming a truly roster-absent page IS pressed). A `page.goto`
      entry naming a page with NO v1 ID (e.g. a hand-written keymap.toml
      binding a key to "screensaver") falls back to the bare
      `"-> {name}"` -- still more informative than the literal action
      name, just without an ID tag it genuinely doesn't have."""
    if isinstance(entry, dict) and entry.get("action") == "page.jump" and roster:
        position = entry.get("args", {}).get("position")
        if isinstance(position, int) and 1 <= position <= len(roster):
            return f"-> {roster[position - 1]}"
        # Out-of-range position: show key with (unassigned) label if key provided
        if key:
            return f"{key} (unassigned)"
    if isinstance(entry, dict) and entry.get("action") == "page.goto":
        name = entry.get("args", {}).get("name")
        if isinstance(name, str) and name:
            page_id = _V1_PAGE_IDS.get(name)
            if page_id is not None:
                title = _V1_PAGE_TITLES.get(name, name.upper())
                return f"-> {page_id}:{title}"
            return f"-> {name}"
    return _entry_action_name(entry)


def _section_lines(title: str, section: dict, roster: list[str] | None) -> list[str]:
    if not section:
        return []
    lines = [title]
    for key, entry in sorted(section.items()):
        lines.append(f"  {key}  {_entry_display_label(entry, roster, key)}")
    return lines


def overlay_lines(keymap_global: dict, keymap_page: dict, page_name: str,
                  roster: list[str] | None = None) -> list[str]:
    """The help overlay's full text content, as plain lines -- a GLOBAL
    section (every key from `Engine.keymap_global`, full action names,
    unlike the compact indicator's abbreviated labels: the overlay is the
    place a user goes to actually LOOK UP a binding, so it should be
    unambiguous) followed by a blank separator and a `page_name`-titled
    section for `keymap_page` (skipped entirely when empty -- most pages
    have no page-specific keys today). `clients/tui.py`'s boxed panel and
    `clients/fb/app.py`'s pixel key-table both draw this SAME list, never
    their own independently-assembled copy.

    `roster` (optional, the live page cycle order -- see `_entry_display_
    label`'s own docstring) resolves `page.jump` entries to their actual
    target page name instead of the bare action name; omitted, this
    function's output is unchanged from before that enhancement."""
    lines = _section_lines("GLOBAL", keymap_global, roster)
    page_section = _section_lines(page_name.upper(), keymap_page, roster)
    if page_section:
        if lines:
            lines.append("")
        lines.extend(page_section)
    return lines


def status_text(vm: dict) -> str:
    """Build the one-line transport status text from an `overlay.status`
    view-model. BAR is 0-indexed, BEAT is 1-indexed within a hardcoded 4/4
    bar -- v1's `plugins/timeclock.py` convention (see
    analyzers/transport.py's docstring); TICK is dropped (not in the VM
    contract -- there is no sub-beat data at this event granularity).
    """
    bar = vm.get("bar", 0)
    beat = vm.get("beat", 1)
    state = "RUN" if vm.get("running") else "STOP"
    source = vm.get("source") or "no clock"
    marker = REC_MARKER if vm.get("rec") else ""
    return (
        f"{marker}BAR {bar:04d}  BEAT {beat:02d}   {format_bpm(vm.get('bpm'))} BPM   "
        f"{state}   clock: {source}"
    )


# -- pianoroll client-side extrapolation (Phase 10 Task A, docs/demo-
# feedback-2026-08-12.md items 3+9) ------------------------------------


def extrapolate_pianoroll_vm(vm: dict, elapsed_s: float) -> dict:
    """Advance a `page.pianoroll` view-model's own x-coordinates locally by
    `elapsed_s` real seconds, using the SAME `window.velocity`/`_x()` clamp
    the engine's own `pages/pianoroll.py::_window()` already documents --
    see that function's own docstring for the full derivation (one
    `-1.0/span_s` formula, correct for both projection modes, no mode
    branch needed here either). The ONE shared implementation both the fb
    and TUI clients call every render frame between real snapshots (never
    duplicated per-client) -- see `pages/pianoroll.py`'s module docstring,
    "Client-side extrapolation" section, for the measured investigation
    this exists to fix.

    Pure: takes `elapsed_s` as a plain float rather than reading a clock
    itself, same "renderer/helper stays pure, caller owns the wall-clock
    read + policy" split every other function in this module follows --
    the caller computes `elapsed_s = client_now - vm["window"]["origin_ts"]`
    using ITS OWN `time.time()` (the SAME clock basis `origin_ts` and
    every note's `onset_ts`/`release_ts` already assume -- valid because
    client and engine run on the same machine/system clock in this
    deployment).

    Returns `vm` UNCHANGED (same object, no copy) when there is nothing
    useful to extrapolate: no `window` dict, a non-finite/zero velocity
    (the degenerate zoom/bpm edge case `_window()` itself already guards),
    or `elapsed_s <= 0` (a clock-skew/out-of-order call must never shift
    the paper BACKWARD). Otherwise returns a NEW dict -- `vm` itself, and
    every `notes[]`/`grid` entry, are never mutated in place, so a caller
    that also holds onto the ORIGINAL snapshot (to compute `elapsed_s`
    fresh against `origin_ts` on the NEXT frame, not compounding drift
    from a previously-extrapolated copy) is safe to keep doing so.

    Only `notes[].x0`/`grid.beat_xs`/`grid.bar_xs` shift unconditionally;
    `notes[].x1` shifts ONLY for a note that is `not active` -- a held
    note's `x1` is pinned at 1.0 by definition (module docstring's VM-shape
    bullet: "A still-held note (active: true) always has x1 == 1.0"),
    shifting it too would incorrectly slide a still-sounding note's right
    edge away from "now". `row_tint`/`overlap_flash` are deliberately NOT
    extrapolated (disclosed, not silently dropped) -- both are secondary,
    position-DERIVED decoration (fade/overlap state, not the scrolling
    paper itself), scoped out to keep this function's contract to exactly
    what items 3+9 asked for: smooth NOTE and GRID motion."""
    window = vm.get("window")
    if not isinstance(window, dict):
        return vm
    velocity = window.get("velocity")
    if not isinstance(velocity, (int, float)) or not math.isfinite(velocity) or velocity == 0.0:
        return vm
    if elapsed_s <= 0:
        return vm
    dx = velocity * elapsed_s

    def shift(x: float) -> float:
        return max(0.0, min(1.0, x + dx))

    notes = vm.get("notes")
    new_notes = notes
    if isinstance(notes, list):
        new_notes = [
            {**note, "x0": shift(note["x0"]),
             "x1": note["x1"] if note.get("active") else shift(note["x1"])}
            for note in notes
        ]
    grid = vm.get("grid")
    new_grid = grid
    if isinstance(grid, dict):
        new_grid = {**grid,
                    "beat_xs": [shift(x) for x in grid.get("beat_xs", [])],
                    "bar_xs": [shift(x) for x in grid.get("bar_xs", [])]}
    return {**vm, "notes": new_notes, "grid": new_grid}
