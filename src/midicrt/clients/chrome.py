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
"""

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


OVERLAY_ALERTS_TOPIC = "overlay.alerts"
OVERLAY_TIMESIG_TOPIC = "overlay.timesig"

DEFAULT_ALERTS_VM = {"alerts": []}
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


def alerts_text(vm: dict) -> str:
    """Build v1's "STUCK WARN/CRIT: ..." banner line from an
    `overlay.alerts` view-model. `""` (blank row) when nothing is stuck --
    v1 holds its last message for `HOLD_AFTER` (15s) after clearing; this
    analyzer doesn't carry that history (see analyzers/stucknotes.py's
    module docstring), so the row goes blank the instant the alert list
    empties. Note numbers are shown raw (`n060`), not v1's octave-letter
    name (`_fmt_note`, e.g. note 60 -> "C6(060)") -- see analyzers/
    stucknotes.py's module docstring for why that convention isn't
    reproduced.
    """
    alerts = vm.get("alerts") or []
    if not alerts:
        return ""
    level = "CRIT" if any(a["level"] == "crit" for a in alerts) else "WARN"
    parts = [f"CH{a['ch']:02d} n{a['note']:03d} {a['held_s']:4.1f}s" for a in alerts[:MAX_ALERT_LIST]]
    extra = len(alerts) - MAX_ALERT_LIST
    if extra > 0:
        parts.append(f"+{extra} more")
    return f"STUCK {level}: " + " | ".join(parts)


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


def secondary_status_text(alerts_vm: dict, timesig_vm: dict) -> str:
    """The shared second chrome row's text: `alerts_text()` when any alert
    is active, else `timesig_text()` -- see the module-level comment above
    `alerts_text` for why these two share one row."""
    text = alerts_text(alerts_vm)
    return text if text else timesig_text(timesig_vm)


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
    action name, EXCEPT for a `page.jump` entry when `roster` (the live
    page cycle order) is available: those resolve to the actual TARGET
    PAGE NAME (`"-> voices"`) instead of the literal string "page.jump"
    repeated identically for all 20 roster-positional jump bindings
    (live-verification finding, task-6-report.md's own self-review: the
    overlay's whole purpose is "what does this key actually do" --
    "page.jump" answers that for a human far worse than "jump to voices"
    does). `roster=None` (the default -- no caller currently omits it
    except pre-existing test literals) falls back to the bare action name
    unchanged, so this is purely additive. An out-of-range/malformed
    `position` (shouldn't occur for a build's own DEFAULT_KEYMAP, but a
    hand-edited keymap.toml could bind `page.jump` to a position past a
    SMALLER roster) falls back to `"{key} (unassigned)"` when `key` is
    provided, otherwise the bare action name."""
    if isinstance(entry, dict) and entry.get("action") == "page.jump" and roster:
        position = entry.get("args", {}).get("position")
        if isinstance(position, int) and 1 <= position <= len(roster):
            return f"-> {roster[position - 1]}"
        # Out-of-range position: show key with (unassigned) label if key provided
        if key:
            return f"{key} (unassigned)"
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
