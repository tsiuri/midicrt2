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
