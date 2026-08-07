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
DEFAULT_STATUS_VM = {"bpm": None, "bar": 0, "beat": 1, "running": False, "source": None}


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
    return (
        f"BAR {bar:04d}  BEAT {beat:02d}   {format_bpm(vm.get('bpm'))} BPM   "
        f"{state}   clock: {source}"
    )
