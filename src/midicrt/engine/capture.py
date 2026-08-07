"""Event-sourced session capture (Phase 5 Task 1, docs/phase5-notes.md):
`CaptureSink` records raw MIDI + provenance-tagged action marks to a
per-session JSONL file, for a future replay task to consume.

v1 field/format authority (READ, not invented)
-------------------------------------------------------------------------
v1's `~/codex/midicrt/engine/memory/capture.py::MemoryCaptureManager` (+
`session_model.py`/`storage.py`) is the closest analog on the Pi, and is
this task's authority for "what does a session record": a `SessionHeader`
(session_id, start/stop position, bpm, tempo/time-signature segment
timelines) plus a flat event list, saved as ONE JSON document per session
(`storage.save_session` -- a full-document atomic rewrite at
`_finalize_session`, not an append-as-you-go stream) plus a
`session_index.json` array of lightweight per-session metadata rows.

Two things do NOT carry over verbatim, disclosed here:
  - v1's header is TICK/PPQN-based (`start_tick`/`stop_tick`, driven by its
    own `engine/state/tempo_map.py` transport). v2's `Engine` has no
    engine-wide tick counter at all -- only `MidiEvent.ts` (wall-clock) and
    per-beat `clock_tick` aggregates (see `analyzers/transport.py`'s own
    module docstring on why v2 can't reproduce v1's per-pulse tick math).
    This session format is wall-clock (`ts`) throughout; there is no
    tick/ppqn field.
  - v1 finalizes (writes) a session as one atomic document at STOP. v2's
    sessions can run far longer between explicit stops (a capture is a
    standing recording, not a bar-dump), so writing once-at-the-end would
    mean an unbounded in-memory buffer and total data loss on a crash.
    docs/phase5-notes.md's own carry-over note is explicit about this:
    "capture's writer must NOT copy BindingsFile.save's synchronous
    rewrite-in-loop pattern" -- this sink instead appends JSONL lines,
    queued in memory and drained to disk on a cadence (see "Writer design"
    below), which is the deliberate, disclosed departure from v1's model.

v1's tempo timeline (`SessionHeader.tempo_segments`, appended with
hysteresis by `_should_append_tempo_segment`) is ported here as `"tempo"`
mark LINES (`{"kind": "tempo", "ts", "bpm"}`) written whenever a
`clock_tick` event's own bpm (computed the SAME way
`analyzers/transport.py::TransportAnalyzer._advance_beat` does:
`60.0 / (ts - clock_batch_start)`) differs from the last one recorded --
simpler dedup than v1's own tick-spacing hysteresis (v2 has no tick
spacing to measure against), but the same "don't write a segment for
every single beat, only when tempo actually changes" intent.

Writer design (queued, not synchronous-per-event) -- the loss window
-------------------------------------------------------------------------
`record_event`/`record_action`/`record_page_changed` are the ONLY methods
called from the engine's hot path (`Engine._handle`, once per incoming
MIDI event) -- each just appends a plain dict to an in-memory
`collections.deque`, no I/O, no formatting. `flush()` is the only method
that ever touches disk: it drains the whole deque, `json.dumps`+newline
per line, `write()` then `flush()` + `os.fsync()` once for the whole
batch -- NOT one write+fsync per event (that WOULD be `BindingsFile.
save()`'s discouraged pattern, just per-line instead of per-file-rewrite;
this batches instead).

`maybe_flush(now)` is the cadence gate `Engine.run()` calls once per tick
(same injected-`now` convention as `_tick_analyzers`/`_tick_pages`/
`_tick_behaviors` -- see engine/core.py's module docstring): a flush
actually happens at most once per `flush_interval_s` (default 1.0s).
`stop()` always calls `flush()` unconditionally first, regardless of the
cadence gate, so a clean `capture.stop` never leaves anything buffered.

Disclosed loss window: anything appended to the deque between the last
flush and an UNCLEAN shutdown (a hard crash / `SIGKILL` -- `Engine.stop()`,
reached on a normal `SIGTERM`/`SIGINT` shutdown, itself calls this sink's
`stop()`, which flushes) is lost -- up to ~`flush_interval_s` seconds of
events/marks, plus the session's `index.json` row itself, which is only
ever written at `stop()` (see "Session index" below). The raw `.jsonl`
file up through the last successful flush survives on disk either way
(it's a real file with a header + however many lines made it to disk),
just not listed in `index.json` until a clean stop -- an operator CAN
recover it by hand; nothing after the last flush is recoverable at all.
This window is intentionally NOT covered by a test that races real
wall-clock time against a real crash (inherently flaky/slow to prove
meaningfully); the "buffer then flush" behavior itself IS unit-tested
(inject without flushing, assert nothing on disk yet; flush; assert it
lands) -- see test_capture.py.

Write-failure containment (disk full / EIO -- NOT a crash)
-------------------------------------------------------------------------
Distinct from the loss window above (which is about an UNCLEAN shutdown):
`flush()` itself can raise `OSError` (ENOSPC, EIO, a yanked drive) on a
perfectly healthy, still-running daemon. Before this was caught, `Engine.
run()` called `maybe_flush(now)` completely unguarded -- the exception
propagated straight out of the WHOLE tick loop, silently killing the
`run()` asyncio task with no log line at all (systemd still reported the
unit "active"; no restart; MIDI processing simply stopped forever).
`Engine._tick_capture_flush` now wraps that call in `try/except OSError`
(mirroring `_dispatch_bindings`'s own "never let one failure kill the
loop" precedent) and routes it to `CaptureSink.fail` (see that method's
own docstring for the full "why disable instead of retry" rationale) via
`Engine._capture_write_failed`, which also flips the `rec` chrome flag
off, logs ONE error, and emits an `alert` event. The SAME `fail()` path
also catches a `capture.start`/`.stop` action call whose own I/O raised
`OSError` synchronously (`Engine._capture_start_action`/
`_capture_stop_action` both convert that into a clean `ActionError`
instead of an uncaught exception tearing down the requesting client
connection).

`_atomic_write_json`'s tempfile (`.index-*.json.tmp`, same directory as
`index.json`) can be left behind if the process is `SIGKILL`ed between
`tempfile.mkstemp` and the `os.replace` a few lines later -- a real,
accepted risk with no cleanup code written for it (a `SIGKILL` mid-write
is already an unclean-shutdown scenario the loss-window section above
already asks an operator to tolerate; an orphaned, harmless `.tmp` file
sitting next to `index.json` is the same class of "an operator can clean
this up by hand" residue, not a correctness bug -- the NEXT successful
`_atomic_write_json` call always writes its OWN fresh tempfile name
(`tempfile.mkstemp`'s own uniqueness), so a stale one is never read back
by anything).

Session index (`index.json`)
-------------------------------------------------------------------------
One row per FINISHED session (`id`/`started_ts`/`ended_ts`/`counts`/
`pinned`), written atomically (tempfile + `os.replace`, same pattern
`engine/bindings.py::BindingsFile.save()` already uses for `bindings.toml`
-- safe here because index writes are cold-path, at most once per
`capture.start`/`.stop`/`.pin`, never per-event, unlike the JSONL body
above). A session actively recording has NO index row yet -- it is added
only at `stop()` -- so an unclean shutdown loses the row (see loss-window
note above) even though the `.jsonl` file itself may be mostly intact.

A malformed `index.json` (corrupt JSON, or valid JSON that isn't a list)
is never fatal -- `_load_index` catches the parse failure, logs a
warning, and rebuilds a best-effort index by scanning `self._dir` for
`*.jsonl` files and reading each one's own header line back (mirrors
`engine/keymap.py`/`engine/bindings.py`'s own "a malformed OPTIONAL file
must never crash the daemon" discipline) -- the rebuilt index is
immediately persisted so the recovery sticks.

Retention
-------------------------------------------------------------------------
`start()` sweeps retention BEFORE creating the new session (so the
session about to start is never a candidate for its own sweep): every
row in `index.json` NOT marked `pinned` is a candidate; oldest-first
(`started_ts`), delete however many are needed to bring the UNPINNED
count down to `retention` (default 50, `Config.capture_retention`).
Pinned sessions are immune outright (never counted as excess, never
deleted) -- `capture.pin {id}` sets a completed session's `pinned` flag;
pinning a session that hasn't been stopped yet (no index row exists) is
not supported (Engine's `capture.pin` action raises `ActionError` for an
unknown id, same "genuine caller error" precedent as `bind.remove`).

Storage location (StateDirectory vs dev fallback)
-------------------------------------------------------------------------
Production runs under `packaging/midicrtd.service`'s `StateDirectory=
midicrt` (systemd creates `/var/lib/midicrt`, owned by the unit's own
`User=`/`Group=`, before the daemon starts) -- `DEFAULT_STATE_DIR` below.
A bare dev/test run (no systemd, plain `midicrtd` from a checkout) has no
such directory and no permission to create one under `/var/lib` --
`resolve_capture_dir` falls back to `DEV_FALLBACK_STATE_DIR`
(`~/.local/state/midicrt/sessions`) whenever `DEFAULT_STATE_DIR`'s PARENT
isn't an existing, writable directory (a pure check -- no `mkdir` side
effect at resolution time; `start()` is what actually creates the
resolved directory). `Config.capture_dir` (default `None`) is an explicit
override that skips this whole resolution -- read live off the module
attributes (not cached at import), so tests can `monkeypatch.setattr`
either constant, exactly like `engine/keymap.py`/`engine/bindings.py`'s
own `DEFAULT_PATH` isolation convention (see tests/conftest.py).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections import deque
from typing import Any

_LOG = logging.getLogger(__name__)

FORMAT_VERSION = 1
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_RETENTION = 50
INDEX_FILE = "index.json"

# See module docstring's "Storage location" section -- both read LIVE
# (module-attribute lookup, not a value captured at import time) by
# `resolve_capture_dir`, so a test's `monkeypatch.setattr(capture_mod,
# "DEFAULT_STATE_DIR", ...)` redirects every unconfigured `CaptureSink`
# for that test's duration, mirroring `keymap_mod.DEFAULT_PATH`/
# `bindings_mod.DEFAULT_PATH`'s own isolation story exactly.
DEFAULT_STATE_DIR = "/var/lib/midicrt/sessions"
DEV_FALLBACK_STATE_DIR = os.path.expanduser("~/.local/state/midicrt/sessions")


def resolve_capture_dir(configured: str | None = None) -> str:
    """`configured` (`Config.capture_dir`) wins outright when set. Otherwise:
    `DEFAULT_STATE_DIR` if its PARENT directory already exists and is
    writable (the systemd `StateDirectory=midicrt` case -- `/var/lib/midicrt`
    itself is created by systemd before the daemon starts; this sink only
    ever creates the `sessions` leaf under it), else `DEV_FALLBACK_STATE_DIR`.
    A pure check -- never creates anything itself (`start()` does that, via
    `os.makedirs(self._dir, exist_ok=True)`, once a session actually
    begins)."""
    if configured:
        return configured
    parent = os.path.dirname(os.path.normpath(DEFAULT_STATE_DIR)) or "/"
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return DEFAULT_STATE_DIR
    return DEV_FALLBACK_STATE_DIR


def _atomic_write_json(path: str, payload: Any) -> None:
    """Same tempfile-in-same-directory + `os.replace` atomic pattern as
    `engine/bindings.py::BindingsFile.save()` -- legitimate here (unlike
    the per-MIDI-event JSONL body) because `index.json` is only ever
    rewritten from `capture.start`/`.stop`/`.pin`, all cold-path, human-rate
    operations, never once per event."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".index-", suffix=".json.tmp", dir=directory)
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


