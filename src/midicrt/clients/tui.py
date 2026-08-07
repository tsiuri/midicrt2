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
"""
import json

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


RENDERERS = {"eventlog": render_lines}

_SUBSCRIBE_RATE = 10.0
_KEY_ACTIONS = {"c": "eventlog.clear", "n": "page.next"}


def run_tui(socket_path: str) -> int:
    import blessed

    client = EngineClient(socket_path)
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic], max_rate=_SUBSCRIBE_RATE)
    except ClientError as exc:
        print(f"midicrt tui: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    state = {"page": page, "topic": topic}

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
                    drained = drain_latest(inbox, lambda: {state["topic"]}, on_event=on_event)
                except ClientError:
                    lost = True
                    return 1
                if state["topic"] in drained:
                    vm, dirty = drained[state["topic"]], True
                if dirty:
                    renderer = RENDERERS.get(state["page"], _render_unknown)
                    lines = renderer(vm, term.width, term.height)
                    # Accent (bold) highlighting reaches back into the vm's
                    # own "lines"/"style" shape -- eventlog-specific, but
                    # `.get()` everywhere below keeps a page whose vm lacks
                    # that shape from crashing the loop (it just renders
                    # un-bolded). Factoring this into the per-page renderer
                    # contract is future chrome-factoring work, not task 1.
                    shown = _tail(vm.get("lines", []), term.height - 1)
                    out = [term.home + term.reverse(lines[0]) + term.normal]
                    for i, line in enumerate(lines[1:]):
                        pad = len(lines[1:]) - len(shown)
                        is_accent = i >= pad and shown[i - pad].get("style") == "accent"
                        styled = term.bold(line) if is_accent else line
                        out.append(term.move_xy(0, i + 1) + styled)
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
