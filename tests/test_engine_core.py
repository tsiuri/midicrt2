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


class _FakeTickingAnalyzer:
    """Analyzer double for `_tick_analyzers` wiring tests (phase-3 task 6)
    -- exposes the OPTIONAL `tick`/`drain_alerts` methods `StuckNotesAnalyzer`
    added, with fully scripted return values so these tests never need a
    real wall-clock wait (unlike an end-to-end test against the real
    2s/10s thresholds, which analyzers/stucknotes.py's own unit tests
    already cover with synthetic timestamps)."""

    def __init__(self, tick_dirty=True, alerts=None):
        self._tick_dirty = tick_dirty
        self._alerts = list(alerts or [])
        self.ticked_with: list[float] = []

    def handle(self, ev) -> bool:
        return False

    def tick(self, now: float) -> bool:
        self.ticked_with.append(now)
        return self._tick_dirty

    def drain_alerts(self) -> list[dict]:
        drained, self._alerts = self._alerts, []
        return drained

    def view_model(self) -> dict:
        return {}


class _FakeTickingPage:
    """Page double for `_tick_pages` wiring tests (phase-3 task 7) -- same
    shape as `_FakeTickingAnalyzer` minus `drain_alerts` (a page-only
    concept doesn't exist; `_tick_pages` never looks for it)."""

    def __init__(self, tick_dirty=True):
        self._tick_dirty = tick_dirty
        self.ticked_with: list[float] = []

    def handle(self, ev) -> bool:
        return False

    def tick(self, now: float) -> bool:
        self.ticked_with.append(now)
        return self._tick_dirty

    def view_model(self) -> dict:
        return {}


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

def test_default_roster_from_config_is_eventlog_voices_harmony_pianoroll_spectrum():
    # Phase-3 task 4 added "voices"; task 5 appends "harmony"; task 7
    # appends "pianoroll"; task 8 appends "spectrum"; task 9 appends
    # "screensaver" (see config.py's own comment for why it -- unlike
    # "tuner" -- joins the default roster); task 10 appends "img2txtviz"
    # (same "no unbuilt dependency" precedent, NOT because v1's own
    # cycle_pages happened to include it -- see pages/img2txtviz.py's own
    # module docstring) and "config" (the task-10 brief's explicit ask,
    # a zero-dependency read-only viewer) -- all live by default
    # (config.py's Config.pages default) so they're reachable with no
    # config.toml on a stock deploy. "eventlog" stays first/current --
    # order is preserved from config.pages.
    eng = Engine(Config())
    assert list(eng.pages) == [
        "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
        "img2txtviz", "config", "help", "progchanges",
    ]
    assert eng.current_page == "eventlog"


def test_register_page_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()
    eng.register_page("second", fake)
    assert list(eng.pages) == [
        "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
        "img2txtviz", "config", "help", "progchanges", "second",
    ]
    assert eng.pages["second"] is fake


def test_engine_topics_reflects_roster_order():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges", "page.second",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress",
    ]


def test_handle_marks_dirty_only_for_pages_reporting_true():
    # `ev()` is a real note_on on channel 1 -- the real "voices"/"harmony"/
    # "pianoroll"/"img2txtviz" pages (all live by default) genuinely react
    # to it too, so they're dirty here alongside eventlog, same as any
    # other real page would be. Phase-3 task 6: "alerts" (StuckNotesAnalyzer)
    # is a real analyzer too, and a fresh note-on genuinely starts tracking
    # it -- dirty for the same reason. "timesig" does NOT react
    # (TimesigAnalyzer gates note_on on transport being "running", and no
    # "start" was ever sent here). "config" never reacts to MIDI events at
    # all (a read-only viewer, pages/configview.py's own `handle()`).
    eng = Engine(Config())
    quiet = _FakePage(dirty=False)
    eng.register_page("quiet", quiet)
    eng._handle(ev())
    assert eng._dirty == {
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll",
        "page.img2txtviz", "overlay.alerts",
    }
    assert quiet.seen == 1  # every page still SEES every event...


