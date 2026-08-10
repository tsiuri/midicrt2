"""Session replay through an OFFLINE `Engine` (Phase 5 Task 2, docs/
phase5-notes.md): streams a captured session's `.jsonl` (see engine/
capture.py's own module docstring for the on-disk shape -- `header`/
`event`/`action`/`tempo`/`page_changed` lines) through a real `Engine`
built with no socket server, no `MidiInput`, and no real `MidiOutput`
sends, producing a deterministic end-of-replay summary.

This module is the DRIVER half of the decided design; `engine/core.py`'s
`Engine(..., replay=True)` constructor flag + the `if self._replay:` gate
atop `_handle` (see that method's own comment at the gate) are the ENGINE
half -- read both together. Three public functions:

  - `build_offline_engine(...)`: constructs the `Engine` (replay=True,
    MIDI output stubbed) -- separated from `stream_session` so a caller
    (chiefly test_replay.py) can inspect/reuse the engine object itself
    (e.g. to assert `engine.current_page` or `engine._pending_binding_
    dispatches` after streaming), and so a scratch `bindings_path` can be
    supplied for the suppression-proof tests.
  - `stream_session(engine, path, ...)`: the actual line-by-line driver.
  - `replay_session(path, ...)`: the one-call convenience `clients/cli.py`'s
    `replay` subcommand uses (build + stream + return the summary).

Why sysex is NOT suppressed (unlike bindings/behaviors/learn)
---------------------------------------------------------------------------
docs/phase5-notes.md's decided design says replay "MUST suppress live
binding dispatch, behaviors, and learn arming" -- three items, not four.
SysEx is deliberately left OFF that list and this module does nothing to
gate it: a captured "sysex" event line replays through `Engine._handle`
completely normally, reaching `Engine._handle_sysex` exactly like a live
frame would. This is intentional, not an oversight, for two reasons:

  1. Determinism: `_handle_sysex`'s state changes (`_page_goto`,
     `_pagecycle_enable`) are PURE functions of the sysex bytes + the
     current roster -- unlike a binding (which depends on whatever
     `bindings.toml` happens to be loaded on the replaying machine) or a
     behavior (which depends on idle-timer wall-clock state this replay
     never simulates), replaying the identical bytes through the identical
     roster always produces the identical result. There is no
     `bindings.toml`-drift-shaped non-determinism risk here to suppress.
  2. It's the ONLY origin whose own resulting mark needs no help from this
     module's mark-applier at all -- see "Mark application semantics"
     below. Replaying the raw event ALREADY reproduces the page change a
     live sysex command caused; applying that origin's `page_changed` mark
     again (which this module's mark-applier still does, universally, for
     every `page_changed` line regardless of origin -- see below) is
     merely REDUNDANT for sysex, not required, and costs nothing since
     `_set_current_page` is idempotent.

`_handle_sysex`'s own SysEx REPLIES (`self._midi_out.send_sysex(...)`) are
still real calls during replay -- they just land on `build_offline_
engine`'s stubbed `_midi_out`, which silently drops them (see
`_OfflineMidiOutput` below). "No MidiOutput sends" (task brief) means
exactly this: the call sites are untouched, only the I/O at the bottom is
cut.

Tick-driven state is FROZEN during replay (disclosed limitation)
---------------------------------------------------------------------------
`stream_session` never calls `Engine.run()` -- it feeds "event" lines
straight into `Engine._handle` and applies marks directly, nothing more.
That means `_tick_analyzers(now)`/`_tick_pages(now)`/`_tick_behaviors(now)`
(engine/core.py's own `run()` loop, called once per real tick at
`config.tick_hz`) are NEVER called during a replay, for ANY wall-clock
value, real or log-derived. Concretely: `analyzers/stucknotes.py`'s
long-held-note escalation (which fires from `tick(now)` alone, with NO new
MIDI event required -- see that module's own docstring) simply never
escalates during replay, no matter how long a note in the log stays held
or how much recorded `ts` elapses between events; the same is true for
`pages/pianoroll.py`'s wall-clock scroll advance and any FUTURE module
that adds its own optional `tick(now)`/`drain_outputs(now)` hook. This is
currently harmless: NOTHING in `_build_summary`'s reported fields (voices/
harmony/transport/timesig) is tick-derived -- all four are pure
`handle(ev)` state machines, computed identically whether or not any
`tick()` ever ran. It is nonetheless a real FIDELITY gap disclosed here
rather than silently assumed away: a session that visibly showed a
stuck-note alert or a scrolling pianoroll live will replay with neither,
and a hypothetical future summary field reading `alerts`/pianoroll scroll
position would report stale/frozen values.

Fixing this (NOT attempted by this task -- out of scope, since no current
summary field needs it) would mean driving ticks from the LOG's own
recorded `ts` progression, not real wall-clock time -- e.g. calling
`engine._tick_analyzers(ts)`/`_tick_pages(ts)` at each "event" line's `ts`
(and, for a gap-heavy log, at synthetic intermediate points spaced by
`1/config.tick_hz` between events, mirroring how a live `run()` loop ticks
continuously even between MIDI arrivals) rather than only reacting to
events. `_tick_behaviors` would need the SAME suppression treatment
`_handle`'s own gate already gives bindings/behaviors/learn (docs/
phase5-notes.md's decided design: behaviors must never fire live during
replay) -- so a future fix ticking analyzers/pages must NOT also start
calling `_tick_behaviors`. Flagged here as the seam a future task would
extend, not built now.

Mark application semantics (what "apply AS MARKS, bypass the dispatcher"
means here, concretely)
---------------------------------------------------------------------------
docs/phase5-notes.md point 3 says replay applies page/action marks "AS
MARKS", bypassing the dispatcher, and leaves the exact mechanics as an
open decision for this task ("document what mark application means
offline"). Investigation (see this module's own construction, and
engine/core.py's module docstring's "Dirty tracking" section) shows every
page's and every analyzer's `handle(ev)` runs UNCONDITIONALLY for every
event regardless of `current_page` -- `current_page` selects which ONE
page a live client is currently LOOKING at, nothing more; it has zero
effect on any analyzer's or page's computed STATE. That fact drives the
two concrete rules this module implements:

  - `page_changed` lines: applied as a DIRECT state mutation --
    `Engine._set_current_page(mark["page"])` -- for every such line,
    regardless of which action/origin produced it live. This is the
    ONLY way replay can ever reproduce a CLIENT- or BEHAVIOR-origin page
    navigation (both have zero MIDI trace at all, and both are the kind
    of navigation a `page_changed` mark exists to record) or a
    BINDING-origin one (suppressed at the `_handle` level -- see
    engine/core.py's own comment -- so its `page.goto`'s real effect
    would otherwise be silently lost). Applying the FINAL `page_changed`
    mark rather than re-interpreting the `action` mark that caused it
    (which the task brief's own text leans toward -- "page.goto marks
    set current_page directly") is a deliberate, disclosed refinement:
    `page_changed` is `_set_current_page`'s own single funnel-point
    record of the ABSOLUTE resulting page, emitted identically whether
    the cause was `page.goto`, `page.next`, `page.prev`, a sysex command,
    or an idle behavior -- reusing it means this module needs no special
    knowledge of `page.next`/`page.prev`'s RELATIVE semantics (which
    would require replaying against a specific, position-dependent
    roster order) to reproduce the SAME final answer those actions'
    live dispatch already computed once, for free. See test_replay.py's
    own "page_changed marks: applied as direct state mutations" section
    for the concrete proof this doesn't touch analyzer state.
  - `panic.release` marks (Phase 9 close-out fix wave): applied as a
    DIRECT state mutation, the SAME "state change with zero MIDI trace"
    category `page_changed` above is in -- NOT re-dispatched through
    `ActionRegistry.dispatch` (see the safety argument for "every other
    action mark" right below for why that distinction matters). Live,
    `Engine._release_for_panic` (a stuck-note CRIT panic's own internal
    half, `engine/core.py`) routes each released note through `_dispatch_
    to_state` directly -- exactly like `_handle`'s own tail does for a
    REAL event, fanning out to every analyzer's/page's `handle(ev)` --
    and ONLY records a `panic.release` mark (`{"ch", "notes"}`, `origin:
    "alert"`) as the provenance trace; no `kind: "event"` line is ever
    written for it (docs/phase5-capture.md §2's own "internal action,
    not a fake wire event" framing). Before this fix, replay only ever
    COUNTED this mark (the generic "every other action mark" rule below)
    -- meaning a replayed session's voice/harmony state STILL SHOWED the
    panicked note(s) held, even though live playback had already released
    them: a real, disclosed-as-fixed correctness gap (reviewer-verified:
    `voices.total` stayed nonzero after a replayed panic cycle that had,
    live, driven it back to zero). Fixed by building the SAME synthetic
    `note_off` `MidiEvent` `_release_for_panic` itself builds (`source=
    "panic:release"`, `type="note_off"`, `data2=0`, the SAME `summary`
    format) and calling `engine._dispatch_to_state(ev)` for each note in
    the mark's own `"notes"` list -- one call per note, matching the live
    per-note loop exactly. **Index conversion, the one sharp edge**: the
    mark's own `"ch"` field is 1-INDEXED (v1-style channel numbering,
    matching `_maybe_panic`'s own `alert["ch"]` convention) while every
    `MidiEvent.channel`/analyzer-internal channel value is 0-INDEXED --
    `_release_for_panic` itself converts via `channel=ch - 1` when
    building its OWN synthetic event, and replay's mark-applier must
    perform the IDENTICAL conversion or every released note lands on the
    WRONG channel's held-note ledger (silently correct-LOOKING, since
    nothing raises -- just wrong). A malformed mark (missing/non-`int`
    `"ch"`, non-list `"notes"`, missing/non-numeric `"ts"`) is logged and
    skipped, same "an optional/derived field's corruption is never fatal"
    discipline every other per-line parse failure in this module already
    follows.
  - Every other `action` mark: COUNTED (`actions_by_origin[origin] += 1`
    in the summary) and NEVER re-executed. This is a deliberate safety
    property, not merely "not yet implemented" -- see
    test_replay_session_never_actually_executes_a_dangerous_action_mark
    in test_replay.py for the sharpest case (`capture.start` would, if
    genuinely re-dispatched, try to open a REAL session file on whatever
    machine happens to be replaying this one); `bind.learn`/`bind.remove`
    would mutate a real `bindings.toml`; `sendnotes.key` would (absent the
    output stub) send a real note. A generic "re-dispatch every action
    mark through `ActionRegistry.dispatch`" design was considered and
    rejected for exactly this reason -- there is no safe, general way to
    replay an arbitrary action's mark as a real dispatch without auditing
    every current AND future action for replay-safety one at a time. Mark
    counting has no such risk: it's a pure read of the mark's own
    `origin` field. `panic.release` (and, before it, `page_changed`) are
    the two EXCEPTIONS to this rule, and both are exceptions for the
    SAME reason: neither one is EVER dispatched through `ActionRegistry.
    dispatch` in the first place, live -- both are direct, internal state
    mutations already, so "applying" them in replay carries none of the
    real-file/real-MIDI/real-config risk a genuinely re-dispatched client
    action would. `panic.notes_off` (the OTHER panic-cycle mark, the
    external All-Notes-Off CC send) is deliberately NOT given the same
    treatment -- it has no internal state effect of its own to replay (see
    `engine/core.py::_maybe_panic`'s own docstring: the state-mutating
    half is entirely `panic.release`'s job), so there is nothing for a
    mark-applier to DO with it beyond the counting every other mark
    already gets.
  - `tempo` lines: informational only (v1's tempo-segment-timeline port,
    see engine/capture.py's module docstring) -- ignored by this module
    entirely. Replaying the "clock_tick" events these were derived FROM
    already reproduces `analyzers/transport.py`'s bpm exactly (the SAME
    formula `CaptureSink._maybe_record_tempo` used to decide whether to
    write one), so a tempo line carries no information this module needs
    that isn't already present, more completely, in the event stream.
  - Any OTHER/future mark kind: silently ignored (forward-compatible --
    see `_read_lines`'s own per-`kind` dispatch, a plain `if/elif` chain
    with no `else: raise`).

Timing model (--speed / --instant, "preserving original ts values")
---------------------------------------------------------------------------
Every `MidiEvent` built from an "event" line carries its ORIGINALLY
RECORDED `ts` verbatim, in EVERY mode -- `--instant` only skips the
between-event `time.sleep()` calls; it never rewrites a single `ts` value.
This matters beyond cosmetics: `analyzers/transport.py`'s bpm math and
`analyzers/timesig.py`'s tick-position reconstruction both derive
DIRECTLY from `ev.ts`/`ev.clock_batch_start` -- substituting wall-clock
"now" for a replayed event's `ts` would corrupt those derivations and,
worse, make two replays of the identical file produce DIFFERENT bpm/timesig
estimates depending on how fast each replay happened to run, exactly the
kind of non-determinism this whole task exists to rule out. Pacing (the
real-time `time.sleep()` calls) is based ONLY on consecutive "event" lines'
`ts` deltas -- `action`/`page_changed`/`tempo` lines are applied
instantaneously, with no pacing contribution of their own (they have no
corresponding "wait" in a live engine either: they are OUTCOMES of MIDI
activity, not activity themselves).

  - `instant=True`: no sleeping at all, regardless of `speed` -- the
    fastest a replay can run.
  - `instant=False` (default): sleeps `max(0, ts - prev_ts) / speed`
    between consecutive "event" lines -- `speed=1.0` (the default)
    reproduces the original session's real-time pacing; `speed=2.0` runs
    twice as fast; `speed=0.5` runs at half speed.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent

_LOG = logging.getLogger(__name__)


class _OfflineMidiOutput:
    """Stub `MidiOutput` for the offline replay engine -- task brief:
    "no MidiOutput sends -- sendnotes/sysex replies must not emit real
    MIDI during replay; stub or gate the output". Never opens a real ALSA
    port (unlike the real `engine/midi_out.py::MidiOutput`, which lazily
    opens one on first use) -- every method is an unconditional no-op, so
    a replayed session containing a `sendnotes.key` action mark (never
    re-executed anyway, see this module's own docstring) or a real sysex
    reply frame (`_handle_sysex` IS still exercised during replay, see
    module docstring's "Why sysex is NOT suppressed" section) can never
    reach real hardware, with zero dependency on `mido`/ALSA even being
    importable on the machine doing the replaying."""

    port_name = "midicrt2 Replay (offline, no real output)"

    @property
    def is_open(self) -> bool:
        return False

    def note_on(self, note: int, velocity: int, channel: int) -> None:
        pass

    def note_off(self, note: int, channel: int) -> None:
        pass

    def all_notes_off(self, channel: int) -> None:
        """Defense-in-depth no-op (T2b warm-up item 2, re-review follow-up):
        `Engine._maybe_panic` (Phase 9 Task 2, panic-send) is the one
        caller of `MidiOutput.all_notes_off` and only ever fires from
        `_tick_analyzers`, which nothing in this module calls (replay
        drives `_handle` directly, never `run()`/analyzer ticks -- see this
        module's own docstring's "tick-driven replay is future work"
        note), so this is currently unreachable. Added anyway, matching
        `note_on`/`note_off`/`send_sysex` above's own "every method is an
        unconditional no-op" contract, so a future tick-driven replay mode
        hits an inert stub here instead of an `AttributeError`."""

    def send_sysex(self, data: tuple[int, ...]) -> bool:
        return False

    def close(self) -> None:
        pass


def build_offline_engine(*, config: Config | None = None, keymap_path: str | None = None,
                         bindings_path: str | None = None) -> Engine:
    """Constructs an `Engine` with no socket server, no `MidiInput`, and no
    real `MidiOutput` -- `daemon.py::build()` is NEVER called by this path
    at all (there is no `ProtocolServer`, no `midi_in.MidiInput`), and
    `_midi_out` is replaced with `_OfflineMidiOutput` right after
    construction, before any event is fed in. `bindings_path` is exposed
    (unlike `config`/`keymap_path`, purely for completeness/parity with
    `Engine`'s own constructor) specifically so test_replay.py's
    suppression-proof tests can point an offline engine at a scratch
    `bindings.toml` containing a binding that WOULD match a replayed event,
    to prove it never fires (docs/phase5-notes.md's own "replay with a
    binding configured in a scratch bindings.toml -> binding does NOT
    fire" acceptance test).

    Fix-wave fix (2026-08-07, Important finding, replay isolation, layer 2):
    `Engine.__init__` itself already gates `capture_auto_start` on `not
    self._replay` (see that call site's own comment) -- this is
    belt-and-suspenders defense-in-depth at the SECOND layer: if a caller
    supplies a `config` with `capture_auto_start=True`, the config actually
    used to construct the offline `Engine` is a neutered copy with that
    flag forced off, so this builder never depends solely on the engine
    remembering to check its own `_replay` flag correctly. `dataclasses.
    replace` (not a mutation of the caller's own `config` object) so a
    caller reusing the same `Config` instance elsewhere never sees it
    silently changed underneath them."""
    cfg = config if config is not None else Config()
    if cfg.capture_auto_start:
        cfg = dataclasses.replace(cfg, capture_auto_start=False)
    engine = Engine(cfg, keymap_path=keymap_path, bindings_path=bindings_path, replay=True)
    engine._midi_out = _OfflineMidiOutput()
    return engine


def _midi_event_from_line(line: dict) -> MidiEvent:
    sysex_data = line.get("sysex_data")
    return MidiEvent(
        ts=line["ts"],
        source=line.get("source", ""),
        type=line["type"],
        channel=line.get("channel"),
        data1=line.get("data1"),
        data2=line.get("data2"),
        summary=line.get("summary", ""),
        clock_batch_start=line.get("clock_batch_start"),
        sysex_data=tuple(sysex_data) if sysex_data is not None else None,
    )


def _iter_lines(path: str):
    """Yields parsed JSON OBJECTS (plain `dict`s), one per non-blank line --
    a malformed line is logged and SKIPPED (never raises), matching this
    codebase's established "an optional/external file's per-entry
    corruption is logged and skipped, never fatal" discipline
    (engine/bindings.py's per-binding parsing, engine/capture.py's own
    index-rebuild-from-disk). Every kind of bad line is covered: invalid
    JSON syntax (`json.JSONDecodeError`) AND syntactically valid JSON that
    isn't an object at all -- a bare `42`, `"x"`, `[1]` (fix wave, Minor
    finding: these used to parse through cleanly and reach `stream_session`'s
    `line.get("kind")` call, an `AttributeError` on anything without a
    `.get`) -- both are skipped the same way, so every caller downstream of
    this generator can assume every yielded value really is a `dict`. A
    missing/unreadable FILE itself is a different class of problem (the
    caller asked to replay a specific path that doesn't exist) and is
    deliberately NOT caught here -- `open()`'s own `OSError` propagates
    straight out of `stream_session`, mirroring `CaptureSink.start`/`.stop`
    letting a real disk-level `OSError` propagate rather than swallowing
    it (see that module's own docstring)."""
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                _LOG.warning("replay: skipping malformed JSON on line %d of %s: %s",
                            lineno, path, exc)
                continue
            # Fix wave (2026-08-07, Minor finding): VALID JSON that isn't a
            # JSON OBJECT (a bare `42`, `"x"`, `[1]`) parses cleanly here but
            # has no `.get` for `stream_session`'s `line.get("kind")` to call
            # -- an uncaught `AttributeError` that used to escape this
            # generator entirely, past `clients/cli.py::_handle_replay`'s
            # `except OSError`-only catch. Same "log + skip this one entry,
            # keep going" discipline as the malformed-JSON case right above.
            if not isinstance(parsed, dict):
                _LOG.warning("replay: skipping non-object JSON line %d of %s: %r",
                            lineno, path, parsed)
                continue
            yield parsed


def _apply_panic_release_mark(engine: Engine, line: dict, path: str) -> None:
    """Applies one `panic.release` mark (`{"ch", "notes"}`, `origin:
    "alert"`) as a state mutation -- see module docstring's "Mark
    application semantics" section for the full "why this is safe, why
    the channel needs `- 1`" writeup; this is the mechanical half. Builds
    the SAME synthetic `note_off` `MidiEvent` `Engine._release_for_panic`
    itself builds live (`engine/core.py`) for each note in `"notes"`, and
    routes it through `engine._dispatch_to_state` -- the identical
    analyzer/page fan-out a real event gets, WITHOUT re-entering `_handle`
    (no raw-capture bookkeeping, no `events_total` bump -- this is a mark
    being APPLIED, not a second fake wire event, exactly the same
    distinction `_release_for_panic`'s own docstring draws live).

    A malformed mark (missing/non-numeric `"ts"`, missing/non-`int`
    `"ch"`, non-list `"notes"`) is logged and skipped entirely -- never
    partially applied -- matching this module's own "an optional/derived
    field's corruption is never fatal" discipline for every other
    per-line parse failure (`_iter_lines`'s malformed-JSON handling, the
    `event`/`page_changed` branches' own `try/except` guards)."""
    args = line.get("args") or {}
    ts = line.get("ts")
    ch = args.get("ch")
    notes = args.get("notes")
    if not isinstance(ts, int | float) or not isinstance(ch, int) or not isinstance(notes, list):
        _LOG.warning("replay: skipping malformed panic.release mark in %s: %r", path, line)
        return
    for note in notes:
        if not isinstance(note, int):
            continue
        engine._dispatch_to_state(MidiEvent(
            ts=ts, source="panic:release", type="note_off",
            channel=ch - 1, data1=note, data2=0,
            summary=f"panic release ch{ch} n{note}",
        ))


def stream_session(engine: Engine, path: str, *, speed: float = 1.0,
                   instant: bool = False,
                   sleep_fn: Callable[[float], None] | None = None) -> dict:
    """Streams `path` (a `CaptureSink`-written `.jsonl`) through `engine`
    and returns the end-of-replay summary. See module docstring for the
    full per-`kind` handling (`event` -> `Engine._handle`, `page_changed`/
    `panic.release` -> direct state mutations, every other `action`/
    `tempo` -> counted/ignored) and the timing model (`speed`/`instant`).

    `sleep_fn` defaults to the real `time.sleep` -- injectable (mirrors
    `engine/midi_out.py::MidiOutput`'s own `backend` param convention) so
    tests can supply a recording no-op WITHOUT monkeypatching the process-
    global `time` module: `time.sleep` is a genuinely shared, process-wide
    stdlib function, and this codebase's test suite runs entirely in one
    process -- a global monkeypatch would also intercept any unrelated
    background thread (e.g. `analyzers/spectrum.py`'s own audio-capture
    thread) still alive from an earlier test polling on its own short
    interval, silently corrupting THIS call's sleep-count assertions with
    calls that have nothing to do with it (reproduced live while writing
    this module's own tests -- see test_replay.py's timing-model section).

    `speed` must be strictly positive -- `speed <= 0` is rejected outright
    (review round fix wave: previously silently clamped to `1e-9` via the
    pacing division's own `max(speed, 1e-9)` guard, which turned a `0` or
    negative speed into an effectively-infinite sleep between EVERY event
    instead of a clean error; reproduced live as a genuinely hung
    subprocess while writing this fix's own test). `clients/cli.py`'s
    `--speed` argument ALSO validates at the argparse layer (a clean usage
    error before this function is ever called at all) -- this check is the
    defense-in-depth backstop for any other/future caller of this function
    directly."""
    if speed <= 0:
        raise ValueError(f"speed must be > 0, got {speed!r}")
    sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
    events_by_type: dict[str, int] = {}
    actions_by_origin: dict[str, int] = {}
    marks_by_kind: dict[str, int] = {}
    header: dict[str, Any] | None = None
    prev_event_ts: float | None = None

    for line in _iter_lines(path):
        kind = line.get("kind")
        marks_by_kind[kind] = marks_by_kind.get(kind, 0) + 1

        if kind == "header":
            header = line
            if line.get("format") != _format_version():
                _LOG.warning("replay: %s has format %r, this build expects %r -- "
                            "replaying best-effort anyway", path, line.get("format"),
                            _format_version())
            continue

        if kind == "event":
            # Review round fix wave (Minor): a line can be VALID JSON but
            # still missing/mistyped a field this branch assumes exists
            # ("ts"/"type" via direct indexing, "ts" used in arithmetic
            # below) -- a hand-edited or truncated log, or a future
            # producer bug. Caught HERE (parsing only, before any pacing/
            # engine-mutation happens) and skipped with a warning, matching
            # `_iter_lines`'s own malformed-JSON discipline -- never an
            # uncaught KeyError/TypeError crashing the whole replay over
            # ONE bad line.
            try:
                ts = line["ts"]
                if not isinstance(ts, int | float):
                    # Caught HERE (not left to blow up later in the pacing
                    # subtraction below, which only even runs when
                    # `instant=False`) so a bad "ts" type is skipped
                    # uniformly regardless of --instant.
                    raise TypeError(f"'ts' must be a number, got {type(ts).__name__}")
                ev = _midi_event_from_line(line)
            except (KeyError, TypeError) as exc:
                _LOG.warning("replay: skipping event line with missing/invalid fields "
                            "in %s: %s", path, exc)
                continue
            if not instant and prev_event_ts is not None:
                dt = (ts - prev_event_ts) / speed
                if dt > 0:
                    sleep_fn(dt)
            prev_event_ts = ts
            engine._handle(ev)
            events_by_type[ev.type] = events_by_type.get(ev.type, 0) + 1
            continue

        if kind == "action":
            origin = line.get("origin", "unknown")
            actions_by_origin[origin] = actions_by_origin.get(origin, 0) + 1
            if line.get("name") == "panic.release":
                _apply_panic_release_mark(engine, line, path)
            continue

        if kind == "page_changed":
            # Same discipline as the "event" branch above -- a
            # page_changed line missing its own "page" field is logged and
            # skipped (current_page left exactly as it was), not a crash.
            try:
                page = line["page"]
            except (KeyError, TypeError) as exc:
                _LOG.warning("replay: skipping page_changed line with missing/invalid "
                            "'page' in %s: %s", path, exc)
                continue
            engine._set_current_page(page)
            continue

        # "tempo" and any unknown/future kind: counted in marks_by_kind
        # above, no further handling -- see module docstring.

    return _build_summary(engine, path, header, events_by_type, actions_by_origin, marks_by_kind)


def _format_version() -> int:
    from midicrt.engine import capture as capture_mod
    return capture_mod.FORMAT_VERSION


def _build_summary(engine: Engine, path: str, header: dict | None,
                   events_by_type: dict[str, int], actions_by_origin: dict[str, int],
                   marks_by_kind: dict[str, int]) -> dict:
    voices_page = engine.pages.get("voices")
    harmony_page = engine.pages.get("harmony")
    transport_vm = engine.analyzers["status"].view_model()
    timesig_vm = engine.analyzers["timesig"].view_model()

    voices_state = None
    if voices_page is not None:
        voices_vm = voices_page.view_model()
        voices_state = {"total": voices_vm["total"], "total_peak": voices_vm["total_peak"]}

    harmony_state = None
    if harmony_page is not None:
        harmony_vm = harmony_page.view_model()
        chords = harmony_vm.get("chords") or []
        harmony_state = {
            "key": harmony_vm.get("key"),
            "last_chord": chords[0]["name"] if chords else None,
        }

    return {
        "file": path,
        "session_id": header.get("session_id") if header else None,
        "events_total": engine.events_total,
        "events_by_type": events_by_type,
        "actions_by_origin": actions_by_origin,
        "marks_by_kind": marks_by_kind,
        "current_page": engine.current_page,
        "final_state": {
            "voices": voices_state,
            "harmony": harmony_state,
            "transport": {
                "bar": transport_vm["bar"], "beat": transport_vm["beat"],
                "bpm": transport_vm["bpm"], "running": transport_vm["running"],
            },
            "timesig": timesig_vm,
        },
    }


def replay_session(path: str, *, speed: float = 1.0, instant: bool = False,
                   config: Config | None = None, keymap_path: str | None = None,
                   bindings_path: str | None = None,
                   sleep_fn: Callable[[float], None] | None = None) -> dict:
    """One-call convenience: build an offline engine, stream `path` through
    it, and clean up -- what `clients/cli.py`'s `midicrt replay` subcommand
    calls. Kept separate from `build_offline_engine`/`stream_session`
    (rather than folding this into the CLI handler directly) so a caller
    that needs the built `Engine` afterward (test_replay.py's own
    suppression-proof tests) can call those two functions directly
    instead."""
    engine = build_offline_engine(config=config, keymap_path=keymap_path,
                                  bindings_path=bindings_path)
    try:
        return stream_session(engine, path, speed=speed, instant=instant, sleep_fn=sleep_fn)
    finally:
        engine.stop()
