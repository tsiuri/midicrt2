"""Capture-store maintenance (Phase 9 Task 6, "capture editor"): the
`midicrt sessions` CLI subcommands (`list`/`show`/`trim`/`repair-index`/
`delete`) AND the engine's own read-only `capture.sessions_list`/
`capture.sessions_show` actions (wired for the web panel, engine/core.py)
share EXACTLY the pure functions in this module -- one implementation, two
callers. The CLI operates directly on a resolved `capture_dir` string with
no running `Engine`/daemon required at all (matching `engine/replay.py`'s
own "no daemon needed" shape for `midicrt replay`); the two engine actions
pass `self._capture.dir` plus this engine's own live-recording status.

This module never duplicates `CaptureSink`'s own writer -- hot-path
recording (the append-only `.jsonl` body) is still exclusively
`CaptureSink`'s job (engine/capture.py). Everything here only ever touches
a STOPPED session's files (read, stage-to-trash, or extract-to-a-new-file)
or creates a brand new file (`trim_session`) -- see docs/phase5-capture.md
for the on-disk format (`header`/`event`/`action`/`tempo`/`page_changed`
lines, `index.json`) this module reads/writes.

Liveness: "the engine knows its active session", not mtime guesswork
---------------------------------------------------------------------------
Every function that could destructively touch a still-open session
(`trim_session`, `delete_session`, and -- defensively -- `repair_index`)
takes an explicit `live_session_id: str | None` parameter rather than
inferring liveness from the filesystem: an actively-recorded session's
`.jsonl` file looks, from a bare `stat()`, identical to a crashed/
abandoned one with a recent mtime -- there is no honest way to tell
"still open for append by a live daemon" from "abandoned mid-write" by
inspecting the file alone (task-6-brief.md's own binding constraint:
"determine liveness honestly, not by mtime guesswork"). The CALLER
resolves this id however fits its own vantage point:

  - `clients/cli.py`'s `sessions` subcommand asks the daemon over the SAME
    socket protocol every other subcommand already uses
    (`capture.status`) -- best-effort: an unreachable daemon (not running,
    or pointed at a different `capture_dir` entirely) means "nothing is
    live for THIS store", not an error (see cli.py's own
    `_live_recording_session_id` docstring for the full reasoning).
  - `Engine._capture_sessions_list_action`/`_show_action` (engine/core.py)
    just read `self._capture.is_recording`/`.session_id` directly -- it
    IS the live engine, no socket round-trip needed.

`list_sessions`/`show_session` also accept the SAME parameter, purely to
label the currently-recording entry accurately (`"status": "recording"`
instead of `"orphan"`) -- passing `None` there never blocks anything, it
only affects a cosmetic label.

Why `_atomic_write_json` is a LOCAL copy, not imported from capture.py
---------------------------------------------------------------------------
`engine/capture.py` and `engine/sysex_store.py` each already carry their
OWN identical copy of this exact tempfile+`os.replace` helper (verified by
reading both) rather than one importing it from the other -- the
established convention in this codebase for a small, self-contained
utility like this is to duplicate it per-module, not reach into another
module's private (`_`-prefixed) API. This module follows that same
precedent. `replay.py::_iter_lines` is the one deliberate EXCEPTION (used
by `trim_session`/`repair_index` below, via a lazy, function-scoped
import) -- unlike the trivial atomic-write helper, `_iter_lines` carries
real, already-tested behavior (malformed-JSON and non-object-JSON lines
are logged and skipped, never fatal) that would be a genuine logic
duplication risk to re-derive here. The import is lazy (inside each
function, not at module top) specifically to avoid a circular import:
`replay.py` imports `engine.core` at module scope, and `engine.core` is
about to import THIS module at module scope for the two new actions --
mirrors `clients/cli.py::_handle_replay`'s own "lazy: pulls in engine
.core's roster" comment exactly.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from typing import Any

from midicrt.engine.capture import FORMAT_VERSION, INDEX_FILE

_LOG = logging.getLogger(__name__)

TRASH_SUBDIR = "trash"


class LiveSessionError(ValueError):
    """Raised by `trim_session`/`delete_session` when `session_id` is the
    daemon's currently-recording session -- a `ValueError` SUBCLASS (not a
    bare new `Exception`) so every existing "except ValueError"-shaped
    error-to-exit call site (cli.py's own error handling, a future engine
    action wrapper) keeps working unchanged with zero extra code; a
    distinct subclass FROM plain `ValueError` so a caller that specifically
    cares about THIS refusal (vs. e.g. "unknown session id") can `except
    LiveSessionError` narrowly if it ever needs to."""


class PinnedSessionError(ValueError):
    """Raised by `delete_session` for a pinned session -- same
    ValueError-subclass shape as `LiveSessionError`, for the same reason."""


class UnknownSessionError(ValueError):
    """Raised when `session_id` has neither an index row nor a session
    file on disk."""


# -- shared path/IO helpers --------------------------------------------------

def _session_path(capture_dir: str, session_id: str) -> str:
    return os.path.join(capture_dir, f"{session_id}.jsonl")


def _trash_path(capture_dir: str, session_id: str) -> str:
    return os.path.join(capture_dir, TRASH_SUBDIR, f"{session_id}.jsonl")


def _read_header(path: str) -> dict:
    """Best-effort: the first line of a session file, or `{}` if the file
    is missing/empty/unreadable/its first line isn't a JSON object --
    mirrors `CaptureSink._rebuild_index_from_disk`'s own "read the header
    back" trick (see that method's docstring), just factored out here for
    reuse across this module's own functions."""
    try:
        with open(path, encoding="utf-8") as f:
            first_line = f.readline()
        if not first_line.strip():
            return {}
        doc = json.loads(first_line)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_index_rows(capture_dir: str) -> list[dict]:
    """Read-only, tolerant load of `index.json`. Unlike `CaptureSink.
    _load_index`, this NEVER rewrites the file on a malformed read -- a
    plain `list`/`show` must never have a mutating side effect just from
    being asked a question. A missing file is silently `[]`; a malformed
    one is logged and ALSO treated as `[]` (never guessed-and-persisted
    here -- `repair_index`, below, is the one function in this module
    explicitly about correcting a malformed/drifted index.json on disk)."""
    path = os.path.join(capture_dir, INDEX_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"index.json must be a list, got {type(data).__name__}")  # noqa: TRY004
        return [row for row in data if isinstance(row, dict) and row.get("id")]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _LOG.warning("sessions: index.json malformed (%s); treating as empty for this read "
                    "(run `midicrt sessions repair-index` to fix it on disk)", exc)
        return []