def test_handle_marks_non_current_page_dirty_too():
    # This is the phase-2 latent bug: previously only page.<current_page>
    # was ever marked dirty even though every page consumed every event.
    eng = Engine(Config())
    loud = _FakePage(dirty=True)
    eng.register_page("loud", loud)
    assert eng.current_page == "eventlog"  # "loud" is NOT current
    eng._handle(ev())
    assert eng._dirty == {
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll",
        "page.img2txtviz", "page.loud", "overlay.alerts",
    }


# -- transport analyzer / overlay wiring (phase-3 task 3) --------------------

def test_default_analyzer_roster_is_status_alerts_timesig():
    # Phase-3 task 6 added "alerts" (StuckNotesAnalyzer) and "timesig"
    # (TimesigAnalyzer) the same way task-3 introduced "status"; task 9
    # adds "beatflash"/"loopprogress" the same way again -- all are v1
    # chrome-class features (always visible regardless of page), never
    # config-gated, see engine/core.py's module docstring.
    eng = Engine(Config())
    assert list(eng.analyzers) == ["status", "alerts", "timesig", "beatflash", "loopprogress"]


def test_register_analyzer_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()  # same handle()/view_model() shape as an Analyzer
    eng.register_analyzer("second", fake)
    assert list(eng.analyzers) == [
        "status", "alerts", "timesig", "beatflash", "loopprogress", "second",
    ]
    assert eng.analyzers["second"] is fake


def test_topics_include_overlay_after_page_topics():
    eng = Engine(Config())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress",
    ]


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
    # The eventlog page must never show clock spam; the status overlay
    # (and, since phase-3 task 9, beatflash/loopprogress -- both also
    # transport-driven) must react to it once running (see analyzers/
    # transport.py, analyzers/beatflash.py, analyzers/loopprogress.py).
    eng = Engine(Config())
    eng._handle(MidiEvent(ts=0.0, source="USB", type="start", channel=None,
                          data1=None, data2=None, summary="start"))
    eng._dirty.clear()
    eng._handle(MidiEvent(ts=0.5, source="USB", type="clock_tick", channel=None,
                          data1=24, data2=None, summary="clock_tick",
                          clock_batch_start=None))
    assert eng._dirty == {"overlay.status", "overlay.beatflash", "overlay.loopprogress"}
    assert eng.pages["eventlog"].view_model()["count"] == 1  # only the "start" line


async def test_page_next_prev_cycle_and_emit_page_changed():
    # Roster is now 9 deep by default: eventlog, voices, harmony, pianoroll,
    # spectrum, screensaver, img2txtviz, config (phase-3 tasks 4, 5, 7, 8, 9,
    # and 10's default pages), then the dynamically-registered "second".
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
    assert eng.current_page == "pianoroll"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "spectrum"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "screensaver"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "img2txtviz"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "config"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "help"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "progchanges"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "second"
    await eng.actions.dispatch("page.prev", {})
    assert eng.current_page == "progchanges"

    names = [e["name"] for e in events]
    assert names == ["page_changed"] * 11
    assert [e["data"]["page"] for e in events] == [
        "voices", "harmony", "pianoroll", "spectrum", "screensaver", "img2txtviz",
        "config", "help", "progchanges", "second", "progchanges",
    ]


# -- analyzer wall-clock tick + alert events (phase-3 task 6) ---------------

def test_tick_analyzers_calls_tick_with_the_injected_now_and_marks_dirty():
    eng = Engine(Config())
    fake = _FakeTickingAnalyzer(tick_dirty=True)
    eng.register_analyzer("fake", fake)
    eng._tick_analyzers(12345.0)
    assert fake.ticked_with == [12345.0]
    assert "overlay.fake" in eng._dirty


def test_tick_analyzers_does_not_mark_dirty_when_tick_reports_false():
    eng = Engine(Config())
    fake = _FakeTickingAnalyzer(tick_dirty=False)
    eng.register_analyzer("fake", fake)
    eng._tick_analyzers(1.0)
    assert "overlay.fake" not in eng._dirty


def test_tick_analyzers_ignores_analyzers_with_no_tick_method():
    # "status" (TransportAnalyzer) has no tick() -- must not raise.
    eng = Engine(Config())
    eng._tick_analyzers(1.0)   # must not raise
    assert "overlay.status" not in eng._dirty