class CaptureSink:
    """Owns one directory of session `.jsonl` files + their shared
    `index.json`. `Engine` owns exactly one instance (`self._capture`,
    engine/core.py) -- see this module's own docstring for the full design
    (writer queueing/loss window, index/retention, storage resolution)."""

    def __init__(
        self,
        *,
        capture_dir: str | None = None,
        retention: int = DEFAULT_RETENTION,
        engine_version: str = "",
        instruments: list[str] | None = None,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._dir = resolve_capture_dir(capture_dir)
        self._retention = max(1, int(retention))
        self._engine_version = str(engine_version)
        self._instruments = list(instruments or [])
        self._flush_interval_s = max(0.0, float(flush_interval_s))
        self._recording = False
        self._session_id: str | None = None
        self._started_ts: float | None = None
        self._fh = None
        self._buffer: deque[dict] = deque()
        self._counts: dict[str, int] = {}
        self._last_bpm: float | None = None
        self._last_flush_ts: float = 0.0
        # Fix-wave addition (Minor finding: `status()` used to leave a
        # just-ended session's `counts` sitting under the LIVE `counts`
        # key even after `session_id` went back to `None`, which reads
        # as stale/orphaned data with nothing to attribute it to) --
        # `stop()`/`fail()` both populate this; `status()` reports it
        # under its own `last_session` key instead, so the LIVE `counts`
        # key always accurately reflects "nothing" while idle.
        self._last_session: dict | None = None

    # -- introspection ------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def dir(self) -> str:
        return self._dir

    def session_path(self, session_id: str) -> str:
        return os.path.join(self._dir, f"{session_id}.jsonl")

    def status(self) -> dict:
        """`capture.status` action handler's data. `counts` reflects ONLY
        the currently-active session (empty `{}` while idle -- never a
        stale carryover from whatever session most recently ended);
        `last_session` (`None` until the very first `stop()`/`fail()`)
        is where a just-ended session's own final `id`/`counts`/
        `started_ts`/`ended_ts` (and `error`, if it ended via `fail()`)
        live instead, so a client can still show "last capture: N
        events" while idle without that data being ambiguously mixed
        into the live-session key."""
        return {
            "recording": self._recording,
            "session_id": self._session_id,
            "started_ts": self._started_ts,
            "counts": dict(self._counts) if self._recording else {},
            "last_session": dict(self._last_session) if self._last_session else None,
        }

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> dict:
        """`capture.start` (engine/core.py): sweep retention, open a fresh
        session file, write+flush its header line immediately (so `ls` on
        the sessions directory shows the new file right away, not only
        after the first ~1s flush cadence tick). Starting while already
        recording implicitly stops (and indexes) the prior session first --
        mirrors `MemoryCaptureManager._begin_session`'s own "finalize
        whatever was running" precedent rather than leaking an orphaned
        open file handle."""
        if self._recording:
            self.stop()
        self._sweep_retention()
        os.makedirs(self._dir, exist_ok=True)
        session_id = f"session-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        # Deliberately NOT a `with` block -- this handle is long-lived,
        # spanning every `flush()` call for the rest of this session's
        # life (potentially hours), closed explicitly in `stop()`.
        self._fh = open(self.session_path(session_id), "a", encoding="utf-8")  # noqa: SIM115
        self._session_id = session_id
        self._started_ts = time.time()
        self._counts = {}
        self._last_bpm = None
        self._last_flush_ts = self._started_ts
        self._recording = True
        self._buffer.append({
            "kind": "header",
            "format": FORMAT_VERSION,
            "session_id": session_id,
            "started_ts": self._started_ts,
            "engine_version": self._engine_version,
            "instruments": list(self._instruments),
        })
        self.flush()
        return {"session_id": session_id, "started_ts": self._started_ts}

    def stop(self) -> dict:
        """`capture.stop`: flush unconditionally (never rely on the cadence
        gate at stop time), close the file, write this session's
        `index.json` row. A no-op (not an error) when nothing is
        recording -- mirrors `bind.cancel`'s own "stopping nothing is a
        harmless confirmation" precedent, since `Engine.stop()` calls this
        unconditionally on every shutdown (see engine/core.py's own
        `stop()`)."""
        if not self._recording:
            return {"session_id": None, "counts": {}}
        self.flush()
        session_id = self._session_id
        started_ts = self._started_ts
        ended_ts = time.time()
        counts = dict(self._counts)
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
        self._update_index_on_stop(session_id, started_ts, ended_ts, counts)
        self._last_session = {
            "id": session_id, "started_ts": started_ts, "ended_ts": ended_ts,
            "counts": counts,
        }
        self._recording = False
        self._fh = None
        self._session_id = None
        self._started_ts = None
        self._counts = {}
        return {"session_id": session_id, "counts": counts}

    def fail(self, error: BaseException) -> dict:
        """Write-failure containment (fix wave, Critical finding): an
        unguarded `flush()` `OSError` (ENOSPC/EIO) used to propagate
        straight out of `Engine.run()`'s tick loop with NO log line at
        all, silently killing the whole `run()` asyncio task forever --
        the daemon stayed "active" to systemd (no crash, no restart), and
        MIDI processing simply stopped. `Engine._capture_write_failed`
        calls this from BOTH the background flush-tick call site
        (`run()`) and a foreground `capture.start`/`.stop` action call
        that raised `OSError` synchronously -- either way, THIS is the
        one place capture actually gets disabled.

        Deliberately does NOT retry -- contrast `analyzers/spectrum.py`'s
        own audio-capture retry/cooldown supervisor (the "audio pattern"
        this is modeled after IN SPIRIT, not literally): a vanished USB
        audio device can plausibly reappear on its own; a full disk or a
        read-only filesystem cannot self-heal within seconds, and
        retrying at tick rate would spam log lines far faster than that
        supervisor's 5s cooldown while accomplishing nothing an operator
        can't already fix by hand (free space, remount, replace the
        drive). Disabling immediately is what makes this naturally
        "log ONE error" per incident without needing its own cooldown
        timer: once `self._recording` is `False`, every hot-path
        `record_*` call and the next `maybe_flush` are cheap no-ops, so
        there is nothing left to keep failing (and thus nothing left to
        keep logging) until a genuinely NEW `capture.start` begins a
        genuinely NEW incident.

        Never itself raises -- a SECOND write failure while trying to
        note the first one (the disk is STILL full) is swallowed;
        `index.json` may be left exactly as it was at the moment of the
        original failure, which is the honest outcome of an unwritable
        filesystem. Whatever was still buffered (appended but not yet
        flushed) at the moment of failure is dropped -- the same loss-
        window disclosure the module docstring already makes for an
        unclean shutdown, just triggered by a write error instead of a
        crash."""
        session_id = self._session_id
        started_ts = self._started_ts
        counts = dict(self._counts)
        ended_ts = time.time()
        if self._fh is not None:
            with contextlib.suppress(Exception):
                self._fh.close()
        self._recording = False
        self._fh = None
        self._session_id = None
        self._started_ts = None
        self._counts = {}
        self._buffer.clear()
        error_text = str(error)
        if session_id:
            self._last_session = {
                "id": session_id, "started_ts": started_ts, "ended_ts": ended_ts,
                "counts": counts, "error": error_text,
            }
            with contextlib.suppress(Exception):
                rows = [r for r in self._load_index() if r.get("id") != session_id]
                rows.append({
                    "id": session_id, "started_ts": started_ts, "ended_ts": ended_ts,
                    "counts": counts, "pinned": False, "error": error_text,
                })
                self._save_index(rows)
        return {"session_id": session_id, "counts": counts, "error": error_text}

    def pin(self, session_id: str) -> dict:
        """`capture.pin {id}`: only ever targets a row already in
        `index.json` (i.e. a session that has been STOPPED -- see module
        docstring's "Retention" section for why an in-progress session has
        no row to pin yet). Raises `ValueError` for an unknown id --
        `Engine._capture_pin` translates that into `ActionError`, matching
        `bind.remove`'s own "unknown named resource is a genuine caller
        error" precedent."""
        rows = self._load_index()
        for row in rows:
            if row.get("id") == session_id:
                row["pinned"] = True
                self._save_index(rows)
                return {"pinned": True, "id": session_id}
        raise ValueError(f"unknown capture session: {session_id!r}")

    # -- hot-path recording (no I/O) ------------------------------------------

    def record_event(self, ev) -> None:
        """Called from `Engine._handle` for EVERY event (after the
        self-output filter, before any dispatch branching) -- raw MIDI is
        ground truth (docs/phase5-notes.md's decided design, point 1).
        A cheap no-op when not recording."""
        if not self._recording:
            return
        line: dict[str, Any] = {
            "kind": "event", "ts": ev.ts, "source": ev.source, "type": ev.type,
            "channel": ev.channel, "data1": ev.data1, "data2": ev.data2,
            "summary": ev.summary,
        }
        if ev.type == "clock_tick":
            line["clock_batch_start"] = ev.clock_batch_start
        if ev.sysex_data is not None:
            line["sysex_data"] = list(ev.sysex_data)
        self._buffer.append(line)
        self._counts[ev.type] = self._counts.get(ev.type, 0) + 1
        if ev.type == "clock_tick" and ev.clock_batch_start is not None:
            self._maybe_record_tempo(ev.ts, ev.clock_batch_start)

    def _maybe_record_tempo(self, ts: float, batch_start: float) -> None:
        """Port of v1's tempo-segment timeline (see module docstring) --
        same bpm formula `analyzers/transport.py::TransportAnalyzer.
        _advance_beat` uses, recomputed independently here rather than
        reading the live analyzer's state (keeps this sink self-contained
        and directly testable by feeding synthetic `clock_tick` events,
        with no engine/analyzer wiring needed)."""
        span = ts - batch_start
        if span <= 0:
            return
        bpm = 60.0 / span
        if self._last_bpm is not None and bpm == self._last_bpm:
            return
        self._last_bpm = bpm
        self._buffer.append({"kind": "tempo", "ts": ts, "bpm": bpm})

    def record_action(self, name: str, args: dict, origin: str) -> None:
        """Action-mark line, stamped AT DISPATCH time (the caller's
        responsibility to call this exactly when a dispatch actually
        succeeds -- see engine/core.py's `_on_action_dispatched` hook and
        the three sysex handlers that call this directly since sysex
        never goes through `ActionRegistry.dispatch` at all)."""
        if not self._recording:
            return
        self._buffer.append({
            "kind": "action", "ts": time.time(), "name": name,
            "args": dict(args), "origin": origin,
        })

    def record_page_changed(self, page: str) -> None:
        """Called from `Engine._set_current_page` -- the single funnel
        point for EVERY page transition regardless of cause (action
        dispatch, sysex, an idle behavior) -- so this mark is complete
        without needing its own per-origin plumbing."""
        if not self._recording:
            return
        self._buffer.append({"kind": "page_changed", "ts": time.time(), "page": page})

    # -- writer: queued, flushed on a cadence ---------------------------------

    def maybe_flush(self, now: float) -> None:
        """Called once per `Engine.run()` tick (injected `now`, same
        convention as `_tick_analyzers`/`_tick_pages`/`_tick_behaviors`) --
        gates `flush()` to roughly `flush_interval_s` cadence. A cheap
        no-op both when not recording and when the interval hasn't
        elapsed yet."""
        if not self._recording:
            return
        if now - self._last_flush_ts < self._flush_interval_s:
            return
        self.flush()
        self._last_flush_ts = now

    def flush(self) -> None:
        """Drain the ENTIRE buffer to disk in one batch: one `write()` per
        line, then a SINGLE `flush()`+`os.fsync()` for the whole batch --
        see module docstring's "Writer design" section for why this is not
        `BindingsFile.save()`'s per-event synchronous-rewrite pattern (this
        appends; a rewrite pattern would be O(session-size) per flush, this
        is O(buffered-since-last-flush))."""
        if self._fh is None or not self._buffer:
            return
        while self._buffer:
            item = self._buffer.popleft()
            self._fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # -- index.json ------------------------------------------------------------

    def _index_path(self) -> str:
        return os.path.join(self._dir, INDEX_FILE)

    def _load_index(self) -> list[dict]:
        path = self._index_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"index.json must be a list, got {type(data).__name__}")  # noqa: TRY004
            return [row for row in data if isinstance(row, dict) and row.get("id")]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _LOG.warning("capture: index.json malformed (%s); rebuilding from session "
                        "directory scan", exc)
            rebuilt = self._rebuild_index_from_disk()
            self._save_index(rebuilt)
            return rebuilt

    def _rebuild_index_from_disk(self) -> list[dict]:
        """Best-effort recovery (module docstring's "Session index"
        section): scan `self._dir` for `*.jsonl` files and read each one's
        own first (header) line back for `session_id`/`started_ts`.
        `counts`/`ended_ts` can't be recovered this way (they were only
        ever known in-memory, never written to the header) -- reported as
        `0`/`None` rather than guessed. Never raises: an unreadable or
        headerless session file is skipped with a warning, same "log +
        skip this one entry, keep going" discipline `engine/bindings.py`'s
        own per-entry parsing already uses."""
        if not os.path.isdir(self._dir):
            return []
        rows: list[dict] = []
        for name in sorted(os.listdir(self._dir)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self._dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline()
                header = json.loads(first_line) if first_line.strip() else {}
            except (OSError, json.JSONDecodeError):
                _LOG.warning("capture: skipping unreadable session file during index "
                            "rebuild: %s", path)
                continue
            session_id = str(header.get("session_id") or name[: -len(".jsonl")])
            rows.append({
                "id": session_id,
                "started_ts": header.get("started_ts", 0.0),
                "ended_ts": None,
                "counts": {},
                "pinned": False,
            })
        return rows

    def _save_index(self, rows: list[dict]) -> None:
        _atomic_write_json(self._index_path(), rows)

    def _update_index_on_stop(self, session_id: str, started_ts: float, ended_ts: float,
                              counts: dict) -> None:
        rows = [r for r in self._load_index() if r.get("id") != session_id]
        rows.append({
            "id": session_id, "started_ts": started_ts, "ended_ts": ended_ts,
            "counts": counts, "pinned": False,
        })
        self._save_index(rows)

    def _sweep_retention(self) -> None:
        """Called at the START of `start()`, BEFORE the new session this
        call is about to create exists in `index.json` at all (module
        docstring's "Retention" section) -- unpinned rows beyond
        `self._retention`, oldest (`started_ts`) first, are deleted (both
        the `.jsonl` file and the index row). Pinned rows never count as
        excess.

        Target is `self._retention - 1`, NOT `self._retention` -- this
        call's own about-to-be-created session is the `+1` that brings the
        resident unpinned count back up to exactly `self._retention` right
        after `start()` returns. Sweeping to the full `self._retention`
        here would let the steady-state resident count creep to
        `retention + 1` forever (this session always counted from its very
        next sweep onward, never from this one) -- caught live by
        test_capture.py::test_retention_sweep_never_deletes_a_pinned_session
        expecting the unpinned count to settle at exactly `retention`."""
        rows = self._load_index()
        unpinned = sorted(
            (r for r in rows if not r.get("pinned")),
            key=lambda r: float(r.get("started_ts", 0.0)),
        )
        excess = len(unpinned) - max(0, self._retention - 1)
        if excess <= 0:
            return
        victims = unpinned[:excess]
        victim_ids = {v["id"] for v in victims}
        for victim in victims:
            with contextlib.suppress(OSError):
                os.remove(self.session_path(victim["id"]))
        rows = [r for r in rows if r.get("id") not in victim_ids]
        self._save_index(rows)
