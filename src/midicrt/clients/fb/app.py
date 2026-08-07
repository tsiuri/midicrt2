"""fb/app.py -- midicrt-fb: the framebuffer CRT client entrypoint.

Renders the eventlog page view-model onto a pixel `Surface` (fb/surface.py)
using the vendored PSF console font (fb/text.py), then either writes the
packed RGB565 buffer to a real Linux framebuffer device or, in `--out` test
mode, saves a PNG and exits -- see `main()`/`run()` below.

Colour palette ported from v1's default CRT-green scheme
(`~/codex/midicrt/fb/compositor.py` on the Pi, read-only reference):
    GREEN_BRIGHT_RGB = (0, 255, 80)   # here: HEADER_BG and ACCENT_FG
    GREEN_MID_RGB    = (0, 180, 50)   # here: NORMAL_FG
    BLACK_RGB        = (0, 0, 0)      # here: BG
Accent (note_on) event lines reuse v1's "bright" tone so they read louder
than the "mid" tone used for ordinary lines, and the header bar's
reverse-video fill reuses the same bright tone with text punched out in the
background colour.

This module does not write to /dev/fb0 during Phase 2 Task 3 -- v1 owns the
real CRT until Task 4's supervised smoke test exercises the real-device path
(`_run_device`/`_read_fb_geometry`) for real. All tests here use `--out` PNG
mode against a real (but socket-only) daemon.

Chrome (phase 3 task 3)
------------------------
A bottom status strip (`_draw_status`, `font.height + 2*STATUS_PAD` px
tall) now mirrors the TUI's bottom row: same shared text
(`clients/chrome.py`'s `status_text()`), same reverse-video treatment as
the header. `render_frame` reserves the strip's height when computing how
many event lines fit (`_status_strip_height`) so the page body never draws
under it, but does NOT draw the strip itself -- `render_frame`'s own
signature/contract is unchanged (still `(vm, Surface) -> None`, still only
the eventlog page's own content, no analyzer/overlay knowledge). The run
loops (`run()`'s `--out` branch and `_run_device`) call `_draw_status`
separately, right after the page renderer, same "page owns everything
except the reserved strip" split as tui.py's `render_lines`/
`render_status_row`. Both clients subscribe to `overlay.status` ALONGSIDE
the current page's topic (multi-topic subscribe).
"""
import argparse
import logging
import math
import queue
import threading
from pathlib import Path

from midicrt import config as config_mod
from midicrt.clients import chrome
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    drain_latest,
    switch_topic,
    wait_first_snapshot,
)
from midicrt.clients.fb.surface import Surface, open_fb_mmap
from midicrt.clients.fb.text import draw_text, load_font
from midicrt.clients.tui import _tail

_LOG = logging.getLogger("midicrt-fb")

# -- palette (see module docstring for provenance) ------------------------
BG = (0, 0, 0)
HEADER_BG = (0, 255, 80)
NORMAL_FG = (0, 180, 50)
ACCENT_FG = (0, 255, 80)

# -- layout -----------------------------------------------------------------
HEADER_PAD = 2   # vertical inset (top+bottom) inside the header bar, px
LINE_GAP = 1     # extra vertical gap between event-line rows, px
LEFT_MARGIN = 4  # left inset for header + event text, px
STATUS_PAD = 2   # vertical inset (top+bottom) inside the status strip, px -- mirrors HEADER_PAD

# `--out` mode always renders at this fixed size (task brief: "--out mode
# uses 800x475 fixed"), independent of whatever a real /dev/fb0 reports.
OUT_SIZE = (800, 475)

_SYS_FB = Path("/sys/class/graphics/fb0")


