"""MIDI binding runtime (Phase 4 Task 2, docs/phase4-notes.md): lets a raw
MIDI event (not a client keypress) fire an engine action, with zero clients
attached -- the "sequencer drives the visualizer" capability `engine/
keymap.py`'s per-keypress model can never reach (a keymap needs a human at a
keyboard; a binding needs only a cable). Three pieces, mirroring keymap.py's
own split into a pure loader + a registry-aware validator:

  - `Binding`/`BindingMatch`: the persisted shape of one binding.
  - `BindingsFile`: `bindings.toml` load/save -- pure TOML <-> dataclass
    translation, no awareness of the engine's action registry at all (same
    "load_keymap has no registry to check against" precedent).
  - `BindingDispatcher`: pure event-matching state machine, `handle(ev) ->
    list[(action_name, args)]` -- also registry-unaware; `engine/core.py`'s
    `Engine._handle` collects its intents, `Engine._dispatch_bindings`
    (async, called from `run()` right after `_handle` returns -- see that
    method's own docstring for why dispatch can't happen synchronously
    inside `_handle`) is the one place an intent becomes a real
    `ActionRegistry.dispatch` call, and the ONE place a stale/roster-absent
    binding's `ActionError` is ever seen.
  - `validate_binding`: the separate, registry-aware step (mirrors
    `keymap.filter_known_actions`) that computes whether a binding's action
    exists and its `args` template satisfies that action's schema --
    consulted by `Engine.__init__`/`_config_reload` (to log a warning) and
    by the `bind.list` action handler (to report a `valid`/`error` field per
    binding, see that action's own docstring for why this is "kept-but-
    inert", not "dropped", unlike keymap's unknown-action handling).

Trigger vs continuous (task brief)
---------------------------------------------------------------------------
Every `Binding` is one of two `mode`s:
  - `"trigger"`: fires ONCE per qualifying edge. For `match.type ==
    "note_on"`, that's any note-on with velocity > 0 (a velocity-0 note_on
    is a running-status note-off, not a real trigger -- mirrors `engine/
    core.py::_handle`'s own MIDI-semantics carefulness elsewhere). For
    `match.type == "control_change"`, that's the CC value crossing
    `threshold` UPWARD -- see `BindingDispatcher`'s own docstring for the
    edge-detection state machine and why a binding's very first CC message
    never fires, regardless of value.
  - `"continuous"`: fires on EVERY qualifying `control_change` (no edge
    concept at all), lerping the raw 0-127 CC value into `range` and
    filling one named arg of `args` (the "args template", see
    `BindingDispatcher._continuous`'s own docstring for the design this
    picks for TOML's lack of a null literal).

Continuous mode wants an ABSOLUTE setter action, not a delta/increment one
(review finding, live-reproduced): a CC binding fires once per PHYSICAL
MOVE of the controller, and each firing carries the CC's raw 0-127 value
lerped into `range` -- if the bound action ADDS that value to its current
state (e.g. a `{"delta": float}`-shaped action), a knob sweep keeps adding
on top of whatever the PREVIOUS message already added, saturating to
whatever clamp the action enforces almost immediately regardless of the
knob's actual physical position (reproduced live: a sweep through
`pianoroll.zoom`'s own `{"delta": float}` shape hit `ZOOM_MAX` after a
handful of messages and stayed pinned there for the rest of the sweep --
not the "knob position tracks the parameter" behavior anyone binding a
fader to a continuous parameter actually wants). An action meant to be a
continuous-binding TARGET must instead accept the parameter's intended
ABSOLUTE value directly (e.g. `pianoroll.zoom_level {level: float}`, added
alongside the pre-existing cumulative `pianoroll.zoom {delta: float}`) --
`validate_binding` cannot detect this distinction from the schema alone
(both shapes declare one `"float"` arg), so it is a convention documented
here, not a mechanically enforced one: delta-style actions are for TRIGGER
mode (or a normal keymap/CLI one-shot nudge), absolute-setter actions are
for CONTINUOUS mode.

Every `Binding.action`'s `args` are dispatched THROUGH `ActionRegistry.
dispatch` exactly like any other action call -- schema coercion/validation
happens there, once, not duplicated here (mirrors `dispatch_key` in
`clients/base.py` never re-implementing `ActionRegistry`'s own arg
handling).
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)

DEFAULT_PATH = os.path.expanduser("~/.config/midicrt/bindings.toml")

# Recognized `match.type` values -- the only two MIDI message types a
# binding can watch (mirrors docs/superpowers plan's own "learn must
# qualify note_on (velocity>0) and control_change only" rule for Task 3;
# Task 2 has no learn yet, but a hand-edited/malformed bindings.toml can
# still name anything, so load-time shape validation enforces the same set
# now).
_MATCH_TYPES = {"note_on", "control_change"}
_MODES = {"trigger", "continuous"}


def is_learnable_event(ev) -> bool:
    """Phase 4 Task 3 (DAW-style MIDI learn): is `ev` a MidiEvent a `bind.
    learn` arm can capture? Only `control_change` (any value -- a fader
    sitting anywhere still identifies which controller it is) or `note_on`
    with velocity > 0 (mirrors `BindingDispatcher._trigger`'s own "a
    velocity-0 note_on is a running-status note-off, not a real trigger"
    rule, and `_MATCH_TYPES` above -- a binding can only ever WATCH these
    two event types, so learn can only ever CAPTURE from them too).

    Every other `MidiEvent.type` this engine ever queues -- `clock_tick`
    (synthesized, never a real controller identity), `note_off`, transport
    `start`/`stop`/`continue`/`songpos` (global transport messages, not
    tied to any one controller/knob), `sysex` (consumed engine-side before
    this would ever run, see `Engine._handle_sysex`), `program_change`
    (single data byte, no natural "trigger" semantics a binding's `match`
    shape can represent) -- is disqualified simply by falling through both
    checks below; no explicit type-by-type exclusion list is needed since
    `_MATCH_TYPES` is already the complete set of learnable shapes."""
    if ev.type == "control_change":
        return True
    return ev.type == "note_on" and bool(ev.data2)

# TOML has no null literal, so a continuous binding's "this arg gets filled
# from the lerped CC value" marker can't be a real Python `None` on disk --
# see `BindingDispatcher._continuous`'s own docstring and this module's
# "Continuous args-template <-> TOML" section below for the full
# translation. This exact string is reserved: an action that genuinely
# needs it as a literal static arg value cannot be bound (disclosed,
# extremely unlikely to matter -- no shipped action takes a string arg
# resembling a sentinel like this today).
CONTINUOUS_FILL_TOKEN = "$midicrt_fill_from_cc$"

# Phase 4 Task 3 (DAW-style MIDI learn, docs/phase4-notes.md): how long a
# single armed learn slot (`Engine._bind_learn`) waits for a qualifying
# MidiEvent before auto-cancelling (`Engine._tick_learn`, called from
# `run()` once per tick -- "the engine tick path" the task brief names,
# same mechanism `_tick_analyzers`/`_tick_pages`/`_tick_behaviors` already
# use for injected-wall-clock work). A plain module constant, not a
# `Config` field -- this is a fixed UX timing decision (how long a human
# has to hit a key/knob after arming), not a per-deployment tunable
# anything in this codebase's `config.toml` precedent would suggest making
# adjustable (contrast `tick_hz`/`pagecycle_idle_s`, which really do vary
# per rig). Both `engine/core.py`'s own timeout check AND `clients/cli.py`'s
# `midicrt bind learn` wait-timeout default (this value plus slack for
# wire round-trip latency) read this SAME constant rather than each
# hardcoding an independently-maintained "30".
LEARN_TIMEOUT_S = 30.0


@dataclass
class BindingMatch:
    """What a `Binding` watches for. `type`/`number` are required (a
    binding always names exactly one note or CC number); `channel`/
    `port_pattern` default to `None` ("any channel"/"any port" -- see
    `BindingDispatcher._matches`).

    `channel` is 0-INDEXED, matching `MidiEvent.channel` directly (mido's
    own convention) -- NOT the human 1-indexed display `engine/
    midi_in.py::translate()`'s `summary` strings use (`ch{channel + 1}`).
    `number` is the note number for `type == "note_on"`, the controller
    number for `type == "control_change"` -- `MidiEvent.data1` in both
    cases (see `engine/midi_in.py::translate()`)."""
    type: str
    number: int
    channel: int | None = None
    port_pattern: str | None = None


@dataclass
class Binding:
    """One persisted MIDI binding. `id` is a short, stable, TOML-table-key-
    safe string -- unique by construction once loaded from `bindings.toml`
    (duplicate `[bindings.<id>]` headers are themselves a TOML syntax
    error, so `BindingsFile.load` never has to de-duplicate). `args` is
    always a real Python dict; for `mode == "continuous"` exactly one entry
    is the literal Python `None` -- the "fill from the lerped CC value"
    marker (see `BindingDispatcher._continuous`'s own docstring) -- every
    other entry (both modes) is a static value dispatched verbatim.
    `threshold` only has an effect for `mode == "trigger"` +
    `match.type == "control_change"`; `range` only has an effect for
    `mode == "continuous"` -- both keep their default when irrelevant
    rather than being `None`, so a `Binding` built directly in a test or by
    a future `bind.learn` (Task 3) never has to special-case "which fields
    actually matter for this mode" beyond what `mode` itself already
    says."""
    id: str
    match: BindingMatch
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    mode: str = "trigger"
    threshold: int = 64
    range: tuple[float, float] = (0.0, 1.0)


def _lerp(value: int, lo: float, hi: float) -> float:
    """Map a raw 0-127 MIDI value into `[lo, hi]`. Works unmodified for an
    "inverted" range (`lo > hi`, e.g. `(1.0, 0.0)`) -- plain linear
    interpolation needs no special-casing for which endpoint is numerically
    larger, it only cares which one `t=0`/`t=1` land on."""
    t = max(0, min(127, value)) / 127.0
    return lo + t * (hi - lo)


class BindingDispatcher:
    """Pure event-matching state machine -- no I/O, no `ActionRegistry`
    reference, mirrors `behaviors/pagecycle.py`'s own "returns an intent,
    never dispatches" contract (see that module's docstring) one level up:
    a behavior's `tick()` returns AT MOST one intent per call; `handle(ev)`
    here can return SEVERAL (every matching binding gets a chance at the
    same event -- e.g. two bindings on the same note, different channels,
    or a trigger and a continuous binding sharing a CC number are all
    legitimate simultaneously).

    `engine/core.py::Engine._handle` calls `handle(ev)` for every incoming
    `MidiEvent` and queues the returned intents; `Engine._dispatch_bindings`
    (async, called from `run()` right after `_handle` -- see that module's
    docstring's "dispatch context decision") is the only place an intent
    becomes a real `ActionRegistry.dispatch(...)` call. This split exists
    because `_handle` is synchronous and `ActionRegistry.dispatch` is a
    coroutine -- the exact same constraint `behaviors/pagecycle.py`'s
    `tick()` vs `Engine._tick_behaviors` already solves, reused rather than
    re-invented (docs/phase4-notes.md's own "the behavior pattern;
    recommended" framing)."""

    def __init__(self, bindings: list[Binding] | None = None):
        self.set_bindings(bindings or [])

    def set_bindings(self, bindings: list[Binding]) -> None:
        """Replace the live binding set (used at construction, at
        `config.reload`, and after `bind.remove`). Also clears the CC
        edge-detection baseline (`_last_cc_value`) unconditionally --
        simplest correct option: stale entries keyed by a removed
        binding's id would otherwise leak forever, and re-establishing a
        fresh baseline costs at most one "missed" edge right after a
        reload (see `_cc_trigger`'s own docstring for why a binding's
        very first CC message never fires anyway -- a post-reload baseline
        reset is the SAME cost a freshly booted engine already pays)."""
        self._bindings = list(bindings)
        self._last_cc_value: dict[tuple[str, str, int | None], int] = {}

    def handle(self, ev) -> list[tuple[str, dict]]:
        intents = []
        for binding in self._bindings:
            if not self._matches(binding, ev):
                continue
            intent = self._evaluate(binding, ev)
            if intent is not None:
                intents.append(intent)
        return intents

    def _matches(self, binding: Binding, ev) -> bool:
        m = binding.match
        if ev.type != m.type:
            return False
        if ev.data1 != m.number:
            return False
        if m.channel is not None and ev.channel != m.channel:
            return False
        return m.port_pattern is None or fnmatch.fnmatch(ev.source or "", m.port_pattern)

    def _evaluate(self, binding: Binding, ev) -> tuple[str, dict] | None:
        if binding.mode == "trigger":
            return self._trigger(binding, ev)
        if binding.mode == "continuous":
            return self._continuous(binding, ev)
        return None   # unknown mode -- defensive; load-time validation never produces this

    def _trigger(self, binding: Binding, ev) -> tuple[str, dict] | None:
        if binding.match.type == "note_on":
            if not ev.data2:   # velocity 0 (or, defensively, None) -- not a real trigger
                return None
            return (binding.action, dict(binding.args))
        if binding.match.type == "control_change":
            return self._cc_trigger(binding, ev)
        return None

    def _cc_trigger(self, binding: Binding, ev) -> tuple[str, dict] | None:
        """Fire only on a lo->hi crossing of `binding.threshold`, tracked
        per (binding, source, channel) -- so two bindings sharing a
        physical CC/channel with different thresholds (or the same
        binding's fnmatch port_pattern covering several real ports) each
        get their own independent edge state, never interfering with one
        another.

        No prior value recorded for this key means there is no "lo" side
        to have crossed FROM -- an edge is only observable across TWO
        samples, so a binding's very first CC message ever seen only
        establishes the baseline and never fires, however high its value
        already is. This is a deliberate, disclosed choice over the
        alternative (treat "no prior value" as if it were 0, so an
        already-high controller fires immediately on the first message):
        a hardware fader/knob already sitting above `threshold` when the
        daemon boots (or right after a `config.reload`, see `set_bindings`)
        must not spuriously fire before anyone has actually moved it."""
        key = (binding.id, ev.source, ev.channel)
        prev = self._last_cc_value.get(key)
        self._last_cc_value[key] = ev.data2
        if prev is None:
            return None
        if prev < binding.threshold <= ev.data2:
            return (binding.action, dict(binding.args))
        return None

    def _continuous(self, binding: Binding, ev) -> tuple[str, dict] | None:
        """Every qualifying event produces an intent -- no edge/threshold
        concept for continuous mode, unlike `_cc_trigger`. The lerped value
        fills the ONE `args` entry whose value is `None` (the "args
        template" the task brief describes, e.g. `{"level": None}`) --
        every other `args` entry is copied through unchanged as a static
        value. See `BindingsFile`'s own module-level "Continuous
        args-template <-> TOML" section for why the in-memory `None`
        marker (matching the brief's own illustration) and the on-disk
        `CONTINUOUS_FILL_TOKEN` string are deliberately different
        representations either side of the load/save boundary."""
        fill_keys = [k for k, v in binding.args.items() if v is None]
        if len(fill_keys) != 1:
            # Defensive only -- `BindingsFile.load`'s shape validation
            # already refuses to construct a continuous Binding without
            # exactly one fill marker; a hand-built Binding (tests, a
            # future bind.learn bypassing the file) that violates this is
            # simply inert rather than crashing the engine loop.
            return None
        args = dict(binding.args)
        args[fill_keys[0]] = _lerp(ev.data2, *binding.range)
        return (binding.action, args)


def validate_binding(binding: Binding, actions: dict) -> str | None:
    """Registry-aware validity check (mirrors `keymap.
    filter_known_actions`'s own separation from the pure loader) -- `None`
    means valid, otherwise a short human-readable reason. `actions` is
    `ActionRegistry.describe()`'s own shape (`{name: {"description":...,
    "args": {arg: type_name}}}`).

    Unlike `filter_known_actions` (which DROPS an unbindable keymap entry),
    an invalid binding is never dropped by this function's callers -- see
    `bind.list`'s own docstring (engine/core.py) for why "kept-but-inert,
    with its invalidity visible" is the disposition this task picks: a
    binding can become invalid AFTER being saved (e.g. hand-editing
    bindings.toml, or a future config with a smaller page roster), and a
    user diagnosing "why doesn't my binding fire" needs `bind.list` to be
    able to SHOW them the stale entry, not silently make it vanish.

    For `mode == "trigger"`: `binding.args` must exactly satisfy the
    action's schema (every declared arg present, no unknown extras) --
    `ActionRegistry.dispatch`'s own arg handling is not duplicated here,
    just pre-checked so `bind.list` can report it without waiting for a
    real MIDI event to prove it.

    For `mode == "continuous"`: exactly one `args` entry must be the fill
    marker (`None`), that key must be a schema arg declared type `"float"`
    (the "action's declared float arg" the task brief names), and every
    OTHER `args` entry must satisfy the remaining schema exactly like
    trigger mode.

    Review fix (Minor, task-3 follow-up): for `mode == "trigger"`, NO
    `args` entry may be `None` -- unlike continuous mode, trigger has no
    fill-target concept at all (see `Binding`'s own docstring: "for mode
    == continuous exactly one entry is the literal Python None ... every
    OTHER entry (BOTH modes) is a static value"), so a `None` here can
    only ever be a caller mistake (most plausibly `bind.learn` handed a
    JSON `null` for a static arg). Left unchecked, it would silently
    coerce to the STRING `"None"` at real dispatch time
    (`ActionRegistry.dispatch`'s own `str(None)` for a `"str"`-typed arg)
    instead of failing loudly -- caught here so `bind.learn`'s arm-time
    validation (its own docstring, engine/core.py) surfaces a clear
    `ActionError` instead of ever persisting a binding that would
    misbehave the first time it fires."""
    info = actions.get(binding.action)
    if info is None:
        return f"unknown action: {binding.action!r}"
    schema = info.get("args", {})
    args = dict(binding.args)
    fill_key = None
    if binding.mode == "continuous":
        fill_keys = [k for k, v in args.items() if v is None]
        if len(fill_keys) != 1:
            return (f"continuous binding must have exactly one args entry marked as the "
                    f"fill target, found {len(fill_keys)}")
        fill_key = fill_keys[0]
        if schema.get(fill_key) != "float":
            return (f"fill arg {fill_key!r} is not declared 'float' by action "
                    f"{binding.action!r} (schema: {schema!r})")
        del args[fill_key]
    elif binding.mode == "trigger":
        none_args = sorted(k for k, v in args.items() if v is None)
        if none_args:
            return (f"trigger binding args must not contain None (found in {none_args}) -- "
                    f"None is only meaningful for a continuous binding's fill-target arg")
    provided = set(args)
    declared = set(schema) - ({fill_key} if fill_key is not None else set())
    missing = declared - provided
    if missing:
        return f"missing args for {binding.action!r}: {sorted(missing)}"
    extra = provided - declared
    if extra:
        return f"unknown args for {binding.action!r}: {sorted(extra)}"
    return None


# -- TOML <-> Binding translation ---------------------------------------------
#
# Continuous args-template <-> TOML: `Binding.args`' in-memory fill marker is
# a real Python `None` (matching the task brief's own `{"level": null}`
# illustration of the RUNTIME shape) -- but TOML has no null literal at all,
# so it cannot be written or read back verbatim. `CONTINUOUS_FILL_TOKEN` is
# the on-disk stand-in: `_binding_to_raw` writes it in place of `None`;
# `_parse_binding_entry` reads it back and restores real `None` when
# reconstructing the `Binding`. This keeps the TOML-format quirk confined to
# this load/save boundary -- `BindingDispatcher`/`validate_binding` (both
# tested directly against real `Binding` objects) never see the token at
# all.
#
# Review fix (Important): this translation is scoped to `mode ==
# "continuous"` ONLY, on both the load and save sides -- it used to run
# unconditionally for every binding regardless of mode, so a TRIGGER
# binding carrying the literal token string as a genuine static arg value
# (e.g. an action argument that happens to equal `CONTINUOUS_FILL_TOKEN`)
# was silently corrupted to `None` on load, with no fill-target concept to
# even make sense of it. See `test_bindingsfile_load_trigger_binding_with_
# literal_sentinel_string_keeps_it_verbatim` (test_bindings.py).

def _parse_match(binding_id: str, raw: Any) -> BindingMatch | None:
    if not isinstance(raw, dict):
        _LOG.warning("bindings: %r has a malformed 'match' (must be a table), skipping "
                     "the whole binding: %r", binding_id, raw)
        return None
    match_type = raw.get("type")
    # `isinstance` check MUST come before the `in _MATCH_TYPES` set-membership
    # test, not after -- an unhashable raw value (e.g. `type = ["oops"]`,
    # legal TOML, parses to a Python list) makes `x not in a_set` raise an
    # uncaught `TypeError`, not a clean "unrecognized value" -- the exact
    # "wrong-shaped-but-syntactically-valid TOML crashes the loader" bug
    # class `engine/keymap.py`'s own re-review fix (see that module's
    # docstring) already found and fixed for `keys`; guarded here from the
    # start rather than waiting for the same class of bug to be
    # rediscovered live.
    if not isinstance(match_type, str) or match_type not in _MATCH_TYPES:
        _LOG.warning("bindings: %r has an unrecognized match.type %r (must be one of %s), "
                     "skipping", binding_id, match_type, sorted(_MATCH_TYPES))
        return None
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or not (0 <= number <= 127):
        _LOG.warning("bindings: %r has a malformed match.number (must be an int 0-127), "
                     "got %r, skipping", binding_id, number)
        return None
    channel = raw.get("channel")
    if channel is not None and (not isinstance(channel, int) or isinstance(channel, bool)
                                or not (0 <= channel <= 15)):
        _LOG.warning("bindings: %r has a malformed match.channel (must be an int 0-15 or "
                     "absent), got %r, skipping", binding_id, channel)
        return None
    port_pattern = raw.get("port_pattern")
    if port_pattern is not None and not isinstance(port_pattern, str):
        _LOG.warning("bindings: %r has a malformed match.port_pattern (must be a string "
                     "or absent), got %r, skipping", binding_id, port_pattern)
        return None
    return BindingMatch(type=match_type, number=number, channel=channel,
                        port_pattern=port_pattern)


def _parse_binding_entry(binding_id: str, raw: Any) -> Binding | None:
    """Shape-validate and construct one `[bindings.<id>]` entry -- same
    "log + skip THIS entry, keep going" discipline `engine/keymap.py::
    load_keymap`'s own per-entry validation established, so one malformed
    binding in an otherwise-good bindings.toml never takes down every
    other binding (let alone the whole daemon -- see `BindingsFile.
    load_or_warn` for the file-level, syntax-error half of that same
    resilience contract)."""
    if not isinstance(raw, dict):
        _LOG.warning("bindings: %r is not a table, skipping: %r", binding_id, raw)
        return None
    action = raw.get("action")
    if not isinstance(action, str) or not action:
        _LOG.warning("bindings: %r is missing a valid 'action' string, skipping", binding_id)
        return None
    mode = raw.get("mode", "trigger")
    # Same unhashable-value guard as `_parse_match`'s `match_type` check
    # above (e.g. `mode = ["oops"]`) -- isinstance BEFORE set membership.
    if not isinstance(mode, str) or mode not in _MODES:
        _LOG.warning("bindings: %r has an unrecognized mode %r (must be one of %s), "
                     "skipping", binding_id, mode, sorted(_MODES))
        return None
    match = _parse_match(binding_id, raw.get("match"))
    if match is None:
        return None
    args_raw = raw.get("args", {})
    if not isinstance(args_raw, dict):
        _LOG.warning("bindings: %r has a malformed 'args' (must be a table), skipping",
                     binding_id)
        return None
    threshold = raw.get("threshold", 64)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not (0 <= threshold <= 127):
        _LOG.warning("bindings: %r has a malformed 'threshold' (must be an int 0-127), "
                     "got %r, skipping", binding_id, threshold)
        return None
    # Review fix (Important): the sentinel->None translation must be scoped
    # to `mode == "continuous"` ONLY -- it used to run unconditionally for
    # every binding, so a TRIGGER binding carrying `CONTINUOUS_FILL_TOKEN`
    # as a genuine literal string arg value (an unlikely but real
    # collision) was silently corrupted to `None` even though trigger mode
    # has no fill-target concept at all. A trigger binding's `args` are now
    # copied through completely verbatim; only a continuous binding's args
    # table is ever scanned for the token (and only that single value is
    # actually meaningful there -- see the "exactly one fill marker" check
    # below, which is what identifies THE designated fill key, not merely
    # "any key that happens to match").
    args = dict(args_raw)
    if mode == "continuous":
        args = {k: (None if v == CONTINUOUS_FILL_TOKEN else v) for k, v in args.items()}
    range_ = (0.0, 1.0)
    if mode == "continuous":
        range_raw = raw.get("range")
        if (not isinstance(range_raw, list) or len(range_raw) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                          for v in range_raw)):
            _LOG.warning("bindings: %r is mode=continuous but 'range' is missing/malformed "
                         "(must be [lo, hi]), got %r, skipping", binding_id, range_raw)
            return None
        range_ = (float(range_raw[0]), float(range_raw[1]))
        fill_keys = [k for k, v in args.items() if v is None]
        if len(fill_keys) != 1:
            _LOG.warning("bindings: %r is mode=continuous but its args table has %d fill "
                         "markers (%r) instead of exactly one, skipping", binding_id,
                         len(fill_keys), CONTINUOUS_FILL_TOKEN)
            return None
    return Binding(id=binding_id, match=match, action=action, args=args, mode=mode,
                   threshold=threshold, range=range_)


def _load_raw_bindings(path: str) -> dict[str, Any]:
    """Pure parse: read `path` and return its top-level `bindings` table
    (empty dict if the file has none). Raises `tomllib.TOMLDecodeError` on
    a syntax error and `ValueError` on a wrong-shaped top-level `bindings`
    key -- mirrors `engine/keymap.py::load_keymap`'s own two-tier failure
    modes exactly (see that function's docstring for why `ValueError`, not
    `TypeError` -- ruff TRY004 -- matches `load_or_warn`'s catch tuple)."""
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    bindings = raw.get("bindings", {})
    if not isinstance(bindings, dict):
        raise ValueError(  # noqa: TRY004
            f"bindings.toml's top-level 'bindings' must be a table of id=binding "
            f"pairs, got {type(bindings).__name__}: {bindings!r}")
    return bindings


class BindingsFile:
    """In-memory `bindings.toml` plus its own atomic save -- the ONE place
    the engine ever writes a config file to disk (`config.toml`/
    `keymap.toml` are both engine-READ-ONLY, per docs/superpowers plan's
    own "engine writes bindings.toml ONLY inside bind.learn/bind.remove"
    constraint). `Engine` owns one instance (`self._bindings_file`,
    engine/core.py) and feeds its `.bindings` list into a
    `BindingDispatcher`; `bind.remove` (Task 2) and `bind.learn`/`bind.
    cancel` (Task 3) are the only call sites that ever mutate one and
    save()."""

    def __init__(self, bindings: list[Binding] | None = None, path: str | None = None):
        self.bindings: list[Binding] = list(bindings or [])
        self.path = path or DEFAULT_PATH

    @classmethod
    def load(cls, path: str | None = None) -> BindingsFile:
        """Read `path` (default `~/.config/midicrt/bindings.toml`, tolerant
        of a missing file -- returns an empty `BindingsFile`, there is no
        "built-in default bindings" concept the way `keymap.py` has
        `DEFAULT_KEYMAP`, since a binding only ever comes from an explicit
        `bind.learn` or a hand-written file). Tolerant of unknown keys at
        every level (a binding's table, or its `match` sub-table) --
        anything not recognized is silently ignored rather than rejected,
        matching `config.py::load()`'s own "unknown top-level keys are
        ignored" convention. Raises `ValueError`/`tomllib.TOMLDecodeError`
        on a genuinely malformed FILE (bad syntax, or `bindings` not a
        table at all) -- `load_or_warn` is the safe wrapper production
        call sites actually use, see its own docstring."""
        path = path or DEFAULT_PATH
        raw_bindings = _load_raw_bindings(path)
        bindings = []
        for binding_id, raw in raw_bindings.items():
            binding = _parse_binding_entry(binding_id, raw)
            if binding is not None:
                bindings.append(binding)
        return cls(bindings, path=path)

    @classmethod
    def load_or_warn(cls, path: str | None = None) -> tuple[BindingsFile | None, str | None]:
        """Safe wrapper around `load` (mirrors `engine/keymap.py::
        load_keymap_or_warn` exactly, same live-reproduced-incident
        rationale: a malformed OPTIONAL config file must never crash
        daemon startup, nor tear down a `config.reload` requester's
        connection). Returns `(bindings_file, None)` on success or `(None,
        warning_message)` on failure -- deliberately never raises."""
        try:
            return cls.load(path), None
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            return None, f"bindings.toml failed to load ({path or DEFAULT_PATH}): {exc}"

    def get(self, binding_id: str) -> Binding | None:
        return next((b for b in self.bindings if b.id == binding_id), None)

    def add(self, binding: Binding) -> None:
        self.bindings.append(binding)

    def remove(self, binding_id: str) -> bool:
        """Drop the binding with this id in place. Returns whether
        anything was actually removed -- callers (`Engine._bind_remove`)
        decide what an unknown id means for their own call site (there,
        raising `ActionError`, matching `_page_goto`'s own "unknown named
        resource" precedent)."""
        before = len(self.bindings)
        self.bindings = [b for b in self.bindings if b.id != binding_id]
        return len(self.bindings) != before

    def save(self, path: str | None = None) -> None:
        """Atomically rewrite the whole file from `self.bindings` (tmp file
        in the SAME directory + `os.replace` -- same directory is required
        for `os.replace` to be an atomic rename rather than a cross-
        filesystem copy). Creates the parent directory if missing (a fresh
        `~/.config/midicrt/` with no `keymap.toml` either yet). A save
        always rewrites the ENTIRE file from the in-memory list -- hand
        edits made between loads are preserved as DATA (re-parsed on the
        next `load`/`load_or_warn`), but any comments/formatting a human
        added are not (plain TOML has no comment-preservation story without
        a much heavier round-tripping library this project deliberately
        doesn't depend on -- see module docstring's "Tech Stack" framing in
        the phase-4 plan: a minimal in-repo serializer, no new dependency).
        This is the same disclosed trade-off the phase-4 plan's own
        "machine-managed; hand-edits allowed but comments are not
        preserved by learn-writes" header comment (written below) exists to
        warn a human editor about."""
        path = path or self.path
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        text = _dump_toml(self.bindings)
        fd, tmp_path = tempfile.mkstemp(prefix=".bindings-", suffix=".toml.tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp_path, path)
        except BaseException:
            # Best-effort cleanup of the not-yet-renamed tmp file; the
            # ORIGINAL exception (a full disk, a permissions error, ...) is
            # what actually propagates, never whatever unlink() raises.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise


_HEADER = """\
# midicrt bindings.toml -- machine-managed by `midicrt bind` operations
# (learn/remove). Hand-editing is supported (loaded like any other file),
# but comments and formatting here are NOT preserved: any save rewrites
# this whole file from the engine's in-memory binding list. See
# docs/phase4-bindings.md for the schema reference.
"""


def _toml_string(value: str) -> str:
    """Minimal TOML basic-string literal for `value`. TOML basic strings
    share JSON's escaping rules for the characters that matter here
    (backslash, double-quote, control characters) -- `json.dumps` already
    implements exactly that, correctly, so this reuses it rather than
    hand-rolling a second escaper (see module docstring's "Tech Stack"
    note: no new TOML-writing dependency, but stdlib `json` is free)."""
    return json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    raise TypeError(f"unsupported TOML value for bindings.toml: {value!r}")


def _binding_to_raw(binding: Binding) -> str:
    lines = [f"[bindings.{_toml_string(binding.id)}]"]
    lines.append(f"action = {_toml_string(binding.action)}")
    lines.append(f"mode = {_toml_string(binding.mode)}")
    lines.append(f"threshold = {binding.threshold}")
    if binding.mode == "continuous":
        lines.append(f"range = [{binding.range[0]!r}, {binding.range[1]!r}]")
    if binding.args:
        lines.append("")
        lines.append(f"[bindings.{_toml_string(binding.id)}.args]")
        for key, value in binding.args.items():
            # Symmetric with the load-side fix above: only a CONTINUOUS
            # binding's `None` fill marker is ever translated to the
            # on-disk sentinel. A trigger binding's args are never expected
            # to contain a real `None` at all (there is no fill-target
            # concept there) -- if one somehow did, `_toml_value` raising
            # `TypeError` on it is the honest failure, not a silent,
            # wrong-mode sentinel substitution.
            on_disk = CONTINUOUS_FILL_TOKEN if (binding.mode == "continuous"
                                                and value is None) else value
            lines.append(f"{_toml_string(key)} = {_toml_value(on_disk)}")
    lines.append("")
    lines.append(f"[bindings.{_toml_string(binding.id)}.match]")
    lines.append(f"type = {_toml_string(binding.match.type)}")
    lines.append(f"number = {binding.match.number}")
    if binding.match.channel is not None:
        lines.append(f"channel = {binding.match.channel}")
    if binding.match.port_pattern is not None:
        lines.append(f"port_pattern = {_toml_string(binding.match.port_pattern)}")
    return "\n".join(lines) + "\n"


def _dump_toml(bindings: list[Binding]) -> str:
    parts = [_HEADER]
    for binding in bindings:
        parts.append("")
        parts.append(_binding_to_raw(binding))
    return "\n".join(parts)
