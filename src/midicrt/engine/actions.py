"""Central action registry: every engine capability is a named action (spec §4)."""
import inspect
from collections.abc import Callable

_COERCERS = {"int": int, "float": float, "str": str, "bool": bool}


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
