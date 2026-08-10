"""TDD for engine/midi_out.py::MidiOutput -- see that module's own
docstring for the full v1 (`pages/sendnotes.py::_ensure_out` +
`plugins/sysex.py::_send_reply`) behavioral synthesis and the disclosed
single-shared-port consolidation.
"""
from midicrt.engine.midi_out import DEFAULT_PORT_NAME, MidiOutput


class FakePort:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True


class FakeBackend:
    """Mirrors test_midi_in.py's own FakeBackend convention (a minimal
    double for mido, injected via MidiOutput(backend=...))."""

    def __init__(self, real_device_available=True, raise_on_all=False):
        self.real_device_available = real_device_available
        self.raise_on_all = raise_on_all
        self.opened: list[tuple[str, bool]] = []   # (name, virtual)

    def open_output(self, name, virtual=False):
        if self.raise_on_all:
            raise OSError("no such device")
        if not virtual and not self.real_device_available:
            raise OSError("no such device")
        self.opened.append((name, virtual))
        return FakePort(name)

    def Message(self, type, **kw):
        return {"type": type, **kw}


def test_no_io_happens_at_construction():
    backend = FakeBackend()
    MidiOutput(backend=backend)
    assert backend.opened == []


def test_note_on_opens_the_real_device_when_available():
    backend = FakeBackend(real_device_available=True)
    out = MidiOutput(backend=backend)
    out.note_on(60, 100, 1)
    assert backend.opened == [(DEFAULT_PORT_NAME, False)]
    assert out.is_open is True


def test_note_on_falls_back_to_a_virtual_port_when_the_real_device_is_absent():
    backend = FakeBackend(real_device_available=False)
    out = MidiOutput(backend=backend)
    out.note_on(60, 100, 1)
    assert backend.opened == [(DEFAULT_PORT_NAME, True)]


def test_port_is_opened_only_once_across_multiple_sends():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.note_on(60, 100, 1)
    out.note_off(60, 1)
    out.note_on(62, 100, 1)
    assert len(backend.opened) == 1


def test_note_on_converts_1_based_channel_to_mido_0_based():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.note_on(60, 100, 1)   # channel 1 (1-based) -> mido channel 0
    port = out._port
    assert port.sent[-1] == {"type": "note_on", "note": 60, "velocity": 100, "channel": 0}


def test_note_off_sends_velocity_zero():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.note_off(60, 10)   # channel 10 (1-based) -> mido channel 9
    port = out._port
    assert port.sent[-1] == {"type": "note_off", "note": 60, "velocity": 0, "channel": 9}


def test_all_notes_off_sends_cc123_value_0_on_the_target_channel():
    # Phase 9 Task 2 (panic-send): v1's `zstucknotes.py::_send_all_notes_off`
    # sends a channel-mode "All Notes Off" CC (control=123, value=0), NOT a
    # per-note note-off -- see that module's own docstring/report for the
    # v1 evidence. Ported here as a dedicated MidiOutput method, same
    # "channel is 1-based at this boundary, converted to mido's 0-based
    # once" convention as note_on/note_off above.
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.all_notes_off(3)   # channel 3 (1-based) -> mido channel 2
    port = out._port
    assert port.sent[-1] == {"type": "control_change", "control": 123, "value": 0, "channel": 2}


def test_all_notes_off_opens_the_port_lazily_like_every_other_send():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.all_notes_off(1)
    assert backend.opened == [(DEFAULT_PORT_NAME, False)]


def test_all_notes_off_failure_never_raises():
    class RaisingPort(FakePort):
        def send(self, msg):
            raise RuntimeError("device unplugged mid-send")

    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out._port = RaisingPort("x")
    out.all_notes_off(1)   # must not raise


def test_send_sysex_returns_true_on_success():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    ok = out.send_sysex((0x7D, 0x6D, 0x63, 0x01, 0x08))
    assert ok is True
    port = out._port
    assert port.sent[-1] == {"type": "sysex", "data": (0x7D, 0x6D, 0x63, 0x01, 0x08)}


def test_send_sysex_returns_false_when_no_device_is_reachable_at_all():
    backend = FakeBackend(raise_on_all=True)
    out = MidiOutput(backend=backend)
    assert out.send_sysex((0x7D,)) is False
    assert out.is_open is False


def test_a_send_failure_never_raises():
    class RaisingPort(FakePort):
        def send(self, msg):
            raise RuntimeError("device unplugged mid-send")

    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out._port = RaisingPort("x")   # simulate an already-open port that then fails
    out.note_on(60, 100, 1)        # must not raise
    assert out.send_sysex((0x7D,)) is False


def test_close_is_a_safe_no_op_when_never_opened():
    out = MidiOutput(backend=FakeBackend())
    out.close()   # must not raise
    assert out.is_open is False


def test_close_closes_the_open_port_and_a_later_send_reopens():
    backend = FakeBackend()
    out = MidiOutput(backend=backend)
    out.note_on(60, 100, 1)
    port = out._port
    out.close()
    assert port.closed is True
    assert out.is_open is False
    out.note_on(60, 100, 1)
    assert len(backend.opened) == 2   # reopened after close
