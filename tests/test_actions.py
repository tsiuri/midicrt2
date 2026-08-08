import pytest

from midicrt.engine.actions import ActionError, ActionRegistry


async def test_register_dispatch_describe():
    reg = ActionRegistry()
    calls = []
    reg.register("log.clear", lambda: calls.append(1), description="clear the log")
    reg.register("zoom.set", lambda level: {"level": level}, args={"level": "int"})

    assert await reg.dispatch("log.clear", {}) == {}
    assert calls == [1]
    assert await reg.dispatch("zoom.set", {"level": "3"}) == {"level": 3}  # coerced
    d = reg.describe()
    assert d["log.clear"]["description"] == "clear the log"
    assert d["zoom.set"]["args"] == {"level": "int"}


async def test_async_handler():
    reg = ActionRegistry()

    async def go():
        return {"ran": True}

    reg.register("go", go)
    assert await reg.dispatch("go", {}) == {"ran": True}


async def test_errors():
    reg = ActionRegistry()
    reg.register("zoom.set", lambda level: None, args={"level": "int"})
    with pytest.raises(ActionError):
        await reg.dispatch("nope", {})
    with pytest.raises(ActionError):
        await reg.dispatch("zoom.set", {})            # missing arg
    with pytest.raises(ActionError):
        await reg.dispatch("zoom.set", {"level": "x"})  # uncoercible
    with pytest.raises(ActionError):
        await reg.dispatch("zoom.set", {"level": 1, "bogus": 2})  # unknown arg


async def test_bool_coercion_from_strings():
    reg = ActionRegistry()
    seen = []
    reg.register("toggle.set", lambda enabled: seen.append(enabled),
                  args={"enabled": "bool"})

    await reg.dispatch("toggle.set", {"enabled": "false"})
    assert seen[-1] is False

    await reg.dispatch("toggle.set", {"enabled": "true"})
    assert seen[-1] is True

    await reg.dispatch("toggle.set", {"enabled": True})
    assert seen[-1] is True

    with pytest.raises(ActionError):
        await reg.dispatch("toggle.set", {"enabled": "maybe"})


# -- "dict" coercion (Phase 4 Task 3, docs/phase4-notes.md): added for
# `bind.learn {action, mode, args}`, whose own `args` param is itself a
# nested k/v template for the TARGET action being learned -- the only
# action in the registry whose schema needs a non-scalar arg type. A real
# `client.action(...)` call over the wire already carries `args` as a
# genuine JSON object (no client in this codebase ever needs to stringify
# it), so the common case is a pass-through identity check; the JSON-string
# fallback exists so the generic `midicrt action bind.learn --arg args=...`
# CLI path (whose `--arg k=v` always produces STRING values, see
# clients/cli.py::_parse_args) can reach the exact same coercer as the
# dedicated `midicrt bind learn` subcommand, which sends a real dict
# directly -- one coercer, two equally-valid callers.

async def test_dict_coercion_passes_a_real_dict_through_unchanged():
    reg = ActionRegistry()
    seen = []
    reg.register("bind.learn", lambda args: seen.append(args), args={"args": "dict"})
    await reg.dispatch("bind.learn", {"args": {"level": 5}})
    assert seen[-1] == {"level": 5}


async def test_dict_coercion_parses_a_json_object_string():
    reg = ActionRegistry()
    seen = []
    reg.register("bind.learn", lambda args: seen.append(args), args={"args": "dict"})
    await reg.dispatch("bind.learn", {"args": '{"level": 5}'})
    assert seen[-1] == {"level": 5}


async def test_dict_coercion_rejects_invalid_json_string():
    reg = ActionRegistry()
    reg.register("bind.learn", lambda args: None, args={"args": "dict"})
    with pytest.raises(ActionError):
        await reg.dispatch("bind.learn", {"args": "not json at all {{{"})


async def test_dict_coercion_rejects_a_json_value_that_is_not_an_object():
    reg = ActionRegistry()
    reg.register("bind.learn", lambda args: None, args={"args": "dict"})
    with pytest.raises(ActionError):
        await reg.dispatch("bind.learn", {"args": "[1, 2, 3]"})
    with pytest.raises(ActionError):
        await reg.dispatch("bind.learn", {"args": 5})


