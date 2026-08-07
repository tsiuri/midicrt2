"""Central action registry: every engine capability is a named action (spec §4)."""
import inspect
import json
from collections.abc import Callable


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
        self._actions: dict[str, tuple[Callable, str, dict[str, str]]] = {}

    def register(self, name, handler, description="", args=None):
        self._actions[name] = (handler, description, dict(args or {}))

    def describe(self) -> dict:
        return {
            name: {"description": desc, "args": args}
            for name, (_, desc, args) in sorted(self._actions.items())
        }

    async def dispatch(self, name: str, args: dict) -> dict:
        if name not in self._actions:
            raise ActionError(f"unknown action: {name}")
        handler, _, schema = self._actions[name]
        if set(args) - set(schema):
            raise ActionError(f"unknown args: {sorted(set(args) - set(schema))}")
        coerced = {}
        for arg, type_name in schema.items():
            if arg not in args:
                raise ActionError(f"missing arg: {arg}")
            try:
                coerced[arg] = _COERCERS[type_name](args[arg])
            except (ValueError, TypeError) as exc:
                raise ActionError(f"bad value for {arg}: {exc}") from exc
        result = handler(**coerced)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {}
