"""MIDI input: watch ALSA seq ports matching config patterns, feed the engine queue.

Replaces v1's shell retry-loop: reconnects appear automatically on the next poll.
"""
import fnmatch
import logging
import threading
import time

from midicrt.engine.core import MidiEvent

_LOG = logging.getLogger(__name__)
_IGNORED_TYPES = {"clock", "activesensing"}


def matches(port_name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(port_name, p) for p in patterns)


def translate(msg, source: str, ts: float) -> MidiEvent | None:
    if msg.type in _IGNORED_TYPES:
        return None
    channel = getattr(msg, "channel", None)
    data1 = data2 = None
    if msg.type in ("note_on", "note_off"):
        data1, data2 = msg.note, msg.velocity
        summary = f"{msg.type} ch{channel + 1} n{msg.note} v{msg.velocity}"
    elif msg.type == "control_change":
        data1, data2 = msg.control, msg.value
        summary = f"control_change ch{channel + 1} cc{msg.control} v{msg.value}"
    elif channel is not None:
        summary = f"{msg.type} ch{channel + 1}"
    else:
        summary = msg.type
    return MidiEvent(ts=ts, source=source, type=msg.type,
                     channel=channel, data1=data1, data2=data2, summary=summary)


class MidiInput:
    def __init__(self, patterns, queue, poll_interval=3.0, backend=None):
        if backend is None:
            import mido as backend
        self._backend = backend
        self._patterns = list(patterns)
        self._queue = queue
        self._poll = poll_interval
        self._ports = {}          # name -> port object
        self._thread = None
        self._running = False
        self._loop = None

    @property
    def open_ports(self) -> list[str]:
        return sorted(self._ports)

    def start(self, loop) -> None:
        self._loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._watch, name="midi-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        for port in self._ports.values():
            port.close()
        self._ports.clear()

    def _enqueue(self, msg, source: str) -> None:
        ev = translate(msg, source, time.time())
        if ev is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, ev)

    def _watch(self) -> None:
        while self._running:
            try:
                available = set(self._backend.get_input_names())
            except Exception as exc:  # noqa: BLE001 — backend/driver errors must not kill the poll loop
                _LOG.warning("port scan failed: %s", exc)
                available = set()
            for name in sorted(available):
                if name not in self._ports and matches(name, self._patterns):
                    try:
                        self._ports[name] = self._backend.open_input(
                            name, callback=lambda m, n=name: self._enqueue(m, n))
                        _LOG.info("opened MIDI input: %s", name)
                    except Exception as exc:  # noqa: BLE001 — same: one bad port must not kill the loop
                        _LOG.warning("open failed for %s: %s", name, exc)
            for name in list(self._ports):
                if name not in available:
                    _LOG.info("MIDI input vanished: %s", name)
                    self._ports.pop(name).close()
            time.sleep(self._poll)
