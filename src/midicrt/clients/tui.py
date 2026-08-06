"""Minimal TUI client: renders the eventlog page, sends actions."""
import json
import queue

from midicrt.clients.base import ClientError, EngineClient


def _fit(text: str, width: int) -> str:
    return text[:width].ljust(width)


def _tail(lines: list, body_h: int) -> list:
    """Slice the last body_h items. Guards the height<=0 case where a plain
    `lines[-0:]` slice would (surprisingly) return everything instead of []."""
    return lines[-body_h:] if body_h > 0 else []


def render_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  ({vm['count']} events)  [c]lear [q]uit"
    body_h = height - 1
    tail = _tail(vm["lines"], body_h)
    body = [_fit(" " + ln["text"], width) for ln in tail]
    while len(body) < body_h:
        body.insert(0, " " * width)
    return [_fit(header, width)] + body


def run_tui(socket_path: str) -> int:
    import blessed

    client = EngineClient(socket_path)
    try:
        client.connect()
        client.subscribe(["page.eventlog"], max_rate=10.0)
    except ClientError as exc:
        print(f"midicrt tui: {exc}")
        client.close()
        return 1

    inbox = client.start_reader()
    term = blessed.Terminal()
    vm = {"title": "EVENT LOG", "count": 0, "lines": []}
    lost = False
    try:
        with term.fullscreen(), term.cbreak(), term.hidden_cursor():
            dirty = True
            while True:
                try:
                    while True:
                        msg = inbox.get_nowait()
                        if msg is None:
                            lost = True
                            return 1
                        if msg.get("kind") == "snapshot" and msg["topic"] == "page.eventlog":
                            vm, dirty = msg["data"], True
                except queue.Empty:
                    pass
                if dirty:
                    lines = render_lines(vm, term.width, term.height)
                    shown = _tail(vm["lines"], term.height - 1)
                    out = [term.home + term.reverse(lines[0]) + term.normal]
                    for i, line in enumerate(lines[1:]):
                        pad = len(lines[1:]) - len(shown)
                        styled = term.bold(line) if (
                            i >= pad and shown[i - pad]["style"] == "accent") else line
                        out.append(term.move_xy(0, i + 1) + styled)
                    print("".join(out), end="", flush=True)
                    dirty = False
                key = term.inkey(timeout=0.05)
                if key == "q":
                    return 0
                if key == "c":
                    try:
                        client.action("eventlog.clear")
                    except ClientError:
                        lost = True
                        return 1
    finally:
        client.close()
        if lost:
            print("midicrt tui: engine connection lost")


def main_debug(socket_path="/tmp/midicrt-dev.sock") -> None:  # manual smoke helper
    print(json.dumps({"socket": socket_path}))
