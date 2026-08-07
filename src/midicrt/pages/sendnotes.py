"""Send Notes page (page name "sendnotes"): v1's `~/codex/midicrt/pages/
sendnotes.py` (PAGE_ID 2, "Send Notes", 148 lines, READ-ONLY reference) --
an interactive MIDI-output tool: a QWERTY key maps to a note, held for a
configurable gate time, on a configurable channel/octave/velocity. The
ONLY v1 page or plugin that sends real MIDI output at all (contrast every
analyzer here, which is architecturally "no I/O" -- phase3-notes.md).

Why this needed real, new v2 infrastructure (not just a page)
---------------------------------------------------------------------------
v2's `Page` protocol (`handle(ev) -> bool`, `view_model() -> dict`) has no
I/O method at all -- every page to date is a pure projection of MIDI INPUT.
`engine/midi_out.py::MidiOutput` (new, this task) is v2's first MIDI OUTPUT
surface. Per this project's "engine does I/O, state stays pure" split
(the SAME pattern `analyzers/stucknotes.py`'s `drain_alerts()`/`engine/
core.py`'s `_tick_analyzers` already established for engine-emitted
events): this page's own methods stay pure state mutation +
derived-expiry-list reporting; `engine/core.py` is the one place that
actually calls `MidiOutput.note_on`/`.note_off`, reacting to what this
page's pure methods report, exactly like it already reacts to
`drain_alerts()`'s pending list.

v1-key -> v2-shape mapping
---------------------------------------------------------------------------
- `keypress(chk)` -> `apply_key(key_char, now)`: `now` is INJECTED (this
  project's "no internal clock reads" rule, e.g. `analyzers/stucknotes.
  py`'s own `tick(now)` docstring) rather than a live `time.time()` read
  inside this page -- the ENGINE's `sendnotes.key` action handler reads
  the clock once and passes it in, mirroring `_tick_pages(time.time())`'s
  own call site in `run()`.
- `active` (a `deque` of `(expire_time, note, ch)` tuples, FIFO) ->
  `self._active`, same deque + same FRONT-ONLY expiry check (see
  `drain_expired`'s own docstring for a real, faithfully-preserved v1
  quirk this produces).
- `_note_on`/`_note_off` (real sends) -> NOT called from this page at all
  -- `apply_key` returns a plain dict describing what happened
  (`{"note_on": True, "note", "vel", "ch"}` for a real note trigger,
  `{"note_on": False}` for a control-key adjustment, `None` for an
  unrecognized key), and `drain_expired(now)` returns the `(note, ch)`
  pairs whose gate has elapsed -- `engine/core.py` is the one place that
  turns either into a real `MidiOutput` call.
- `_build_widget_lines`'s status line (`Dev:.../Ch:.../Oct:.../Vel:.../
  Gate:.../Active:...`) -> `view_model()`'s flat fields, same information,
  page-shape not string-shape (a renderer builds the actual status text).

Engine-info wiring (device open/closed) -- same "engine facts no page can
derive on its own" pattern as pages/configview.py/pages/help.py
---------------------------------------------------------------------------
Whether the real output device is open is `MidiOutput`'s own state, which
this page has no reference to (see module docstring above -- pages never
own I/O objects here). `Engine.__init__` calls `bind_device_info()` on
this page (mirroring `ConfigPage.bind_engine_info`/`HelpPage.bind_info`)
with a bound callback returning `{"is_open": bool, "port_name": str}`.

A KEYMAP quirk preserved byte-for-byte (read v1's `keypress()` fully
before assuming KEYMAP's own keys are all reachable as notes)
---------------------------------------------------------------------------
v1's `keypress()` checks the CONTROL keys (`,`/`.`  channel, `[`/`]`
octave, `-`/`=` velocity, `g`/`h` gate -- case-INSENSITIVE for g/h via
`s.lower() == 'g'`) BEFORE ever consulting `KEYMAP`. But `KEYMAP` itself
ALSO maps `,` (offset 12), `.` (offset 14), `g` (offset 6), and `h`
(offset 8) to notes -- so those four entries can NEVER actually trigger a
note-send: the control-key branches always intercept them first, `return`
before `KEYMAP` is ever consulted for those characters. This is a real,
observable v1 behavior (confirmed by reading `keypress()`'s full
if/elif-chain-as-early-returns shape, not a porting guess), preserved
here verbatim in `apply_key`'s own branch order -- NOT "fixed" by
reordering, since that would change real, shipped v1 behavior this task's
"faithful port is the default" instruction asks to preserve.
"""
from __future__ import annotations

from collections import deque

# QWERTY key -> semitone offset from the current octave's C, ported
# verbatim from v1's `KEYMAP` -- see module docstring for which four of
# these are structurally unreachable as note triggers (',', '.', 'g', 'h').
KEYMAP = {
    "z": 0, "s": 1, "x": 2, "d": 3, "c": 4,
    "v": 5, "g": 6, "b": 7, "h": 8, "n": 9,
    "j": 10, "m": 11,
    ",": 12, "l": 13, ".": 14, ";": 15, "/": 16,
}

_DEFAULT_DEVICE_INFO = {"is_open": False, "port_name": None}


