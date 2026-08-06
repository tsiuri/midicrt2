"""midicrt — protocol client CLI (also the debugging tool)."""
import argparse
import json
import socket

from midicrt import proto


def request(socket_path: str, cmd: str, **kw) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        raise SystemExit(f"midicrt: cannot connect to {socket_path}: {exc}") from exc
    with sock, sock.makefile("rwb") as f:
        json.loads(f.readline())                                    # server hello
        f.write(proto.encode({"id": 1, "cmd": "hello", "proto_version": proto.PROTO_VERSION}))
        f.flush()
        if not json.loads(f.readline()).get("ok"):
            raise SystemExit("midicrt: protocol version rejected by engine")
        f.write(proto.encode({"id": 2, "cmd": cmd, **kw}))
        f.flush()
        while True:
            resp = json.loads(f.readline())
            if resp.get("id") == 2:
                break
    if not resp.get("ok"):
        raise SystemExit(f"midicrt: {resp.get('error', 'request failed')}")
    return resp["data"]


def _parse_args(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"midicrt: --arg wants k=v, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="midicrt")
    ap.add_argument("--socket", default="/run/midicrt/ctl.sock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("describe")
    p_action = sub.add_parser("action")
    p_action.add_argument("name")
    p_action.add_argument("--arg", action="append", default=[])
    sub.add_parser("tui")
    args = ap.parse_args()

    if args.cmd == "tui":
        from midicrt.clients.tui import run_tui  # lazy: needs blessed
        raise SystemExit(run_tui(args.socket))
    if args.cmd == "action":
        data = request(args.socket, "action", name=args.name, args=_parse_args(args.arg))
    else:
        data = request(args.socket, args.cmd)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
