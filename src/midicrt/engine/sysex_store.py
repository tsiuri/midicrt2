"""SysEx MANAGER (Phase 9 Task 5, user-requested NEW feature: "record,
save, and play sysex at will from the browser"): a bounded RING of
recently-received raw sysex frames, plus a named, on-disk LIBRARY a user
explicitly saves entries into (and can later play back or delete). Engine-
owned, like `engine/capture.py::CaptureSink` -- constructed once in
`Engine.__init__` (`self._sysex_store`), fed directly from `Engine._handle`
(NOT through the analyzer `handle(ev)` fan-out -- see `engine/core.py`'s
`_SysexStatusOverlay` for the thin analyzer-shaped WRAPPER that exposes
this store's status text to chrome, mirroring `_PolyLimitOverlay`'s own
"wrap a shared, non-analyzer object's state into a small view model" shape).

Distinct from `engine/sysex.py` + `Engine._handle_sysex`
---------------------------------------------------------------------------
This codebase already has a SEPARATE, pre-existing SysEx subsystem: v1's
Cirklon remote-control COMMAND protocol (`F0 7D 6D 63 <cmd> [args] F7`,
parsed by `engine/sysex.py::parse_command`, dispatched by
`Engine._handle_sysex`). That subsystem only ever reacts to frames
addressed to midicrt specifically (`parse_command` returns `None`, a true
no-op, for anything else) and has nothing to do with recording/saving/
replaying arbitrary sysex traffic. THIS module is unrelated and additive:
`Engine._handle` records EVERY incoming sysex frame into the ring
regardless of whether it also happens to be a valid midicrt command (see
that method's own comment at the call site) -- the user-requested feature
is "record, save, and play sysex at will", not just midicrt's own command
channel.

Ring: bounded, in-memory, no I/O (mind Pi RAM)
---------------------------------------------------------------------------
`record_received()` is called from `Engine._handle`'s hot path (once per
incoming MIDI event) -- like `CaptureSink.record_event`, it must never
touch disk. `RING_SIZE` (20) bounds frame COUNT; `MAX_FRAME_BYTES` (64
KiB) additionally bounds per-frame memory -- a sysex frame is frequently a
multi-KB patch/bank dump (unlike every other MIDI message type this engine
handles, which is a handful of bytes), so an unbounded ring, or a ring of
frames with no per-frame ceiling, could let a single malfunctioning/
malicious device inflate this daemon's resident memory unboundedly on a
machine (a Raspberry Pi) with none to spare. Worst case with the shipped
defaults: `RING_SIZE * MAX_FRAME_BYTES` = 20 * 64KiB = 1.25 MiB, trivial
even on a Pi -- real-world sysex patch dumps are almost always well under
this per-frame ceiling. A frame over the ceiling is NOT silently dropped
entirely: its metadata (timestamp/source/size/manufacturer) still lands in
`recent()` with `"truncated": True`, so a user can still SEE that
something large arrived; only its raw bytes are discarded, so it can never
be `save()`d (a clear `ValueError`, not a silent no-op).

Library: named, on-disk, atomic writes, ENOSPC-safe, stage-don't-delete
---------------------------------------------------------------------------
`save()`/`play()`/`delete()` are cold-path, human-rate operations (a
browser click), unlike the ring above -- real disk I/O is fine here, same
"hot path never touches disk, cold path does" split `engine/capture.py`
already established for its own JSONL body vs `index.json`. `save()`
writes one `<name>.json` file per entry via `_atomic_write_json` (tempfile
in the SAME directory + `os.replace`, the identical pattern `engine/
capture.py::_atomic_write_json` uses for `index.json` -- a partial write
from a mid-write crash or an `OSError` never leaves a half-written library
file for `list_library()`/`play()` to trip over). `delete()` stages to a
`trash/` subdirectory (`os.replace`, a same-filesystem move, not a copy+
unlink) rather than actually removing the file -- this codebase's
established stage-don't-delete convention (mirrors `engine/capture.py`'s
own session retention sweep moving nothing at all destructively... actually
retention DOES delete; the closer precedent is this project's own
operating convention of never `rm`ing user data outright when a "move
aside" is just as cheap and reversible by hand).

`ENOSPC must not kill the engine loop` (task brief): `save()`/`delete()`
can raise `OSError` (a full disk, a read-only filesystem) exactly like
`CaptureSink.flush()`/`.start()`/`.stop()` can -- this module does NOT
catch it itself (matching `CaptureSink`'s own "let it propagate, the
CALLER converts to ActionError" contract, see that module's docstring's
"Write-failure containment" section): `Engine._sysex_save_action`/
`_sysex_delete_action` (engine/core.py) catch `OSError` and raise a clean
`ActionError` instead, the SAME pattern `Engine._capture_start_action`/
`_capture_stop_action` already use, so a full disk degrades one client
request to a clean error message rather than tearing down a connection or
(worse) escaping `Engine.run()`'s own tick loop the way an unguarded
`CaptureSink.flush()` once did (see that module's docstring for the
incident this pattern now defends against by construction, in every new
disk-writing call site, not just capture's own).

Name sanitization: path traversal must be structurally impossible
---------------------------------------------------------------------------
`sanitize_name()` is a strict ALLOWLIST (`[A-Za-z0-9_-]` for the first
character, `[A-Za-z0-9 _.-]` after, 1-64 chars) -- `/`, `\\`, and a leading
`.` are never even reachable through the charset, so `"../../etc/passwd"`
is rejected outright (it contains `/`) long before any path-joining
happens. `_resolve_within()` is a SECOND, independent belt-and-suspenders
check (same two-layer-defense shape `engine/midi_out.py`'s own self-
subscription filter uses): after building the candidate path, it resolves
symlinks (`os.path.realpath`) and verifies the result still lives inside
the library directory, catching any FUTURE loosening of the charset a
reviewer might not immediately connect back to this exact traversal
concern. Both layers are exercised directly by tests/test_sysex_store.py's
own traversal-attempt tests.

Manufacturer-ID decode: best-effort, not authoritative
---------------------------------------------------------------------------
`_decode_manufacturer()` is a SMALL, deliberately incomplete table of MIDI
System Exclusive manufacturer IDs this codebase can verify with high
confidence (the three MMA-reserved special IDs -- `0x7D` non-commercial/
private-use, the SAME prefix `engine/sysex.py`'s own docstring already
cites for midicrt's own command protocol; `0x7E`/`0x7F` universal non-
realtime/realtime -- plus a handful of very well-known 1-byte commercial
IDs: Roland `0x41`, Korg `0x42`, Yamaha `0x43`, Kawai `0x40`) and the
`0x00`-prefixed 3-byte EXTENDED ID mechanism (decoded generically as
`ext:HHHH`, not name-resolved -- this module does not carry a full
extended-ID table). Anything else renders as its raw hex byte
(`"0x99"`) rather than guessing -- the task brief's own "if cheap"
qualifier is read here as "cheap AND honest": a wrong manufacturer name
would be actively misleading in a diagnostic display, whereas a bare hex
byte is always correct, if less friendly.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)

# See module docstring's "Storage location" precedent (engine/capture.py's
# own DEFAULT_STATE_DIR/DEV_FALLBACK_STATE_DIR) -- both read LIVE (module-
# attribute lookup, not captured at import time) by `resolve_sysex_dir`, so
# tests/conftest.py's autouse fixture can monkeypatch both for the whole
# suite's duration, the identical isolation story capture.py's own
# constants already have. A SIBLING of capture's `/var/lib/midicrt/
# sessions` under the SAME systemd `StateDirectory=midicrt` (task brief) --
# no packaging change is needed, `StateDirectory=` grants write access to
# the whole `/var/lib/midicrt` tree, not just the `sessions` leaf.
DEFAULT_SYSEX_DIR = "/var/lib/midicrt/sysex"
DEV_FALLBACK_SYSEX_DIR = os.path.expanduser("~/.local/state/midicrt/sysex")

TRASH_SUBDIR = "trash"

# See module docstring's "Ring" section for the memory-budget reasoning.
RING_SIZE = 20
MAX_FRAME_BYTES = 65536   # 64 KiB per-frame ceiling on what the ring KEEPS

# v1's `plugins/loopprogress.py::SYSEX_DISPLAY_SECS` -- ported verbatim
# (see docs task-5-report.md's v1-extraction section): how long the
# chrome-row status text stays visible after the last sysex activity
# (received / saved / played / deleted) before reverting to blank.
SYSEX_DISPLAY_SECS = 5.0

_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9 _.-]{0,62}$")
_MAX_NAME_LEN = 64

_MANUFACTURER_NAMES = {
    0x7D: "Non-Commercial",
    0x7E: "Universal Non-Realtime",
    0x7F: "Universal Realtime",
    0x41: "Roland",
    0x42: "Korg",
    0x43: "Yamaha",
    0x40: "Kawai",
}


def resolve_sysex_dir(configured: str | None = None) -> str:
    """`configured` (`Config.sysex_dir`) wins outright when set. Otherwise:
    `DEFAULT_SYSEX_DIR` if its PARENT directory already exists and is
    writable (the systemd `StateDirectory=midicrt` case), else
    `DEV_FALLBACK_SYSEX_DIR` -- identical shape to `engine/capture.py::
    resolve_capture_dir`, see that function's own docstring. A pure check,
    never creates anything itself."""
    if configured:
        return configured
    parent = os.path.dirname(os.path.normpath(DEFAULT_SYSEX_DIR)) or "/"
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return DEFAULT_SYSEX_DIR
    return DEV_FALLBACK_SYSEX_DIR


def sanitize_name(name: Any) -> str:
    """Strict allowlist -- see module docstring's "Name sanitization"
    section for why this alone already makes `/`/`\\`/a leading `.`
    unreachable (path traversal, `"../../etc/x"`, is rejected here purely
    because it contains `/`, long before any path is ever built)."""
    if not isinstance(name, str):
        raise ValueError(f"sysex name must be a string, got {name!r}")  # noqa: TRY004
    stripped = name.strip()
    if not stripped or len(stripped) > _MAX_NAME_LEN or not _NAME_RE.match(stripped):
        raise ValueError(f"invalid sysex name: {name!r}")
    return stripped


def _decode_manufacturer(data: tuple[int, ...]) -> str:
    if not data:
        return "?"
    first = data[0]
    if first == 0x00 and len(data) >= 3:
        return f"ext:{data[1]:02X}{data[2]:02X}"
    return _MANUFACTURER_NAMES.get(first, f"0x{first:02X}")


def _atomic_write_json(path: str, payload: Any) -> None:
    """Same tempfile-in-same-directory + `os.replace` atomic pattern as
    `engine/capture.py::_atomic_write_json` (see that function's own
    docstring) -- legitimate here for the identical reason: a library save
    is a cold-path, human-rate operation (a browser click), never once per
    MIDI event."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".sysex-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


