"""midicrt-web -- aiohttp HTTP/WS wiring around Bridge (see bridge.py for
the EngineClient<->asyncio fan-out design this module is just plumbing
for).

Control posture (Phase 9 Task 4, user ruling: "web control ON, no auth" --
flips the Phase 6 parity-era default): control is ON by default. `main()`'s
CLI flag inverted from an opt-IN `--allow-control` to an opt-OUT
`--read-only` -- see `main()`'s own comment for why keeping the OLD flag
name with its default merely flipped would have been confusing (a flag
literally named "allow control" that's already true by default can't be
used to turn control back OFF). The internal `allow_control: bool` plumbing
itself (`create_app`, `_ALLOW_CONTROL_KEY`, `_handle_action`'s 403 gate,
the served page's `ALLOW_CONTROL` JS constant) is unchanged -- only the CLI
boundary in `main()` translates the new flag to that same internal value,
so `create_app(bridge, allow_control=...)`'s own contract (and every test
against it) needed no changes.

With control ON, `/api/action` accepts requests and the served page shows
its control UI via the `ALLOW_CONTROL` flag baked into the page at request
time; with `--read-only` passed, `/api/action` refuses with 403 and the
control UI stays hidden -- both gates exist independently so a browser
that ignores the hidden UI (hand-crafted fetch/curl) still can't drive the
engine when `--read-only` is set.
"""
import argparse
import asyncio
import logging
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from midicrt import config as config_mod
from midicrt.clients.base import ClientError, EngineClient
from midicrt.clients.web.bridge import Bridge, WebSink

_LOG = logging.getLogger("midicrt-web")

DEFAULT_PORT = 8766  # v1 (fb/tui-adjacent) owns 8765; web is a distinct port
_PAGE_HTML_PATH = Path(__file__).parent / "page.html"

_BRIDGE_KEY = web.AppKey("bridge", Bridge)
_ALLOW_CONTROL_KEY = web.AppKey("allow_control", bool)


def _load_page_html(allow_control: bool) -> str:
    html = _PAGE_HTML_PATH.read_text()
    flag = "true" if allow_control else "false"
    return html.replace("__ALLOW_CONTROL__", flag)


# -- HTTP handlers -----------------------------------------------------------

async def _handle_index(request: web.Request) -> web.Response:
    html = _load_page_html(request.app[_ALLOW_CONTROL_KEY])
    return web.Response(text=html, content_type="text/html")


async def _handle_describe(request: web.Request) -> web.Response:
    bridge: Bridge = request.app[_BRIDGE_KEY]
    try:
        data = await asyncio.to_thread(bridge.describe)
    except ClientError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(data)


async def _handle_action(request: web.Request) -> web.Response:
    if not request.app[_ALLOW_CONTROL_KEY]:
        return web.json_response(
            {"error": "control disabled (start midicrt-web with --allow-control)"}, status=403)
    bridge: Bridge = request.app[_BRIDGE_KEY]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "malformed JSON body"}, status=400)
    name = body.get("name") if isinstance(body, dict) else None
    if not name:
        return web.json_response({"error": "missing 'name'"}, status=400)
    args = body.get("args") or {}
    try:
        data = await asyncio.to_thread(bridge.action, name, args)
    except ClientError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(data)


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """One WebSink per websocket, fed by Bridge's fan-out (bridge.py). This
    handler is a pure transport shim: it never touches engine protocol
    semantics directly, just forwards whatever the bridge already decided
    to fan out, plus an initial `hello` frame for pages that connect
    mid-session. Browser->server frames aren't part of the v1 protocol
    (control goes through /api/action instead) -- the receive task exists
    only to notice the browser closing the socket.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    bridge: Bridge = request.app[_BRIDGE_KEY]
    sink = WebSink()
    bridge.add_sink(sink)
    await ws.send_json(bridge.hello_message())

    recv_task = asyncio.ensure_future(ws.receive())
    send_task = asyncio.ensure_future(sink.queue.get())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                msg = recv_task.result()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                 WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                recv_task = asyncio.ensure_future(ws.receive())
            if send_task in done:
                item = send_task.result()
                if item is None:  # bridge.py's EOF sentinel: engine connection lost
                    break
                await ws.send_json(item)
                send_task = asyncio.ensure_future(sink.queue.get())
    finally:
        bridge.remove_sink(sink)
        for task in (recv_task, send_task):
            if not task.done():
                task.cancel()
        if not ws.closed:
            await ws.close(code=WSCloseCode.GOING_AWAY)
    return ws


# -- app factory ---------------------------------------------------------------

def create_app(bridge: Bridge, allow_control: bool) -> web.Application:
    app = web.Application()
    app[_BRIDGE_KEY] = bridge
    app[_ALLOW_CONTROL_KEY] = allow_control
    app.router.add_get("/", _handle_index)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_get("/api/describe", _handle_describe)
    app.router.add_post("/api/action", _handle_action)

    async def _on_startup(app: web.Application) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(bridge.start, loop)

    async def _on_cleanup(app: web.Application) -> None:
        await asyncio.to_thread(bridge.stop)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def _build_arg_parser() -> argparse.ArgumentParser:
    """Split out from `main()` so the flag semantics (defaults, help text)
    are testable without invoking `web.run_app` (which blocks forever)."""
    ap = argparse.ArgumentParser(prog="midicrt-web")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    # Phase 9 Task 4 (user ruling: "web control ON, no auth" -- control is
    # now the DEFAULT posture, inverting Phase 6's parity-era opt-IN
    # `--allow-control` flag). An opt-OUT `--read-only` flag replaces it
    # rather than just flipping `--allow-control`'s own default: a flag
    # literally named "allow control" that already defaults to true offers
    # no way to turn control back OFF (`store_true` can only ever push a
    # flag's value AWAY from its default, never restore it) -- the flag
    # NAME has to invert along with its default, or the CLI becomes
    # unusable for the one case (an operator who explicitly wants
    # read-only) that most needs a flag at all.
    ap.add_argument("--read-only", action="store_true",
                    help="disable the control surface (default: control is ON)")
    return ap


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    args = _build_arg_parser().parse_args()
    allow_control = not args.read_only

    socket_path = args.socket or config_mod.load(None).socket_path
    client = EngineClient(socket_path)
    bridge = Bridge(client)
    app = create_app(bridge, allow_control=allow_control)
    _LOG.info("midicrt-web up on http://%s:%d (allow_control=%s, socket=%s)",
              args.host, args.port, allow_control, socket_path)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