# -- register(..., defaults=...): an OPTIONAL schema arg (Phase 5 Task 3,
# docs/phase5-notes.md cheap-wins bundle: CLI `--range` for continuous
# learn) -----------------------------------------------------------------
#
# Every schema arg was, before this, unconditionally required -- adding a
# genuinely new wire arg to an existing action (`bind.learn`'s `range`)
# without breaking dozens of pre-existing callers that never pass it needs
# a per-arg fallback used ONLY when the caller omits that key outright.

async def test_dispatch_uses_the_default_when_the_caller_omits_the_arg():
    reg = ActionRegistry()
    seen = []
    reg.register("zoom.set", lambda level, unit: seen.append((level, unit)),
                 args={"level": "int", "unit": "str"}, defaults={"unit": "px"})
    await reg.dispatch("zoom.set", {"level": "3"})
    assert seen[-1] == (3, "px")


async def test_dispatch_prefers_the_callers_value_over_the_default():
    reg = ActionRegistry()
    seen = []
    reg.register("zoom.set", lambda level, unit: seen.append((level, unit)),
                 args={"level": "int", "unit": "str"}, defaults={"unit": "px"})
    await reg.dispatch("zoom.set", {"level": "3", "unit": "pt"})
    assert seen[-1] == (3, "pt")


async def test_dispatch_still_requires_a_schema_arg_with_no_default():
    reg = ActionRegistry()
    reg.register("zoom.set", lambda level, unit: None,
                 args={"level": "int", "unit": "str"}, defaults={"unit": "px"})
    with pytest.raises(ActionError, match="level"):
        await reg.dispatch("zoom.set", {"unit": "pt"})


async def test_describe_does_not_expose_defaults_wire_shape_is_unchanged():
    reg = ActionRegistry()
    reg.register("zoom.set", lambda level, unit: None,
                 args={"level": "int", "unit": "str"}, defaults={"unit": "px"})
    assert reg.describe()["zoom.set"] == {
        "description": "", "args": {"level": "int", "unit": "str"}}


async def test_register_with_no_defaults_behaves_exactly_as_before():
    reg = ActionRegistry()
    reg.register("zoom.set", lambda level: None, args={"level": "int"})
    with pytest.raises(ActionError, match="missing arg"):
        await reg.dispatch("zoom.set", {})


# -- register(...): "origin" is a reserved schema arg name (fix wave,
# 2026-08-07, Minor finding, closes a ledgered landmine from the Task-1
# review) -------------------------------------------------------------------
#
# `dispatch()` injects a REAL `origin` kwarg into `handler_kwargs` for any
# handler that opts in via `wants_origin` (a param literally named `origin`
# in the handler's own signature) -- a schema that ALSO declares a
# client-suppliable arg named `origin` would either be silently overwritten
# (handler wants origin) or raise a raw `TypeError` past `dispatch`'s own
# `except ActionError` narrowing (handler doesn't want origin). `register`
# now rejects this outright, at registration time, before either failure
# mode can ever reach a live dispatch.

def test_register_rejects_a_schema_that_declares_an_arg_named_origin():
    reg = ActionRegistry()
    with pytest.raises(ValueError, match="origin"):
        reg.register("some.action", lambda origin: None, args={"origin": "str"})


def test_register_rejects_origin_in_schema_even_when_the_handler_does_not_want_origin():
    reg = ActionRegistry()
    with pytest.raises(ValueError, match="origin"):
        reg.register("some.action", lambda value: None,
                     args={"value": "int", "origin": "str"})


def test_register_still_accepts_a_handler_that_wants_origin_with_no_such_schema_arg():
    # Control: `wants_origin` itself (a handler PARAMETER named `origin`,
    # with no matching `args` schema entry -- `Engine._capture_stop_action`'s
    # own real shape) is the intended, unaffected use of the mechanism.
    reg = ActionRegistry()
    reg.register("capture.stop", lambda *, origin="unknown": {"origin": origin})
