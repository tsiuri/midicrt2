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


def build(cfg, socket_path: str, use_midi: bool, config_path: str | None = None):
    # `config_path` (Phase 4 Task 1, docs/phase4-notes.md): threaded straight
    # from `main()`'s own `args.config` (the SAME path `config_mod.load()`
    # already used to build `cfg` above it in the call chain) so
    # `config.reload`'s config.toml half re-reads the file this daemon was
    # actually started with -- not always the hardcoded default path when a
    # custom `--config` was given.
    engine = Engine(cfg, config_path=config_path)
    server = ProtocolServer(engine, socket_path)
    midi = None
    if use_midi:
        from midicrt.engine.midi_in import MidiInput  # lazy: needs rtmidi
        # Phase-3 task 12 fix (Critical, live-reproduced self-subscription
        # feedback loop): never let MidiInput's own wildcard scan open the
        # engine's own MidiOutput port as an input -- see engine/midi_in.py's
        # module docstring for the full incident writeup.
        midi = MidiInput(cfg.midi_sources, engine.queue,
                         exclude_names=(engine.midi_output_port_name,))
        # Phase 5 Task 3 (bind.list port_present, docs/phase5-notes.md
        # cheap-wins bundle): the ONE production wiring site for
        # `Engine.set_open_ports_provider` -- see that method's own
        # docstring for the full engine<->MidiInput seam contract. Reads
        # `midi.open_ports` LIVE on every call (a bound property access,
        # not a value captured here) so a port vanishing/appearing between
        # `bind.list` calls is always reflected on the next one.
        engine.set_open_ports_provider(lambda: midi.open_ports)
    return engine, server, midi


async def run(cfg, socket_path: str, use_midi: bool, use_audio: bool = True,
              config_path: str | None = None) -> None:
    engine, server, midi = build(cfg, socket_path, use_midi, config_path)
    await server.start()
    if midi:
        midi.start(asyncio.get_running_loop())
    # Phase-3 task 8: start the spectrum page's audio-capture thread the
    # same way midi.start() is wired above -- constructed unconditionally
    # by Engine (it's in the default roster, config.py), started here only
    # when both `use_audio` (the `--no-audio` opt-out, mirroring
    # `--no-midi`) is true AND the page actually exists in this build's
    # roster (a `config.pages` override could drop "spectrum" entirely,
    # same guard shape as engine/core.py's pianoroll-actions registration).
    spectrum_page = engine.pages.get("spectrum")
    audio_active = use_audio and spectrum_page is not None
    if audio_active:
        spectrum_page.start_capture()
    _LOG.info("midicrtd up on %s (midi=%s, audio=%s)", socket_path, bool(midi), audio_active)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine_task = asyncio.create_task(engine.run())
    await stop.wait()
    _LOG.info("shutting down")
    if midi:
        midi.stop()
    if audio_active:
        spectrum_page.stop_capture()
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
    ap.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    asyncio.run(run(cfg, args.socket or cfg.socket_path,
                    use_midi=not args.no_midi, use_audio=not args.no_audio,
                    config_path=args.config))


if __name__ == "__main__":
    main()
