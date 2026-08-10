import asyncio

import mido

from midicrt.analyzers.transport import TransportAnalyzer
from midicrt.engine import midi_in
from midicrt.engine.midi_in import MidiInput, matches, translate


def test_translate_note_and_cc():
    ev = translate(mido.Message("note_on", channel=0, note=60, velocity=100), "USB", 1.0)
    assert (ev.type, ev.channel, ev.data1, ev.data2) == ("note_on", 0, 60, 100)
    assert ev.summary == "note_on ch1 n60 v100"
    ev = translate(mido.Message("control_change", channel=3, control=7, value=90), "USB", 1.0)
    assert ev.summary == "control_change ch4 cc7 v90"
    assert translate(mido.Message("clock"), "USB", 1.0) is None


def test_translate_ignores_active_sensing():
    # Regression: `_IGNORED_TYPES` used to contain the typo "activesensing"
    # (no underscore), which never matched mido's real type string --
    # `mido.Message("active_sensing").type == "active_sensing"` -- so
    # active-sensing messages were passing translate() completely
    # unfiltered (every ~300ms from most controllers) instead of being
    # dropped like "clock".
    assert mido.Message("active_sensing").type == "active_sensing"
    assert translate(mido.Message("active_sensing"), "USB", 1.0) is None


def test_translate_sysex_carries_raw_payload_bytes():
    # Phase-3 task 12 (gap ports): sysex_data carries msg.data verbatim
    # (no F0/F7 framing, matching mido's own convention) since data1/data2
    # (single-byte ints) can't hold an arbitrary-length sequence.
    msg = mido.Message("sysex", data=(0x7D, 0x6D, 0x63, 0x01, 0x08))
    ev = translate(msg, "USB", 1.0)
    assert ev.type == "sysex"
    assert ev.channel is None
    assert ev.data1 is None and ev.data2 is None
    assert ev.sysex_data == (0x7D, 0x6D, 0x63, 0x01, 0x08)
    assert ev.summary == "sysex (5 bytes)"


def test_translate_sysex_empty_payload():
    msg = mido.Message("sysex", data=())
    ev = translate(msg, "USB", 1.0)
    assert ev.sysex_data == ()
    assert ev.summary == "sysex (0 bytes)"


def test_translate_non_sysex_events_have_no_sysex_data():
    ev = translate(mido.Message("note_on", channel=0, note=60, velocity=100), "USB", 1.0)
    assert ev.sysex_data is None


# -- device identity (Phase 9 Task 1) ----------------------------------------

def test_translate_device_id_defaults_to_none():
    # Every pre-existing direct translate() call site (this whole test
    # file included) doesn't pass device_id at all -- must not regress.
    ev = translate(mido.Message("note_on", channel=0, note=60, velocity=100), "USB", 1.0)
    assert ev.device_id is None


def test_translate_threads_device_id_onto_the_event():
    ev = translate(mido.Message("note_on", channel=0, note=60, velocity=100), "USB", 1.0,
                   "usb:1234:5678:SN1")
    assert ev.device_id == "usb:1234:5678:SN1"


def test_translate_threads_device_id_onto_a_sysex_event_too():
    msg = mido.Message("sysex", data=(0x7D,))
    ev = translate(msg, "USB", 1.0, "virt:Midi Through:Midi Through Port-0")
    assert ev.device_id == "virt:Midi Through:Midi Through Port-0"


def test_matches():
    assert matches("NetMIDI:Network 128:0", ["NetMIDI*"])
    assert not matches("Midi Through:0", ["NetMIDI*", "USB*"])
    assert matches("anything", ["*"])


# -- self-subscription feedback-loop fix (live-reproduced Critical) --------
#
# MidiOutput's own virtual port (e.g. "midicrt2 Output") is enumerable by
# MidiInput's default wildcard `["*"]` pattern -- ALSA/RtMidi virtual ports
# are bidirectionally discoverable, so creating one as an OUTPUT also
# creates a name `get_input_names()` reports. Confirmed live via `aconnect`
# (the daemon's own reader thread held "Connected From" its own output
# port) and reproduced: any reply-eliciting SysEx (even the standard
# CMD_CAPABILITIES handshake) loops forever, since `build_reply()`'s own
# marker byte makes every reply re-parse as a fresh command with no origin
# check; every sendnotes note also echoed back as phantom external MIDI.
# `_is_own_output` is layer 1 of the two-layer fix (layer 2 is
# `Engine._handle`'s own source filter, see test_engine_core.py).
def test_is_own_output_matches_the_exact_configured_name():
    assert midi_in._is_own_output("midicrt2 Output", ("midicrt2 Output",))


