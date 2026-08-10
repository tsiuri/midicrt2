"""midicrt — protocol client CLI (also the debugging tool)."""
import argparse
import json
import queue
import time

from midicrt import config as config_mod
from midicrt.clients.base import ClientError, EngineClient
from midicrt.engine import capture as capture_mod
from midicrt.engine import sessions as sessions_mod
from midicrt.engine.bindings import LEARN_TIMEOUT_S


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


# -- `midicrt bind ...` (Phase 4 Task 3, docs/phase4-notes.md) ---------------
#
# `list`/`remove`/`cancel` are plain one-shot request/response, same shape
# as the generic `action` subcommand above (just pre-filling the action
# name/args) -- `_bind_learn_cli` is the one exception: arming is a
# request/response, but the RESULT (`learn_bound`/`learn_cancelled`) only
# ever arrives later, as an EVENT, so it needs a persistent connection with
# a reader thread (mirrors `clients/tui.py::run_tui`'s own
# `start_reader()`-then-block pattern) rather than the fire-and-forget
# `request()` helper above.

def _format_learn_bound(data: dict) -> str:
    """Build the text `midicrt bind learn` prints for a `learn_bound`
    event's data payload (review fix: replace-on-relearn reporting,
    docs/phase4-notes.md task-3 follow-up). `data["replaced"]` is the
    (possibly empty) list of bindings `Engine._capture_learn` removed
    because their `match` was EXACTLY equal to the one just captured (see
    that method's own docstring) -- surfaced here as one "replaced ..."
    line per entry, ABOVE the new binding's own JSON, so an operator
    re-mapping an already-bound key/knob sees explicitly what got
    replaced instead of silently losing track of the old binding. No
    "replaced" line at all when the list is empty (the common case: a
    genuinely NEW match)."""
    lines = [f"midicrt: replaced binding {r.get('id')} ({r.get('action')})"
             for r in data.get("replaced", [])]
    lines.append(json.dumps(data["binding"], indent=2))
    return "\n".join(lines)


def _bind_learn_cli(socket_path: str, action_name: str, mode: str, arg_pairs: list[str],
                    timeout: float, range_spec: str | None = None) -> None:
    """`midicrt bind learn <action>`: arms the engine's single learn slot
    (`bind.learn`, engine/core.py) then blocks on the SAME connection's
    reader thread for the resulting `learn_bound`/`learn_cancelled` event.
    No `subscribe` call is needed to see either event -- `ProtocolServer.
    _on_engine_message` broadcasts every event to every GREETED client
    unconditionally, regardless of topic subscriptions (see engine/
    server.py); only snapshots are subscription-gated.

    `timeout` defaults to `LEARN_TIMEOUT_S` (the engine's own arm window)
    plus a slack margin for wire round-trip/scheduling latency, so a
    client waiting under normal conditions is never the one to time out
    first -- the engine's own `learn_cancelled {reason: "timeout"}` should
    always win that race and produce a readable message here instead of a
    bare "timed out waiting" with no explanation.

    `range_spec` (Phase 5 Task 3, docs/phase5-notes.md cheap-wins bundle):
    `--range lo,hi`'s raw string, or `None` when the flag was never given
    -- `None` is deliberately NOT sent as `{"range": None}` (the engine's
    `range: str` schema arg would reject a non-string via `_COERCERS`);
    omitting the wire key entirely lets `ActionRegistry.dispatch`'s own
    `defaults={"range": ""}` (`Engine.__init__`'s `bind.learn`
    registration) supply the exact same "not supplied" sentinel
    `Engine._bind_learn` already checks for."""
    client = EngineClient(socket_path)
    try:
        client.connect()
    except ClientError as exc:
        raise SystemExit(f"midicrt: {exc}") from exc
    inbox = client.start_reader()
    try:
        # `.action()` itself raises `ClientError` for a rejected arm (see
        # `EngineClient.request`) -- its response has no further data this
        # caller needs beyond "the arm succeeded", so it's discarded here.
        learn_args = {"action": action_name, "mode": mode, "args": _parse_args(arg_pairs)}
        if range_spec is not None:
            learn_args["range"] = range_spec
        client.action("bind.learn", learn_args)
    except ClientError as exc:
        client.close()
        raise SystemExit(f"midicrt: {exc}") from exc
    print(f"midicrt: armed -- waiting for MIDI (timeout {timeout:.0f}s)...")
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("midicrt: timed out waiting for a learn result")
            try:
                msg = inbox.get(timeout=remaining)
            except queue.Empty:
                raise SystemExit("midicrt: timed out waiting for a learn result") from None
            if msg is None:
                raise SystemExit("midicrt: engine connection lost")
            if msg.get("kind") != "event":
                continue
            name = msg.get("name")
            if name == "learn_bound":
                print(_format_learn_bound(msg["data"]))
                return
            if name == "learn_cancelled":
                reason = msg.get("data", {}).get("reason", "unknown")
                raise SystemExit(f"midicrt: learn cancelled ({reason})")
            # Any other event (page_changed, keymap_changed, alert, ...)
            # arriving while armed is unrelated -- keep waiting.
    finally:
        client.close()


