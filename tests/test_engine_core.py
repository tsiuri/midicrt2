import asyncio
import time

import pytest

from midicrt.config import Config
from midicrt.engine.actions import ActionError
from midicrt.engine.core import Engine, MidiEvent


def ev(**kw):
    base = {"ts": time.time(), "source": "test", "type": "note_on",
            "channel": 0, "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100"}
    base.update(kw)
    return MidiEvent(**base)


class _FakePage:
    """Minimal page double for roster/dirty-tracking tests -- no factory
    registered for it, so tests attach it via `Engine.register_page()`."""

    def __init__(self, dirty=True):
        self._dirty = dirty
        self.seen = 0

    def handle(self, ev) -> bool:
        self.seen += 1
        return self._dirty

    def view_model(self) -> dict:
        return {"seen": self.seen}


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


def test_eventlog_page_ignores_clock_tick_no_spam():
    # phase-3 task 3: engine/midi_in.py aggregates raw MIDI clock into one
    # clock_tick MidiEvent per beat for the transport analyzer -- the
    # eventlog page must not surface it as a line.
    eng = Engine(Config())
    page = eng.pages["eventlog"]
    changed = page.handle(ev(type="clock_tick", summary="clock_tick"))
    assert changed is False
    assert page.view_model()["count"] == 0


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


# -- multi-page roster (phase-3 task 1) --------------------------------------

def test_default_roster_from_config_is_eventlog_voices_then_harmony():
    # Phase-3 task 4 added "voices"; task 5 appends "harmony" the same way
    # -- both live by default (config.py's Config.pages default) so they're
    # reachable with no config.toml on a stock deploy. "eventlog" stays
    # first/current -- order is preserved from config.pages.
    eng = Engine(Config())
    assert list(eng.pages) == ["eventlog", "voices", "harmony"]
    assert eng.current_page == "eventlog"


def test_register_page_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()
    eng.register_page("second", fake)
    assert list(eng.pages) == ["eventlog", "voices", "harmony", "second"]
    assert eng.pages["second"] is fake


def test_engine_topics_reflects_roster_order():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.second", "overlay.status",
    ]


def test_handle_marks_dirty_only_for_pages_reporting_true():
    # `ev()` is a real note_on on channel 1 -- the real "voices" and
    # "harmony" pages (both live by default) genuinely react to it too, so
    # they're dirty here alongside eventlog, same as any other real page
    # would be.
    eng = Engine(Config())
    quiet = _FakePage(dirty=False)
    eng.register_page("quiet", quiet)
    eng._handle(ev())
    assert eng._dirty == {"page.eventlog", "page.voices", "page.harmony"}
    assert quiet.seen == 1  # every page still SEES every event...


def test_handle_marks_non_current_page_dirty_too():
    # This is the phase-2 latent bug: previously only page.<current_page>
    # was ever marked dirty even though every page consumed every event.
    eng = Engine(Config())
    loud = _FakePage(dirty=True)
    eng.register_page("loud", loud)
    assert eng.current_page == "eventlog"  # "loud" is NOT current
    eng._handle(ev())
    assert eng._dirty == {"page.eventlog", "page.voices", "page.harmony", "page.loud"}


# -- transport analyzer / overlay wiring (phase-3 task 3) --------------------

def test_default_analyzer_roster_is_status_only():
    eng = Engine(Config())
    assert list(eng.analyzers) == ["status"]


def test_register_analyzer_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()  # same handle()/view_model() shape as an Analyzer
    eng.register_analyzer("second", fake)
    assert list(eng.analyzers) == ["status", "second"]
    assert eng.analyzers["second"] is fake


def test_topics_include_overlay_after_page_topics():
    eng = Engine(Config())
    assert eng.topics == ["page.eventlog", "page.voices", "page.harmony", "overlay.status"]


def test_handle_marks_overlay_dirty_when_analyzer_reports_true():
    eng = Engine(Config())
    loud = _FakePage(dirty=True)
    eng.register_analyzer("loud", loud)
    eng._handle(ev())
    assert "overlay.loud" in eng._dirty
    assert loud.seen == 1  # analyzers see every event too, same as pages


def test_handle_does_not_mark_overlay_dirty_when_analyzer_reports_false():
    eng = Engine(Config())
    quiet = _FakePage(dirty=False)
    eng.register_analyzer("quiet", quiet)
    eng._handle(ev())
    assert "overlay.quiet" not in eng._dirty
    assert quiet.seen == 1


def test_snapshot_now_serves_overlay_topic():
    eng = Engine(Config())
    snap = eng.snapshot_now("overlay.status")
    assert snap is not None
    assert snap["topic"] == "overlay.status"
    assert snap["data"] == {
        "bpm": None, "bar": 0, "beat": 1, "running": False, "source": None,
    }


def test_snapshot_now_unknown_overlay_returns_none():
    eng = Engine(Config())
    assert eng.snapshot_now("overlay.nonexistent") is None


async def test_clock_tick_does_not_dirty_eventlog_but_dirties_overlay_once_running():
    # The eventlog page must never show clock spam; the status overlay must
    # react to it (once transport is running -- see analyzers/transport.py).
    eng = Engine(Config())
    eng._handle(MidiEvent(ts=0.0, source="USB", type="start", channel=None,
                          data1=None, data2=None, summary="start"))
    eng._dirty.clear()
    eng._handle(MidiEvent(ts=0.5, source="USB", type="clock_tick", channel=None,
                          data1=24, data2=None, summary="clock_tick",
                          clock_batch_start=None))
    assert eng._dirty == {"overlay.status"}
    assert eng.pages["eventlog"].view_model()["count"] == 1  # only the "start" line


async def test_page_next_prev_cycle_and_emit_page_changed():
    # Roster is now 4 deep by default: eventlog, voices, harmony
    # (phase-3 tasks 4 and 5's default pages), then the dynamically-
    # registered "second".
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)

    assert eng.current_page == "eventlog"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "voices"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "harmony"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "second"
    await eng.actions.dispatch("page.prev", {})
    assert eng.current_page == "harmony"

    names = [e["name"] for e in events]
    assert names == ["page_changed"] * 4
    assert [e["data"]["page"] for e in events] == ["voices", "harmony", "second", "harmony"]


async def test_page_goto_valid_and_unknown():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    r = await eng.actions.dispatch("page.goto", {"name": "second"})
    assert eng.current_page == "second"
    assert r["page"] == "second"
    with pytest.raises(ActionError):
        await eng.actions.dispatch("page.goto", {"name": "nonexistent"})
    assert eng.current_page == "second"  # unchanged on error