def _atomic_write_json(path: str, payload: Any) -> None:
    """Tempfile-in-same-directory + `os.replace` -- see module docstring's
    "local copy, not imported" section for why this duplicates capture.py/
    sysex_store.py's own identical helper rather than importing either."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".sessions-index-", suffix=".json.tmp", dir=directory)
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


def _write_index_rows(capture_dir: str, rows: list[dict]) -> None:
    _atomic_write_json(os.path.join(capture_dir, INDEX_FILE), rows)


_INDEX_LOCK_FILE = ".index.lock"


@contextlib.contextmanager
def _index_write_lock(capture_dir: str):
    """Advisory `fcntl.flock` (exclusive) held for the ENTIRE read-modify-
    write span of an `index.json` mutation -- review-round fix (Important
    finding, live-reproducible race): every index-mutating function in
    THIS module (`trim_session`, `delete_session`, `repair_index`) used to
    `_read_index_rows`/`_write_index_rows` with NO lock at all, and so
    does `CaptureSink`'s own `_update_index_on_stop`/`pin`/`fail`/
    `_sweep_retention` (engine/capture.py) -- production has
    `capture_auto_start` LIVE, so the daemon can call any of those at
    session-boundary moments completely independent of an operator
    running `midicrt sessions delete/trim/repair-index` at the same time.
    Without a shared lock, two concurrent read-then-write cycles can
    interleave (`sessions.py` reads rows A, daemon reads rows A, daemon
    appends+writes A+new, `sessions.py` finishes and writes its OWN
    edit of A -- silently erasing the daemon's just-added row) -- a real,
    silent DATA-LOSS bug, not a cosmetic one, given task-6-brief.md's own
    "concurrent-safety and production-store byte-integrity are
    contractual" framing.

    `engine/capture.py` carries an IDENTICAL copy of this exact context
    manager (same name, same `.index.lock` filename, same directory
    convention: `<capture_dir>/.index.lock`) -- see this module's own
    docstring's "why `_atomic_write_json` is a local copy" section for
    why duplicating SMALL helpers across these two modules is this
    codebase's established convention; what actually matters for
    correctness here is that BOTH copies lock the SAME file, which they
    do by construction (both key off `capture_dir`, the one value every
    caller on both sides already agrees on). `fcntl.flock` (not a
    separate lockfile-as-mutex library) because both lockers are always
    on the SAME host, SAME local filesystem (`docs/phase5-capture.md`'s
    own storage-location section -- `/var/lib/midicrt/sessions` or the
    dev fallback, never network storage), where POSIX advisory locking is
    correct and needs no extra dependency; NFS's well-known flock
    unreliability is not a concern this codebase's storage model ever
    triggers.

    Read-only callers (`list_sessions`/`show_session`/`CaptureSink.
    status`/`_load_index`'s own plain reads) deliberately do NOT take this
    lock -- `os.replace()`'s own atomicity (both `_atomic_write_json`
    implementations use tempfile+`os.replace`) already guarantees any
    concurrent reader sees either the fully-old or fully-new document,
    never a torn/partial one; the lock exists ONLY to serialize
    WRITER-vs-WRITER read-modify-write races, which a read alone can never
    cause."""
    os.makedirs(capture_dir, exist_ok=True)
    lock_path = os.path.join(capture_dir, _INDEX_LOCK_FILE)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_jsonl_atomic(path: str, lines: list[dict]) -> None:
    """Same atomic tempfile+`os.replace` shape as `_atomic_write_json`
    above, adapted for a multi-line JSONL body (`trim_session`'s output) --
    a mid-write crash/`OSError` can never leave a half-written `.jsonl`
    file at the FINAL path for `sessions list`/`show`/a later replay to
    trip over; the tempfile is cleaned up on any failure instead."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".session-", suffix=".jsonl.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _scan_session_files(capture_dir: str) -> list[str]:
    """Non-recursive `*.jsonl` filenames' session ids, sorted -- mirrors
    `CaptureSink._rebuild_index_from_disk`'s own directory scan. Never
    descends into `trash/`: a subdirectory name can never match the
    `.jsonl` suffix filter, and `os.listdir` doesn't recurse anyway."""
    if not os.path.isdir(capture_dir):
        return []
    return sorted(name[: -len(".jsonl")] for name in os.listdir(capture_dir)
                 if name.endswith(".jsonl"))


def _check_not_live(session_id: str, live_session_id: str | None, verb: str) -> None:
    if live_session_id is not None and session_id == live_session_id:
        raise LiveSessionError(
            f"refusing to {verb} {session_id!r}: it is the daemon's CURRENTLY-RECORDING "
            "session (stop it first with `capture.stop`, or wait for it to finish)")


# -- list ---------------------------------------------------------------------

def list_sessions(capture_dir: str, *, live_session_id: str | None = None) -> dict:
    """Index view (task brief: id/started/ended/size/event counts/pinned
    flag), healed with an HONEST view of drift against the files actually
    on disk -- never crashes on a missing file for an index row, or an
    orphan file with no row (task brief's own wording). Every row carries
    a `"status"` field, one of:

      - `"finished"`: a normal index row whose `.jsonl` file exists.
      - `"missing_file"`: an index row whose `.jsonl` file is GONE (an
        operator removed it by hand, or a bug) -- `size` is `None`.
      - `"recording"`: a `.jsonl` file with NO index row (nothing has ever
        stopped it) that IS the daemon's live session right now
        (`live_session_id` match) -- `started_ts` read from the file's own
        header, `ended_ts: None`, `pinned: False` (an in-progress session
        cannot be pinned, see `CaptureSink.pin`'s own docstring).
      - `"orphan"`: a `.jsonl` file with no index row that is NOT the live
        session -- almost always a crash/`SIGKILL` survivor (see engine/
        capture.py's own "loss window" docs) or (more rarely) a
        `sessions.trim` output whose index-write step failed after the
        file itself landed. Same shape as `"recording"` otherwise.

    This function performs NO writes -- `repair_index` (below) is the
    separate, explicit operation that actually heals `index.json` on disk.
    Sorted newest-first (`started_ts` descending; a row with no readable
    `started_ts` sorts as if it were `0.0`, i.e. oldest)."""
    rows_by_id = {r["id"]: r for r in _read_index_rows(capture_dir)}
    file_ids = set(_scan_session_files(capture_dir))
    out: list[dict] = []
    for session_id, row in rows_by_id.items():
        exists = session_id in file_ids
        path = _session_path(capture_dir, session_id)
        out.append({
            "id": session_id,
            "started_ts": row.get("started_ts"),
            "ended_ts": row.get("ended_ts"),
            "size": os.path.getsize(path) if exists else None,
            "counts": dict(row.get("counts") or {}),
            "pinned": bool(row.get("pinned", False)),
            "status": "finished" if exists else "missing_file",
        })
    for session_id in sorted(file_ids - set(rows_by_id)):
        path = _session_path(capture_dir, session_id)
        header = _read_header(path)
        is_live = live_session_id is not None and session_id == live_session_id
        out.append({
            "id": session_id,
            "started_ts": header.get("started_ts"),
            "ended_ts": None,
            "size": os.path.getsize(path),
            "counts": {},
            "pinned": False,
            "status": "recording" if is_live else "orphan",
        })
    out.sort(key=lambda r: r["started_ts"] if r["started_ts"] is not None else 0.0, reverse=True)
    return {"capture_dir": capture_dir, "sessions": out}


# -- show -----------------------------------------------------------------

def show_session(capture_dir: str, session_id: str, *, live_session_id: str | None = None,
                 replay_fn: Callable[[str], dict] | None = None) -> dict:
    """Summary via the REPLAY engine's own summarizer (`engine/replay.py::
    replay_session`, reused verbatim -- event counts by kind
    (`events_by_type`), provenance-origin counts (`actions_by_origin`),
    `marks_by_kind` -- no NEW event-scanning logic is written here). This
    also doubles as a "the file is replayable" honesty check for every
    session shown, not just a freshly-trimmed one: a truncated/corrupted
    file surfaces here as a visibly short/empty summary via replay's own
    tolerant line-skipping, not a crash.

    Session-level facts replay has no reason to know -- id/`started_ts`/
    `ended_ts`/`duration_s`/`pinned`/`size`/whether this is the live
    session -- come from `index.json` (or, for a not-yet-stopped session,
    a header peek) instead of any new summarization pass. "First/last
    timestamp" (task brief) is read here as the SESSION's own
    `started_ts`/`ended_ts` (i.e. the header write time / the `stop()`
    time) -- not a second, redundant per-EVENT ts scan replay's own
    `events_by_type` counting pass didn't already need to do.

    `replay_fn` is injectable (defaults to a thin wrapper around
    `engine.replay.replay_session(path, instant=True)`, lazily imported --
    see module docstring's "why `_iter_lines` is the one exception"
    section for the identical circular-import reason) purely so tests can
    avoid paying a real offline-`Engine` construction cost when they only
    care about THIS function's own session-level bookkeeping, mirroring
    `replay.py`'s own `sleep_fn` injection convention.

    Raises `UnknownSessionError` if `session_id` has neither an index row
    nor a `.jsonl` file."""
    if replay_fn is None:
        def replay_fn(path: str) -> dict:
            from midicrt.engine.replay import replay_session  # lazy: see module docstring
            return replay_session(path, instant=True)

    path = _session_path(capture_dir, session_id)
    row = next((r for r in _read_index_rows(capture_dir) if r.get("id") == session_id), None)
    file_exists = os.path.exists(path)
    if row is None and not file_exists:
        raise UnknownSessionError(f"unknown session: {session_id!r}")

    is_live = live_session_id is not None and session_id == live_session_id
    if row is not None:
        started_ts = row.get("started_ts")
        ended_ts = row.get("ended_ts")
        pinned = bool(row.get("pinned", False))
        status = "finished" if file_exists else "missing_file"
    else:
        header = _read_header(path)
        started_ts = header.get("started_ts")
        ended_ts = None
        pinned = False
        status = "recording" if is_live else "orphan"

    duration_s = (ended_ts - started_ts
                 if started_ts is not None and ended_ts is not None else None)

    result: dict[str, Any] = {
        "id": session_id,
        "started_ts": started_ts,
        "ended_ts": ended_ts,
        "duration_s": duration_s,
        "pinned": pinned,
        "size": os.path.getsize(path) if file_exists else None,
        "status": status,
        "replay": None,
    }
    if not file_exists:
        return result

    header = _read_header(path)
    derived_from = header.get("derived_from")
    if derived_from:
        result["derived_from"] = derived_from

    replay_summary = replay_fn(path)
    result["replay"] = {
        "events_total": replay_summary.get("events_total"),
        "events_by_type": replay_summary.get("events_by_type"),
        "actions_by_origin": replay_summary.get("actions_by_origin"),
        "marks_by_kind": replay_summary.get("marks_by_kind"),
    }
    return result


# -- trim -------------------------------------------------------------------

def _new_trim_session_id(now: float) -> str:
    """Same `session-<YYYYMMDD-HHMMSS>-<uuid8>` shape `CaptureSink.start()`
    generates (`time.strftime` + `uuid.uuid4().hex[:8]`) -- a trimmed
    session is visually indistinguishable, in `sessions list`, from an
    ordinary recorded one. `now` is the injected clock (see this
    function's one caller, `trim_session`) rather than a bare `time.time()`
    call, so tests get a deterministic id."""
    return f"session-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{uuid.uuid4().hex[:8]}"


def trim_session(capture_dir: str, session_id: str, from_s: float, to_s: float, *,
                 live_session_id: str | None = None,
                 now_fn: Callable[[], float] = time.time,
                 id_fn: Callable[[float], str] = _new_trim_session_id) -> dict:
    """Time-range extract to a NEW session file -- the SOURCE file is
    opened read-only and never modified (see task-6-report.md for the live
    checksum proof this holds against the real production store).

    Time semantics: SESSION-RELATIVE seconds. `from_s`/`to_s` are offsets
    from the SOURCE session's own `started_ts` (its header's `started_ts`
    field -- i.e. `0.0` == the instant `capture.start` was called for the
    source), never wall-clock epoch seconds. `to_s` may exceed the
    session's actual recorded span -- "gracefully" means simply "fewer/
    zero lines match", never an error: the exact extent of a session's
    content isn't always cheaply knowable up front (e.g. for the
    currently-recording session, though see the `LiveSessionError` refusal
    below -- this also matters for a STOPPED session an operator hasn't
    separately inspected first).

    Refuses (raises, never mutates anything on a refusal):
      - `LiveSessionError` if `session_id == live_session_id` (binding
        constraint: never read-and-extract from a session the daemon might
        still be appending to concurrently).
      - `ValueError` for a non-numeric/NaN `from_s`/`to_s`, `from_s < 0`
        (a session has no meaningful negative offset), or `to_s <= from_s`
        (an inverted or empty/zero-width range).
      - `UnknownSessionError` if `session_id` has no `.jsonl` file at all
        (an index row alone, with no file, has nothing to extract FROM).
      - `ValueError` if the source file has no readable header/
        `started_ts` (can't compute a session-relative window without it).

    New session id / provenance: `id_fn`/`now_fn` are injected (mirrors
    `replay.py`'s own `sleep_fn` convention) so tests get a deterministic
    id instead of asserting against a live wall-clock/uuid4 value.
    Provenance ("derived-by-trim from <id> with the range", task brief) is
    carried ENTIRELY in the new file's own HEADER
    (`"derived_from": {"session_id", "trim_from_s", "trim_to_s",
    "trimmed_at"}`) -- deliberately NOT as a synthetic `action`/
    `page_changed` MARK line: every documented mark `kind` (docs/
    phase5-capture.md) is a real-time engine fact with a defined `origin`
    vocabulary; this is post-hoc file-surgery metadata, a different kind
    of fact entirely, and belongs in the one place a session already
    carries build-time-only facts -- its header (`engine_version`,
    `instruments`).

    Boundary-state synthesis for notes sustained across `--from`
    (SECOND review round, Important finding, live-reproduced): a note
    turned on BEFORE the window and still held (no `note_off` yet) AT
    `abs_start` used to simply vanish -- its `note_on` falls outside the
    kept window (excluded, correctly, by the plain `abs_start <= ts <=
    abs_end` filter), but its EVENTUAL `note_off` (occurring INSIDE the
    window) was still kept as a dangling, unmatched event. A replay of
    the trimmed file never saw the note turn on at all, so the note_off
    was silently absorbed as a no-op by every consumer that guards
    against "note-off with nothing held" -- undercounting voice/harmony
    state for the ENTIRE window (reviewer's own reproduction: `total_
    peak` read 1 instead of 2 for a session with one note already held
    across `--from`).

    Fix: a first pass over every line with `ts < abs_start` (i.e.
    strictly BEFORE the window -- see the boundary-exact note below)
    tracks, per `(channel, data1)`, whether the most recent `note_on`/
    `note_off` for that note leaves it ACTIVE (a `note_on` with no
    subsequent `note_off`) at the moment the window begins. For every
    note still active, a SYNTHETIC copy of its ORIGINAL `note_on` line is
    inserted at the very start of the kept output, with `"ts"` moved to
    `abs_start` and a NEW `"synthetic": true` field added -- preserving
    the note's real `channel`/`data1`/`data2` (velocity)/`source`, so a
    replay's voice/harmony state is exactly as accurate as if the window
    had genuinely started with that note already sounding (which is,
    honestly, what happened). `"synthetic": true` is the ONLY new,
    additive field on an "event" line this fix introduces -- documented
    in docs/phase5-capture.md's own "event" section; it NEVER appears on
    a real captured event, only on a trim-derived boundary-state note_on,
    so a consumer can always tell a synthesized event apart from a real
    one on inspection (task brief's own explicit requirement).

    Boundary-exact case (`--from` lands EXACTLY on a real `note_on`'s own
    `ts`): the pre-window scan only considers `ts < abs_start` (strictly
    less-than); a `note_on` with `ts == abs_start` falls into the KEPT
    window instead (`abs_start <= ts`, inclusive), so it is kept as a
    real event and never ALSO tracked as "still active before the
    window" -- no duplicate synthesis, by construction, not by a special
    case (see test_sessions.py's own boundary-exact test).

    Disclosed, deliberate scope limit: this fix covers `note_on`/
    `note_off` sustain ONLY (the reviewer's own reproduction, and the
    ONE stateful MIDI condition every current replay consumer -- voices/
    harmony -- actually derives from). Other potentially-"held-across-a-
    boundary" MIDI state (a sustain pedal, CC64, held down; an active
    pitch-bend; the current program/patch) is NOT synthesized by this
    fix -- a real, disclosed gap, not silently assumed away (see task-6-
    report.md §2.3/§7)."""
    _check_not_live(session_id, live_session_id, "trim")
    try:
        from_s = float(from_s)
        to_s = float(to_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--from/--to must be numbers: {exc}") from exc
    if math.isnan(from_s) or math.isnan(to_s):
        raise ValueError("--from/--to must not be NaN")
    if from_s < 0:
        raise ValueError(f"--from must be >= 0 (session-relative seconds), got {from_s!r}")
    if to_s <= from_s:
        raise ValueError(f"--to ({to_s!r}) must be greater than --from ({from_s!r})")

    src_path = _session_path(capture_dir, session_id)
    if not os.path.exists(src_path):
        raise UnknownSessionError(f"unknown session (no session file): {session_id!r}")
    src_header = _read_header(src_path)
    src_started_ts = src_header.get("started_ts")
    if src_started_ts is None:
        raise ValueError(f"session {session_id!r} has no readable header/started_ts "
                         "-- cannot compute a trim window")
    abs_start = src_started_ts + from_s
    abs_end = src_started_ts + to_s

    from midicrt.engine.replay import _iter_lines  # lazy: see module docstring

    kept: list[dict] = []
    counts: dict[str, int] = {}
    last_ts = abs_start
    # (channel, data1) -> the ORIGINAL note_on line, for any note that is
    # still held (no note_off seen yet) as of the last pre-window line
    # processed -- see this function's own "Boundary-state synthesis"
    # docstring section above.
    active_before_window: dict[tuple, dict] = {}
    for line in _iter_lines(src_path):
        if line.get("kind") == "header":
            continue
        ts = line.get("ts")
        if not isinstance(ts, int | float):
            continue
        if ts < abs_start:
            if line.get("kind") == "event":
                note_type = line.get("type")
                if note_type == "note_on":
                    active_before_window[(line.get("channel"), line.get("data1"))] = line
                elif note_type == "note_off":
                    active_before_window.pop((line.get("channel"), line.get("data1")), None)
            continue
        if ts > abs_end:
            continue
        kept.append(line)
        last_ts = max(last_ts, ts)
        if line.get("kind") == "event":
            event_type = line.get("type", "?")
            counts[event_type] = counts.get(event_type, 0) + 1

    # Synthesize boundary-state note_on events for anything still held at
    # abs_start -- inserted BEFORE every real kept line (sorted for
    # determinism; all share ts == abs_start so real replay/analyzer
    # ordering among them doesn't matter, only that they precede every
    # note_off that will legitimately turn them back off).
    synthesized = []
    for (channel, data1), original in sorted(
            active_before_window.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)):
        synth = dict(original)
        synth["ts"] = abs_start
        synth["synthetic"] = True
        synthesized.append(synth)
        counts["note_on"] = counts.get("note_on", 0) + 1
    kept = synthesized + kept

    now = now_fn()
    new_id = id_fn(now)
    header = {
        "kind": "header", "format": FORMAT_VERSION, "session_id": new_id,
        "started_ts": abs_start, "engine_version": src_header.get("engine_version", ""),
        "instruments": list(src_header.get("instruments") or []),
        "derived_from": {"session_id": session_id, "trim_from_s": from_s, "trim_to_s": to_s,
                         "trimmed_at": now},
    }
    dest_path = _session_path(capture_dir, new_id)
    _write_jsonl_atomic(dest_path, [header, *kept])

    # Locked (review-round fix -- see `_index_write_lock`'s own docstring):
    # the source file read above needs no lock (a STOPPED session's file
    # is never written again by anything, live or not -- `_check_not_live`
    # already ruled out "still recording"), but this index.json read-
    # modify-write race REALLY DOES with a concurrently-running daemon's
    # own `capture.start`/`.stop`/`.pin`/retention-sweep writes.
    with _index_write_lock(capture_dir):
        rows = [r for r in _read_index_rows(capture_dir) if r.get("id") != new_id]
        rows.append({"id": new_id, "started_ts": abs_start, "ended_ts": last_ts,
                    "counts": counts, "pinned": False})
        _write_index_rows(capture_dir, rows)

    return {"id": new_id, "source_id": session_id, "from_s": from_s, "to_s": to_s,
           "counts": counts, "events_kept": sum(counts.values()), "path": dest_path}


# -- repair-index -------------------------------------------------------------

def repair_index(capture_dir: str, *, live_session_id: str | None = None) -> dict:
    """Rebuilds `index.json` from what's ACTUALLY on disk, reconciling
    every drift class `list_sessions` can only REPORT (this is the one
    function in this module that actually WRITES a fix). Idempotent -- a
    second call against an already-repaired store reports everything as
    `"kept"`, nothing adopted/dropped (see test_sessions.py's own
    idempotency test).

      - A `.jsonl` file with an EXISTING, matching index row: kept
        VERBATIM. An operator-invoked repair must never throw away a
        stopped session's real `ended_ts`/`counts`/`pinned` state just
        because its file also happens to still be there -- unlike
        `CaptureSink._rebuild_index_from_disk`'s own crash-path recovery
        (which has no such row to preserve, and reports `counts: {}`/
        `ended_ts: None` as an honest "couldn't recover this" placeholder
        for every file it sees), THIS function has the chance to be
        choosier and simply leave a healthy row alone.
      - A `.jsonl` file with NO index row, and NOT the live session: an
        ORPHAN, ADOPTED. Unlike the crash-path recovery above, this scans
        the WHOLE file for real `counts` (per event `type`) and `ended_ts`
        (the last timestamped line seen) rather than reporting
        placeholders -- this is a deliberate, cold-path, human-invoked
        repair, not a hot construction-time fallback, so the extra full
        scan is an acceptable, disclosed cost.
      - A `.jsonl` file that IS `live_session_id`: SKIPPED -- never
        adopted, never re-derived. Touching it -- either adopting a
        synthetic "it ended here" row or overwriting whatever's already
        there -- would misrepresent a session that hasn't actually
        finished, and could let a later retention sweep (`CaptureSink.
        _sweep_retention`, which only ever looks at `index.json` rows)
        treat a still-recording session as a deletion candidate.
        Review-round fix (Minor finding): "skipped" means "leave exactly
        as found," not "drop" -- if an index row already happens to exist
        for this id (an edge case: ids are unique per capture in
        practice, but a hand-restored/edited `index.json` could contain
        one), that row is preserved in the rewritten index UNCHANGED,
        not silently erased just because this function chose not to
        inspect or update it. Reported under `"skipped_live"` either way.
      - An index row with NO matching `.jsonl` file: DROPPED -- a dead
        reference to a file that's gone.

    Returns `{"kept": [...ids], "adopted": [...ids], "dropped": [...ids],
    "skipped_live": [...ids]}` (task brief: "report what it did").

    Locked (review-round fix -- see `_index_write_lock`'s own docstring)
    for the function's ENTIRE body, including the per-orphan full-file
    scan below -- a real, disclosed trade-off: `repair-index` is a rare,
    human-invoked, cold-path maintenance operation, and holding the lock
    for its (bounded, but potentially multi-second against a large
    orphan) full duration is far simpler to reason about correctly than a
    two-phase "scan outside the lock, reconcile inside it" scheme, which
    would still need to handle a file appearing/vanishing between the two
    phases.

    The cost, corrected (SECOND review round -- the ORIGINAL text here
    claimed "the daemon's own MIDI tick loop is NOT gated on this at
    all," which was WRONG and reviewer-caught: before that round's own
    fix, a concurrently-running daemon's own `capture.start`/`.stop`/
    `.pin` (all reachable from `ActionRegistry.dispatch`, which runs
    every handler SYNCHRONOUSLY on the ONE asyncio event loop) really
    could block the WHOLE loop -- MIDI draining, ticks, rendering,
    every other client's request -- for up to however long a
    `repair-index` run held this lock, live-reproduced by the reviewer
    as a 2227ms stall from a 2.0s hold). Now: `CaptureSink`'s own
    engine-side lock acquisition (`_try_index_write_lock`, engine/
    capture.py) is BOTH bounded (`capture_mod.ENGINE_LOCK_TIMEOUT_S`)
    AND run off the event loop entirely (`asyncio.to_thread`, wired at
    `Engine._capture_start_action_dispatch`/`_capture_stop_action_
    dispatch`/`_capture_pin_action`, engine/core.py) -- so a concurrent
    `repair-index` run genuinely CANNOT block the daemon's tick loop,
    ever. What it CAN still do: delay the ONE client request waiting on
    `capture.start`/`.stop`/`.pin`'s own response by up to that bound
    (`capture.start`/`.stop` degrade gracefully instead of failing --
    see `CaptureSink.start`/`.stop`'s own docstrings; only `capture.pin`
    can report a clean busy `ActionError` if the bound is genuinely
    exceeded). Disclosed in task-6-report.md, §9 item 1 (second review
    round) for the full incident and fix."""
    from midicrt.engine.replay import _iter_lines  # lazy: see module docstring

    kept: list[str] = []
    adopted: list[str] = []
    skipped_live: list[str] = []
    rows: list[dict] = []
    with _index_write_lock(capture_dir):
        existing = {r["id"]: r for r in _read_index_rows(capture_dir)}
        file_ids = set(_scan_session_files(capture_dir))
        for session_id in sorted(file_ids):
            if live_session_id is not None and session_id == live_session_id:
                skipped_live.append(session_id)
                if session_id in existing:
                    rows.append(existing[session_id])   # preserve, untouched
                continue
            if session_id in existing:
                rows.append(existing[session_id])
                kept.append(session_id)
                continue
            path = _session_path(capture_dir, session_id)
            header = _read_header(path)
            counts: dict[str, int] = {}
            last_ts = header.get("started_ts")
            for line in _iter_lines(path):
                ts = line.get("ts")
                if isinstance(ts, int | float):
                    last_ts = ts if last_ts is None else max(last_ts, ts)
                if line.get("kind") == "event":
                    event_type = line.get("type", "?")
                    counts[event_type] = counts.get(event_type, 0) + 1
            rows.append({"id": session_id, "started_ts": header.get("started_ts"),
                        "ended_ts": last_ts, "counts": counts, "pinned": False})
            adopted.append(session_id)

        dropped = sorted(set(existing) - file_ids)
        _write_index_rows(capture_dir, rows)
    return {"kept": kept, "adopted": adopted, "dropped": dropped, "skipped_live": skipped_live}


# -- delete -------------------------------------------------------------------

def delete_session(capture_dir: str, session_id: str, *,
                   live_session_id: str | None = None) -> dict:
    """Stage-don't-delete: moves `<id>.jsonl` to `<capture_dir>/trash/`
    (`os.replace`, a same-filesystem move, not a copy+unlink) rather than
    actually removing it -- identical pattern to `engine/sysex_store.py::
    SysexStore.delete`. Drops the `index.json` row, if any.

    Refuses (raises, never mutates anything on a refusal):
      - `LiveSessionError` if this is the daemon's live session.
      - `PinnedSessionError` if the index row says `pinned: true` (task
        brief: "pinned sessions REFUSE deletion").
      - `UnknownSessionError` if there is neither an index row nor a file
        for `session_id`.

    Trash collisions: a second delete of the SAME session id overwrites
    whatever already sits in `trash/` under that name -- the identical,
    deliberate precedent `SysexStore.delete`'s own docstring states
    (`trash/` is a "moved aside" landing spot, not a versioned archive).
    In practice a real collision can only happen for a session id that's
    already unique per capture (a timestamp + a uuid8 suffix), so this is
    exercised mostly for parity/defense, not because real collisions are
    expected in production.

    Locked (review-round fix -- see `_index_write_lock`'s own docstring)
    across the ENTIRE read-check-move-write span, not just the final
    write: this also closes a narrower, secondary race the review didn't
    name explicitly but the same fix covers for free -- a concurrent
    `capture.pin {id}` landing between this function's own pinned-check
    read and its index write could otherwise let an about-to-be-pinned
    session slip through deleted anyway."""
    _check_not_live(session_id, live_session_id, "delete")
    with _index_write_lock(capture_dir):
        rows = _read_index_rows(capture_dir)
        row = next((r for r in rows if r.get("id") == session_id), None)
        src_path = _session_path(capture_dir, session_id)
        file_exists = os.path.exists(src_path)
        if row is None and not file_exists:
            raise UnknownSessionError(f"unknown session: {session_id!r}")
        if row is not None and row.get("pinned"):
            raise PinnedSessionError(f"session {session_id!r} is pinned; refusing to delete "
                                     "(unpin it first)")

        if file_exists:
            dest = _trash_path(capture_dir, session_id)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src_path, dest)

        remaining = [r for r in rows if r.get("id") != session_id]
        if len(remaining) != len(rows):
            _write_index_rows(capture_dir, remaining)
    return {"deleted": True, "id": session_id, "trashed": file_exists}