def test_tick_analyzers_emits_one_alert_event_per_drained_alert():
    eng = Engine(Config())
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    fake = _FakeTickingAnalyzer(alerts=[
        {"ch": 1, "note": 60, "level": "warn", "held_s": 2.1},
        {"ch": 2, "note": 64, "level": "crit", "held_s": 11.0},
    ])
    eng.register_analyzer("fake", fake)
    eng._tick_analyzers(1.0)
    alert_events = [e for e in events if e["name"] == "alert"]
    assert [e["data"] for e in alert_events] == [
        {"ch": 1, "note": 60, "level": "warn", "held_s": 2.1},
        {"ch": 2, "note": 64, "level": "crit", "held_s": 11.0},
    ]


def test_tick_analyzers_emits_nothing_when_drain_alerts_is_empty():
    eng = Engine(Config())
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    eng.register_analyzer("fake", _FakeTickingAnalyzer(alerts=[]))
    eng._tick_analyzers(1.0)
    assert [e for e in events if e["name"] == "alert"] == []


def test_tick_analyzers_ignores_analyzers_with_no_drain_alerts_method():
    eng = Engine(Config())
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    eng.register_analyzer("plain", _FakePage())   # no tick, no drain_alerts
    eng._tick_analyzers(1.0)   # must not raise
    assert events == []


async def test_run_loop_calls_tick_analyzers_and_publishes_stucknotes_alert_end_to_end():
    # End-to-end proof (real StuckNotesAnalyzer, real run() loop, real
    # wall-clock `time.time()` reads from the engine -- NOT a scripted
    # fake) that a note held with no new MIDI event still escalates and
    # reaches a subscribed client as both an `overlay.alerts` snapshot AND
    # an `alert` event, using a near-zero WARN_AFTER override so the test
    # doesn't need to sleep for the real 2s default.
    import midicrt.analyzers.stucknotes as stucknotes_mod

    original_warn_after = stucknotes_mod.WARN_AFTER
    stucknotes_mod.WARN_AFTER = 0.05
    try:
        eng = Engine(Config(tick_hz=200.0))
        got = []
        eng.add_listener(got.append)
        task = asyncio.create_task(eng.run())
        await eng.queue.put(ev(type="note_on", channel=0, data1=60, data2=100))
        await asyncio.sleep(0.2)
        eng.stop()
        await task
    finally:
        stucknotes_mod.WARN_AFTER = original_warn_after

    alert_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "alert"]
    assert alert_events and alert_events[0]["data"]["note"] == 60
    alert_snaps = [m for m in got if m.get("kind") == "snapshot" and m["topic"] == "overlay.alerts"]
    assert alert_snaps and alert_snaps[-1]["data"]["alerts"][0]["note"] == 60


# -- page wall-clock tick (phase-3 task 7) -----------------------------------

def test_tick_pages_calls_tick_with_the_injected_now_and_marks_dirty():
    eng = Engine(Config())
    fake = _FakeTickingPage(tick_dirty=True)
    eng.register_page("fake", fake)
    eng._tick_pages(12345.0)
    assert fake.ticked_with == [12345.0]
    assert "page.fake" in eng._dirty


def test_tick_pages_does_not_mark_dirty_when_tick_reports_false():
    eng = Engine(Config())
    fake = _FakeTickingPage(tick_dirty=False)
    eng.register_page("fake", fake)
    eng._tick_pages(1.0)
    assert "page.fake" not in eng._dirty


def test_tick_pages_ignores_pages_with_no_tick_method():
    # "eventlog"/"voices"/"harmony" have no tick() -- must not raise.
    eng = Engine(Config(pages=["eventlog", "voices", "harmony"]))
    eng._tick_pages(1.0)   # must not raise
    assert eng._dirty == set()


def test_tick_pages_ticks_the_real_pianoroll_page():
    eng = Engine(Config())
    eng._tick_pages(1.0)
    # Nothing has ever played -- PianorollState.tick() reports not-dirty
    # (see pages/pianoroll.py's own test coverage for that contract).
    assert "page.pianoroll" not in eng._dirty
    eng._handle(ev())   # a real note_on -- pianoroll now has an active note
    eng._dirty.clear()
    eng._tick_pages(2.0)
    assert "page.pianoroll" in eng._dirty


