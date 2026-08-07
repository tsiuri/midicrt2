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
"""
import argparse
import logging
import math
import queue
import threading
from pathlib import Path

from midicrt import config as config_mod
from midicrt.clients.base import (
    ClientError,
    EngineClient,
    current_page_topic,
    drain_latest,
    switch_topic,
    wait_first_snapshot,
)
from midicrt.clients.fb.surface import Surface
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
    whatever fits the remaining height at one text-line per row. Tailing
    reuses `clients.tui._tail`'s exact slicing (imported, not duplicated)
    so the fb and TUI clients agree on "what's visible" for the same
    view-model. Accent-styled lines (currently note_on events -- see
    pages/eventlog.py) draw in the brighter tone.
    """
    font = load_font()
    surface.clear(BG)

    header_h = font.height + 2 * HEADER_PAD
    surface.rect(0, 0, surface.width, header_h, HEADER_BG)
    header_text = f"{vm['title']}  ({vm['count']} events)"
    draw_text(surface, LEFT_MARGIN, HEADER_PAD, header_text, BG, font)

    line_h = font.height + LINE_GAP
    body_h = max(0, (surface.height - header_h) // line_h)
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


RENDERERS = {"eventlog": render_frame}


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
    """
    width, height, stride = _read_fb_geometry()
    surface = Surface(width, height)
    state = {"page": page, "topic": topic}
    on_event = _make_page_switcher(client, state, fps)

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
    surface.write_fb(fb_path, stride=stride)

    period = 1.0 / fps
    while not quit_event.is_set():
        if quit_event.wait(period):
            break
        # Callable, not a frozen `{state["topic"]}` snapshot: `on_event`
        # (invoked from inside this very call) can switch `state["topic"]`
        # mid-drain, and a same-batch snapshot for the NEW topic must
        # still be recognised, not dropped by a stale membership check.
        drained = drain_latest(inbox, lambda: {state["topic"]}, on_event=on_event)
        if state["topic"] in drained:
            vm = drained[state["topic"]]
            renderer = RENDERERS.get(state["page"], _render_unknown)
            renderer(vm, surface)
            surface.write_fb(fb_path, stride=stride)
    return 0


def run(socket_path: str, fb_path: str, out_path: str | None,
        no_input: bool, fps: float) -> int:
    client = EngineClient(socket_path)
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic], max_rate=fps)
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
            # plain fixed `topic`, no `on_event`, is correct as-is.
            vm = wait_first_snapshot(inbox, topic)
            surface = Surface(*OUT_SIZE)
            renderer = RENDERERS.get(page, _render_unknown)
            renderer(vm, surface)
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