@dataclass
class _RingFrame:
    ts: float
    source: str
    size: int
    manufacturer: str
    data: tuple[int, ...] | None   # None == dropped, too large (see MAX_FRAME_BYTES)


class SysexStore:
    """Owns one ring of recently-received frames + one library directory.
    `Engine` owns exactly one instance (`self._sysex_store`,
    engine/core.py)."""

    def __init__(
        self,
        *,
        library_dir: str | None = None,
        ring_size: int = RING_SIZE,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        display_secs: float = SYSEX_DISPLAY_SECS,
    ) -> None:
        self._dir = resolve_sysex_dir(library_dir)
        self._ring_size = max(1, int(ring_size))
        self._max_frame_bytes = max(0, int(max_frame_bytes))
        self._display_secs = max(0.0, float(display_secs))
        self._ring: deque[_RingFrame] = deque(maxlen=self._ring_size)
        # Loopprogress-style chrome status (module docstring) -- `None`
        # until the very first activity of any kind (received/saved/
        # played/deleted). `status_active(now)` is what `_SysexStatusOverlay.
        # tick()` (engine/core.py) consults every engine tick.
        self.status_text: str | None = None
        self._status_ts: float = 0.0

    @property
    def dir(self) -> str:
        return self._dir

    # -- ring: hot path, in-memory only, no I/O --------------------------------

    def record_received(self, ev) -> None:
        """Called from `Engine._handle` for EVERY `type == "sysex"` event,
        regardless of whether it also parses as a midicrt command (see
        module docstring) -- must never touch disk. `ev.ts` (not a live
        clock read) stamps the status text, keeping this store on the SAME
        clock domain as the event itself (also correct, harmlessly, when
        this fires during a session replay -- see docs/phase5-capture.md
        §6's "SysEx is the one exception" precedent this module's ring
        recording extends: a pure in-memory append, no real I/O, safe to
        replay exactly like `_handle_sysex`'s own page-switch dispatch)."""
        data = tuple(ev.sysex_data or ())
        size = len(data)
        manufacturer = _decode_manufacturer(data)
        keep = data if size <= self._max_frame_bytes else None
        self._ring.appendleft(_RingFrame(
            ts=ev.ts, source=ev.source or "", size=size,
            manufacturer=manufacturer, data=keep))
        if keep is None:
            self.status_text = f"sx: rx {size}B dropped (too large)"
        else:
            self.status_text = f"sx: rx {size}B {manufacturer}"
        self._status_ts = ev.ts

    def status_active(self, now: float) -> bool:
        """`True` iff there has been SOME sysex activity (received / saved
        / played / deleted) within the last `display_secs` seconds of
        `now` -- the loopprogress-style decay window (module docstring),
        matching v1's own `SYSEX_DISPLAY_SECS` gate exactly."""
        return self.status_text is not None and (now - self._status_ts) < self._display_secs

    def recent(self) -> list[dict]:
        """Newest-first (index 0 == most recently received) -- matches the
        intuitive "the thing I just saw" browser UX `sysex.save {name,
        index}`'s own `index` argument keys off of."""
        return [
            {"index": i, "ts": f.ts, "source": f.source, "size": f.size,
             "manufacturer": f.manufacturer, "truncated": f.data is None}
            for i, f in enumerate(self._ring)
        ]

    def _ring_frame(self, index: int) -> _RingFrame:
        try:
            if index < 0:
                raise IndexError(index)
            return self._ring[index]
        except IndexError as exc:
            raise ValueError(f"no recent sysex frame at index {index}") from exc

    # -- library: cold path, real disk I/O -------------------------------------

    def _resolve_within(self, base_dir: str, filename: str) -> str:
        """Defense-in-depth belt-and-suspenders on top of `sanitize_name`'s
        own charset allowlist -- see module docstring's "Name
        sanitization" section."""
        candidate = os.path.realpath(os.path.join(base_dir, filename))
        base_real = os.path.realpath(base_dir)
        if candidate != base_real and not candidate.startswith(base_real + os.sep):
            raise ValueError(f"refusing to resolve outside the sysex library dir: {filename!r}")
        return candidate

    def _library_path(self, safe_name: str) -> str:
        return self._resolve_within(self._dir, f"{safe_name}.json")

    def _trash_path(self, safe_name: str) -> str:
        return self._resolve_within(os.path.join(self._dir, TRASH_SUBDIR), f"{safe_name}.json")

    def save(self, name: str, index: int = 0, *, now: float | None = None) -> dict:
        """Persists the ring frame at `index` (0 == most recent, see
        `recent()`) under `name` -- atomic write (see module docstring).
        Raises `ValueError` for an invalid name, an out-of-range index, or
        a frame that was too large to keep in memory (`recent()`'s own
        `"truncated": True` flag) -- can also raise a real `OSError`
        (ENOSPC etc.), deliberately NOT caught here (module docstring's
        "ENOSPC must not kill the engine loop" section: the ENGINE-level
        caller converts it to `ActionError`, this store stays a thin,
        honest wrapper around the filesystem)."""
        safe = sanitize_name(name)
        frame = self._ring_frame(index)
        if frame.data is None:
            raise ValueError(
                f"recent frame at index {index} was too large to keep in memory "
                f"({frame.size}B > {self._max_frame_bytes}B) -- cannot save it")
        saved_ts = now if now is not None else time.time()
        payload = {
            "name": safe, "saved_ts": saved_ts, "source": frame.source,
            "size": frame.size, "manufacturer": frame.manufacturer,
            "data": list(frame.data),
        }
        _atomic_write_json(self._library_path(safe), payload)
        self.status_text = f"sx: saved '{safe}' ({frame.size}B)"
        self._status_ts = saved_ts
        return {"name": safe, "size": frame.size}

    def list_library(self) -> list[dict]:
        """Scans `self._dir` for `*.json` files (the `trash/` subdirectory
        itself never matches -- it's a directory, `os.path.isfile` skips
        it). A malformed/corrupt entry is logged and SKIPPED, never fatal
        -- same "an optional file's per-entry corruption doesn't crash the
        daemon" discipline `engine/capture.py::_load_index`/`engine/
        bindings.py`'s own per-binding parsing already establish."""
        if not os.path.isdir(self._dir):
            return []
        rows: list[dict] = []
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                if not isinstance(doc, dict):
                    raise ValueError("library entry is not a JSON object")  # noqa: TRY004
                rows.append({
                    "name": doc.get("name", fname[:-len(".json")]),
                    "saved_ts": doc.get("saved_ts"),
                    "size": doc.get("size", len(doc.get("data") or [])),
                    "manufacturer": doc.get("manufacturer", "?"),
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                _LOG.warning("sysex: skipping unreadable library entry %s: %s", path, exc)
        return rows

    def play(self, name: str, midi_out, *, now: float | None = None) -> dict:
        """Loads `name`'s saved frame and sends it via `midi_out.
        send_sysex(data)` (the SAME shared, self-subscription-guarded
        `MidiOutput` every other real send in this engine uses -- see
        `engine/midi_out.py`'s own module docstring). Raises `ValueError`
        for an unknown/invalid name or a corrupt library file (never lets
        a bad on-disk entry crash the caller); `midi_out.send_sysex`'s own
        `bool` return (never raises, per that class's contract) becomes
        this method's `"sent"` field."""
        safe = sanitize_name(name)
        path = self._library_path(safe)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError as exc:
            raise ValueError(f"unknown sysex library entry: {safe!r}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"sysex library entry {safe!r} is unreadable/corrupt: {exc}") from exc
        data = tuple(doc.get("data") or ())
        sent = midi_out.send_sysex(data)
        play_ts = now if now is not None else time.time()
        self.status_text = (f"sx: play '{safe}' ({len(data)}B)" if sent
                            else f"sx: play '{safe}' failed")
        self._status_ts = play_ts
        return {"sent": sent, "size": len(data)}

    def delete(self, name: str, *, now: float | None = None) -> dict:
        """Stages `name` to `library_dir/trash/` (an `os.replace` move,
        NOT a real delete -- stage-don't-delete) rather than removing it.
        Raises `ValueError` for an unknown/invalid name; can also raise a
        real `OSError` (same containment contract as `save()`, see that
        method's own docstring). A second delete of the same name
        overwrites whatever was previously in `trash/` under that name --
        `trash/` is a "moved aside" landing spot, not a versioned
        archive."""
        safe = sanitize_name(name)
        src = self._library_path(safe)
        if not os.path.isfile(src):
            raise ValueError(f"unknown sysex library entry: {safe!r}")
        dest = self._trash_path(safe)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(src, dest)
        self.status_text = f"sx: deleted '{safe}'"
        self._status_ts = now if now is not None else time.time()
        return {"deleted": True}
