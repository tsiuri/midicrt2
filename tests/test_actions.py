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
