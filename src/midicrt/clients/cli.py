"""midicrt — protocol client CLI (also the debugging tool)."""
import argparse
import json

from midicrt import config as config_mod
from midicrt.clients.base import ClientError, EngineClient


def request(socket_path: str, cmd: str, **kw) -> dict:
    client = EngineClient(socket_path)
    try:
        client.connect()
        resp = client.request(cmd, **kw)
    except ClientError as exc:
        raise SystemExit(f"midicrt: {exc}") from exc
    finally:
        client.close()
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
    ap.add_argument("--socket", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("describe")
    p_action = sub.add_parser("action")
    p_action.add_argument("name")
    p_action.add_argument("--arg", action="append", default=[])
    sub.add_parser("tui")
    args = ap.parse_args()

    socket_path = args.socket or config_mod.load(None).socket_path

    if args.cmd == "tui":
        from midicrt.clients.tui import run_tui  # lazy: needs blessed
        raise SystemExit(run_tui(socket_path))
    if args.cmd == "action":
        data = request(socket_path, "action", name=args.name, args=_parse_args(args.arg))
    else:
        data = request(socket_path, args.cmd)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
