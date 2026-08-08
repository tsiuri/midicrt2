"""Central action registry: every engine capability is a named action (spec §4)."""
import inspect
import json
from collections.abc import Callable
from typing import Any


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"not a bool: {value!r}")


def _parse_dict(value):
    """Phase 4 Task 3 (docs/phase4-notes.md): the one non-scalar arg type,
    added for `bind.learn {action, mode, args}` -- `args` is itself a
    nested k/v template for the TARGET action being learned. A real
    `client.action(...)` call over the wire already carries `args` as a
    genuine JSON object (proto.py encodes/decodes plain JSON, so a client
    never needs to stringify a dict it's about to send), so the common case
    is a pass-through identity check. The JSON-string fallback exists so
    the generic `midicrt action bind.learn --arg args=...` CLI path (whose
    `--arg k=v` always produces STRING values, see clients/cli.py::
    _parse_args) can reach this exact same coercer, not just the dedicated
    `midicrt bind learn` subcommand (which sends a real dict directly)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {value!r}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"not a JSON object: {value!r}")  # noqa: TRY004
        return parsed
    raise ValueError(f"not an object: {value!r}")


_COERCERS = {"int": int, "float": float, "str": str, "bool": _parse_bool, "dict": _parse_dict}


class ActionError(Exception):
    pass


class ActionRegistry:
    def __init__(self):
        # Fix wave (2026-08-07, Minor finding): this annotation was a stale
        # 4-tuple left over from before `register()`'s own `defaults`
        # parameter (Phase 5 Task 3, docs/phase5-notes.md cheap-wins bundle)
        # started storing a genuine 5th element (`dict(defaults or {})`,
        # see `register()` below) -- the runtime value has been a 5-tuple
        # ever since; only the type annotation never caught up.
        self._actions: dict[str, tuple[Callable, str, dict[str, str], bool, dict[str, Any]]] = {}
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md): an
        # OPTIONAL hook, set at most once (`Engine.__init__` wires it to
        # `Engine._on_action_dispatched`) and fired after every SUCCESSFUL
        # dispatch (never for one that raised `ActionError` -- see below).
        # This is how three of capture's four provenance origins
        # ("binding:<id>", "behavior", "client") get their action marks
        # stamped without every dispatch call site needing to know
        # `CaptureSink` exists at all -- only the NEW `origin` kwarg. The
        # fourth origin ("sysex") never reaches here at all (sysex mutates
        # engine state through private methods directly, bypassing this
        # registry entirely -- see engine/core.py's sysex handlers, which
        # call `Engine._capture.record_action(..., origin="sysex")`
        # themselves). `None` (the default, e.g. any test constructing a
        # bare `ActionRegistry()`) means no hook is called at all -- this
        # method's existing callers/behavior are completely unchanged when
        # no hook is wired.
        self._on_dispatch: Callable[[str, dict, str, dict], None] | None = None

    def register(self, name, handler, description="", args=None, defaults=None):
        # Fix-wave addition (docs/phase5-notes.md, capture mark-
        # completeness review): register-time introspection for an
        # OPT-IN "this handler wants its own dispatch origin" signal --
        # a handler is origin-aware iff it declares a parameter literally
        # named `origin` (checked ONCE here, not on every dispatch).
        # `Engine._capture_stop_action` is the one handler that needs
        # this today: it must record its OWN action mark BEFORE actually
        # stopping (the NORMAL post-dispatch hook fires too late for
        # capture.stop specifically -- see that method's own docstring),
        # which requires knowing its origin before `dispatch()` would
        # otherwise report it. `origin` is NEVER exposed as a client-
        # suppliable wire arg -- see `dispatch`'s own comment at the
        # injection site for why it's kept out of `args`/`schema`
        # entirely.
        #
        # Fix wave (2026-08-07, Minor finding, closes a ledgered landmine
        # from the Task-1 review): that "never exposed" claim right above
        # was only ever true by convention, not enforced -- nothing stopped
        # a future action's `args` schema from declaring its OWN arg
        # literally named `origin`. Two ways that goes wrong at dispatch
        # time depending on `wants_origin`: (1) the handler DOES want
        # origin -- `dispatch`'s `handler_kwargs["origin"] = origin`
        # silently OVERWRITES whatever the client supplied under that same
        # key, so a client-suppliable `origin` schema arg would be
        # accepted, coerced, recorded in the action mark's own `args` dict
        # by `_on_dispatch`/`record_action` (confusingly duplicating the
        # mark's real top-level `origin` field with client-controlled
        # noise) and then just... discarded, never reaching the handler at
        # all; (2) the handler does NOT want origin, but the schema arg is
        # still named `origin` -- `handler(**{"origin": coerced_value})`
        # then raises a raw `TypeError` for any handler whose signature
        # doesn't accept it, ESCAPING `dispatch`'s own `except ActionError`
        # narrowing (`engine/server.py::ProtocolServer._dispatch`'s
        # identical narrowing too) and tearing down the requesting client
        # connection -- the same class of raw-exception-past-ActionError
        # bug this fix wave's `_capture_start_action`/`_capture_stop_action`
        # OSError conversions exist to prevent, just reachable from a
        # schema typo instead of a full disk. Raising here, at REGISTER
        # time (once, at daemon boot / test construction), turns a future
        # landmine into an immediate, readable startup failure instead of
        # a live dispatch-time surprise.
        if "origin" in (args or {}):
            raise ValueError(
                f"action {name!r}: schema arg name 'origin' is reserved for the "
                "origin-injection mechanism (see the comment right above) -- "
                "rename this arg in its `args` schema"
            )
        wants_origin = "origin" in inspect.signature(handler).parameters
        # Phase 5 Task 3 (CLI `--range` for continuous learn, docs/
        # phase5-notes.md cheap-wins bundle): `defaults` is an OPT-IN,
        # per-arg fallback used ONLY when the caller's wire-level `args`
        # dict omits that key outright -- see `dispatch`'s own comment at
        # the consumption site for why this is the minimal way to add a
        # genuinely OPTIONAL wire arg without breaking every existing
        # caller of an action that predates the new arg (every schema arg
        # was, until now, unconditionally REQUIRED -- see the "missing
        # arg" check below). Stored ALREADY-COERCED (the caller passes the
        # real Python value an action handler expects, e.g. `range=""` for
        # `bind.learn`'s optional `range: str` arg) -- `dispatch` never
        # re-runs a default through `_COERCERS`, only a genuinely
        # CALLER-supplied value ever is. `describe()` deliberately does
        # NOT expose this (unchanged wire shape: `{"description":...,
        # "args": {arg: type_name}}`) -- a default's mere EXISTENCE isn't
        # something any current consumer (a client, `validate_binding`,
        # `keymap.filter_known_actions`) needs to know; "is this arg
        # required" isn't a distinction this codebase's action vocabulary
        # has ever needed to expose over the wire.
        self._actions[name] = (handler, description, dict(args or {}), wants_origin,
                               dict(defaults or {}))

    def set_dispatch_hook(self, callback: Callable[[str, dict, str, dict], None] | None) -> None:
        self._on_dispatch = callback

    def describe(self) -> dict:
        return {
            name: {"description": desc, "args": args}
            for name, (_, desc, args, _wants_origin, _defaults) in sorted(self._actions.items())
        }

    async def dispatch(self, name: str, args: dict, *, origin: str = "unknown") -> dict:
        if name not in self._actions:
            raise ActionError(f"unknown action: {name}")
        handler, _, schema, wants_origin, defaults = self._actions[name]
        if set(args) - set(schema):
            raise ActionError(f"unknown args: {sorted(set(args) - set(schema))}")
        coerced = {}
        for arg, type_name in schema.items():
            if arg not in args:
                # `defaults` (register()'s own opt-in, see its docstring):
                # a schema arg the CALLER omitted falls back to its
                # already-coerced default instead of a hard "missing arg"
                # error, but ONLY for an arg actually named in `defaults`
                # -- every other schema arg is still unconditionally
                # required, unchanged from before this feature existed.
                if arg in defaults:
                    coerced[arg] = defaults[arg]
                    continue
                raise ActionError(f"missing arg: {arg}")
            try:
                coerced[arg] = _COERCERS[type_name](args[arg])
            except (ValueError, TypeError) as exc:
                raise ActionError(f"bad value for {arg}: {exc}") from exc
        # `origin` is injected into a SEPARATE kwargs dict for the actual
        # handler call -- `coerced` itself (the schema-only args) is what
        # the post-dispatch hook below receives as the recorded mark's
        # `args`, so an origin-aware handler never leaks `origin` into
        # its own action mark's `args` field.
        handler_kwargs = dict(coerced)
        if wants_origin:
            handler_kwargs["origin"] = origin
        result = handler(**handler_kwargs)
        if inspect.isawaitable(result):
            result = await result
        result = result if isinstance(result, dict) else {}
        if self._on_dispatch is not None:
            self._on_dispatch(name, coerced, origin, result)
        return result
