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
import queue
import threading
from pathlib import Path

from midicrt import config as config_mod
from midicrt.clients.base import ClientError, EngineClient
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
    `q` sets `quit_event` for a clean exit; `c` fires `eventlog.clear`.
    Any discovery/permission/IO failure logs one line and returns -- input
    is a nice-to-have, never fatal to the render loop.
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
    except OSError as exc:
        _LOG.info("input device error (%s); continuing without input", exc)


# -- run loops ---------------------------------------------------------------

def _drain_latest(inbox: queue.Queue, vm: dict) -> tuple[dict, bool]:
    """Non-blocking drain of pending eventlog snapshots (mirrors
    `tui.run_tui`'s drain-then-render pattern). Returns the newest vm seen
    (or the input `vm` unchanged) and whether anything new arrived. Raises
    `ClientError` on the reader thread's EOF sentinel, so callers treat a
    lost connection the same way a failed connect/subscribe is treated.
    """
    dirty = False
    try:
        while True:
            msg = inbox.get_nowait()
            if msg is None:
                raise ClientError("engine connection lost")
            if msg.get("kind") == "snapshot" and msg.get("topic") == "page.eventlog":
                vm, dirty = msg["data"], True
    except queue.Empty:
        pass
    return vm, dirty


def _wait_first_snapshot(inbox: queue.Queue) -> dict:
    """Block for the first page.eventlog snapshot. subscribe()'s response
    is inline but the snapshot itself arrives via the pusher up to
    1/max_rate later (docs/phase2-notes.md) -- never assume it's already
    queued right after subscribe() returns.
    """
    while True:
        msg = inbox.get()
        if msg is None:
            raise ClientError("engine connection lost")
        if msg.get("kind") == "snapshot" and msg.get("topic") == "page.eventlog":
            return msg["data"]


def _run_device(client: EngineClient, inbox: queue.Queue, fb_path: str,
                 no_input: bool, fps: float) -> int:
    """Real-/dev/fb0 render loop. Coded per the task brief's geometry spec
    but NOT exercised by this task's tests -- v1 owns the CRT until Task
    4's supervised smoke window runs this path for real.
    """
    width, height, stride = _read_fb_geometry()
    surface = Surface(width, height)
    vm = {"title": "EVENT LOG", "count": 0, "lines": []}

    quit_event = threading.Event()
    if not no_input:
        threading.Thread(target=_input_loop, args=(client, quit_event), daemon=True).start()

    render_frame(vm, surface)
    surface.write_fb(fb_path, stride=stride)

    period = 1.0 / fps
    while not quit_event.is_set():
        if quit_event.wait(period):
            break
        vm, dirty = _drain_latest(inbox, vm)
        if dirty:
            render_frame(vm, surface)
            surface.write_fb(fb_path, stride=stride)
    return 0


def run(socket_path: str, fb_path: str, out_path: str | None,
        no_input: bool, fps: float) -> int:
    client = EngineClient(socket_path)
    try:
        client.connect()
        client.subscribe(["page.eventlog"], max_rate=fps)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    try:
        if out_path is not None:
            # Headless test/acceptance mode: render exactly one frame from
            # the first snapshot and exit -- never touches evdev or a real
            # fb device regardless of --no-input/--fb.
            vm = _wait_first_snapshot(inbox)
            surface = Surface(*OUT_SIZE)
            render_frame(vm, surface)
            surface.save_png(out_path)
            return 0
        return _run_device(client, inbox, fb_path, no_input, fps)
    except ClientError as exc:
        print(f"midicrt-fb: {exc}")
        return 1
    finally:
        client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="midicrt-fb")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--fb", default="/dev/fb0")
    ap.add_argument("--out", default=None, metavar="PATH",
                     help="render one frame to this PNG and exit (headless test mode)")
    ap.add_argument("--no-input", action="store_true")
    ap.add_argument("--fps", type=float, default=30.0)
    args = ap.parse_args()

    socket_path = args.socket or config_mod.load(None).socket_path
    raise SystemExit(run(socket_path, args.fb, args.out, args.no_input, args.fps))


if __name__ == "__main__":
    main()
