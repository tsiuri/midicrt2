"""Minimal TUI client: renders the eventlog page, sends actions."""
import json
import queue
import socket
import threading

from midicrt import proto


def _fit(text: str, width: int) -> str:
    return text[:width].ljust(width)


def render_lines(vm: dict, width: int, height: int) -> list[str]:
    header = f"{vm['title']}  ({vm['count']} events)  [c]lear [q]uit"
    body_h = height - 1
    tail = vm["lines"][-body_h:] if body_h > 0 else []
    body = [_fit(" " + ln["text"], width) for ln in tail]
    while len(body) < body_h:
        body.insert(0, " " * width)
    return [_fit(header, width)] + body


def run_tui(socket_path: str) -> int:
    import blessed

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
    except OSError as exc:
        print(f"midicrt tui: cannot connect to {socket_path}: {exc}")
        return 1

    inbox: queue.Queue = queue.Queue()
    wfile = sock.makefile("wb")

    def send(msg: dict) -> None:
        wfile.write(proto.encode(msg))
        wfile.flush()

    def reader() -> None:
        dec = proto.LineDecoder()
        while data := sock.recv(65536):
            for msg in dec.feed(data):
                inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    send({"id": 1, "cmd": "hello", "proto_version": proto.PROTO_VERSION})
    send({"id": 2, "cmd": "subscribe", "topics": ["page.eventlog"], "max_rate": 10.0})

    term = blessed.Terminal()
    vm = {"title": "EVENT LOG", "count": 0, "lines": []}
    next_id = 3
    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        dirty = True
        while True:
            try:
                while True:
                    msg = inbox.get_nowait()
                    if msg is None:
                        return 1  # server went away
                    if msg.get("kind") == "snapshot" and msg["topic"] == "page.eventlog":
                        vm, dirty = msg["data"], True
            except queue.Empty:
                pass
            if dirty:
                lines = render_lines(vm, term.width, term.height)
                shown = vm["lines"][-(term.height - 1):]
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
                send({"id": next_id, "cmd": "action", "name": "eventlog.clear", "args": {}})
                next_id += 1


def main_debug(socket_path="/tmp/midicrt-dev.sock") -> None:  # manual smoke helper
    print(json.dumps({"socket": socket_path}))
