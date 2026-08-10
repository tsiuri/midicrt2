"""SysEx command PARSING (pure) — the frame-format half of v1's
`~/codex/midicrt/plugins/sysex.py` (275 lines, READ-ONLY reference): the
Cirklon remote-control receiver, a real, actively-used production feature
confirmed live via `sysex.d/` on the Pi containing genuine captured command
files (page-switch, page-cycle toggle), not vestigial. See
`engine/core.py::Engine._handle_sysex` for the DISPATCH half (page
switching, screensaver/pagecycle control, capability replies) — split the
same way `analyzers/theory.py`'s pure matching is split from
`analyzers/harmony.py`'s stateful wrapper: this module has NO side effects
at all (no MIDI send, no page switch, no logging), matching phase3-notes.md's
"analyzers/pure modules stay no-I/O, the engine acts" rule applied here to
a receiver instead of an analyzer.

Message formats (v1 README's own documentation, byte-verified against
`~/codex/midicrt/sysex.d/*.syx` captures on the Pi)
---------------------------------------------------------------------------
    Legacy:    F0  7D  6D  63  <cmd>  [args...]  F7
    Versioned: F0  7D  6D  63  <ver>  <cmd>  [args...]  F7

`msg.data` (what `engine/midi_in.py::translate()` hands this module as
`sysex_data`) is the payload WITHOUT the F0/F7 framing bytes (mido's own
convention) -- so `PREFIX` below matches directly against `data[:3]`.

- `7D`    = MIDI non-commercial / private-use manufacturer ID
- `6D 63` = ASCII "mc" (midicrt identifier)
- `<ver>` = protocol byte: `0x40` = negotiate highest supported, `0x41..`
  = version N = byte - 0x40
- `<cmd>` = command byte (`CMD_*` below)
- `[args...]` = zero or more argument bytes (0-127)

`parse_command` ports v1's `handle()`'s prefix-check + version-negotiation
branch EXACTLY, including which error cases the ORIGINAL never sends a
reply for at all (see its own docstring) -- every side effect v1's
`handle()` has beyond parsing (the screensaver-wake, the `_log()` calls,
the actual `_send_reply()` sends, `_dispatch()`'s command execution) is
engine-level, not here.
"""
PREFIX = (0x7D, 0x6D, 0x63)
VERSION_BASE = 0x40                       # v1's NEGOTIATE_VERSION_BYTE
SUPPORTED_PROTOCOL_VERSIONS = (1,)

CMD_SWITCH_PAGE = 0x01
CMD_SCREENSAVER = 0x02
CMD_PAGE_CYCLE = 0x03
CMD_CAPTURE_RECENT = 0x04
CMD_CAPABILITIES = 0x10


def parse_command(data: tuple[int, ...]) -> dict | None:
    """Pure parse of a raw SysEx payload (no F0/F7). Returns `None` when
    `data` is not addressed to midicrt at all (too short, or the first 3
    bytes don't match `PREFIX`) -- a true no-op, matching v1's own
    `handle()` early `return` for exactly this case (any OTHER SysEx
    traffic on the wire, e.g. real Cirklon MMC-style messages captured
    alongside midicrt's own commands in `sysex.d/`, must pass through
    silently unrecognised).

    Otherwise returns `{"version": int|None, "cmd": int|None,
    "args": tuple[int, ...], "error": str|None}`:
    - `version` is `None` for a LEGACY frame (marker byte < VERSION_BASE,
      i.e. the marker itself IS the command byte), or the resolved
      protocol version int for a versioned frame (negotiated to
      `max(SUPPORTED_PROTOCOL_VERSIONS)` when the version byte is exactly
      `VERSION_BASE`).
    - `error` is `None` (ready to dispatch), `"missing-cmd"` (a versioned
      frame too short to contain a command byte at all -- v1 logs this and
      returns with NO reply of any kind, since there's no cmd to reply
      about), or `"unsupported-version"` (a versioned frame naming an
      unsupported version explicitly, not `VERSION_BASE`'s
      negotiate-to-highest byte, which can never fail this check by
      construction).
    """
    n = len(PREFIX)
    if len(data) < n + 1 or tuple(data[:n]) != PREFIX:
        return None

    marker = data[n]
    if marker < VERSION_BASE:
        # Legacy frame: F0 7D 6D 63 <cmd> [args...] F7 -- the marker byte
        # IS the command byte, no version negotiation at all.
        return {"version": None, "cmd": marker, "args": tuple(data[n + 1:]), "error": None}

    if len(data) < n + 2:
        return {"version": None, "cmd": None, "args": (), "error": "missing-cmd"}

    cmd = data[n + 1]
    args = tuple(data[n + 2:])
    if marker == VERSION_BASE:
        version = max(SUPPORTED_PROTOCOL_VERSIONS)   # negotiate to highest supported
    else:
        version = marker - VERSION_BASE
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            return {"version": version, "cmd": cmd, "args": args, "error": "unsupported-version"}
    return {"version": version, "cmd": cmd, "args": args, "error": None}


def build_status_text(version: int | None, cmd: int, status: str, detail: str = "") -> str:
    """Pure port of v1's `_format_status` (`plugins/sysex.py:92-95`) --
    the loopprogress-status TEXT format for a CMD-dispatch outcome (v1's
    `midicrt.sysex_status`, read by `plugins/loopprogress.py:43-46`, see
    `engine/sysex_store.py::SysexStore.record_command_status`'s own
    docstring for the v2 wiring). `"sx:legacy cmd=0x01 ok page->3"`-style:
    `version=None` renders `"legacy"` (a legacy F0 7D 6D 63 <cmd> frame),
    otherwise `f"v{version}"`; `detail` is appended (space-separated,
    stripped) only when non-empty, matching v1's own `f"{base} {detail}".
    strip()` exactly -- an empty `detail` (v1's own default) leaves `base`
    untouched rather than a trailing space."""
    vtxt = "legacy" if version is None else f"v{version}"
    base = f"sx:{vtxt} cmd=0x{cmd:02X} {status}"
    return f"{base} {detail}".strip()


def build_reply(version: int, cmd: int, status: int, payload: tuple[int, ...] = ()) -> tuple[int, ...]:
    """Pure port of v1's `_send_reply`'s frame construction (the byte
    layout only -- the actual MIDI send is `engine/midi_out.py::
    MidiOutput.send_sysex`'s job, called by `Engine._handle_sysex`).
    Reply shape: `F0 7D 6D 63 <ver> <cmd> <status> [payload...] F7`
    (F0/F7 added by `mido.Message("sysex", data=...)` itself, not here --
    matching what `parse_command` receives, this returns the payload only).
    `status` is `0x00`=ok, `0x01`=error, per v1's own convention.
    """
    return PREFIX + (VERSION_BASE + int(version), cmd, status) + tuple(payload)