def render_frame(vm: dict, surface: Surface) -> None:
    """Render an eventlog view-model onto `surface`. Pure: reads only `vm`
    and the cached default font, writes only to `surface`'s pixels -- no
    I/O, no clock, no global state beyond the font's (side-effect-free)
    glyph cache.

    Layout: a reverse-video header bar (`HEADER_BG` fill, text painted in
    `BG` so it reads as inverted) showing "<title>  (<count> events)", then
    event lines below it, oldest-to-newest top-to-bottom, tailed to
    whatever fits the remaining height at one text-line per row. The
    bottom `_status_strip_height(font)` px are reserved (left as
    background -- NOT drawn here, see `_draw_status`) so the page body
    never overlaps the chrome status strip the run loops paint after this.
    Tailing reuses `clients.tui._tail`'s exact slicing (imported, not
    duplicated) so the fb and TUI clients agree on "what's visible" for
    the same view-model. Accent-styled lines (currently note_on events --
    see pages/eventlog.py) draw in the brighter tone.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    header_text = f"{vm['title']}  ({vm['count']} events)"
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, header_text, BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _status_strip_height(font)
    body_h = max(0, (usable_h - header_h) // line_h)
    for i, line in enumerate(_tail(vm["lines"], body_h)):
        color = ACCENT_FG if line["style"] == "accent" else NORMAL_FG
        draw_text(surface, LEFT_MARGIN, header_h + i * line_h, line["text"], color, font)


def _render_unknown(vm: dict, surface: Surface) -> None:
    """Fallback for a page name this client build has no renderer for --
    see clients/tui.py's `_render_unknown` for the rationale (wire compat
    is additive-only, so an older client can meet a newer server's extra
    page without crashing). Just clears to background; no text drawn since
    an unrecognised vm shape may not have a "title"/"count" to show."""
    surface.clear(BG)


# -- voices page (phase-3 task 4) --------------------------------------------
#
# Layout: the same reverse-video header convention as `render_frame`'s
# eventlog header, then one row per channel -- "<ch> <name>" text plus a
# bordered vertical poly-meter (`box` for the frame, `fill_column` for the
# live fill, `hline` for the peak-hold tick) and a numeric "<active>/<peak>"
# readout. BAR_MAX=8 matches v1's zvoicemonitor.py per-channel poly-limit
# default (POLY_LIMIT_CH) -- a fixed visual scale only, not an enforced
# limit (no limit/warning behavior is ported -- see analyzers/voices.py's
# module docstring); a channel with 8+ held voices just shows a full meter,
# the numeric label stays exact. Deliberately NOT sharing bar math with the
# TUI renderer's own `_voices_bar` (see that function's comment) -- this
# isn't the page-agnostic chrome status text clients/chrome.py exists to
# keep byte-identical across clients.
ROW_PAD = 1            # vertical inset (top+bottom) inside each row's meter box, px
NAME_COL_CHARS = 15    # "01 " + up to a 12-char instrument name
NAME_MAX_CHARS = 12    # matches TUI's own name-column width (clients/tui.py's
                       # _VOICES_NAME_WIDTH) -- keeps "01 <name>" within
                       # NAME_COL_CHARS so it can never paint into `bar_x`'s
                       # gap or the meter box itself (review fix: a
                       # config-overridden name longer than this used to draw
                       # untruncated, corrupting the layout).
BAR_GAP = 8            # px between the name column and the meter, and meter and label
BAR_W = 40             # px, includes the 1px outline on each side
BAR_MAX = 8            # visual scale -- see comment above


def _voices_header_text(vm: dict) -> str:
    return f"{vm['title']}  (poly {vm['total']}/{vm['total_peak']})"


def render_voices_frame(vm: dict, surface: Surface) -> None:
    """Render the voices page view-model (pages/voices.py, wrapping
    analyzers/voices.py's VoiceMonitorAnalyzer) onto `surface`. Pure: reads
    only `vm` and the cached default font, writes only to `surface`'s
    pixels -- no I/O, no clock, no global state beyond the font's
    (side-effect-free) glyph cache. See module docstring for the layout.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _voices_header_text(vm), BG, font)

    rows = vm["rows"]
    if not rows:
        return
    usable_h = surface.height - _status_strip_height(font) - header_h
    row_h = usable_h // len(rows)
    if row_h <= 0:
        return

    bar_x = LEFT_MARGIN + NAME_COL_CHARS * font.width + BAR_GAP
    for i, row in enumerate(rows):
        row_y = header_h + i * row_h
        text_y = row_y + max(0, (row_h - font.height) // 2)
        name = row["name"][:NAME_MAX_CHARS]
        draw_text(surface, LEFT_MARGIN, text_y, f"{row['ch']:02d} {name}", NORMAL_FG, font)

        bar_y = row_y + ROW_PAD
        bar_h = row_h - 2 * ROW_PAD
        if bar_h <= 0:
            continue
        surface.box(bar_x, bar_y, BAR_W, bar_h, NORMAL_FG)
        inner_h = max(0, bar_h - 2)
        inner_w = max(0, BAR_W - 2)
        fill_h = round(inner_h * min(row["active"], BAR_MAX) / BAR_MAX)
        if fill_h > 0:
            fill_color = ACCENT_FG if row["active"] > 0 else NORMAL_FG
            surface.fill_column(bar_x + 1, bar_y + bar_h - 2, fill_h, fill_color, width=inner_w)
        peak_h = round(inner_h * min(row["peak"], BAR_MAX) / BAR_MAX)
        if peak_h > 0:
            peak_y = bar_y + bar_h - 1 - peak_h
            surface.hline(bar_x + 1, peak_y, inner_w, ACCENT_FG)

        label_x = bar_x + BAR_W + BAR_GAP
        draw_text(surface, label_x, text_y, f"{row['active']}/{row['peak']}", NORMAL_FG, font)


# -- harmony page (phase-3 task 5) -------------------------------------------
#
# Layout: same reverse-video header convention as `render_frame`/
# `render_voices_frame`, then one text row per v1 Notes-page harmony field
# (Chord/Scale "Last/2nd/3rd/4th" slots -- collapsed to one text line each
# here rather than the TUI's separate label+values rows, since a pixel
# surface doesn't need a second row just to spell out "Last 2nd 3rd 4th"
# -- Inside/Outside, conf+missing, Key), a PRIMITIVES-drawn tension bar
# (`box` for the outline + `rect` for the live fill, per the task brief:
# "fb: text rows + tension bar via rect/fill primitives" -- deliberately
# NOT the TUI's block-character bar), then Harm.rhy/Motif. Row TEXT is
# generated independently of `clients/tui.py`'s own harmony helpers --
# same non-sharing convention as `render_voices_frame`/`_voices_bar` vs
# `render_voices_lines`/`_voices_bar` (a per-page body widget isn't the
# page-agnostic chrome status text `clients/chrome.py` exists to keep
# byte-identical across clients). Rows are drawn top-down and simply
# stop (rather than wrap/scroll) once they'd cross into the reserved
# bottom status strip, mirroring `render_frame`'s own reservation
# convention.
HARMONY_TENSION_BAR_W = 160   # px


def _harmony_header_text(vm: dict) -> str:
    return f"{vm['title']}  (key: {vm['key'] or '?'})"


def _harmony_slots_text(prefix: str, items: list[dict]) -> str:
    names = []
    for i in range(4):
        name = items[i]["name"] if i < len(items) else None
        names.append(name or "--")
    return f"{prefix} " + "  ".join(names)


def _harmony_conf_missing_text(prefix: str, items: list[dict]) -> str:
    if items and items[0]["conf"] is not None:
        missing = " ".join(items[0]["missing"]) or "-"
        return f"{prefix} {items[0]['conf']:.2f}  missing: {missing}"
    return f"{prefix} --  missing: -"


def _harmony_key_text(vm: dict) -> str:
    text = f"Key: {vm['key'] or '?'}"
    if vm.get("key_alternatives"):
        text += f"  (alts: {', '.join(vm['key_alternatives'])})"
    return text


def _harmony_rhythm_text(vm: dict) -> str:
    hr = vm["harmonic_rhythm"]
    if hr and hr.get("changes_per_bar") is not None:
        return f"Harm.rhy: {hr['changes_per_bar']:.1f} ch/bar  {hr['label']}"
    return "Harm.rhy: --"


def _harmony_motif_text(vm: dict) -> str:
    motif = vm["motif"]
    if motif and motif.get("found"):
        return f"Motif: {motif['pattern']}  [x{motif['count']}]"
    return "Motif: --"


def render_harmony_frame(vm: dict, surface: Surface) -> None:
    """Render the harmony page view-model (pages/harmony.py, wrapping
    analyzers/harmony.py's HarmonyAnalyzer) onto `surface`. Pure: reads
    only `vm` and the cached default font, writes only to `surface`'s
    pixels -- no I/O, no clock, no global state beyond the font's
    (side-effect-free) glyph cache. See module comment above for layout.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, _harmony_header_text(vm), BG, font)

    line_h = font.height + LINE_GAP
    usable_h = surface.height - _status_strip_height(font)
    y = header_h

    def _row(text: str) -> None:
        nonlocal y
        if y + font.height > usable_h:
            return
        draw_text(surface, LEFT_MARGIN, y, text, NORMAL_FG, font)
        y += line_h

    _row(_harmony_slots_text("Chord:", vm["chords"]))
    _row(_harmony_slots_text("Scale:", vm["scales"]))
    _row(f"Inside: {' '.join(vm['inside']) or '-'}")
    _row(f"Outside: {' '.join(vm['outside']) or '-'}")
    _row(_harmony_conf_missing_text("Chord conf:", vm["chords"]))
    _row(_harmony_conf_missing_text("Scale conf:", vm["scales"]))
    _row(_harmony_key_text(vm))

    if y + font.height <= usable_h:
        bar_h = font.height
        prefix_w = draw_text(surface, LEFT_MARGIN, y, "Tension:", NORMAL_FG, font)
        bar_x = LEFT_MARGIN + prefix_w + BAR_GAP
        surface.box(bar_x, y, HARMONY_TENSION_BAR_W, bar_h, NORMAL_FG)
        inner_w = max(0, HARMONY_TENSION_BAR_W - 2)
        fill_w = round(inner_w * min(max(vm["tension"], 0.0), 1.0))
        if fill_w > 0:
            fill_color = ACCENT_FG if vm["tension"] > 0.5 else NORMAL_FG
            surface.rect(bar_x + 1, y + 1, fill_w, bar_h - 2, fill_color)
        label = f" {vm['tension']:.2f}  {vm.get('tension_label', '')}"
        draw_text(surface, bar_x + HARMONY_TENSION_BAR_W + BAR_GAP, y, label,
                  NORMAL_FG, font)
        y += line_h

    _row(_harmony_rhythm_text(vm))
    _row(_harmony_motif_text(vm))


RENDERERS = {"eventlog": render_frame, "voices": render_voices_frame,
             "harmony": render_harmony_frame}


# -- chrome: status strip (phase-3 task 3) -----------------------------------


def _status_strip_height(font) -> int:
    """Pixel height of the bottom status strip -- mirrors the header's own
    `font.height + 2*HEADER_PAD` sizing convention (see module docstring)."""
    return font.height + 2 * STATUS_PAD


def _draw_status(surface: Surface, vm: dict, font) -> None:
    """Paint the bottom status strip onto `surface`: a reverse-video bar
    (same `HEADER_BG` fill / `BG` text convention as the page header)
    showing the shared chrome status text (`clients/chrome.py` --
    word-for-word identical to the TUI's bottom row, per the task-3
    brief's "mirrors it"). Pinned to the bottom `_status_strip_height(font)`
    px of `surface`, which `render_frame` already leaves clear for this.
    Pure aside from the font glyph cache, same contract as `render_frame`.
    """
    strip_h = _status_strip_height(font)
    y = surface.height - strip_h
    surface.rect(0, y, surface.width, strip_h, HEADER_BG)
    draw_text(surface, LEFT_MARGIN, y + STATUS_PAD, chrome.status_text(vm), BG, font)


# -- real-device geometry (coded here, exercised only in Task 4) -----------

def _read_fb_geometry() -> tuple[int, int, int]:
    """Read (width, height, stride) for the real /dev/fb0 device from sysfs
    at runtime, rather than hardcoding -- v1 confirmed 800x475/stride 1600
    on this hardware (task-2 report), but this must survive a kernel/driver
    change. `stride` falls back to width*2 (tightly packed rows, matching
    `Surface.write_fb`'s own default) when the sysfs node is absent.
    """
    w_str, h_str = (_SYS_FB / "virtual_size").read_text().strip().split(",")
    width, height = int(w_str), int(h_str)
    stride_path = _SYS_FB / "stride"
    stride = int(stride_path.read_text().strip()) if stride_path.exists() else width * 2
    return width, height, stride


# -- evdev input (background thread; never fatal) --------------------------

def _find_input_device():
    """Return the first evdev device exposing KEY_Q, or None if none do."""
    import evdev

    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if evdev.ecodes.KEY_Q in dev.capabilities().get(evdev.ecodes.EV_KEY, []):
            return dev
    return None


def _input_loop(client: EngineClient, quit_event: threading.Event) -> None:
    """Background-thread evdev reader (brief: "Input runs in a thread").
    `q` sets `quit_event` for a clean exit; `c` fires `eventlog.clear`; `n`
    fires `page.next` (the resulting `page_changed` event and resubscribe
    are handled by the render loop's `drain_latest(on_event=...)`, not
    here). Any discovery/permission/IO failure logs one line and returns --
    input is a nice-to-have, never fatal to the render loop.
    """
    try:
        import evdev

        dev = _find_input_device()
    except Exception as exc:  # noqa: BLE001 -- any evdev/discovery failure is non-fatal
        _LOG.info("input unavailable (%s); continuing without input", exc)
        return
    if dev is None:
        _LOG.info("no input device with KEY_Q capability found; continuing without input")
        return
    try:
        for event in dev.read_loop():
            if event.type != evdev.ecodes.EV_KEY or event.value != 1:  # key-down only
                continue
            if event.code == evdev.ecodes.KEY_Q:
                quit_event.set()
                return
            if event.code == evdev.ecodes.KEY_C:
                try:
                    client.action("eventlog.clear")
                except ClientError:
                    pass  # connection loss surfaces via the render loop's EOF check
            if event.code == evdev.ecodes.KEY_N:
                try:
                    client.action("page.next")
                except ClientError:
                    pass  # connection loss surfaces via the render loop's EOF check
    except OSError as exc:
        _LOG.info("input device error (%s); continuing without input", exc)


# -- run loops ---------------------------------------------------------------
#
# Page dispatch mirrors clients/tui.py: `RENDERERS` maps page name -> its
# `(vm, Surface) -> None` renderer, connect asks `describe` for the CURRENT
# page instead of assuming "eventlog" (`current_page_topic`), and a
# `page_changed` event triggers `switch_topic` (unsubscribe old, subscribe
# new) via `drain_latest`'s `on_event` callback. `_drain_latest`/
# `_wait_first_snapshot` used to be private copies of this exact logic --
# now shared with the TUI client via `clients/base.py`.


def _make_page_switcher(client: EngineClient, state: dict, max_rate: float):
    """Return a `drain_latest(on_event=...)` callback that reacts to
    `page_changed` by resubscribing and updating `state["page"]`/`
    state["topic"]` in place."""

    def on_event(msg: dict) -> None:
        if msg.get("kind") == "event" and msg.get("name") == "page_changed":
            new_page = msg["data"]["page"]
            new_topic = f"page.{new_page}"
            switch_topic(client, state["topic"], new_topic, max_rate)
            state["page"], state["topic"] = new_page, new_topic

    return on_event


def _run_device(client: EngineClient, inbox: queue.Queue, fb_path: str,
                 no_input: bool, fps: float, page: str, topic: str) -> int:
    """Real-/dev/fb0 render loop. Coded per the task brief's geometry spec
    but NOT exercised by this task's tests -- v1 owns the CRT until Task
    4's supervised smoke window runs this path for real.

    Opens and mmaps the framebuffer device ONCE for the life of this loop
    (`open_fb_mmap`) and writes each frame into that mapping
    (`Surface.write_to_mmap`) rather than `write_fb()`'s reopen-the-device-
    every-frame path -- reopening a character device 30 times a second is
    needless syscall overhead once the pixel pack itself is fast (see
    surface.py's module docstring for the to_rgb565 benchmark that made
    this worth doing).
    """
    width, height, stride = _read_fb_geometry()
    surface = Surface(width, height)
    font = load_font()
    state = {"page": page, "topic": topic, "status_vm": dict(chrome.DEFAULT_STATUS_VM)}
    on_event = _make_page_switcher(client, state, fps)

    fb_file, fb_mm = open_fb_mmap(fb_path, stride * height)
    try:
        quit_event = threading.Event()
        if not no_input:
            # `on_event` is already constructed above (before this thread
            # starts): the input thread can fire `page.next` immediately, and
            # the main thread is about to block in `wait_first_snapshot` below
            # -- without event-awareness there, that page_changed would be
            # silently dropped and the client would stay on the stale topic
            # forever (this was the actual freeze: the input thread calling
            # client.action() while the main thread's own switch_topic() call
            # from a later on_event races it over the same connection).
            threading.Thread(target=_input_loop, args=(client, quit_event), daemon=True).start()

        vm = wait_first_snapshot(inbox, lambda: state["topic"], on_event)
        renderer = RENDERERS.get(state["page"], _render_unknown)
        renderer(vm, surface)
        _draw_status(surface, state["status_vm"], font)
        surface.write_to_mmap(fb_mm, stride=stride)

        period = 1.0 / fps
        while not quit_event.is_set():
            if quit_event.wait(period):
                break
            # Callable, not a frozen `{state["topic"]}` snapshot: `on_event`
            # (invoked from inside this very call) can switch `state["topic"]`
            # mid-drain, and a same-batch snapshot for the NEW topic must
            # still be recognised, not dropped by a stale membership check.
            # `overlay.status` is a fixed second member -- updates on its own
            # schedule (once a beat), independent of the page topic.
            drained = drain_latest(
                inbox, lambda: {state["topic"], chrome.OVERLAY_STATUS_TOPIC},
                on_event=on_event)
            page_updated = state["topic"] in drained
            if page_updated:
                vm = drained[state["topic"]]
            status_updated = chrome.OVERLAY_STATUS_TOPIC in drained
            if status_updated:
                state["status_vm"] = drained[chrome.OVERLAY_STATUS_TOPIC]
            if page_updated or status_updated:
                # `render_frame` clears the WHOLE surface, so the status
                # strip must be repainted on every redraw, not just when
                # `status_vm` itself changed.
                renderer = RENDERERS.get(state["page"], _render_unknown)
                renderer(vm, surface)
                _draw_status(surface, state["status_vm"], font)
                surface.write_to_mmap(fb_mm, stride=stride)
        return 0
    finally:
        fb_mm.close()
        fb_file.close()


def run(socket_path: str, fb_path: str, out_path: str | None,
        no_input: bool, fps: float) -> int:
    client = EngineClient(socket_path)
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic, chrome.OVERLAY_STATUS_TOPIC], max_rate=fps)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    try:
        if out_path is not None:
            # Headless test/acceptance mode: render exactly one frame from
            # the first snapshot and exit -- never touches evdev or a real
            # fb device regardless of --no-input/--fb. No input thread
            # exists in this path (deliberately, per the docstring above),
            # so there's no concurrent source of a page_changed here -- a
            # plain fixed `topic` for the PAGE wait is correct as-is; the
            # overlay.status snapshot (seeded server-side at subscribe()
            # time, same as the page's) is captured opportunistically via
            # `on_event` -- both are typically delivered in the very first
            # push tick, but a sane default covers the case it hasn't
            # landed yet by the time the page snapshot does.
            status = {"vm": dict(chrome.DEFAULT_STATUS_VM)}

            def _capture_status(msg: dict) -> None:
                if msg.get("kind") == "snapshot" and msg.get("topic") == chrome.OVERLAY_STATUS_TOPIC:
                    status["vm"] = msg["data"]

            vm = wait_first_snapshot(inbox, topic, _capture_status)
            surface = Surface(*OUT_SIZE)
            renderer = RENDERERS.get(page, _render_unknown)
            renderer(vm, surface)
            _draw_status(surface, status["vm"], load_font())
            surface.save_png(out_path)
            return 0
        return _run_device(client, inbox, fb_path, no_input, fps, page, topic)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        return 1
    finally:
        client.close()


def _fps_type(value: str) -> float:
    """argparse `type=` for --fps: must parse as a finite, positive float.
    Rejecting here (before connect()) keeps a bad value off the wire --
    the server rejects non-positive/non-finite max_rate too, but the
    client never checked that response, so a rejected subscribe() left
    `run()` blocked forever in `_wait_first_snapshot` (connection alive,
    no snapshot ever coming). See EngineClient.request()/subscribe() for
    the other half of this fix -- callers should no longer get a silent
    ok:false response either way, but validating here means a bad --fps
    never has to round-trip to find that out.
    """
    try:
        fps = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if not math.isfinite(fps) or fps <= 0:
        raise argparse.ArgumentTypeError(f"--fps must be a finite number > 0, got {value!r}")
    return fps


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="midicrt-fb")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--fb", default="/dev/fb0")
    ap.add_argument("--out", default=None, metavar="PATH",
                     help="render one frame to this PNG and exit (headless test mode)")
    ap.add_argument("--no-input", action="store_true")
    ap.add_argument("--fps", type=_fps_type, default=30.0)
    args = ap.parse_args()

    socket_path = args.socket or config_mod.load(None).socket_path
    raise SystemExit(run(socket_path, args.fb, args.out, args.no_input, args.fps))


if __name__ == "__main__":
    main()