# -- `midicrt replay <file>` (Phase 5 Task 2, docs/phase5-notes.md) ---------
#
# The one subcommand here that talks to NO daemon at all -- everything else
# in this file is a thin client over a real `midicrtd` socket (`request()`/
# `EngineClient` above); `replay` instead builds its own throwaway, offline
# `Engine` in-process (`engine/replay.py::replay_session`) and streams a
# captured session file through it. `main()` special-cases `args.cmd ==
# "replay"` BEFORE the `socket_path = ...` line the rest of this file
# shares, mirroring how `"tui"`/`"bind"` are already special-cased below --
# unlike those two, replay needs no socket at all, so there is nothing for
# it to reuse from that line anyway.

def _positive_speed(value: str) -> float:
    """`--speed` argparse `type=` -- review round fix wave (Minor): a
    non-positive speed used to reach `engine.replay.stream_session`'s own
    pacing division unchecked, where `max(speed, 1e-9)` turned it into an
    effectively-infinite sleep between every replayed event (a genuinely
    hung process, reproduced live) instead of a clean error. Raising
    `argparse.ArgumentTypeError` here gives the normal argparse usage-error
    treatment (readable message, exit code 2) before a bad value ever
    reaches the engine at all -- `stream_session` ALSO validates this
    itself (`ValueError`) as a defense-in-depth backstop for any other
    caller that isn't going through this CLI."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"--speed must be > 0, got {value!r}")
    return parsed


def _handle_replay(args) -> None:
    from midicrt.engine.replay import replay_session  # lazy: pulls in engine.core's roster
    try:
        summary = replay_session(args.file, speed=args.speed, instant=args.instant)
    except OSError as exc:
        raise SystemExit(f"midicrt: {exc}") from exc
    print(json.dumps(summary, indent=2))


def _handle_bind(socket_path: str, args) -> None:
    if args.bind_cmd == "learn":
        _bind_learn_cli(socket_path, args.action, args.mode, args.arg, args.timeout, args.range)
        return
    if args.bind_cmd == "list":
        data = request(socket_path, "action", name="bind.list", args={})
    elif args.bind_cmd == "remove":
        data = request(socket_path, "action", name="bind.remove", args={"id": args.id})
    elif args.bind_cmd == "cancel":
        data = request(socket_path, "action", name="bind.cancel", args={})
    print(json.dumps(data, indent=2))


# -- `midicrt sessions ...` (Phase 9 Task 6, capture editor) -----------------
#
# Operates directly on the event-sourced capture STORE on disk
# (engine/sessions.py's pure functions) -- unlike every other subcommand in
# this file, `sessions` needs no running daemon to do its actual work (a
# corrupted index.json must be repairable even while `midicrtd` is down),
# mirroring `replay`'s own "no socket needed" shape one section up. The ONE
# place a daemon matters is liveness: `trim`/`delete` must never touch the
# session the daemon is actively appending to right now (task-6-brief.md's
# binding constraint) -- `_live_recording_session_id` below is a BEST-EFFORT
# query over the SAME socket protocol every other subcommand already uses
# (`capture.status`, registered since Phase 5 Task 1), not a hard
# requirement: an unreachable daemon means "nothing can be live for this
# store right now" (see that function's own docstring), not a fatal error.

def _live_recording_session_id(socket_path: str) -> str | None:
    """Best-effort liveness probe: connects to `socket_path` and asks
    `capture.status` (the same action `capture.status` CLI/web callers
    already use) which session, if any, the daemon is CURRENTLY recording.
    Returns `None` -- "nothing is live for this store" -- whenever:

      - no daemon is listening at `socket_path` at all (not running, or a
        stale/nonexistent socket path -- `EngineClient.connect()` wraps
        every such `OSError` in `ClientError`, see clients/base.py), or
      - a daemon IS reachable but isn't currently recording anything.

    Both cases are treated identically and non-fatally: a session file can
    only ever be open for append by a RUNNING daemon process (see engine/
    capture.py's own module docstring -- `CaptureSink` is the sole writer,
    constructed once per `Engine`), so "no daemon reachable at this
    socket" is itself a fully honest answer to "is anything live", not a
    degraded one -- there is nothing this function silently guesses at.
    A daemon reachable at `socket_path` but pointed at a DIFFERENT
    `capture_dir` than the one `sessions` is operating on will simply never
    match any id this caller checks against -- also harmless."""
    client = EngineClient(socket_path)
    try:
        client.connect()
        resp = client.action("capture.status")
    except ClientError:
        return None
    finally:
        client.close()
    data = resp.get("data", {})
    if not data.get("recording"):
        return None
    return data.get("session_id")


def _handle_sessions(args) -> None:
    cfg = config_mod.load(args.config)
    capture_dir = capture_mod.resolve_capture_dir(cfg.capture_dir)
    socket_path = args.socket or cfg.socket_path
    live_id = _live_recording_session_id(socket_path)
    try:
        if args.sessions_cmd == "list":
            data = sessions_mod.list_sessions(capture_dir, live_session_id=live_id)
        elif args.sessions_cmd == "show":
            data = sessions_mod.show_session(capture_dir, args.id, live_session_id=live_id)
        elif args.sessions_cmd == "trim":
            data = sessions_mod.trim_session(capture_dir, args.id, args.from_s, args.to_s,
                                             live_session_id=live_id)
        elif args.sessions_cmd == "repair-index":
            data = sessions_mod.repair_index(capture_dir, live_session_id=live_id)
        elif args.sessions_cmd == "delete":
            data = sessions_mod.delete_session(capture_dir, args.id, live_session_id=live_id)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"midicrt: {exc}") from exc
    print(json.dumps(data, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(prog="midicrt")
    ap.add_argument("--socket", default=None)
    # Phase 9 Task 6 (capture editor): `sessions` is the first subcommand
    # that needs to know `capture_dir` WITHOUT necessarily talking to a
    # daemon at all -- see `_handle_sessions`'s own comment. `midicrtd`
    # (daemon.py) and `midicrt-web` (clients/web/app.py) both already have
    # their own `--config`; this mirrors that exact flag name/semantics
    # (`config_mod.load(args.config)`) rather than inventing a
    # `sessions`-only flag. Threaded into the pre-existing
    # `socket_path = ...` line below too (previously always
    # `config_mod.load(None)`) so `--config` consistently affects every
    # subcommand's socket-path resolution, not just `sessions`'s.
    ap.add_argument("--config", default=None,
                    help="Path to config.toml (default: ~/.config/midicrt/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("describe")
    p_action = sub.add_parser("action")
    p_action.add_argument("name")
    p_action.add_argument("--arg", action="append", default=[])
    sub.add_parser("tui")

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("file")
    p_replay_speed = p_replay.add_mutually_exclusive_group()
    p_replay_speed.add_argument("--speed", type=_positive_speed, default=1.0,
                                help="Playback speed multiplier (default: 1.0x real-time)")
    p_replay_speed.add_argument("--instant", action="store_true",
                                help="Replay as fast as possible, ignoring original timing")

    p_bind = sub.add_parser("bind")
    bind_sub = p_bind.add_subparsers(dest="bind_cmd", required=True)
    bind_sub.add_parser("list")
    p_bind_remove = bind_sub.add_parser("remove")
    p_bind_remove.add_argument("id")
    bind_sub.add_parser("cancel")
    p_bind_learn = bind_sub.add_parser("learn")
    p_bind_learn.add_argument("action")
    p_bind_learn.add_argument("--mode", default="trigger", choices=["trigger", "continuous"])
    p_bind_learn.add_argument("--arg", action="append", default=[])
    p_bind_learn.add_argument("--timeout", type=float, default=LEARN_TIMEOUT_S + 5.0)
    p_bind_learn.add_argument(
        "--range", default=None, metavar="LO,HI",
        help="continuous mode only: lerp target range (default: 0.0,1.0)")

    p_sessions = sub.add_parser("sessions")
    sessions_sub = p_sessions.add_subparsers(dest="sessions_cmd", required=True)
    sessions_sub.add_parser("list")
    p_sessions_show = sessions_sub.add_parser("show")
    p_sessions_show.add_argument("id")
    p_sessions_trim = sessions_sub.add_parser("trim")
    p_sessions_trim.add_argument("id")
    p_sessions_trim.add_argument(
        "--from", dest="from_s", type=float, required=True, metavar="SECONDS",
        help="session-relative start offset in seconds (0.0 == session start)")
    p_sessions_trim.add_argument(
        "--to", dest="to_s", type=float, required=True, metavar="SECONDS",
        help="session-relative end offset in seconds")
    sessions_sub.add_parser("repair-index")
    p_sessions_delete = sessions_sub.add_parser("delete")
    p_sessions_delete.add_argument("id")

    args = ap.parse_args()

    if args.cmd == "replay":
        _handle_replay(args)
        return
    if args.cmd == "sessions":
        _handle_sessions(args)
        return

    socket_path = args.socket or config_mod.load(args.config).socket_path

    if args.cmd == "tui":
        from midicrt.clients.tui import run_tui  # lazy: needs blessed
        raise SystemExit(run_tui(socket_path))
    if args.cmd == "bind":
        _handle_bind(socket_path, args)
        return
    if args.cmd == "action":
        data = request(socket_path, "action", name=args.name, args=_parse_args(args.arg))
    else:
        data = request(socket_path, args.cmd)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
