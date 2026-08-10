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

Self-subscription exclusion (phase-3 task 12 fix -- Critical, live-
reproduced)
---------------------------------------------------------------------------
`engine/midi_out.py::MidiOutput` lazily opens a virtual ALSA output port
(default name "midicrt2 Output") the first time the engine sends anything
(a Send Notes trigger, a SysEx reply). ALSA/RtMidi virtual ports are
bidirectionally discoverable -- creating one as an OUTPUT also creates a
name `get_input_names()` reports (observed live, exact string: `"RtMidiOut
Client:midicrt2 Output 142:0"`) -- so `MidiInput`'s default wildcard
pattern (`config.midi_sources = ["*"]`) would happily match and OPEN the
daemon's own output as an input source, creating a true self-subscription
feedback loop. Confirmed live via `aconnect` (the daemon's own reader
thread held "Connected From" its own output port) with two reproduced
consequences: (a) any reply-eliciting SysEx command -- even the standard
`CMD_CAPABILITIES` handshake -- loops FOREVER, since `engine/sysex.py::
build_reply()`'s own marker byte makes every reply itself re-parse as a
brand-new valid command with no origin check anywhere in the dispatch
path (sustained ~175 events/sec for 20+ minutes until a manual
`systemctl restart`); (b) every real Send Notes trigger echoes back as a
PHANTOM external note-on, contaminating `voices`/`harmony`/`stucknotes`/
`eventlog` with fake incoming MIDI that never actually arrived from
anywhere real.

