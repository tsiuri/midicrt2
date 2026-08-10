"""Device-identity resolution for MIDI sources (Phase 9 Task 1,
.superpowers/sdd/2026-08-09-midicrt2-phase9-instruments/task-1-brief.md).

The problem `engine/bindings.py::glob_port_pattern` (Phase 5 Task 3) never
tried to solve: stripping the volatile ALSA `<client>:<port>` numbering
suffix buys reboot/replug durability, but it also throws away the one thing
that could distinguish two DIFFERENT physical MIDI interfaces that ALSA
happens to enumerate under the exact same NAME (most commonly two units of
the same USB MIDI interface model) -- see that module's own "Disclosed
limitation: identical-device collision" and docs/phase5-capture.md §7 for
the full writeup this task follows up on. This module resolves a raw
ALSA-enumerated port name to a `device_id` string that is stable ACROSS a
reboot/replug/port-move (like `glob_port_pattern`) but, unlike a bare
suffix-stripped name, can also DISTINGUISH two same-model devices when the
hardware exposes a USB serial number.

Resolution ladder
------------------
Given `source` (the raw string `MidiInput`/`translate()` sees, e.g.
`"USB Midi:USB Midi MIDI 1 24:0"` or `"Midi Through:Midi Through Port-0
14:0"`):

  1. Extract the trailing ALSA `<client>:<port>` numbering suffix
     (`_ALSA_CLIENT_PORT_RE`) to get the numeric ALSA sequencer CLIENT
     number (24, 14, ... above). No suffix at all -> skip straight to
     step 4 (nothing to look up a card for).
  2. Ask `aconnect -l` (already a required system tool on this box --
     `engine/midi_in.py`'s own module docstring already documents using it
     as a live diagnostic; this is the first PROGRAMMATIC consumer, not a
     new runtime dependency) which physical sound CARD, if any, that
     client number is backed by. `aconnect -l` prints
     `client N: '...' [type=kernel,card=M]` for a client the kernel
     created directly from a card's rawmidi substream(s) (verified against
     the `aconnect` binary's own compiled-in format string on this Pi:
     `strings $(which aconnect) | grep card` -> `,card=%d`) -- a client
     with NO card (every virtual/software client observed on this Pi:
     "Midi Through", "pivisualizer" (rtpmidid), every "RtMidi{In,Out}
     Client") simply omits the `,card=M` suffix entirely, which is exactly
     how "no card backs this client" is distinguished from "card 0".
  3. If a card number was found, read `/proc/asound/card<M>/usbid` --
     present ONLY for a USB-backed card, `"<vendor>:<product>"` in lowercase
     hex (confirmed live on this Pi's one actually-attached USB audio
     interface: `/proc/asound/card1/usbid` -> `0d8c:0014`, a C-Media USB
     Audio Device -- not a MIDI interface, but the exact sysfs/procfs shape
     a USB MIDI interface's card would also have). If present:
       a. `device_id = "usb:<vendor>:<product>:<serial>"` when a USB
          serial number is ALSO available (step 3b below).
       b. Serial lookup: `/sys/class/sound/card<M>/device` is a symlink to
          the USB INTERFACE's sysfs node (e.g.
          `.../usb1/1-1/1-1.4/1-1.4:1.0`); its PARENT directory (exactly
          one level up, e.g. `.../1-1/1-1.4`) is the USB DEVICE node,
          where `idVendor`/`idProduct`/`manufacturer`/`product`/`serial`
          live for a directly-attached device. This is the ONE level this
          module ever walks up, guarded by re-checking `idVendor` is
          actually present at that exact directory before trusting
          anything else found there. **This bound is load-bearing, not
          cosmetic** -- live-probed on this Pi's own USB Audio Device: its
          immediate device directory (`1-1.4`) has no `serial` file at all
          (a very common cost-cutting omission on cheap USB MIDI/audio
          chipsets, C-Media's included), but ONE level further up
          (`1-1`, the upstream USB hub) DOES have its own `idVendor`/
          `idProduct` -- and further up still (`usb1`, the root hub/host
          controller), a `serial` file reading `"3f980000.usb"`, the
          Pi's OWN platform bus address, not anything about the plugged-in
          device at all. Walking further than one level "to find A
          serial, any serial" would silently misattribute the HOST's own
          platform identifier to every USB MIDI device on this Pi as if
          it were that device's serial -- collapsing the exact
          serial-based disambiguation this module exists to provide,
          silently, for every device that simply doesn't expose one. No
          serial at the correct one-level-up device node is the honest
          "this device doesn't have one" case (step 4).
  4. No serial found (device node exists but has no `serial` attribute):
     `device_id = "usb:<vendor>:<product>"` -- the documented,
     still-colliding case: two simultaneously-connected units of the same
     serial-less model resolve to the IDENTICAL `device_id`, exactly like
     they collided under the old suffix-stripped `port_pattern` scheme.
     Disclosed, not fixed -- see this module's own module-level "Disclosed
     limitation" cross-reference and docs/phase4-bindings.md /
     docs/phase5-capture.md §7 (both updated by this task).
  5. Every other case -- no ALSA suffix on `source` at all, `aconnect`
     unavailable/erroring, the client has no `,card=` at all (every
     virtual/software client on this Pi today), or a card WITH a number
     but no `usbid` file (a non-USB kernel card, e.g. this Pi's onboard
     `bcm2835 Headphones` -- not a MIDI source in practice, but nothing
     here assumes that): `device_id = "virt:<source-with-the-volatile-
     ALSA-suffix-stripped>"`, reusing `engine/bindings.py::
     strip_alsa_port_suffix` -- the EXACT same "what counts as the
     volatile suffix" rule `glob_port_pattern` already established, not a
     second independently-drifting regex for the same ALSA fact. This is
     durable across the same reboot/replug/rtpmidid-restart churn
     `glob_port_pattern` was built for, but (like the old scheme) still
     collides if two virtual/software ports ever share the exact same
     stripped name -- not something normally possible for the shipped
     virtual clients (each has a unique client name), so undisclosed as a
     separate caveat.

Never crashes. Every I/O seam below (`run_aconnect`/`read_text`/
`read_link`) swallows its own real-world failure (missing binary, missing
/proc or /sys path, a symlink that doesn't exist) and returns `None`/empty,
letting `_resolve` fall through to the next rung of the ladder -- mirrors
`engine/midi_in.py::_watch`'s own "one bad port scan must not kill the poll
loop" discipline applied to device-identity resolution specifically.

Testability
-----------
All three I/O seams (`run_aconnect`, `read_text`, `read_link`) are
constructor-injectable on `IdentityResolver` -- tests build a fully fake
ALSA/sysfs world (a dict of path -> text, and a canned `aconnect -l`
transcript) with ZERO real subprocess calls or filesystem access, per this
task's own "TDD (fake sysfs/identity provider)" requirement. No real USB
MIDI interface is attached to this Pi as of this task (confirmed live:
`cat /proc/asound/cards` shows only the onboard `bcm2835 Headphones` and a
USB Audio-only C-Media device, neither MIDI-capable) -- the `usb:` rung of
the ladder is therefore verified ENTIRELY by these fakes, never against
real hardware; the `virt:` rung is the one live-verified against this Pi's
real `Midi Through`/rtpmidid ports (see the task-1 report's live-smoke
transcript).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable

from midicrt.engine.bindings import strip_alsa_port_suffix

_LOG = logging.getLogger(__name__)

# The trailing " <client>:<port>" ALSA sequencer-numbering suffix -- same
# shape `engine/bindings.py::_ALSA_PORT_SUFFIX_RE` matches, but this one
# captures the CLIENT number (group 1) since that's what `aconnect -l`
# keys its own "client N: ..." lines on. Kept as an independent regex
# object (not imported from bindings.py) because it needs a capture group
# bindings.py's own suffix-stripping regex has no use for; the underlying
# "what counts as the suffix" shape is identical, and reused verbatim from
# there via `strip_alsa_port_suffix` wherever this module actually strips
# rather than extracts a client number.
_ALSA_CLIENT_PORT_RE = re.compile(r"\s(\d+):(\d+)$")

# `aconnect -l` output, one line per client, e.g.:
#   client 24: 'USB Midi' [type=kernel,card=2]
#   client 14: 'Midi Through' [type=kernel]              <- no card: virtual
#   client 130: 'pivisualizer' [type=user,pid=339032]    <- no card: user/rtpmidid
#
# Minor review fix: `aconnect` never escapes an apostrophe embedded in a
# client's own name (its `printf`-style quoting is just `'%s'`, verbatim)
# -- a device genuinely named e.g. "Roland's SC-55" would break the
# ORIGINAL `'[^']*'` character-class version of this pattern (it stops at
# the FIRST `'`, landing mid-name with no `[type=...` immediately after,
# so the whole line simply fails to match and that client's card, if any,
# is silently never found -- falls all the way back to `virt:`, wrong for
# a real card-backed device). The lazy `'.*?'` below instead expands past
# an embedded apostrophe as needed to reach the actual `' [type=` that
# closes the name -- `.` does not match `\n` here (no `re.DOTALL`), so
# this still can't run past a single line even though it's not anchored
# on `'` alone. See test_aconnect_client_name_containing_an_apostrophe_
# still_resolves_the_card in test_midi_identity.py.
_ACONNECT_CLIENT_RE = re.compile(
    r"^client (\d+): '.*?' \[type=\w+(?:,card=(\d+))?", re.MULTILINE)

_USBID_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*$")


def _real_run_aconnect() -> str:
    return subprocess.run(
        ["aconnect", "-l"], capture_output=True, text=True, check=True, timeout=5,
    ).stdout


def _real_read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _real_read_link(path: str) -> str | None:
    try:
        return os.path.realpath(path) if os.path.islink(path) else None
    except OSError:
        return None


class IdentityResolver:
    """Resolves an ALSA-enumerated MIDI source name to a `device_id`
    string via the ladder documented in this module's own docstring above.
    `MidiInput` (engine/midi_in.py) owns exactly one instance and resolves
    once per newly-OPENED port (see that module's own comment for why --
    the real I/O this does, real or faked, is too costly to repeat per
    incoming MIDI message).

    The three constructor args are the ONLY I/O this class performs --
    each defaults to the real OS call, and each is independently
    overridable so a test can fake `aconnect -l`'s stdout and an entire
    sysfs/procfs tree as plain in-memory data, with no real subprocess or
    filesystem dependency at all."""

    def __init__(
        self,
        run_aconnect: Callable[[], str] | None = None,
        read_text: Callable[[str], str | None] | None = None,
        read_link: Callable[[str], str | None] | None = None,
    ):
        self._run_aconnect = run_aconnect or _real_run_aconnect
        self._read_text = read_text or _real_read_text
        self._read_link = read_link or _real_read_link

    def resolve(self, source: str) -> str:
        """Pure per-call resolution (no caching here -- `MidiInput` is the
        caching layer, keyed by the exact port name at open time). Never
        raises: any failure at any rung of the ladder falls through to the
        `virt:` fallback rather than propagating."""
        client_num = self._client_number(source)
        if client_num is not None:
            card_num = self._card_for_client(client_num)
            if card_num is not None:
                usb_id = self._usb_identity(card_num)
                if usb_id is not None:
                    return usb_id
        return f"virt:{strip_alsa_port_suffix(source)}"

    @staticmethod
    def _client_number(source: str) -> int | None:
        match = _ALSA_CLIENT_PORT_RE.search(source)
        return int(match.group(1)) if match else None

    def _card_for_client(self, client_num: int) -> int | None:
        try:
            text = self._run_aconnect()
        except Exception:  # noqa: BLE001 -- missing/erroring `aconnect` must fall back, never crash
            _LOG.debug("device-identity: aconnect -l unavailable", exc_info=True)
            return None
        for match in _ACONNECT_CLIENT_RE.finditer(text):
            if int(match.group(1)) != client_num:
                continue
            return int(match.group(2)) if match.group(2) else None
        return None

    def _usb_identity(self, card_num: int) -> str | None:
        usbid_text = self._read_text(f"/proc/asound/card{card_num}/usbid")
        if not usbid_text:
            return None
        match = _USBID_RE.match(usbid_text.strip())
        if match is None:
            return None
        vendor, product = match.group(1).lower(), match.group(2).lower()
        serial = self._find_serial(card_num)
        if serial:
            return f"usb:{vendor}:{product}:{serial}"
        return f"usb:{vendor}:{product}"

    def _find_serial(self, card_num: int) -> str | None:
        """See this module's own docstring, step 3b, for why this walks
        EXACTLY one level up from the USB interface node and no further --
        live-probed on this Pi's own hardware, an ancestor further up the
        bus tree has its own unrelated `serial` (the platform host
        controller's bus address, not the plugged-in device's)."""
        interface_dir = self._read_link(f"/sys/class/sound/card{card_num}/device")
        if not interface_dir:
            return None
        device_dir = os.path.dirname(interface_dir)
        if self._read_text(os.path.join(device_dir, "idVendor")) is None:
            # Not the shape expected (sanity guard, not a real vendor/
            # product cross-check -- those already came from
            # /proc/asound's own usbid) -- refuse to guess further up the
            # tree. See the docstring's own live-reproduced "hub/host-
            # controller serial" gotcha for exactly why.
            return None
        serial = self._read_text(os.path.join(device_dir, "serial"))
        return serial.strip() if serial else None
