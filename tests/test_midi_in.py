import asyncio

import mido

from midicrt.engine.midi_in import MidiInput, matches, translate


def test_translate_note_and_cc():
    ev = translate(mido.Message("note_on", channel=0, note=60, velocity=100), "USB", 1.0)
    assert (ev.type, ev.channel, ev.data1, ev.data2) == ("note_on", 0, 60, 100)
    assert ev.summary == "note_on ch1 n60 v100"
    ev = translate(mido.Message("control_change", channel=3, control=7, value=90), "USB", 1.0)
    assert ev.summary == "control_change ch4 cc7 v90"
    assert translate(mido.Message("clock"), "USB", 1.0) is None


def test_matches():
    assert matches("NetMIDI:Network 128:0", ["NetMIDI*"])
    assert not matches("Midi Through:0", ["NetMIDI*", "USB*"])
    assert matches("anything", ["*"])


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


async def test_translate_still_ignores_clock_when_called_directly():
    # Regression guard: translate() itself must stay untouched -- _enqueue's
    # aggregation intercepts "clock" before translate() ever sees it, but
    # translate() called directly (e.g. from other tests/tools) must still
    # drop clock on its own, per _IGNORED_TYPES.
    assert translate(mido.Message("clock"), "USB", 1.0) is None
