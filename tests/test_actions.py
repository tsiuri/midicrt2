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
