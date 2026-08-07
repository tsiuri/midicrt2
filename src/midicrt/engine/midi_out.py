"""MIDI output: a single, lazily-opened engine-owned virtual port used by
TWO phase-3 task 12 gap ports -- `pages/sendnotes.py` (v1's interactive
note-sender, PAGE_ID 2, the only v1 page/plugin that sends real MIDI) and
`engine/sysex.py`'s reply frames (v1's `plugins/sysex.py`, versioned SysEx
command replies). Neither v1 source shares a port the way this module does
-- see below for the disclosed consolidation.

v1 comparison
---------------------------------------------------------------------------
`pages/sendnotes.py::_ensure_out()`: tries `mido.open_output(out_name)`
against an EXISTING device named "GreenCRT Sender" first, falling back to
`mido.open_output(out_name, virtual=True)` (create a new ALSA sequencer
client port) if none exists -- exactly the pattern ported here. Every
`port.send()` call in v1 is wrapped in a bare `try/except: pass` (a MIDI-out
hiccup must never crash the note-sending page); same "send never raises"
contract here.

`plugins/sysex.py::_send_reply()` does something DIFFERENT: it searches
`mido.get_output_names()` for whichever ALREADY-OPEN port matches
"greencrt monitor"/"rtmidiin client" by name (i.e. tries to reply back down
roughly the SAME wire Cirklon is already connected to), only opening that
EXISTING port, never creating a new virtual one. v2 has no equivalent
"reply on whichever port matches an input's name" concept --
`engine/midi_in.py` opens ports by GLOB PATTERN against `config.
midi_sources`, potentially several at once, with no single well-known name
to search for. This module deliberately does NOT attempt that: both
capabilities (note-sending, sysex replies) share ONE lazily-opened virtual
output port instead, named distinctly from v1's own "GreenCRT Sender" (to
avoid any ALSA seq port-name collision with a live v1 instance) -- a real
Cirklon integration wanting sysex replies would need to be told this new
port name in its own patch config, a deployment concern out of this port's
scope, same as `analyzers/spectrum.py`'s own `audio_device` config knob.

No I/O happens at construction or import time
---------------------------------------------------------------------------
`MidiOutput()` never opens anything itself -- `_ensure()` is called lazily
from the first `note_on`/`note_off`/`send_sysex` call, mirroring v1's own
`_ensure_out()` being called from `draw()`/`keypress()`, never at plugin
load time. `Engine.__init__` can therefore always construct one
unconditionally (see `engine/core.py`) without ever touching a real MIDI
port unless a `sendnotes.key` action or a real Cirklon SysEx command
actually arrives.
"""
import logging

_LOG = logging.getLogger(__name__)

# Distinct from v1's "GreenCRT Sender" (see module docstring) -- avoids any
# ALSA sequencer port-name collision with a live v1 instance during the
# supervised smoke windows this project's process requires (docs/
# phase2-smoke.md/phase3-smoke.md).
DEFAULT_PORT_NAME = "midicrt2 Output"


class MidiOutput:
    """Lazily-opened MIDI output port. `note_on`/`note_off`/`send_sysex`
    never raise -- any open/send failure is swallowed (logged once at
    DEBUG), matching v1's own bare `try/except: pass` around every real
    send. `backend` is injectable for tests, mirroring `engine/midi_in.py::
    MidiInput`'s own `backend=None` convention."""

    def __init__(self, port_name: str = DEFAULT_PORT_NAME, backend=None) -> None:
        if backend is None:
            import mido as backend
        self._backend = backend
        self._port_name = port_name
        self._port = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    @property
    def port_name(self) -> str:
        return self._port_name

    def _ensure(self) -> None:
        if self._port is not None:
            return
        try:
            try:
                self._port = self._backend.open_output(self._port_name)
            except OSError:
                self._port = self._backend.open_output(self._port_name, virtual=True)
        except Exception as exc:  # noqa: BLE001 -- a MIDI-out hiccup must never crash the caller
            _LOG.debug("midi output unavailable (%s)", exc)
            self._port = None

    def note_on(self, note: int, velocity: int, channel: int) -> None:
        """`channel` is 1-based (matches every other v2 page-facing
        convention, e.g. `pages/voices.py`'s own rows) -- converted to
        mido's 0-based `channel` here, once, at the I/O boundary."""
        self._ensure()
        if self._port is None:
            return
        try:
            self._port.send(self._backend.Message(
                "note_on", note=note, velocity=velocity, channel=channel - 1))
        except Exception as exc:  # noqa: BLE001 -- see class docstring
            _LOG.debug("midi note_on send failed (%s)", exc)

    def note_off(self, note: int, channel: int) -> None:
        self._ensure()
        if self._port is None:
            return
        try:
            self._port.send(self._backend.Message(
                "note_off", note=note, velocity=0, channel=channel - 1))
        except Exception as exc:  # noqa: BLE001 -- see class docstring
            _LOG.debug("midi note_off send failed (%s)", exc)

    def send_sysex(self, data: tuple[int, ...]) -> bool:
        """Used by `engine/sysex.py`'s versioned-frame replies. Returns
        whether the send actually happened (the caller logs/emits an event
        either way -- see `Engine._handle_sysex`) rather than raising."""
        self._ensure()
        if self._port is None:
            return False
        try:
            self._port.send(self._backend.Message("sysex", data=data))
            return True
        except Exception as exc:  # noqa: BLE001 -- see class docstring
            _LOG.debug("midi sysex send failed (%s)", exc)
            return False

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception as exc:  # noqa: BLE001 -- shutdown must never raise
                _LOG.debug("midi output close failed (%s)", exc)
            self._port = None
