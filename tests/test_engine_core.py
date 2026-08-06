import asyncio
import time

from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent


def ev(**kw):
    base = {"ts": time.time(), "source": "test", "type": "note_on",
            "channel": 0, "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100"}
    base.update(kw)
    return MidiEvent(**base)


def test_eventlog_page_capacity_and_vm():
    eng = Engine(Config(eventlog_capacity=2))
    page = eng.pages["eventlog"]
    page.handle(ev(summary="one"))
    page.handle(ev(summary="two", type="control_change"))
    page.handle(ev(summary="three"))
    vm = page.view_model()
    assert vm["title"] == "EVENT LOG"
    assert vm["count"] == 3
    assert [ln["text"] for ln in vm["lines"]] == ["two", "three"]  # capacity 2, newest last
    assert vm["lines"][0]["style"] == "normal"   # control_change
    assert vm["lines"][1]["style"] == "accent"   # note_on


async def test_engine_publishes_dirty_snapshots():
    eng = Engine(Config(tick_hz=50.0))
    got = []
    eng.add_listener(got.append)
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(summary="hello"))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    snaps = [m for m in got if m.get("kind") == "snapshot" and m["topic"] == "page.eventlog"]
    assert snaps and snaps[-1]["data"]["lines"][-1]["text"] == "hello"
    assert eng.events_total == 1
    # no further events -> not dirty -> seq stops advancing
    assert snaps[-1]["seq"] >= 1


async def test_zero_tick_hz_does_not_crash():
    eng = Engine(Config(tick_hz=0.0))
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    eng.stop()
    await task  # must not raise (e.g. ZeroDivisionError)


async def test_clear_action_and_status():
    eng = Engine(Config())
    eng.pages["eventlog"].handle(ev())
    await eng.actions.dispatch("eventlog.clear", {})
    assert eng.pages["eventlog"].view_model()["count"] == 0
    st = eng.status()
    assert st["page"] == "eventlog" and "uptime_s" in st and st["events_total"] == 0
