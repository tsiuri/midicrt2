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
"""
import json

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


RENDERERS = {"eventlog": render_lines}

_SUBSCRIBE_RATE = 10.0
_KEY_ACTIONS = {"c": "eventlog.clear", "n": "page.next"}


def run_tui(socket_path: str) -> int:
    import blessed

    client = EngineClient(socket_path)
    try:
        client.connect()
        page, topic = current_page_topic(client)
        client.subscribe([topic, chrome.OVERLAY_STATUS_TOPIC], max_rate=_SUBSCRIBE_RATE)
    except ClientError as exc:
        print(f"midicrt tui: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    state = {"page": page, "topic": topic, "status_vm": dict(chrome.DEFAULT_STATUS_VM)}

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
                    # `overlay.status` is a FIXED second member -- it is
                    # never switched, so it needs no such closure trick.
                    drained = drain_latest(
                        inbox, lambda: {state["topic"], chrome.OVERLAY_STATUS_TOPIC},
                        on_event=on_event)
                except ClientError:
                    lost = True
                    return 1
                if state["topic"] in drained:
                    vm, dirty = drained[state["topic"]], True
                if chrome.OVERLAY_STATUS_TOPIC in drained:
                    state["status_vm"] = drained[chrome.OVERLAY_STATUS_TOPIC]
                    dirty = True
                if dirty:
                    # Chrome reserves the LAST row; the page renders header +
                    # body into the remaining `height - 1` rows (see module
                    # docstring's "page owns height-2 rows" note).
                    renderer = RENDERERS.get(state["page"], _render_unknown)
                    page_lines = renderer(vm, term.width, term.height - 1)
                    status_line = render_status_row(state["status_vm"], term.width)
                    header_line, body_lines = page_lines[0], page_lines[1:]
                    # Accent (bold) highlighting reaches back into the vm's
                    # own "lines"/"style" shape -- eventlog-specific, but
                    # `.get()` everywhere below keeps a page whose vm lacks
                    # that shape from crashing the loop (it just renders
                    # un-bolded). Factoring this into the per-page renderer
                    # contract is future chrome-factoring work, not task 3.
                    shown = _tail(vm.get("lines", []), len(body_lines))
                    out = [term.home + term.reverse(header_line) + term.normal]
                    for i, line in enumerate(body_lines):
                        pad = len(body_lines) - len(shown)
                        is_accent = i >= pad and shown[i - pad].get("style") == "accent"
                        styled = term.bold(line) if is_accent else line
                        out.append(term.move_xy(0, i + 1) + styled)
                    out.append(term.move_xy(0, term.height - 1)
                               + term.reverse(status_line) + term.normal)
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
