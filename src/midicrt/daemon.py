"""midicrtd — engine daemon entrypoint."""
import argparse
import asyncio
import contextlib
import logging
import signal

from midicrt import config as config_mod
from midicrt.engine.core import Engine
from midicrt.engine.server import ProtocolServer

_LOG = logging.getLogger("midicrtd")


def build(cfg, socket_path: str, use_midi: bool):
    engine = Engine(cfg)
    server = ProtocolServer(engine, socket_path)
    midi = None
    if use_midi:
        from midicrt.engine.midi_in import MidiInput  # lazy: needs rtmidi
        midi = MidiInput(cfg.midi_sources, engine.queue)
    return engine, server, midi


async def run(cfg, socket_path: str, use_midi: bool) -> None:
    engine, server, midi = build(cfg, socket_path, use_midi)
    await server.start()
    if midi:
        midi.start(asyncio.get_running_loop())
    _LOG.info("midicrtd up on %s (midi=%s)", socket_path, bool(midi))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine_task = asyncio.create_task(engine.run())
    await stop.wait()
    _LOG.info("shutting down")
    if midi:
        midi.stop()
    engine.stop()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(engine_task, timeout=2)
    await server.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="midicrtd")
    ap.add_argument("--socket", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-midi", action="store_true")
    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    asyncio.run(run(cfg, args.socket or cfg.socket_path, use_midi=not args.no_midi))


if __name__ == "__main__":
    main()