class SendNotesPage:
    name = "sendnotes"

    def __init__(self) -> None:
        self._channel = 1     # 1-16, matches v1's default
        self._octave = 4      # MIDI 60 == C4 when octave=4, matches v1
        self._velocity = 96
        self._gate_ms = 120
        # FIFO of {"expire_ts", "note", "ch"} -- deque, matching v1's own
        # deque(active) + front-only expiry peek (see drain_expired).
        self._active: deque[dict] = deque()
        self._device_info_provider = None

    def bind_device_info(self, provider) -> None:
        """Wired once by `Engine.__init__` in production; never called by a
        page constructed directly by a test (see module docstring)."""
        self._device_info_provider = provider

    def handle(self, ev) -> bool:
        return False   # not MIDI-driven at all -- v1's sendnotes.py has no handle()

    # -- key handling (pure state; I/O happens at the engine layer) --------

    def apply_key(self, key_char: str, now: float) -> dict | None:
        """Mirrors v1's `keypress(chk)` branch order EXACTLY (see module
        docstring's KEYMAP-shadowing note) -- control keys first, note keys
        last. Returns `None` for a key this page does not recognize at all
        (v1's `keypress()` returning `False`), `{"note_on": False}` for a
        control-key adjustment (state changed, nothing to send), or
        `{"note_on": True, "note", "vel", "ch"}` for a real note trigger
        (the caller -- `engine/core.py` -- is the one that actually calls
        `MidiOutput.note_on` with this)."""
        s = str(key_char)
        if not s:
            return None

        if s == ",":
            self._channel = max(1, self._channel - 1)
            return {"note_on": False}
        if s == ".":
            self._channel = min(16, self._channel + 1)
            return {"note_on": False}
        if s == "[":
            self._octave = max(-1, self._octave - 1)
            return {"note_on": False}
        if s == "]":
            self._octave = min(9, self._octave + 1)
            return {"note_on": False}
        if s == "-":
            self._velocity = max(1, self._velocity - 8)
            return {"note_on": False}
        if s == "=":
            self._velocity = min(127, self._velocity + 8)
            return {"note_on": False}
        if s.lower() == "g":
            self._gate_ms = max(20, self._gate_ms - 20)
            return {"note_on": False}
        if s.lower() == "h":
            self._gate_ms = min(2000, self._gate_ms + 20)
            return {"note_on": False}

        offset = KEYMAP.get(s.lower())
        if offset is None:
            return None
        note = 12 * (self._octave + 1) + offset   # C4=60 when octave=4, offset=0
        self._active.append({
            "expire_ts": now + (self._gate_ms / 1000.0),
            "note": note, "ch": self._channel,
        })
        return {"note_on": True, "note": note, "vel": self._velocity, "ch": self._channel}

    def drain_expired(self, now: float) -> list[tuple[int, int]]:
        """Pop every note whose gate has elapsed, oldest first, returning
        `(note, ch)` pairs for the caller to send a real `note_off` for
        (mirrors `analyzers/stucknotes.py`'s own `drain_alerts()` "pure
        module reports, engine acts" split).

        Real, faithfully-preserved v1 quirk (front-only check, matching
        `_expire_notes`'s `while active and active[0][0] <= now:`): this
        stops at the FIRST not-yet-expired entry rather than scanning the
        whole queue. If `gate_ms` is shortened between two key presses, a
        LATER-appended note with a SHORTER gate can end up with an EARLIER
        absolute expiry than an note still ahead of it in the queue -- and
        will not actually be released until that earlier entry ALSO
        expires (held longer than its own gate). v1 has the identical
        bug (a plain FIFO `deque`, no re-sorting) -- ported verbatim, not
        fixed, per this task's "faithful port is the default" instruction.
        """
        expired: list[tuple[int, int]] = []
        while self._active and self._active[0]["expire_ts"] <= now:
            entry = self._active.popleft()
            expired.append((entry["note"], entry["ch"]))
        return expired

    def flush_all(self) -> list[tuple[int, int]]:
        """Unconditionally release EVERY still-gated active note,
        regardless of whether its gate has elapsed yet -- called ONCE,
        synchronously, from `Engine.stop()` (Important fix, live-
        reproduced): before this existed, a routine daemon restart
        mid-note just closed `MidiOutput` out from under `drain_expired`'s
        future release, leaving a real downstream synth holding a stuck
        note with no way to release it short of the synth's own local
        panic/timeout, if it even has one.

        Unlike `drain_expired(now)`, this takes NO clock argument at all
        -- at shutdown there is nothing left to wait for, so every queued
        entry is released immediately, in the same FIFO order they were
        pressed (matches `drain_expired`'s own ordering, just unconditional
        rather than gated on `expire_ts`). Idempotent: a second call on an
        already-empty queue returns `[]`.
        """
        flushed = [(entry["note"], entry["ch"]) for entry in self._active]
        self._active.clear()
        return flushed

    # -- view model ---------------------------------------------------------

    def view_model(self) -> dict:
        info = self._device_info_provider() if self._device_info_provider else _DEFAULT_DEVICE_INFO
        return {
            "title": "SEND NOTES",
            "device": info["port_name"] if info["is_open"] else None,
            "channel": self._channel,
            "octave": self._octave,
            "velocity": self._velocity,
            "gate_ms": self._gate_ms,
            "active": len(self._active),
        }
