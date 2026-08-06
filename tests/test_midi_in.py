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