def test_is_own_output_matches_the_real_observed_prefixed_alsa_form():
    # Live-observed exact string (task-12 fix live verification): a
    # virtual output port named "midicrt2 Output" is enumerated back by
    # get_input_names() wrapped in backend-specific client/port framing --
    # exact-match would miss this entirely.
    name = "RtMidiOut Client:midicrt2 Output 142:0"
    assert midi_in._is_own_output(name, ("midicrt2 Output",))


def test_is_own_output_no_match_for_an_unrelated_port():
    assert not midi_in._is_own_output("NetMIDI 128:0", ("midicrt2 Output",))


def test_is_own_output_empty_exclude_list_never_matches():
    assert not midi_in._is_own_output("RtMidiOut Client:midicrt2 Output 142:0", ())


def test_is_own_output_ignores_falsy_exclude_entries():
    # Defensive: a blank/None exclude name (e.g. before MidiOutput's port
    # is known) must never match everything via an empty-string `in` check.
    assert not midi_in._is_own_output("anything at all", ("", None))


class FakeBackend:
    def __init__(self):
        self.names = ["NetMIDI 128:0", "Midi Through 14:0"]
        self.opened = {}

    def get_input_names(self):
        return list(self.names)

    def open_input(self, name, callback):
        self.opened[name] = callback
        return type("P", (), {"close": lambda self2: self.opened.pop(name, None),
                              "name": name})()


async def test_midi_input_opens_matching_and_enqueues():
    backend = FakeBackend()
    queue = asyncio.Queue()
    mi = MidiInput(["NetMIDI*"], queue, poll_interval=0.05, backend=backend)
    mi.start(asyncio.get_running_loop())
    for _ in range(50):
        if mi.open_ports:
            break
        await asyncio.sleep(0.05)
    assert mi.open_ports == ["NetMIDI 128:0"]
    assert "Midi Through 14:0" not in backend.opened
    backend.opened["NetMIDI 128:0"](mido.Message("note_on", note=61, velocity=1))
    ev = await asyncio.wait_for(queue.get(), timeout=2)
    assert ev.data1 == 61 and ev.source == "NetMIDI 128:0"
    # port vanishes -> forgotten
    backend.names = []
    for _ in range(50):
        if not mi.open_ports:
            break
        await asyncio.sleep(0.05)
    assert mi.open_ports == []


async def test_midi_input_never_opens_its_own_daemons_output_port():
    # Reproduces the live incident: a wildcard ["*"] pattern would
    # otherwise match the daemon's own MidiOutput port (enumerated in its
    # real, backend-prefixed form) -- exclude_names must prevent it from
    # EVER being opened, across repeated poll cycles, while a genuinely
    # separate real port with a similar-looking name still opens normally.
    backend = FakeBackend()
    backend.names = [
        "RtMidiOut Client:midicrt2 Output 142:0",   # the daemon's own output
        "USB MIDI 20:0",                             # a real, unrelated device
    ]
    queue = asyncio.Queue()
    mi = MidiInput(["*"], queue, poll_interval=0.05, backend=backend,
                    exclude_names=("midicrt2 Output",))
    mi.start(asyncio.get_running_loop())
    for _ in range(50):
        if mi.open_ports:
            break
        await asyncio.sleep(0.05)
    # Give it a few more poll cycles to make sure it doesn't get opened later.
    await asyncio.sleep(0.2)
    assert mi.open_ports == ["USB MIDI 20:0"]
    assert "RtMidiOut Client:midicrt2 Output 142:0" not in backend.opened
    mi.stop()
    mi.stop()