async def test_run_loop_calls_tick_pages_without_crashing():
    # End-to-end: the real run() loop must call _tick_pages every cycle
    # alongside _tick_analyzers with no page in the default roster raising.
    eng = Engine(Config(tick_hz=200.0))
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev())
    await asyncio.sleep(0.05)
    eng.stop()
    await task   # must not raise


# -- tuner page (phase-3 task 6) ---------------------------------------------

def test_tuner_is_not_in_the_default_roster():
    # Disclosed scope choice (pages/tuner.py's module docstring): the tuner
    # page can only ever show v1's idle state until a separate, not-yet-
    # built audio-capture task feeds it real pitch samples, so it is NOT
    # forced into config.py's default `pages` list the way voices/harmony
    # were in tasks 4/5.
    eng = Engine(Config())
    assert "tuner" not in eng.pages


def test_tuner_is_reachable_via_config_pages():
    eng = Engine(Config(pages=["eventlog", "tuner"]))
    assert list(eng.pages) == ["eventlog", "tuner"]
    assert eng.pages["tuner"].view_model()["title"] == "TUNER"


async def test_page_goto_valid_and_unknown():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    r = await eng.actions.dispatch("page.goto", {"name": "second"})
    assert eng.current_page == "second"
    assert r["page"] == "second"
    with pytest.raises(ActionError):
        await eng.actions.dispatch("page.goto", {"name": "nonexistent"})
    assert eng.current_page == "second"  # unchanged on error


# -- pianoroll page (phase-3 task 7) -----------------------------------------

def test_pianoroll_is_in_the_default_roster():
    eng = Engine(Config())
    assert "pianoroll" in eng.pages
    assert eng.pages["pianoroll"].view_model()["title"] == "PIANOROLL"


def test_pianoroll_actions_are_registered_when_the_page_is_present():
    eng = Engine(Config())
    described = eng.actions.describe()
    assert {"pianoroll.zoom", "pianoroll.projection", "pianoroll.channels"} <= set(described)


def test_pianoroll_actions_are_absent_when_the_page_is_not_in_the_roster():
    # Mirrors "eventlog.clear" always assuming eventlog exists -- guarded
    # registration means a build without "pianoroll" in config.pages simply
    # never advertises these actions (no KeyError at dispatch time either).
    eng = Engine(Config(pages=["eventlog"]))
    described = eng.actions.describe()
    assert not ({"pianoroll.zoom", "pianoroll.projection", "pianoroll.channels"} & set(described))


async def test_pianoroll_zoom_action_mutates_and_marks_dirty():
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.zoom", {"delta": "1.0"})
    assert r["zoom"] == pytest.approx(2.0)
    assert "page.pianoroll" in eng._dirty


async def test_pianoroll_projection_action_mutates_and_marks_dirty():
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.projection", {"mode": "tempo"})
    assert r["mode"] == "tempo"
    assert eng.pages["pianoroll"].view_model()["window"]["mode"] == "tempo"
    assert "page.pianoroll" in eng._dirty


async def test_pianoroll_projection_action_rejects_unknown_mode():
    eng = Engine(Config())
    with pytest.raises(ActionError):
        await eng.actions.dispatch("pianoroll.projection", {"mode": "bogus"})


async def test_pianoroll_channels_action_mutates_and_marks_dirty():
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.channels", {"spec": "1,2,3"})
    assert r["channels"] == [1, 2, 3]
    assert "page.pianoroll" in eng._dirty


async def test_pianoroll_channels_action_rejects_malformed_spec():
    eng = Engine(Config())
    with pytest.raises(ActionError):
        await eng.actions.dispatch("pianoroll.channels", {"spec": "not-a-channel"})


# -- spectrum page (phase-3 task 8) ------------------------------------------

def test_spectrum_is_in_the_default_roster():
    # Unlike "tuner" (task 6), "spectrum" DOES join the default roster --
    # see config.py's/pages/spectrum.py's own docstrings for why (graceful
    # "no audio input" degradation, not a permanently-idle page).
    eng = Engine(Config())
    assert "spectrum" in eng.pages
    vm = eng.pages["spectrum"].view_model()
    assert vm["title"] == "SPECTRUM"
    assert vm["available"] is False   # capture is never auto-started by construction


