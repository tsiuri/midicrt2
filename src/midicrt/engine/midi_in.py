"""MIDI input: watch ALSA seq ports matching config patterns, feed the engine queue.

Replaces v1's shell retry-loop: reconnects appear automatically on the next poll.

Clock aggregation (Phase 3 Task 3)
-----------------------------------
Raw MIDI clock arrives at 24 pulses per quarter note (ppqn) -- ~50-100
msgs/sec across the tempo range this app cares about. Queuing one
`MidiEvent` per pulse would flood the engine queue and every page's
`handle()` (including the eventlog page, which must NOT show clock spam)
50-100 times a second for no visual benefit. Clock stays out of the queue
as an individual event -- `translate()` still returns None for it via
`_IGNORED_TYPES`, unchanged -- and instead `MidiInput` counts raw pulses
itself and emits ONE aggregated `MidiEvent(type="clock_tick")` every 24 of
them (one beat). This is the "midi_in counts clocks and emits an
aggregated event at most every 24 clocks" option from the task-3 brief,
chosen over a separate analyzer callback path for simplicity: analyzers
already consume `MidiEvent`s off the same queue as pages, so clock_tick
needs no new plumbing.

Each `clock_tick` carries `data1=24` (the batch size) and
`clock_batch_start`: the timestamp of the PREVIOUS 24-pulse boundary (this
batch's own last pulse becomes `ts`). The span `ts - clock_batch_start` is
EXACTLY 24 raw clock intervals -- one quarter note -- letting
`analyzers/transport.py` derive bpm with no averaging of its own (see that
module's docstring). `clock_batch_start` is None for the first batch after
start-up, and is reset (cleared to None) on every "start"/"stop"/"continue"
message (which still pass through normally as their own MidiEvents,
translate() never ignored them) -- without this reset, the first batch
completed after a stop-then-resume gap would measure a bpm across the
silent gap instead of across real beats.

`_clock_count` (the raw pulse tally within the current batch) is reset to
0 ONLY on "start" -- NOT on "stop"/"continue". MIDI's "continue" message
resumes the clock from the EXACT tick it was stopped at, not from the
start of a beat: a stop at pulse 10-of-24 followed by continue means only
14 MORE pulses complete that beat, not a fresh 24. Zeroing `_clock_count`
on stop/continue (an earlier version of this module did) silently
discarded that in-flight tally, delaying the next `clock_tick` by up to a
full beat and permanently offsetting `analyzers/transport.py`'s bar/beat
count from the real transport position (that analyzer deliberately does
NOT reset its own beat counter on continue, matching v1 -- so a phase
error introduced here would never self-correct). Only "start" truly
restarts the beat grid from tick 0, so only "start" zeroes the pulse
tally.
"""
import fnmatch
import logging
import threading
import time

from midicrt.engine.core import MidiEvent

_LOG = logging.getLogger(__name__)
# mido's real type string is "active_sensing" (verified via
# `mido.Message("active_sensing").type`) -- "activesensing" (no underscore)
# never matched, so active-sensing messages were passing translate()
# unfiltered until this fix.
_IGNORED_TYPES = {"clock", "active_sensing"}
_CLOCK_BATCH_SIZE = 24  # standard MIDI clock: 24 pulses per quarter note
_CLOCK_FULL_RESET_TYPES = {"start"}            # zero the in-batch pulse tally too
_CLOCK_BOUNDARY_RESET_TYPES = {"start", "stop", "continue"}  # always clear the bpm reference


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
    elif msg.type == "program_change":
        # Phase-3 task 10 (analyzers/img2txtviz.py) finding: this branch
        # previously fell through to the generic `elif channel is not
        # None` case below, which sets ONLY `summary` -- `data1`/`data2`
        # stayed None for every program_change event, so no analyzer could
        # ever recover the actual program number (v1's img2txtviz.py reads
        # `msg.program` directly; nothing in v2 carried it). Minimal,
        # additive fix: populate `data1` with the program number, mirroring
        # the note/cc branches above -- `data2` stays None (program_change
        # has no second value). No existing caller relied on data1 being
        # None here (grepped: no prior code branches on `type ==
        # "program_change"` at all before this task).
        data1 = msg.program
        summary = f"program_change ch{channel + 1} p{msg.program}"
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
        self._clock_count = 0
        self._clock_batch_start: float | None = None

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
        if msg.type == "clock":
            self._on_clock(source)
            return
        if msg.type in _CLOCK_BOUNDARY_RESET_TYPES:
            self._clock_batch_start = None
        if msg.type in _CLOCK_FULL_RESET_TYPES:
            self._clock_count = 0
        ev = translate(msg, source, time.time())
        if ev is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, ev)

    def _on_clock(self, source: str) -> None:
        """Count one raw clock pulse; emit an aggregated `clock_tick`
        MidiEvent every `_CLOCK_BATCH_SIZE` pulses -- see module docstring."""
        now = time.time()
        self._clock_count += 1
        if self._clock_count < _CLOCK_BATCH_SIZE:
            return
        ev = MidiEvent(ts=now, source=source, type="clock_tick", channel=None,
                       data1=_CLOCK_BATCH_SIZE, data2=None, summary="clock_tick",
                       clock_batch_start=self._clock_batch_start)
        self._clock_batch_start = now
        self._clock_count = 0
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