Fixed here as LAYER 1 of a two-layer defense (layer 2:
`engine/core.py::Engine._handle`'s own unconditional source filter, belt-
and-suspenders against any FUTURE code path that opens a port without
going through this scan, or a bug here): `MidiInput.__init__` takes an
`exclude_names` collection (daemon.py wires in `engine.midi_output_port_
name`, the engine's OWN configured output-port name); `_watch()`'s scan
loop never opens a port whose enumerated name CONTAINS one of those
names. Substring containment, not exact match -- the real observed form
above wraps the configured name in backend-specific client/port framing
that an exact-match check would silently miss entirely, which is exactly
how this bug slipped through code review and unit tests the first time:
nothing had ever exercised what `get_input_names()` ACTUALLY returns for
a self-opened virtual port on real hardware.

Device identity (Phase 9 Task 1, device-identity bindings)
------------------------------------------------------------
Every port this class opens also gets a resolved, stable `device_id`
(`engine/midi_identity.py::IdentityResolver` -- see that module's own
docstring for the full `usb:<vendor>:<product>[:<serial>]` / `virt:<name>`
resolution ladder) -- resolved right when `_watch()` opens the port, and
cached in `self._device_ids` for as long as that port stays open. This is
deliberately NOT re-resolved per incoming MIDI message: the real resolver
does actual I/O (a subprocess call plus a handful of /proc and /sys
reads), and a port's identity cannot change while it stays the same open
ALSA port anyway. `_enqueue` looks the cached value up by `source` name
and threads it onto every `MidiEvent` (via `translate()`'s new
`device_id` parameter) it queues for that port, including the synthesized
`clock_tick` aggregate. `BindingMatch.device` (engine/bindings.py) is
what actually consumes this at match time -- see that module's own
docstring for the full precedence rule.

**Resolution retry (Important review fix):** the open-time resolve is
best-effort, not guaranteed -- a transient failure (real I/O: `aconnect`,
`/proc`, `/sys`) used to leave a port permanently absent from
`self._device_ids` for its whole open lifetime, with no retry at all.
`_watch()` now retries any name that's still open but missing a
`device_id` on every subsequent poll (`_resolve_device_id`, shared with
the open-time call site, caps its own failure log to once per name via
`self._identity_retry_warned` rather than once per `poll_interval`
forever). This narrows, but cannot fully close, the window during which
an event from that port carries `device_id=None` --
`BindingDispatcher._matches`'s own port_pattern rescue (see that method's
docstring) is what covers events arriving during whatever window remains.
"""
import fnmatch
import logging
import threading
import time

from midicrt.engine.core import MidiEvent
from midicrt.engine.midi_identity import IdentityResolver

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


def _is_own_output(port_name: str, exclude_names) -> bool:
    """True if `port_name` (an ALSA-enumerated INPUT-port name, as
    returned by `get_input_names()`) refers to one of `exclude_names` --
    the daemon's own `MidiOutput` port(s). See this module's own docstring
    ("Self-subscription exclusion") for why substring containment, not
    exact match, is required, and for the real observed name form this
    was written against. Falsy entries in `exclude_names` (e.g. `None`/
    `""`, before the output port's name is known) are ignored -- an empty
    string would otherwise match every port name via `in`."""
    return any(name and name in port_name for name in exclude_names)


def translate(msg, source: str, ts: float, device_id: str | None = None) -> MidiEvent | None:
    """`device_id` (Phase 9 Task 1, device-identity bindings): the stable
    identity `MidiInput` already resolved for `source` at port-open time
    (see that class's own docstring) -- threaded straight onto the
    returned `MidiEvent`, no resolution happens HERE (this function stays
    a pure, backend-agnostic message translator, same charter as before).
    Defaults to `None` so every existing direct call site (tests, tools)
    that doesn't know about device identity at all keeps working
    unchanged."""
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
    elif msg.type == "sysex":
        # Phase-3 task 12 (gap ports, engine/sysex.py's Cirklon
        # remote-control receiver): `msg.data` is the payload bytes
        # WITHOUT F0/F7 framing -- neither `data1`/`data2` (single-byte
        # ints) can hold that, so `MidiEvent.sysex_data` carries it
        # instead (see that field's own comment in engine/core.py). mido
        # sysex messages have no `.channel` attribute, matching the
        # `channel = getattr(msg, "channel", None)` -> None already
        # computed above.
        sysex_data = tuple(msg.data)
        summary = f"sysex ({len(sysex_data)} bytes)"
        return MidiEvent(ts=ts, source=source, type=msg.type, channel=channel,
                         data1=None, data2=None, summary=summary, sysex_data=sysex_data,
                         device_id=device_id)
    elif channel is not None:
        summary = f"{msg.type} ch{channel + 1}"
    else:
        summary = msg.type
    return MidiEvent(ts=ts, source=source, type=msg.type,
                     channel=channel, data1=data1, data2=data2, summary=summary,
                     device_id=device_id)


class MidiInput:
    def __init__(self, patterns, queue, poll_interval=3.0, backend=None, exclude_names=(),
                 identity_resolver=None):
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
        # Phase-3 task 12 fix (self-subscription feedback loop, Critical --
        # see module docstring): never OPEN a port whose enumerated name
        # refers to one of these -- normally the daemon's own MidiOutput
        # port name, wired in by daemon.py via `engine.midi_output_port_
        # name`.
        self._exclude_names = tuple(exclude_names)
        self._excluded_warned: set[str] = set()   # log each skipped name once, not every poll
        # Phase 9 Task 1 (device-identity bindings): `IdentityResolver`
        # (engine/midi_identity.py) does real I/O (a subprocess call, a
        # handful of /proc and /sys reads) -- resolved ONCE per newly-
        # OPENED port name, in `_watch()` below, and cached here for the
        # port's whole open lifetime rather than re-resolved on every
        # incoming MIDI message (`_enqueue` just looks this dict up, an
        # O(1) string hash, matching the task brief's own "MidiEvent gains
        # device_id ... cheap string" requirement). Injectable for tests,
        # same "constructor seam, real default" shape as `backend` above.
        self._identity = identity_resolver or IdentityResolver()
        self._device_ids: dict[str, str] = {}   # name -> resolved device_id
        # Important review fix (Phase 9 Task 1 follow-up): a transient
        # `IdentityResolver` failure at open time used to strand that port
        # with NO `_device_ids` entry for its entire open lifetime -- every
        # device-bound binding on it would silently stop firing (well,
        # rescue to port_pattern instead -- see `BindingDispatcher._matches`'
        # own fix -- but never regain its device identity) until the next
        # reboot/replug. `_watch()` now retries any open-but-unresolved
        # name on every subsequent poll; this set caps the failure log to
        # ONE line per name (cleared on success or when the port vanishes)
        # instead of one every `poll_interval`, forever, on a persistent
        # outage -- same "log each skipped name once" discipline
        # `_excluded_warned` right above already established.
        self._identity_retry_warned: set[str] = set()

    @property
    def open_ports(self) -> list[str]:
        return sorted(self._ports)

    @property
    def open_device_ids(self) -> list[str]:
        """Phase 9 Task 1: the DEDUPED set of `device_id`s currently
        resolved across every open port -- feeds `Engine._device_present`
        via `daemon.py`'s `set_open_device_ids_provider` wiring, exactly
        mirroring `open_ports`/`set_open_ports_provider`'s own shape.
        Deduped (not one entry per port) because two ports CAN legitimately
        share a device_id -- the documented no-serial collision case (see
        engine/midi_identity.py's own docstring) -- and `_device_present`
        only ever asks "is this id present at all", never "how many
        ports"."""
        return sorted(set(self._device_ids.values()))

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
        self._device_ids.clear()
        self._identity_retry_warned.clear()

    def _resolve_device_id(self, name: str, *, context: str) -> None:
        """Shared by the open-time resolve and the retry-on-poll pass
        below (Important review fix, Phase 9 Task 1 follow-up) -- one
        place that calls `IdentityResolver.resolve` and swallows its own
        failure, so a hiccup never propagates past this class regardless
        of which call site hit it. `context` is purely for the log
        message ("opened"/"retry"); the caching/warn-capping behavior is
        identical either way."""
        try:
            self._device_ids[name] = self._identity.resolve(name)
        except Exception as exc:  # noqa: BLE001 — identity resolution is best-effort
            if name not in self._identity_retry_warned:
                _LOG.warning("device-identity resolution failed for %s (%s): %s",
                            name, context, exc)
                self._identity_retry_warned.add(name)
            return
        if context == "retry":
            _LOG.info("device-identity resolved on retry for %s: %s", name,
                     self._device_ids[name])
        self._identity_retry_warned.discard(name)

    def _enqueue(self, msg, source: str) -> None:
        device_id = self._device_ids.get(source)
        if msg.type == "clock":
            self._on_clock(source, device_id)
            return
        if msg.type in _CLOCK_BOUNDARY_RESET_TYPES:
            self._clock_batch_start = None
        if msg.type in _CLOCK_FULL_RESET_TYPES:
            self._clock_count = 0
        ev = translate(msg, source, time.time(), device_id)
        if ev is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, ev)

    def _on_clock(self, source: str, device_id: str | None = None) -> None:
        """Count one raw clock pulse; emit an aggregated `clock_tick`
        MidiEvent every `_CLOCK_BATCH_SIZE` pulses -- see module docstring."""
        now = time.time()
        self._clock_count += 1
        if self._clock_count < _CLOCK_BATCH_SIZE:
            return
        ev = MidiEvent(ts=now, source=source, type="clock_tick", channel=None,
                       data1=_CLOCK_BATCH_SIZE, data2=None, summary="clock_tick",
                       clock_batch_start=self._clock_batch_start, device_id=device_id)
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
                if name in self._ports or not matches(name, self._patterns):
                    continue
                if _is_own_output(name, self._exclude_names):
                    # Critical fix (self-subscription feedback loop -- see
                    # module docstring): NEVER open our own daemon's output
                    # port as an input, however it matches `_patterns`.
                    if name not in self._excluded_warned:
                        _LOG.info("skipping own output port (would self-subscribe): %s", name)
                        self._excluded_warned.add(name)
                    continue
                try:
                    self._ports[name] = self._backend.open_input(
                        name, callback=lambda m, n=name: self._enqueue(m, n))
                    # Phase 9 Task 1: resolve device identity ONCE at open
                    # time -- see this class's own docstring comment on
                    # `self._identity` for why this must not happen
                    # per-message. A resolver failure (real I/O: subprocess/
                    # procfs/sysfs) must not prevent the port itself from
                    # opening -- `_resolve_device_id` swallows its own
                    # failure independently of `open_input`'s own try/except
                    # right below.
                    self._resolve_device_id(name, context="opened")
                    _LOG.info("opened MIDI input: %s (device=%s)",
                             name, self._device_ids.get(name))
                except Exception as exc:  # noqa: BLE001 — same: one bad port must not kill the loop
                    _LOG.warning("open failed for %s: %s", name, exc)
            for name in list(self._ports):
                if name not in available:
                    _LOG.info("MIDI input vanished: %s", name)
                    self._ports.pop(name).close()
                    self._device_ids.pop(name, None)
                    self._identity_retry_warned.discard(name)
            # Important review fix (Phase 9 Task 1 follow-up): retry
            # resolution for any port that's STILL open but never got a
            # device_id -- a transient failure at open time (above) must
            # not strand that port unresolved for its whole open lifetime.
            # Skips ports that resolved fine (the overwhelming majority,
            # every poll) and any port that just vanished in the loop right
            # above (no longer in `self._ports` at all).
            for name in list(self._ports):
                if name not in self._device_ids:
                    self._resolve_device_id(name, context="retry")
            time.sleep(self._poll)
