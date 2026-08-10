"""midicrtd — engine daemon entrypoint."""
import argparse
import asyncio
import contextlib
import logging
import os
import signal

from midicrt import config as config_mod
from midicrt.engine.core import Engine
from midicrt.engine.server import ProtocolServer

_LOG = logging.getLogger("midicrtd")


class ShutdownWatchdog:
    """Delivers SIGTERM/SIGINT to an `asyncio.Event` through a PRIVATE
    self-pipe, deliberately NOT `loop.add_signal_handler` (Phase 9 Task 2b,
    docs/... task-2b-report.md -- the 2026-08-10 production shutdown-hang
    incident, `systemctl stop midicrtd` needing SIGKILL after `TimeoutStopSec`
    twice in one night).

    Root cause (proven by reproduction, see the task report): asyncio's
    `loop.add_signal_handler` -- the ONLY thing this daemon used to use --
    makes `signal.set_wakeup_fd()` point at the loop's own internal self-pipe
    (a `socket.socketpair()`, `loop._csock`), the EXACT SAME fd every
    `loop.call_soon_threadsafe()` call (from ANY thread) also writes a
    wakeup byte to -- that's how `MidiInput._enqueue` gets a queued MIDI
    event from the RtMidi callback thread over to the loop thread. Under a
    sustained callback-thread flood (an ALSA input-error storm) combined
    with ANY stretch where the loop thread itself isn't back at
    `epoll_wait()` (e.g. `Engine.run()`'s own per-tick burst-drain loop
    processing a large backlog synchronously -- see `core.py`'s
    `_MAX_BURST_PER_TICK` for the matching other half of this fix), that
    shared self-pipe's kernel send buffer (212992 bytes on this Pi,
    measured) can genuinely saturate. Once full, ANY write to it --
    including the raw OS signal trampoline's own wakeup-byte write for a
    real SIGTERM -- gets `EWOULDBLOCK` and is SILENTLY DROPPED (CPython
    prints "Exception ignored when trying to write to the signal wakeup
    fd" from `asyncio/unix_events.py`'s `_sighandler_noop` and moves on --
    exactly the incident's own log line). Nothing retries it. Since
    `stop.set()` was only ever wired to fire via that exact dropped byte
    being read back out of the self-pipe, the SIGTERM vanishes completely
    and `run()` never returns -- systemd's `stop-sigterm` state times out
    and escalates to SIGKILL.

    This class sidesteps the shared pipe entirely: `signal.signal()` (POSIX,
    raw -- does NOT touch `set_wakeup_fd`) registers a handler that CPython
    always runs on the main thread at the next bytecode safepoint, no self-
    pipe write from the OS signal trampoline involved at all. That handler
    does the self-pipe trick itself, but against a dedicated `os.pipe()`
    that NOTHING else on this process ever writes to -- a MIDI-flood-driven
    `call_soon_threadsafe` storm, however large, cannot ever contend for
    this fd's buffer, so the one wakeup byte this pipe ever needs to carry
    can never be crowded out."""

    def __init__(self, loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event,
                sigs: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)) -> None:
        self._loop = loop
        self._stop_event = stop_event
        self._sigs = tuple(sigs)
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_blocking(self._write_fd, False)
        self._prev_handlers: dict[signal.Signals, object] = {}
        for sig in self._sigs:
            self._prev_handlers[sig] = signal.signal(sig, self._on_signal)
        loop.add_reader(self._read_fd, self._on_pipe_readable)

    def _on_signal(self, signum, frame) -> None:
        # Runs on the main thread at the next bytecode safepoint (CPython's
        # own guarantee for `signal.signal()`-registered handlers) -- NOT
        # inside the raw OS signal trampoline, so an ordinary blocking
        # `os.write` is safe here (the textbook self-pipe trick, just
        # against a fd nothing else can ever fill). One pending byte is all
        # `_on_pipe_readable` needs, so an already-full (i.e. one byte
        # already pending) pipe is a harmless no-op, never a lost signal.
        try:
            os.write(self._write_fd, b"\x00")
        except BlockingIOError:
            pass

    def _on_pipe_readable(self) -> None:
        with contextlib.suppress(BlockingIOError):
            os.read(self._read_fd, 4096)
        self._stop_event.set()

    def close(self) -> None:
        self._loop.remove_reader(self._read_fd)
        for sig, prev in self._prev_handlers.items():
            signal.signal(sig, prev)
        os.close(self._read_fd)
        os.close(self._write_fd)


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
        # Phase 9 Task 1 (device-identity bindings): sibling wiring to the
        # line right above -- see `Engine.set_open_device_ids_provider`'s
        # own docstring for the full contract. Reads `midi.open_device_ids`
        # LIVE on every call, same "not a snapshot" property as open_ports.
        engine.set_open_device_ids_provider(lambda: midi.open_device_ids)
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
    # Phase 9 Task 3: identical wiring for the tuner page's OWN independent
    # audio-capture thread -- see pages/tuner.py's module docstring for why
    # this is a second, separate `AudioCapture` rather than sharing
    # spectrum's (a disclosed architecture decision, not an oversight).
    # Same `--no-audio` opt-out covers both -- there is one physical audio
    # input either page might be reading from, so one flag stops both.
    tuner_page = engine.pages.get("tuner")
    tuner_audio_active = use_audio and tuner_page is not None
    if tuner_audio_active:
        tuner_page.start_capture()
    _LOG.info("midicrtd up on %s (midi=%s, audio=%s, tuner_audio=%s)",
             socket_path, bool(midi), audio_active, tuner_audio_active)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Phase 9 Task 2b (shutdown-hang fix): NOT `loop.add_signal_handler`
    # anymore -- see `ShutdownWatchdog`'s own docstring for the full
    # incident this replaces it for (a saturated, SHARED self-pipe could
    # silently drop SIGTERM under an ALSA-error-storm-driven
    # call_soon_threadsafe flood, hanging shutdown until SIGKILL).
    watchdog = ShutdownWatchdog(loop, stop)
    engine_task = asyncio.create_task(engine.run())
    await stop.wait()
    _LOG.info("shutting down")
    if midi:
        midi.stop()
    if audio_active:
        spectrum_page.stop_capture()
    if tuner_audio_active:
        tuner_page.stop_capture()
    engine.stop()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(engine_task, timeout=2)
    await server.close()
    watchdog.close()


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