def test_spectrum_handle_never_marks_itself_dirty_for_midi_events():
    # analyzers/spectrum.py's SpectrumAnalyzer.handle() is a true no-op
    # (not MIDI-driven, mirrors analyzers/tuner.py's TunerAnalyzer) -- a
    # real note_on must not appear in the dirty set for it.
    eng = Engine(Config())
    eng._handle(ev())
    assert "page.spectrum" not in eng._dirty


def test_spectrum_tick_is_wired_through_tick_pages():
    # SpectrumPage.tick() exists (peak-hold decay) but reports not-dirty
    # while nothing has ever been captured -- must not raise either way.
    eng = Engine(Config())
    eng._tick_pages(1.0)
    assert "page.spectrum" not in eng._dirty


# -- behaviors: pagecycle + screensaver (phase-3 task 9) ---------------------

def test_last_activity_ts_seeded_to_now_not_epoch_zero():
    before = time.time()
    eng = Engine(Config())
    after = time.time()
    assert before <= eng._last_activity_ts <= after


def test_activity_ts_updates_on_note_on_note_off_control_change_only():
    eng = Engine(Config())
    baseline = eng._last_activity_ts
    eng._handle(MidiEvent(ts=12345.0, source="USB", type="clock_tick", channel=None,
                          data1=24, data2=None, summary="clock_tick", clock_batch_start=None))
    assert eng._last_activity_ts == baseline   # clock_tick is not "activity"
    eng._handle(MidiEvent(ts=12345.0, source="USB", type="start", channel=None,
                          data1=None, data2=None, summary="start"))
    assert eng._last_activity_ts == baseline   # transport messages are not "activity"
    eng._handle(ev(type="note_on", ts=99999.0))
    assert eng._last_activity_ts == 99999.0
    eng._handle(ev(type="note_off", ts=99999.5))
    assert eng._last_activity_ts == 99999.5
    eng._handle(ev(type="control_change", ts=100000.0))
    assert eng._last_activity_ts == 100000.0