class FakeIdentity:
    """Minimal identity-resolver double for MidiInput wiring tests --
    real resolution ladder mechanics are covered exhaustively in
    test_midi_identity.py; these tests only need to prove MidiInput calls
    `.resolve(name)` at open time, caches the result, and exposes/clears
    it correctly, not re-derive the ladder itself."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[str] = []

    def resolve(self, name: str) -> str:
        self.calls.append(name)
        return self.mapping.get(name, f"virt:{name}")


async def test_midi_input_resolves_and_caches_device_id_at_open_time():
    backend = FakeBackend()
    backend.names = ["NetMIDI 128:0"]
    identity = FakeIdentity({"NetMIDI 128:0": "usb:1234:5678:SN1"})
    queue = asyncio.Queue()
    mi = MidiInput(["NetMIDI*"], queue, poll_interval=0.05, backend=backend,
                    identity_resolver=identity)
    mi.start(asyncio.get_running_loop())
    for _ in range(50):
        if mi.open_ports:
            break
        await asyncio.sleep(0.05)
    assert mi.open_device_ids == ["usb:1234:5678:SN1"]

    backend.opened["NetMIDI 128:0"](mido.Message("note_on", note=61, velocity=1))
    ev = await asyncio.wait_for(queue.get(), timeout=2)
    assert ev.device_id == "usb:1234:5678:SN1"

    # Cached, not re-resolved per poll cycle.
    await asyncio.sleep(0.2)
    assert identity.calls == ["NetMIDI 128:0"]
    mi.stop()


async def test_midi_input_drops_device_id_when_its_port_vanishes():
    backend = FakeBackend()
    backend.names = ["NetMIDI 128:0"]
    identity = FakeIdentity({"NetMIDI 128:0": "usb:1234:5678:SN1"})
    queue = asyncio.Queue()
    mi = MidiInput(["NetMIDI*"], queue, poll_interval=0.05, backend=backend,
                    identity_resolver=identity)
    mi.start(asyncio.get_running_loop())
    for _ in range(50):
        if mi.open_device_ids:
            break
        await asyncio.sleep(0.05)
    assert mi.open_device_ids == ["usb:1234:5678:SN1"]

    backend.names = []
    for _ in range(50):
        if not mi.open_ports:
            break
        await asyncio.sleep(0.05)
    assert mi.open_device_ids == []


async def test_midi_input_defaults_to_a_real_identity_resolver_when_none_given():
    # Regression: the daemon's real production wiring never passes
    # identity_resolver explicitly -- must not crash/require one.
    mi = MidiInput(["*"], asyncio.Queue())
    from midicrt.engine.midi_identity import IdentityResolver
    assert isinstance(mi._identity, IdentityResolver)


async def test_midi_input_two_ports_with_the_same_device_id_dedupe_in_open_device_ids():
    """The documented no-serial collision case, one layer up: MidiInput
    itself must not report a device_id twice just because two open ports
    happen to resolve to the identical identity."""
    backend = FakeBackend()
    backend.names = ["NetMIDI 128:0", "Midi Through 14:0"]
    identity = FakeIdentity({"NetMIDI 128:0": "usb:1234:5678",
                            "Midi Through 14:0": "usb:1234:5678"})
    queue = asyncio.Queue()
    mi = MidiInput(["*"], queue, poll_interval=0.05, backend=backend, identity_resolver=identity)
    mi.start(asyncio.get_running_loop())
    for _ in range(50):
        if len(mi.open_ports) == 2:
            break
        await asyncio.sleep(0.05)
    assert mi.open_device_ids == ["usb:1234:5678"]
    mi.stop()


# -- clock aggregation (phase-3 task 3) --------------------------------------
#
# `_enqueue` is called directly here (no `start()`/`_watch()` thread, no
# backend) since the aggregation logic doesn't touch port discovery at all --
# these are unit tests of the counter/boundary bookkeeping in isolation.
# Every `_enqueue` call schedules its queue put via `call_soon_threadsafe`,
# which only runs on the NEXT loop iteration, so each assertion is preceded
# by an `await asyncio.sleep(0)` to let already-scheduled callbacks flush.


def _clockless_input(queue):
    mi = MidiInput(["*"], queue)
    mi._loop = asyncio.get_running_loop()
    return mi


async def test_23_clock_pulses_produce_no_event():
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(23):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 0


async def test_24th_clock_pulse_emits_one_aggregated_clock_tick():
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 1
    ev = q.get_nowait()
    assert ev.type == "clock_tick"
    assert ev.source == "USB"
    assert ev.data1 == 24
    assert ev.clock_batch_start is None   # no prior boundary yet


async def test_second_batch_boundary_spans_exactly_the_first_batchs_end():
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    first = q.get_nowait()

    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 1
    second = q.get_nowait()
    assert second.clock_batch_start == first.ts
    assert second.ts >= first.ts


async def test_non_clock_messages_pass_through_untouched_by_the_counter():
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(10):
        mi._enqueue(mido.Message("clock"), "USB")
    mi._enqueue(mido.Message("note_on", channel=0, note=60, velocity=100), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 1
    ev = q.get_nowait()
    assert ev.type == "note_on"          # not swallowed by the clock counter
    # the 10 clocks already counted must still count toward the next batch
    for _ in range(14):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 1
    assert q.get_nowait().type == "clock_tick"


async def test_start_message_resets_the_clock_counter_and_boundary():
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(10):
        mi._enqueue(mido.Message("clock"), "USB")
    mi._enqueue(mido.Message("start"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 1
    assert q.get_nowait().type == "start"   # transport message itself still passes through

    # Without the reset this would be pulse 24 (10 + 14) and fire early.
    for _ in range(14):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 0


async def test_stop_and_continue_also_reset_the_clock_batch_boundary():
    # Boundary-ALIGNED case: stop/continue happen right after a completed
    # batch, when `_clock_count` is already 0 -- clearing `clock_batch_start`
    # here is what prevents the NEXT batch's bpm from spanning the silent
    # stop gap. See test_continue_mid_batch_resumes_the_partial_pulse_tally
    # below for the boundary-MISALIGNED (partial-batch) case.
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    q.get_nowait()  # first clock_tick, establishes a boundary

    mi._enqueue(mido.Message("stop"), "USB")
    mi._enqueue(mido.Message("continue"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 2
    q.get_nowait(), q.get_nowait()

    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    ev = q.get_nowait()
    assert ev.clock_batch_start is None   # boundary was cleared by stop/continue


async def test_continue_mid_batch_resumes_the_partial_pulse_tally():
    # Regression: MIDI "continue" resumes the clock from the EXACT tick it
    # was stopped at -- stopping at pulse 10-of-24 then continuing means
    # only 14 MORE pulses complete that beat, not a fresh 24. An earlier
    # version of `_enqueue` zeroed `_clock_count` on stop/continue too,
    # which silently discarded that in-flight tally: the next clock_tick
    # would need a full 24 more pulses, landing up to a full beat late and
    # permanently offsetting TransportAnalyzer's bar/beat count (which
    # deliberately does NOT reset on continue, so a phase error here would
    # never self-correct).
    q = asyncio.Queue()
    mi = _clockless_input(q)
    for _ in range(10):
        mi._enqueue(mido.Message("clock"), "USB")
    mi._enqueue(mido.Message("stop"), "USB")
    mi._enqueue(mido.Message("continue"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 2
    q.get_nowait(), q.get_nowait()  # stop, continue passthrough events

    # 13 more pulses (10 + 13 = 23) must NOT complete the batch yet -- a
    # buggy fresh-24-count would also not fire here (14 < 24), so the real
    # proof is the NEXT assertion: exactly one more pulse (the TRUE 24th)
    # must fire it, not 10 more (which a buggy reset would require).
    for _ in range(13):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.qsize() == 0

    mi._enqueue(mido.Message("clock"), "USB")   # the true 24th pulse (10 + 14)
    await asyncio.sleep(0)
    assert q.qsize() == 1
    ev = q.get_nowait()
    assert ev.type == "clock_tick"
    assert ev.clock_batch_start is None   # bpm reference was cleared by stop/continue

    # Feed the resulting clock_tick into a real analyzer: the beat must
    # advance by EXACTLY one (proving the boundary landed at the true
    # 24-pulse mark, not early/late/duplicated).
    from midicrt.engine.core import MidiEvent

    analyzer = TransportAnalyzer()
    analyzer.handle(MidiEvent(ts=0.0, source="USB", type="start", channel=None,
                              data1=None, data2=None, summary="start"))
    before = analyzer.view_model()
    assert before["bar"] == 0 and before["beat"] == 1
    analyzer.handle(ev)
    after = analyzer.view_model()
    assert after["bar"] == 0 and after["beat"] == 2   # advanced by exactly one beat


async def test_enqueue_threads_the_cached_device_id_onto_queued_events():
    """`_enqueue` looks up the port's cached device_id (populated by
    `_watch()` at open time in production; set directly here since this
    helper bypasses `_watch()` entirely, matching this section's own
    "no thread, no backend" unit-test style) and threads it onto BOTH a
    regular translated event and a synthesized clock_tick."""
    q = asyncio.Queue()
    mi = _clockless_input(q)
    mi._device_ids["USB"] = "usb:1234:5678:SN1"

    mi._enqueue(mido.Message("note_on", channel=0, note=60, velocity=100), "USB")
    await asyncio.sleep(0)
    assert q.get_nowait().device_id == "usb:1234:5678:SN1"

    for _ in range(24):
        mi._enqueue(mido.Message("clock"), "USB")
    await asyncio.sleep(0)
    assert q.get_nowait().device_id == "usb:1234:5678:SN1"


async def test_translate_still_ignores_clock_when_called_directly():
    # Regression guard: translate() itself must stay untouched -- _enqueue's
    # aggregation intercepts "clock" before translate() ever sees it, but
    # translate() called directly (e.g. from other tests/tools) must still
    # drop clock on its own, per _IGNORED_TYPES.
    assert translate(mido.Message("clock"), "USB", 1.0) is None