async def test_tick_behaviors_dispatches_page_next_when_pagecycle_behavior_fires():
    eng = Engine(Config(pagecycle_idle_s=5.0, screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(0.0)   # bootstraps the idle window -- no fire yet
    assert eng.current_page == "eventlog"
    await eng._tick_behaviors(5.0)
    assert eng.current_page == "voices"


async def test_tick_behaviors_pagecycle_dispatch_is_observable_via_a_spy():
    # Task brief: "fake-clock idle progression -> page.next fired via a spy
    # on dispatch" -- exercised literally here, alongside the state-based
    # assertion above.
    eng = Engine(Config(pagecycle_idle_s=5.0, screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    calls = []
    original_dispatch = eng.actions.dispatch

    async def spy(name, args):
        calls.append((name, args))
        return await original_dispatch(name, args)

    eng.actions.dispatch = spy
    await eng._tick_behaviors(0.0)
    await eng._tick_behaviors(5.0)
    assert calls == [("page.next", {})]


def test_pagecycle_disabled_by_config_never_fires():
    eng = Engine(Config(pagecycle_idle_s=5.0, pagecycle_enabled=False, screensaver_enabled=False))
    eng._last_activity_ts = 0.0

    async def run_ticks():
        await eng._tick_behaviors(0.0)
        await eng._tick_behaviors(5.0)
        await eng._tick_behaviors(1000.0)

    asyncio.run(run_ticks())
    assert eng.current_page == "eventlog"


async def test_tick_behaviors_activates_and_restores_screensaver():
    eng = Engine(Config(screensaver_after_s=10.0, pagecycle_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(10.0)
    assert eng.current_page == "screensaver"
    # Simulate real activity arriving -- restore the remembered page.
    eng._handle(ev(type="note_on", ts=10.5))
    await eng._tick_behaviors(10.6)
    assert eng.current_page == "eventlog"


async def test_tick_behaviors_screensaver_remembers_the_actual_current_page():
    eng = Engine(Config(screensaver_after_s=10.0, pagecycle_enabled=False))
    eng._last_activity_ts = 0.0
    await eng.actions.dispatch("page.goto", {"name": "harmony"})
    await eng._tick_behaviors(10.0)
    assert eng.current_page == "screensaver"
    eng._handle(ev(type="note_on", ts=10.5))
    await eng._tick_behaviors(10.6)
    assert eng.current_page == "harmony"


def test_screensaver_disabled_by_config_never_fires():
    eng = Engine(Config(screensaver_after_s=5.0, screensaver_enabled=False, pagecycle_enabled=False))
    eng._last_activity_ts = 0.0

    async def run_ticks():
        await eng._tick_behaviors(0.0)
        await eng._tick_behaviors(5.0)
        await eng._tick_behaviors(1000.0)

    asyncio.run(run_ticks())
    assert eng.current_page == "eventlog"


async def test_tick_behaviors_swallows_action_error_from_a_stripped_roster():
    # A custom config that drops "screensaver" from config.pages must not
    # crash the engine when the behavior still tries to `page.goto` it --
    # see `Engine._tick_behaviors`'s own docstring.
    eng = Engine(Config(pages=["eventlog"], screensaver_after_s=5.0, pagecycle_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(5.0)   # must not raise
    assert eng.current_page == "eventlog"


async def test_run_loop_fires_pagecycle_via_real_wall_clock():
    # End-to-end proof (real run() loop, real time.time() reads) that
    # _tick_behaviors is actually wired into the run loop, not just
    # unit-callable in isolation.
    eng = Engine(Config(pagecycle_idle_s=0.05, screensaver_enabled=False, tick_hz=200.0))
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.25)
    eng.stop()
    await task
    assert eng.current_page != "eventlog"


async def test_pagecycle_does_not_unblank_screensaver_with_shipped_defaults():
    # CRITICAL fix (task-9 review): reproduced against the actual shipped
    # defaults (pagecycle_idle_s=300, screensaver_after_s=60, BOTH
    # enabled -- Config()'s own defaults, no overrides). Before the fix,
    # PageCycleBehavior.tick() never looked at current_page at all, so at
    # t=300 (its own idle threshold, measured from the SAME
    # last_activity_ts screensaver uses) it unconditionally dispatched
    # page.next -- un-blanking the screensaver that had already activated
    # at t=60 -- and then kept doing so every further 300s while the
    # engine stayed fully idle (t=600, t=900, ...). See behaviors/
    # pagecycle.py's own "Arbitration with the screensaver" docstring
    # section for the fix. A fully idle engine must reach the screensaver
    # at t=60 and STAY there through at least t=900.
    eng = Engine(Config())
    eng._last_activity_ts = 0.0
    seen_screensaver_at = None
    for now in range(901):
        await eng._tick_behaviors(float(now))
        if eng.current_page == "screensaver" and seen_screensaver_at is None:
            seen_screensaver_at = now
        if now >= 60:
            assert eng.current_page == "screensaver", (
                f"page wandered to {eng.current_page!r} at t={now} while fully idle "
                "-- pagecycle un-blanked the screensaver"
            )
    assert seen_screensaver_at == 60


async def test_pagecycle_does_not_immediately_override_a_manual_screensaver_escape():
    # 2nd review pass: fixing the Critical bug above introduced a NEW
    # Important interaction -- with the fix as first shipped, pagecycle's
    # `_idle_since` stays frozen while blocked, so a MANUAL escape from the
    # screensaver (a client dispatching page.goto/next directly, with no
    # MIDI activity) left the stale elapsed check already satisfied on the
    # very next tick, firing page.next immediately and discarding the
    # user's manual choice -- the exact symptom the Important fix above
    # closed, now via the sibling behavior. See behaviors/pagecycle.py's
    # own "Re-arming after a manual escape" docstring section for the fix.
    #
    # This is an engine-level smoke test using REAL dual-behavior wiring
    # (both enabled, shipped defaults) for the setup + the critical
    # non-firing assertion. It deliberately does NOT project all the way
    # out to pagecycle's own full re-armed idle_s (that exact timing
    # contract is covered in isolation by test_behaviors_pagecycle.py::
    # test_rearms_after_a_manual_screensaver_escape_instead_of_firing_immediately)
    # -- with the shipped defaults, screensaver's own SHORTER after_s (60s)
    # will legitimately reclaim the display again well before pagecycle's
    # longer idle_s (300s) could ever elapse uninterrupted (a real,
    # disclosed, and arguably correct consequence of screensaver taking
    # priority -- see behaviors/pagecycle.py's `Engine.__init__` comment),
    # which would make asserting a specific later page value here brittle
    # and would actually be testing screensaver's OWN timer, not
    # pagecycle's.
    eng = Engine(Config())
    eng._last_activity_ts = 0.0
    for now in range(501):
        await eng._tick_behaviors(float(now))
    assert eng.current_page == "screensaver"

    # A manual escape: some OTHER client dispatches page.goto directly --
    # NOT via either behavior, and with NO MIDI activity in between.
    await eng.actions.dispatch("page.goto", {"name": "voices"})
    assert eng.current_page == "voices"

    calls = []
    original_dispatch = eng.actions.dispatch

    async def spy(name, args):
        calls.append((name, args))
        return await original_dispatch(name, args)

    eng.actions.dispatch = spy

    await eng._tick_behaviors(500.5)
    assert eng.current_page == "voices", (
        "pagecycle overrode the manual escape on the very next tick"
    )
    assert ("page.next", {}) not in calls


# -- img2txtviz page (phase-3 task 10) ---------------------------------------

def test_img2txtviz_is_in_the_default_roster():
    # Unlike "tuner" -- see pages/img2txtviz.py's own module docstring for
    # why v1's own cycle_pages omission isn't the deciding signal here.
    eng = Engine(Config())
    assert "img2txtviz" in eng.pages
    assert eng.pages["img2txtviz"].view_model()["title"] == "IMG2TXT"


def test_img2txtviz_actions_are_registered_when_the_page_is_present():
    eng = Engine(Config())
    described = eng.actions.describe()
    assert {"img2txtviz.charset", "img2txtviz.invert"} <= set(described)


def test_img2txtviz_actions_are_absent_when_the_page_is_not_in_the_roster():
    eng = Engine(Config(pages=["eventlog"]))
    described = eng.actions.describe()
    assert not ({"img2txtviz.charset", "img2txtviz.invert"} & set(described))


async def test_img2txtviz_charset_action_mutates_and_marks_dirty():
    eng = Engine(Config())
    before = eng.pages["img2txtviz"].view_model()["charset"]
    r = await eng.actions.dispatch("img2txtviz.charset", {})
    assert r["charset"] != before
    assert eng.pages["img2txtviz"].view_model()["charset"] == r["charset"]
    assert "page.img2txtviz" in eng._dirty


async def test_img2txtviz_invert_action_mutates_and_marks_dirty():
    eng = Engine(Config())
    r = await eng.actions.dispatch("img2txtviz.invert", {})
    assert r["invert"] is True
    assert eng.pages["img2txtviz"].view_model()["invert"] is True
    assert "page.img2txtviz" in eng._dirty


def test_run_loop_ticks_img2txtviz_without_crashing():
    eng = Engine(Config())
    eng._tick_pages(1.0)
    assert "page.img2txtviz" in eng._dirty   # always dirty, see analyzer's tick() docstring


# -- config page (phase-3 task 10) -------------------------------------------

def test_config_is_in_the_default_roster():
    eng = Engine(Config())
    assert "config" in eng.pages
    assert eng.pages["config"].view_model()["title"] == "CONFIG"


def test_config_page_engine_info_is_wired_to_the_real_engine():
    # Engine.__init__ binds `_config_engine_info` into the page right after
    # building `self.pages` -- see pages/configview.py's own "Engine-info
    # wiring" docstring section. Proves it's the REAL live engine, not the
    # page's own idle fallback.
    eng = Engine(Config())
    vm = eng.pages["config"].view_model()
    rows = {r["label"]: r["value"] for r in vm["engine_rows"]}
    assert rows["current_page"] == "eventlog"
    assert "img2txtviz" in rows["pages_live"]
    assert "config" in rows["pages_live"]
    assert "status" in rows["analyzers_live"]
    import midicrt
    assert rows["engine_version"] == midicrt.__version__


def test_config_page_engine_info_tracks_page_navigation():
    eng = Engine(Config())
    eng._page_goto("voices")
    rows = {r["label"]: r["value"]
            for r in eng.pages["config"].view_model()["engine_rows"]}
    assert rows["current_page"] == "voices"


def test_config_page_is_absent_when_not_in_the_roster_has_no_crash_on_engine_init():
    # A custom config that drops "config" entirely must not crash
    # Engine.__init__'s guarded bind_engine_info() call.
    eng = Engine(Config(pages=["eventlog"]))
    assert "config" not in eng.pages
