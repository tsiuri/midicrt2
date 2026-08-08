import asyncio
import errno
import json
import time

import pytest

from midicrt.config import Config, ConfigError
from midicrt.engine.actions import ActionError
from midicrt.engine.bindings import LEARN_TIMEOUT_S
from midicrt.engine.core import Engine, MidiEvent, _LearnArm


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
        "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
        "chordkey", "sendnotes",
    ]
    assert eng.current_page == "eventlog"


def test_register_page_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()
    eng.register_page("second", fake)
    assert list(eng.pages) == [
        "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
        "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
        "chordkey", "sendnotes", "second",
    ]
    assert eng.pages["second"] is fake


def test_engine_topics_reflects_roster_order():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges",
        "page.ccmonitor", "page.ccdashboard", "page.chordkey", "page.sendnotes",
        "page.second",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress",
    ]


def test_handle_marks_dirty_only_for_pages_reporting_true():
    # `ev()` is a real note_on on channel 1 -- the real "voices"/"harmony"/
    # "pianoroll"/"img2txtviz" pages (all live by default) genuinely react
    # to it too, so they're dirty here alongside eventlog, same as any
    # other real page would be. Phase-3 task 6: "alerts" (StuckNotesAnalyzer)
    # is a real analyzer too, and a fresh note-on genuinely starts tracking
    # it -- dirty for the same reason. Phase-3 task 12: "chordkey" wraps its
    # own HarmonyAnalyzer instance (same class as "harmony"'s), so it reacts
    # to a real note-on too. "timesig" does NOT react (TimesigAnalyzer gates
    # note_on on transport being "running", and no "start" was ever sent
    # here). "config" never reacts to MIDI events at all (a read-only
    # viewer, pages/configview.py's own `handle()`).
    eng = Engine(Config())
    quiet = _FakePage(dirty=False)
    eng.register_page("quiet", quiet)
    eng._handle(ev())
    assert eng._dirty == {
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll",
        "page.img2txtviz", "page.chordkey", "overlay.alerts",
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
        "page.img2txtviz", "page.chordkey", "page.loud", "overlay.alerts",
    }


# -- empty/invalid roster guard (Must-fix, 2026-08-07 fix wave) --------------
#
# Before this fix: an empty (or all-unknown-name) `config.pages` resolved to
# an empty `self.pages` dict, but `self.current_page` still fell back to the
# HARDCODED string "eventlog" (`next(iter(self.pages), "eventlog")`) -- a
# page that, in this scenario, does NOT actually exist in the roster.
# `PageCycleBehavior`'s autonomous `page.next` dispatch (behaviors/
# pagecycle.py) then calls `Engine._page_next` -> `order.index(self.
# current_page)` against an EMPTY `order` list, raising a bare `ValueError`
# that escapes `_tick_behaviors`'s `except ActionError` guard entirely and
# kills the whole `run()` loop -- an unattended background behavior tick
# crashing the daemon. Fixed by failing fast, loudly, and BEFORE any of
# that ever gets a chance to run: Engine.__init__ now raises `ConfigError`
# the moment the resolved roster is empty, so a misconfigured `config.toml`
# (or a test) never even gets an `Engine` instance to hand to `daemon.py`'s
# `ProtocolServer`/`run()` at all.

def test_empty_pages_list_raises_configerror_at_construction():
    with pytest.raises(ConfigError):
        Engine(Config(pages=[]))


def test_all_unknown_page_names_raises_configerror_at_construction():
    # Every name here is a typo/nonsense -- none matches a _PAGE_FACTORIES
    # key, so the resolved roster is empty exactly like `pages=[]` above,
    # just reached via a different, arguably more realistic, misconfig.
    with pytest.raises(ConfigError):
        Engine(Config(pages=["totally-bogus", "also-not-a-page"]))


def test_configerror_message_is_readable_and_names_the_bad_config():
    with pytest.raises(ConfigError, match="empty"):
        Engine(Config(pages=[]))


def test_a_single_known_page_name_is_not_an_empty_roster():
    # Sanity check the guard's boundary: one real page is enough to pass.
    eng = Engine(Config(pages=["eventlog"]))
    assert list(eng.pages) == ["eventlog"]


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
        "page.ccmonitor", "page.ccdashboard", "page.chordkey", "page.sendnotes",
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
        "bpm": None, "bar": 0, "beat": 1, "running": False, "source": None, "rec": False,
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
    assert eng.current_page == "ccmonitor"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "ccdashboard"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "chordkey"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "sendnotes"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "second"
    await eng.actions.dispatch("page.prev", {})
    assert eng.current_page == "sendnotes"

    names = [e["name"] for e in events]
    assert names == ["page_changed"] * 15
    assert [e["data"]["page"] for e in events] == [
        "voices", "harmony", "pianoroll", "spectrum", "screensaver", "img2txtviz",
        "config", "help", "progchanges", "ccmonitor", "ccdashboard", "chordkey",
        "sendnotes", "second", "sendnotes",
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
    assert {"pianoroll.zoom", "pianoroll.zoom_level", "pianoroll.projection",
            "pianoroll.channels"} <= set(described)


def test_pianoroll_actions_are_absent_when_the_page_is_not_in_the_roster():
    # Mirrors "eventlog.clear" always assuming eventlog exists -- guarded
    # registration means a build without "pianoroll" in config.pages simply
    # never advertises these actions (no KeyError at dispatch time either).
    eng = Engine(Config(pages=["eventlog"]))
    described = eng.actions.describe()
    assert not ({"pianoroll.zoom", "pianoroll.zoom_level", "pianoroll.projection",
                "pianoroll.channels"} & set(described))


async def test_pianoroll_zoom_level_action_sets_absolute_value_and_marks_dirty():
    # The ABSOLUTE counterpart to pianoroll.zoom's cumulative delta (review
    # finding, Important -- see engine/bindings.py's own "Trigger vs
    # continuous" docstring section for the live-reproduced saturation bug
    # this exists to fix).
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.zoom_level", {"level": "2.5"})
    assert r["zoom"] == pytest.approx(2.5)
    assert "page.pianoroll" in eng._dirty
    # NOT cumulative -- a second dispatch with a LOWER level jumps straight
    # there rather than adding on top of the first.
    r = await eng.actions.dispatch("pianoroll.zoom_level", {"level": "1.0"})
    assert r["zoom"] == pytest.approx(1.0)


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

    async def spy(name, args, **kwargs):
        calls.append((name, args))
        return await original_dispatch(name, args, **kwargs)

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

    async def spy(name, args, **kwargs):
        calls.append((name, args))
        return await original_dispatch(name, args, **kwargs)

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


# -- help page (phase-3 task 12) ----------------------------------------------

def test_help_is_in_the_default_roster():
    eng = Engine(Config())
    assert "help" in eng.pages
    assert eng.pages["help"].view_model()["title"] == "HELP"


def test_help_page_info_is_wired_to_the_real_engine_roster_and_actions():
    # Engine.__init__ binds `_help_info` into the page right after building
    # self.pages -- see pages/help.py's own "Engine-info wiring" docstring.
    eng = Engine(Config())
    vm = eng.pages["help"].view_model()
    labels = {r["label"] for r in vm["action_rows"]}
    assert "page.goto" in labels and "eventlog.clear" in labels
    assert "voices" in vm["page_rows"][0]["value"]


def test_help_page_is_absent_when_not_in_the_roster_has_no_crash_on_engine_init():
    eng = Engine(Config(pages=["eventlog"]))
    assert "help" not in eng.pages


# -- send notes page (phase-3 task 12) ----------------------------------------

class _FakeMidiOut:
    """Test double for engine/midi_out.py::MidiOutput -- captures calls
    instead of touching real MIDI, mirroring _FakeTickingAnalyzer's own
    "scripted double, no real I/O" convention above."""

    def __init__(self):
        self.note_on_calls: list[tuple[int, int, int]] = []
        self.note_off_calls: list[tuple[int, int]] = []
        self.closed = False
        self.port_name = "fake"
        self.is_open = True
        # Phase-3 task 12 fix: ordered log of every call, so tests can
        # assert RELATIVE ordering (e.g. "all note_offs happen before
        # close"), not just that each individually occurred.
        self.call_order: list[tuple] = []

    def note_on(self, note, velocity, channel):
        self.note_on_calls.append((note, velocity, channel))
        self.call_order.append(("note_on", note, velocity, channel))

    def note_off(self, note, channel):
        self.note_off_calls.append((note, channel))
        self.call_order.append(("note_off", note, channel))

    def close(self):
        self.closed = True
        self.call_order.append(("close",))


def test_sendnotes_is_in_the_default_roster():
    eng = Engine(Config())
    assert "sendnotes" in eng.pages
    assert eng.pages["sendnotes"].view_model()["title"] == "SEND NOTES"


def test_sendnotes_action_is_registered_when_the_page_is_present():
    eng = Engine(Config())
    assert "sendnotes.key" in eng.actions.describe()


def test_sendnotes_action_is_absent_when_the_page_is_not_in_the_roster():
    eng = Engine(Config(pages=["eventlog"]))
    assert "sendnotes.key" not in eng.actions.describe()
    assert "sendnotes" not in eng.pages   # bind_device_info() guard never crashed init


async def test_sendnotes_key_action_sends_a_real_note_on_through_midi_out():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    r = await eng.actions.dispatch("sendnotes.key", {"key": "z"})
    assert r == {"applied": True}
    assert fake.note_on_calls == [(60, 96, 1)]
    assert "page.sendnotes" in eng._dirty
    assert eng.pages["sendnotes"].view_model()["active"] == 1


async def test_sendnotes_key_action_control_key_never_touches_midi_out():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    r = await eng.actions.dispatch("sendnotes.key", {"key": "]"})   # octave up
    assert r == {"applied": True}
    assert fake.note_on_calls == []
    assert eng.pages["sendnotes"].view_model()["octave"] == 5


async def test_sendnotes_key_action_unrecognized_key_is_disclosed_via_applied_false():
    eng = Engine(Config())
    r = await eng.actions.dispatch("sendnotes.key", {"key": "q"})
    assert r == {"applied": False}


def test_tick_pages_drains_expired_sendnotes_and_sends_real_note_off():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng.pages["sendnotes"].apply_key("z", now=1000.0)   # gate_ms=120 -> expires 1000.12
    eng._tick_pages(1000.5)   # past expiry
    assert fake.note_off_calls == [(60, 1)]
    assert "page.sendnotes" in eng._dirty
    assert eng.pages["sendnotes"].view_model()["active"] == 0


def test_tick_pages_does_not_touch_midi_out_when_nothing_has_expired():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng.pages["sendnotes"].apply_key("z", now=1000.0)
    eng._tick_pages(1000.01)   # well before the 120ms gate elapses
    assert fake.note_off_calls == []


def test_tick_pages_is_a_no_op_when_sendnotes_is_not_in_the_roster():
    eng = Engine(Config(pages=["eventlog"]))
    eng._tick_pages(1000.0)   # must not raise (no "sendnotes" key in self.pages)


# -- tick-then-drain ordering contract (fix, code-review finding) -----------
#
# `_tick_pages` must run ALL pages' tick() first, then ALL drain_outputs()
# second -- never interleaved -- regardless of roster order. The shipped
# default roster happens to have "sendnotes" (the only drain_outputs()
# implementer today) LAST, so a single merged per-page loop (tick-then-drain
# for each page before moving to the next) looks identical to the correct
# two-pass behavior there -- it only diverges under a roster where a
# draining page precedes a ticking page, which is exactly what this test
# constructs via register_page().

class _FakeOrderedDrainingPage:
    """Registered FIRST (so it would run first in a naive single merged
    loop) -- exposes ONLY drain_outputs(), records into a list shared with
    the ticking page below."""

    def __init__(self, call_log: list[str]):
        self._call_log = call_log

    def handle(self, ev) -> bool:
        return False

    def view_model(self) -> dict:
        return {}

    def drain_outputs(self, now: float) -> list[tuple[int, int]]:
        self._call_log.append("drain")
        return []


class _FakeOrderedTickingPage:
    """Registered SECOND (after the draining page above) -- exposes ONLY
    tick(), records into the SAME shared list."""

    def __init__(self, call_log: list[str]):
        self._call_log = call_log

    def handle(self, ev) -> bool:
        return False

    def view_model(self) -> dict:
        return {}

    def tick(self, now: float) -> bool:
        self._call_log.append("tick")
        return False


def test_tick_pages_drains_strictly_after_all_ticks_even_when_drain_page_precedes_ticker_in_roster():
    eng = Engine(Config())
    call_log: list[str] = []
    eng.register_page("drainer", _FakeOrderedDrainingPage(call_log))
    eng.register_page("ticker", _FakeOrderedTickingPage(call_log))
    eng._tick_pages(1.0)
    # "drainer" is EARLIER in roster order than "ticker" -- if tick/drain
    # were interleaved per-page (the bug), "drain" would be logged first.
    # The contract is roster-order-independent: every tick() everywhere
    # runs before any drain_outputs() anywhere.
    assert call_log == ["tick", "drain"]


def test_engine_stop_closes_midi_out():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng.stop()
    assert fake.closed is True


# -- self-subscription feedback-loop fix (live-reproduced Critical) --------
#
# Layer 2 (belt and suspenders alongside engine/midi_in.py's own
# exclude_names, layer 1): Engine._handle drops ANY event -- not just
# sysex -- whose `source` refers to our own MidiOutput port, before any
# processing at all. Reproduced live: every real Send Notes trigger
# echoed back as a phantom external note-on, contaminating
# voices/harmony/stucknotes/eventlog with fake incoming MIDI.

def test_handle_drops_an_event_whose_source_is_our_own_output_port():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    before_total = eng.events_total
    eng._handle(ev(source=fake.port_name))
    assert eng.events_total == before_total          # true drop, not even counted
    assert eng._dirty == set()                        # no page/analyzer saw it
    assert eng.pages["voices"].view_model()["total"] == 0   # no phantom note tracked


def test_handle_drops_an_event_with_the_real_prefixed_alsa_source_form():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    fake.port_name = "midicrt2 Output"
    eng._midi_out = fake
    eng._handle(ev(source="RtMidiOut Client:midicrt2 Output 142:0"))
    assert eng.pages["voices"].view_model()["total"] == 0


def test_handle_still_processes_an_event_from_a_genuinely_different_source():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    before_total = eng.events_total
    eng._handle(ev(source="USB MIDI Keyboard"))
    assert eng.events_total == before_total + 1
    assert eng.pages["voices"].view_model()["total"] == 1


# -- guaranteed note-offs on shutdown (Important, live-reproduced) ---------
#
# Engine.stop() used to close MidiOutput without draining SendNotesPage's
# still-gated active notes -- a routine restart mid-note left a real
# downstream synth holding a stuck note with no way to release it.

async def test_engine_stop_flushes_pending_sendnotes_before_closing_midi_out():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    await eng.actions.dispatch("sendnotes.key", {"key": "z"})    # note 60, ch 1, gate 120ms
    await eng.actions.dispatch("sendnotes.key", {"key": "s"})    # note 61, ch 1
    fake.note_on_calls.clear()   # only care about the SHUTDOWN-time note_offs below
    fake.call_order.clear()
    eng.stop()
    assert set(fake.note_off_calls) == {(60, 1), (61, 1)}
    assert eng.pages["sendnotes"].view_model()["active"] == 0
    # Order matters: both note_offs must reach midi_out BEFORE close(),
    # while the port is still usable -- not after.
    close_index = fake.call_order.index(("close",))
    note_off_indices = [i for i, c in enumerate(fake.call_order) if c[0] == "note_off"]
    assert note_off_indices and all(i < close_index for i in note_off_indices)


def test_engine_stop_is_a_no_op_when_sendnotes_is_not_in_the_roster():
    eng = Engine(Config(pages=["eventlog"]))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng.stop()   # must not raise (no "sendnotes" key in self.pages)
    assert fake.closed is True


def test_engine_stop_with_no_pending_notes_sends_no_note_offs():
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng.stop()
    assert fake.note_off_calls == []


# -- subscriber-aware snapshot materialization (Important, 2026-08-07 fix
# wave, finding 1) ------------------------------------------------------
#
# Root cause (live-measured on the Pi): Img2TxtVizAnalyzer.tick() ALWAYS
# returns True by design (its own docstring: "a continuous animation with
# no 'nothing changed' state"), so `page.img2txtviz` is in `self._dirty`
# EVERY tick at tick_hz=30 regardless of whether a single client has ever
# subscribed to it -- and the old `run()` loop called `snapshot_now(topic)`
# (materializing the page's 6.94ms view_model()) for EVERY dirty topic
# unconditionally, burning 35-40% idle CPU with zero subscribers. Fix:
# `Engine._flush_dirty()` (extracted from `run()`'s own loop body so it's
# directly callable from a synchronous test, no real asyncio run() task
# needed) now skips `snapshot_now` for any topic a wired
# `_topic_refcount_provider` reports as having zero subscribers.
# `events`/`describe` are untouched -- this only ever gates the dirty-topic
# SNAPSHOT loop, never `emit_event`/`snapshot_now` itself (see the
# dedicated "subscribe-time path bypasses the gate" tests below).

class _SpyPage:
    """Counts view_model() calls -- the thing finding 1 needs to observe
    directly (a snapshot topic's cost lives entirely in materializing this
    return value, not in marking the topic dirty)."""

    def __init__(self):
        self.view_model_calls = 0

    def handle(self, ev) -> bool:
        return True

    def view_model(self) -> dict:
        self.view_model_calls += 1
        return {"n": self.view_model_calls}


def test_flush_dirty_with_no_provider_wired_always_materializes():
    # Default (unwired) behavior MUST be preserved exactly: a bare Engine
    # with no ProtocolServer ever attached (e.g. every add_listener()-based
    # test earlier in this very file, none of which ever call
    # set_topic_refcount_provider) has nobody who could possibly report
    # subscriber counts -- "nobody told me who's subscribed" is NOT the
    # same claim as "nobody is subscribed", so the unwired default must
    # keep materializing every dirty topic exactly like before this fix.
    eng = Engine(Config())
    spy = _SpyPage()
    eng.register_page("spy", spy)
    eng._dirty.add("page.spy")
    eng._flush_dirty()
    assert spy.view_model_calls == 1
    assert eng._dirty == set()   # still cleared either way


def test_flush_dirty_skips_materialization_when_refcount_is_zero():
    eng = Engine(Config())
    spy = _SpyPage()
    eng.register_page("spy", spy)
    eng.set_topic_refcount_provider(lambda topic: 0)
    eng._dirty.add("page.spy")
    eng._flush_dirty()
    assert spy.view_model_calls == 0   # the whole point of the fix
    assert eng._dirty == set()


def test_flush_dirty_materializes_when_refcount_is_positive():
    eng = Engine(Config())
    spy = _SpyPage()
    eng.register_page("spy", spy)
    eng.set_topic_refcount_provider(lambda topic: 1)
    eng._dirty.add("page.spy")
    eng._flush_dirty()
    assert spy.view_model_calls == 1


def test_flush_dirty_respects_refcount_independently_per_topic():
    eng = Engine(Config())
    subscribed, unsubscribed = _SpyPage(), _SpyPage()
    eng.register_page("has_sub", subscribed)
    eng.register_page("no_sub", unsubscribed)
    eng.set_topic_refcount_provider(lambda topic: 1 if topic == "page.has_sub" else 0)
    eng._dirty |= {"page.has_sub", "page.no_sub"}
    eng._flush_dirty()
    assert subscribed.view_model_calls == 1
    assert unsubscribed.view_model_calls == 0


def test_flush_dirty_stops_materializing_after_refcount_drops_back_to_zero():
    # Simulates an unsubscribe/disconnect mid-run: a mutable counter the
    # provider reads live (matching how ProtocolServer's own refcount dict
    # works -- see engine/server.py), not a value baked in at subscribe
    # time.
    eng = Engine(Config())
    spy = _SpyPage()
    eng.register_page("spy", spy)
    refcount = {"page.spy": 1}
    eng.set_topic_refcount_provider(lambda topic: refcount.get(topic, 0))

    eng._dirty.add("page.spy")
    eng._flush_dirty()
    assert spy.view_model_calls == 1

    refcount["page.spy"] = 0
    eng._dirty.add("page.spy")
    eng._flush_dirty()
    assert spy.view_model_calls == 1   # unchanged -- no new materialization


def test_snapshot_now_is_never_gated_by_the_refcount_provider():
    # A NEW subscriber must still get its initial snapshot via
    # ProtocolServer._dispatch's own subscribe-time `engine.snapshot_now
    # (topic)` call -- that path is DELIBERATELY separate from
    # `_flush_dirty()`'s dirty-loop and must never consult the refcount
    # provider at all (a fresh subscriber is, by definition, not yet
    # counted for a topic it just asked for -- gating snapshot_now itself
    # would starve every subscribe of its very first frame). Proven here
    # with a provider that reports zero for everything.
    eng = Engine(Config())
    eng.set_topic_refcount_provider(lambda topic: 0)
    snap = eng.snapshot_now("page.eventlog")
    assert snap is not None


# -- harmony/chordkey shared HarmonyAnalyzer (Important, finding 2b,
# 2026-08-07 fix wave) -----------------------------------------------------
#
# analyzers/harmony.py's own module docstring covers the dedup guard that
# makes sharing safe (not just cheaper). These tests cover the OTHER half:
# Engine actually wires the SAME instance to both pages when both make the
# roster.

def test_harmony_and_chordkey_pages_share_one_analyzer_instance():
    eng = Engine(Config())
    assert eng.pages["harmony"].analyzer is eng.pages["chordkey"].analyzer


def test_harmony_and_chordkey_do_not_double_count_a_shared_event():
    eng = Engine(Config())
    shared = eng.pages["harmony"].analyzer
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0))
    # "harmony" precedes "chordkey" in the default roster (config.py) --
    # Engine._handle() calls harmony's handle(ev) first (real processing),
    # then chordkey's (the dedup guard's no-op branch, same ev object).
    assert len(shared._recent_notes) == 1
    assert shared.recent_pcs == {0}


def test_harmony_and_chordkey_report_the_same_underlying_key_after_a_shared_note():
    eng = Engine(Config())
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0))
    # Same C-major-tonic key fact, surfaced through each page's own VM
    # shape (harmony.py's bare label vs chordkey.py's {label, pct, ...}) --
    # both derive from the SAME shared analyzer instance.
    assert eng.pages["harmony"].view_model()["key"] == "C maj"
    assert eng.pages["chordkey"].view_model()["key"]["label"] == "C maj"


def test_a_solo_harmony_page_still_gets_its_own_working_analyzer():
    # Boundary case: a custom roster with "harmony" but not "chordkey" --
    # nothing to share WITH, but harmony must still work exactly as before.
    eng = Engine(Config(pages=["eventlog", "harmony"]))
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0))
    assert eng.pages["harmony"].view_model()["key"] == "C maj"


# -- config-served keymap (Phase 4 Task 1, docs/phase4-notes.md) ------------
#
# `Engine.keymap` is computed ONCE at construction (`keymap.load_keymap` +
# `keymap.filter_known_actions`, gated on a nonexistent path by default so
# these tests never touch a real `~/.config/midicrt/keymap.toml`) and
# refreshed by the `config.reload` action below. `known_actions` for the
# filter step is `set(self.actions.describe())` -- built AFTER every
# action (engine-owned and page-declared) is registered, so it correctly
# reflects THIS build's roster-dependent vocabulary.

def test_engine_keymap_defaults_when_no_keymap_file(tmp_path):
    from midicrt.engine import keymap as keymap_mod

    eng = Engine(Config(), keymap_path=str(tmp_path / "nope.toml"))
    assert eng.keymap == keymap_mod.DEFAULT_KEYMAP


def test_engine_keymap_reads_a_real_keymap_file(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "eventlog.clear"\n')
    eng = Engine(Config(), keymap_path=str(p))
    assert eng.keymap["v"] == "eventlog.clear"
    assert eng.keymap["q"] == "client.quit"   # untouched default, merge semantics


def test_engine_keymap_filters_out_an_action_absent_from_this_roster(tmp_path, caplog):
    # "sendnotes.key" only exists when "sendnotes" is in the roster (see
    # engine/core.py's own guarded registration) -- a minimal roster
    # without it makes this keymap entry genuinely unknown to THIS build.
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nz = "sendnotes.key"\n')
    with caplog.at_level("WARNING"):
        eng = Engine(Config(pages=["eventlog"]), keymap_path=str(p))
    assert "z" not in eng.keymap
    assert "sendnotes.key" in caplog.text


def test_engine_keymap_keeps_client_pseudo_action_regardless_of_roster(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nx = "client.quit"\n')
    eng = Engine(Config(pages=["eventlog"]), keymap_path=str(p))
    assert eng.keymap["x"] == "client.quit"


def test_engine_keymap_filters_out_an_action_requiring_args(tmp_path, caplog):
    # Bindings review, live-reproduced Critical finding: "page.goto" is
    # registered unconditionally (always reachable regardless of roster)
    # but needs a "name" arg `dispatch_key` can never supply from a single
    # keypress -- must be dropped at construction, not left as a landmine.
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "page.goto"\n')
    with caplog.at_level("WARNING"):
        eng = Engine(Config(), keymap_path=str(p))
    assert "v" not in eng.keymap
    assert "page.goto" in caplog.text
    assert "args" in caplog.text.lower()


def test_engine_keymap_falls_back_to_defaults_when_keymap_toml_is_malformed_at_startup(
        tmp_path, caplog):
    # Bindings review, live-reproduced Critical finding: a malformed
    # keymap.toml must never crash daemon startup -- the appliance ethos
    # (a bad OPTIONAL config file is never fatal) already established by
    # `config.py`'s own "no file -> defaults" contract, extended here to
    # "unparseable file -> defaults, loudly logged" rather than raising.
    p = tmp_path / "keymap.toml"
    p.write_text("this is not valid toml {{{ [[[ ===\n")
    with caplog.at_level("WARNING"):
        eng = Engine(Config(), keymap_path=str(p))   # must not raise
    from midicrt.engine import keymap as keymap_mod
    assert eng.keymap == keymap_mod.DEFAULT_KEYMAP
    assert "keymap.toml" in caplog.text.lower()


def test_engine_keymap_falls_back_to_defaults_when_keys_is_wrong_shaped_at_startup(
        tmp_path, caplog):
    # Re-review, live-reproduced: `keys = "oops"` is SYNTACTICALLY VALID
    # TOML -- the previous fix (widening load_keymap_or_warn's catch
    # tuple) did NOT cover this, since the failure was an uncaught
    # AttributeError, not a ValueError/TOMLDecodeError. "The daemon won't
    # start after I edited keymap.toml" is one bug class regardless of
    # syntax-vs-shape typo.
    p = tmp_path / "keymap.toml"
    p.write_text('keys = "oops"\n')
    with caplog.at_level("WARNING"):
        eng = Engine(Config(), keymap_path=str(p))   # must not raise
    from midicrt.engine import keymap as keymap_mod
    assert eng.keymap == keymap_mod.DEFAULT_KEYMAP
    assert "keymap.toml" in caplog.text.lower()


def test_engine_default_keymap_path_is_the_real_config_dir_path():
    # No keymap_path override -- must fall back to keymap.DEFAULT_PATH
    # (~/.config/midicrt/keymap.toml), the same default `config.py`'s own
    # DEFAULT_PATH convention establishes, rather than silently going
    # unbound.
    from midicrt.engine import keymap as keymap_mod

    eng = Engine(Config())
    assert eng._keymap_path == keymap_mod.DEFAULT_PATH


def test_describe_style_action_registry_contains_config_reload():
    eng = Engine(Config())
    assert "config.reload" in eng.actions.describe()


# -- config.reload action (Phase 4 Task 1) -----------------------------------

async def test_config_reload_rereads_keymap_and_emits_keymap_changed(tmp_path):
    keymap_path = tmp_path / "keymap.toml"
    eng = Engine(Config(), keymap_path=str(keymap_path))
    assert eng.keymap["n"] == "page.next"   # pre-reload: built-in default

    got = []
    eng.add_listener(got.append)
    keymap_path.write_text('[keys]\nn = "page.prev"\n')
    result = await eng.actions.dispatch("config.reload", {})

    assert eng.keymap["n"] == "page.prev"
    assert result["keymap"]["n"] == "page.prev"
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "keymap_changed"]
    assert events, "config.reload must emit a keymap_changed event"
    assert events[-1]["data"]["keymap"]["n"] == "page.prev"


async def test_config_reload_with_no_keymap_file_stays_at_defaults(tmp_path):
    from midicrt.engine import keymap as keymap_mod

    eng = Engine(Config(), keymap_path=str(tmp_path / "nope.toml"))
    result = await eng.actions.dispatch("config.reload", {})
    assert result["keymap"] == keymap_mod.DEFAULT_KEYMAP


async def test_config_reload_malformed_keymap_toml_keeps_last_good_and_warns(tmp_path, caplog):
    # Bindings review, live-reproduced Critical finding: an unguarded
    # `load_keymap` call here used to raise `tomllib.TOMLDecodeError`
    # straight out of the action handler -- an uncaught exception that
    # tears down the REQUESTING CONNECTION (escapes `ActionRegistry.
    # dispatch`'s own `except ActionError` narrowing entirely). Must
    # instead surface as a `warnings` entry, keep the keymap from BEFORE
    # this call untouched, and never raise.
    keymap_path = tmp_path / "keymap.toml"
    keymap_path.write_text('[keys]\nv = "eventlog.clear"\n')
    eng = Engine(Config(), keymap_path=str(keymap_path))
    good_keymap = dict(eng.keymap)
    assert good_keymap["v"] == "eventlog.clear"

    keymap_path.write_text("this is not valid toml {{{ [[[ ===\n")
    with caplog.at_level("WARNING"):
        result = await eng.actions.dispatch("config.reload", {})   # must not raise

    assert eng.keymap == good_keymap   # unchanged -- last-good kept
    assert result["keymap"] == good_keymap
    assert any("keymap.toml" in w.lower() for w in result["warnings"])
    assert "keymap.toml" in caplog.text.lower()


async def test_config_reload_keys_wrong_shaped_keeps_last_good_and_warns(tmp_path, caplog):
    # Re-review, live-reproduced: `keys = "oops"` at reload time is the
    # SAME uncaught-AttributeError shape the startup test above covers,
    # just through `_config_reload` instead of `__init__` -- must surface
    # as a `warnings` entry (ok:true, connection alive), never raise.
    keymap_path = tmp_path / "keymap.toml"
    keymap_path.write_text('[keys]\nv = "eventlog.clear"\n')
    eng = Engine(Config(), keymap_path=str(keymap_path))
    good_keymap = dict(eng.keymap)

    keymap_path.write_text('keys = "oops"\n')
    with caplog.at_level("WARNING"):
        result = await eng.actions.dispatch("config.reload", {})   # must not raise

    assert eng.keymap == good_keymap
    assert result["keymap"] == good_keymap
    assert any("keys" in w.lower() for w in result["warnings"])


async def test_config_reload_warns_when_config_toml_pages_roster_changed(tmp_path, caplog):
    config_path = tmp_path / "config.toml"
    config_path.write_text('pages = ["eventlog", "voices"]\n')
    eng = Engine(Config(pages=["eventlog", "voices"]), config_path=str(config_path))
    original_roster = list(eng.pages)

    config_path.write_text('pages = ["eventlog"]\n')
    with caplog.at_level("WARNING"):
        result = await eng.actions.dispatch("config.reload", {})

    # The live roster is NEVER rebuilt from a reload -- restart required.
    assert list(eng.pages) == original_roster
    assert any("restart" in w for w in result["warnings"])
    assert "restart" in caplog.text.lower()


async def test_config_reload_updates_voices_instruments_from_config_toml(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('instruments = ["Custom One"]\n')
    eng = Engine(Config(), config_path=str(config_path))
    assert eng.pages["voices"].view_model()["rows"][0]["name"] == "Kawai XD5"

    await eng.actions.dispatch("config.reload", {})

    vm = eng.pages["voices"].view_model()
    assert vm["rows"][0]["name"] == "Custom One"
    assert vm["rows"][1]["name"] == "CH 2"   # falls back same as VoicesPage always has
    assert eng._dirty >= {"page.voices"}


async def test_config_reload_is_a_noop_convenience_when_no_config_toml_exists(tmp_path):
    # `config_path` points at a file that doesn't exist -- config_mod.load()
    # itself already tolerates that (returns Config() defaults), so this
    # must not raise or spuriously warn about a "roster change".
    eng = Engine(Config(), config_path=str(tmp_path / "nope.toml"))
    result = await eng.actions.dispatch("config.reload", {})
    assert result["warnings"] == []


# -- MIDI bindings (Phase 4 Task 2, docs/phase4-notes.md) --------------------
#
# Pure BindingDispatcher/BindingsFile/validate_binding logic is tested in
# test_bindings.py (same split as test_keymap.py vs this file's own keymap
# section above) -- everything below is registry-aware ENGINE wiring: the
# dispatch-context split (`_handle` collects, `_dispatch_bindings` -- called
# from `run()` -- actually dispatches), `bind.list`/`bind.remove`,
# `config.reload`'s bindings.toml half, and the headline zero-client-firing
# proof (docs/phase4-notes.md's whole reason this task exists).

def _write_trigger_binding(path, binding_id="b1", action="page.next", args_toml="", **match):
    match.setdefault("type", "note_on")
    match.setdefault("number", 60)
    match_lines = "\n".join(f"{k} = {v!r}" if not isinstance(v, str) else f'{k} = "{v}"'
                            for k, v in match.items() if v is not None)
    args_section = f"\n[bindings.{binding_id}.args]\n{args_toml}\n" if args_toml else ""
    path.write_text(
        f'[bindings.{binding_id}]\n'
        f'action = "{action}"\n'
        f'{args_section}'
        f'\n[bindings.{binding_id}.match]\n'
        f'{match_lines}\n'
    )


def test_engine_bindings_path_default_is_the_real_config_dir_path():
    from midicrt.engine import bindings as bindings_mod

    eng = Engine(Config())
    assert eng._bindings_path == bindings_mod.DEFAULT_PATH


def test_engine_starts_with_no_bindings_file_present(tmp_path):
    eng = Engine(Config(), bindings_path=str(tmp_path / "nope.toml"))
    assert eng._bindings_file.bindings == []


def test_engine_loads_a_real_bindings_file(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p)
    eng = Engine(Config(), bindings_path=str(p))
    assert [b.id for b in eng._bindings_file.bindings] == ["b1"]


def test_engine_bindings_falls_back_to_empty_when_file_is_malformed_at_startup(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text("this is not valid toml {{{ [[[ ===\n")
    with caplog.at_level("WARNING"):
        eng = Engine(Config(), bindings_path=str(p))   # must not raise
    assert eng._bindings_file.bindings == []
    assert "bindings.toml" in caplog.text.lower()


def test_engine_startup_keeps_a_roster_absent_binding_but_logs_a_warning(tmp_path, caplog):
    # "kept-but-inert" (bind.list's own docstring, engine/bindings.py's
    # validate_binding docstring): a binding referencing an action absent
    # from THIS build's roster is NOT dropped at load, unlike a keymap
    # entry -- it stays visible (e.g. to `bind.list`) so a user can
    # diagnose it, and simply never fires (see the dispatch-time test
    # below).
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="sendnotes.key", args_toml='key = "z"')
    with caplog.at_level("WARNING"):
        eng = Engine(Config(pages=["eventlog"]), bindings_path=str(p))
    assert [b.id for b in eng._bindings_file.bindings] == ["b1"]   # kept
    assert "sendnotes.key" in caplog.text


def test_describe_style_action_registry_contains_bind_list_and_remove():
    eng = Engine(Config())
    actions = eng.actions.describe()
    assert "bind.list" in actions
    assert "bind.remove" in actions
    assert actions["bind.remove"]["args"] == {"id": "str"}


# -- zero-client firing: the headline proof --------------------------------

async def test_binding_fires_action_and_changes_engine_state_with_zero_clients(tmp_path):
    # THE proof this whole task exists for: a real Engine, a real run()
    # loop, a real queued MidiEvent -- and NO `add_listener()` call at all
    # (unlike test_engine_publishes_dirty_snapshots above, which always
    # attaches one). A sequencer/controller can drive page navigation with
    # nobody watching a fb/tui client.
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    assert eng.current_page == "eventlog"
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    assert eng.current_page == "voices"   # page.next advanced -- zero clients ever attached


async def test_binding_cc_trigger_fires_with_zero_clients(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next", type="control_change", number=20)
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="control_change", data1=20, data2=10))   # baseline only
    await asyncio.sleep(0.05)
    assert eng.current_page == "eventlog"
    await eng.queue.put(ev(type="control_change", data1=20, data2=90))   # crosses threshold 64
    await asyncio.sleep(0.05)
    eng.stop()
    await task
    assert eng.current_page == "voices"


async def test_binding_continuous_fills_a_real_float_arg_with_zero_clients(tmp_path):
    # Review finding (Important, live-reproduced): this test originally
    # targeted "pianoroll.zoom" (a CUMULATIVE delta) as "just plumbing
    # proof" -- but that is exactly the semantically-wrong target a
    # continuous binding should never actually use in practice (see
    # engine/bindings.py's own "Trigger vs continuous" docstring section).
    # Now targets "pianoroll.zoom_level", the ABSOLUTE setter added
    # specifically for this mode.
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.c1]\n'
        'action = "pianoroll.zoom_level"\n'
        'mode = "continuous"\n'
        'range = [0.5, 2.5]\n'
        '\n'
        '[bindings.c1.args]\n'
        'level = "$midicrt_fill_from_cc$"\n'
        '\n'
        '[bindings.c1.match]\n'
        'type = "control_change"\n'
        'number = 22\n'
    )
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    assert eng.pages["pianoroll"].view_model()["window"]["zoom"] == 1.0
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="control_change", data1=22, data2=127))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    assert eng.pages["pianoroll"].view_model()["window"]["zoom"] == pytest.approx(2.5)


async def test_binding_continuous_sweep_sets_pianoroll_zoom_level_proportionally(tmp_path):
    # Reviewer finding (Important), the actual "does this behave like a
    # knob user expects" proof: bind pianoroll.zoom_level (the ABSOLUTE
    # setter) to a CC, range=[ZOOM_MIN, ZOOM_MAX], and drive a whole sweep
    # of messages through the REAL dispatch path (queue -> run() ->
    # _handle -> _dispatch_bindings -> ActionRegistry.dispatch), not a
    # single message -- proves each message sets the zoom to its OWN
    # proportional position, unmoved by how many prior messages already
    # fired (the cumulative-delta saturation bug this whole fix exists
    # for: reviewer live-reproduced "pianoroll.zoom" pinning to ZOOM_MAX
    # after only a handful of sweep messages).
    from midicrt.pages.pianoroll import ZOOM_MAX, ZOOM_MIN

    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.c1]\n'
        'action = "pianoroll.zoom_level"\n'
        'mode = "continuous"\n'
        f'range = [{ZOOM_MIN!r}, {ZOOM_MAX!r}]\n'
        '\n'
        '[bindings.c1.args]\n'
        'level = "$midicrt_fill_from_cc$"\n'
        '\n'
        '[bindings.c1.match]\n'
        'type = "control_change"\n'
        'number = 23\n'
    )
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    task = asyncio.create_task(eng.run())

    async def _sweep_to(cc_value):
        await eng.queue.put(ev(type="control_change", data1=23, data2=cc_value))
        await asyncio.sleep(0.02)
        return eng.pages["pianoroll"].view_model()["window"]["zoom"]

    # A full sweep of many messages FIRST -- if zoom_level were cumulative
    # (the bug this fixes), this alone would already have pinned it to
    # ZOOM_MAX. It must not have: the very next single message below still
    # lands EXACTLY where its own CC value maps to, regardless of this
    # sweep having happened first.
    for cc_value in range(0, 128, 4):
        await _sweep_to(cc_value)

    assert await _sweep_to(0) == pytest.approx(ZOOM_MIN)
    assert await _sweep_to(127) == pytest.approx(ZOOM_MAX)
    assert await _sweep_to(64) == pytest.approx(
        ZOOM_MIN + (64 / 127) * (ZOOM_MAX - ZOOM_MIN), rel=1e-6)

    eng.stop()
    await task


# -- dispatch-time graceful skip (never crash the loop, no alert storm) -----

async def test_dispatch_bindings_skips_roster_absent_action_and_logs_without_crashing(
        tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="sendnotes.key", args_toml='key = "z"')
    eng = Engine(Config(pages=["eventlog"]), bindings_path=str(p))
    eng._handle(ev(type="note_on", data1=60, data2=100))
    with caplog.at_level("WARNING"):
        await eng._dispatch_bindings()   # must not raise
    assert "sendnotes.key" in caplog.text
    assert eng.current_page == "eventlog"


async def test_dispatch_bindings_never_emits_an_alert_event_for_a_skipped_action(tmp_path):
    # Task-2 brief: "log + skip, NOT an alert storm" -- a stale binding
    # firing repeatedly must never spam the alert channel every time it's
    # (harmlessly) triggered.
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="sendnotes.key", args_toml='key = "z"')
    eng = Engine(Config(pages=["eventlog"]), bindings_path=str(p))
    got = []
    eng.add_listener(got.append)
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()
    alerts = [m for m in got if m.get("kind") == "event" and m.get("name") == "alert"]
    assert alerts == []


async def test_dispatch_bindings_is_a_noop_when_nothing_pending():
    eng = Engine(Config())
    await eng._dispatch_bindings()   # must not raise with nothing queued


# -- bind.list / bind.remove actions -----------------------------------------

async def test_bind_list_reports_a_valid_binding():
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(type="note_on", number=60),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert len(result["bindings"]) == 1
    entry = result["bindings"][0]
    assert entry["id"] == "b1"
    assert entry["action"] == "page.next"
    assert entry["valid"] is True
    assert entry["error"] is None


async def test_bind_list_reports_an_invalid_roster_absent_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="sendnotes.key", args_toml='key = "z"')
    eng = Engine(Config(pages=["eventlog"]), bindings_path=str(p))
    result = await eng.actions.dispatch("bind.list", {})
    entry = result["bindings"][0]
    assert entry["valid"] is False
    assert "sendnotes.key" in entry["error"]


async def test_bind_remove_drops_the_binding_and_persists_atomically(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    eng = Engine(Config(), bindings_path=str(p))
    result = await eng.actions.dispatch("bind.remove", {"id": "b1"})
    assert result["removed"] is True
    assert eng._bindings_file.bindings == []
    # Persisted -- reloading the file from disk shows it gone too.
    from midicrt.engine.bindings import BindingsFile
    assert BindingsFile.load(str(p)).bindings == []


async def test_bind_remove_disarms_the_dispatcher_immediately():
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(type="note_on", number=60),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    await eng.actions.dispatch("bind.remove", {"id": "b1"})
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()
    assert eng.current_page == "eventlog"   # the removed binding no longer fires


async def test_bind_remove_unknown_id_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="b1"):
        await eng.actions.dispatch("bind.remove", {"id": "b1"})


def _binding_module():
    from midicrt.engine import bindings
    return bindings


# -- config.reload also reloads bindings.toml --------------------------------

async def test_config_reload_picks_up_a_newly_added_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text("# empty -- no bindings yet\n")
    eng = Engine(Config(), bindings_path=str(p))
    assert eng._bindings_file.bindings == []

    _write_trigger_binding(p, action="page.next")
    await eng.actions.dispatch("config.reload", {})

    assert [b.id for b in eng._bindings_file.bindings] == ["b1"]
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()
    assert eng.current_page == "voices"


async def test_config_reload_malformed_bindings_toml_keeps_last_good_and_warns(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    eng = Engine(Config(), bindings_path=str(p))
    good = list(eng._bindings_file.bindings)

    p.write_text("this is not valid toml {{{ [[[ ===\n")
    with caplog.at_level("WARNING"):
        result = await eng.actions.dispatch("config.reload", {})   # must not raise

    assert eng._bindings_file.bindings == good
    assert any("bindings.toml" in w.lower() for w in result["warnings"])


async def test_config_reload_with_no_bindings_file_is_a_noop_convenience(tmp_path):
    eng = Engine(Config(), bindings_path=str(tmp_path / "nope.toml"))
    result = await eng.actions.dispatch("config.reload", {})
    assert eng._bindings_file.bindings == []
    assert result["warnings"] == []


# -- bind.learn / bind.cancel: DAW-style MIDI learn (Phase 4 Task 3,
# docs/phase4-notes.md) --------------------------------------------------
#
# Pure `is_learnable_event`/`LEARN_TIMEOUT_S` logic is tested in
# test_bindings.py (same split test_bindings.py's own header comment
# establishes for Task 2). Everything below is registry-aware ENGINE
# wiring: arm-time validation (reuses `validate_binding` against a probe
# Binding), the `_handle`-time capture-vs-dispatch consumption decision,
# persistence, events, re-arm, and the tick-driven timeout.

async def test_describe_style_action_registry_contains_bind_learn_and_cancel():
    eng = Engine(Config())
    actions = eng.actions.describe()
    assert "bind.learn" in actions
    assert actions["bind.learn"]["args"] == {"action": "str", "mode": "str", "args": "dict"}
    assert "bind.cancel" in actions
    assert actions["bind.cancel"]["args"] == {}


# -- bind.learn: arm-time validation -----------------------------------------

async def test_bind_learn_arms_and_emits_learn_armed_event():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    result = await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    assert result == {"armed": True, "action": "page.next", "mode": "trigger"}
    assert eng._learn_armed is not None
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_armed"]
    assert events
    assert events[-1]["data"]["action"] == "page.next"
    assert events[-1]["data"]["mode"] == "trigger"


async def test_bind_learn_unknown_action_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="bogus.action"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "bogus.action", "mode": "trigger", "args": {}})
    assert eng._learn_armed is None


async def test_bind_learn_unknown_mode_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="mode"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "page.next", "mode": "bogus", "args": {}})
    assert eng._learn_armed is None


async def test_bind_learn_trigger_missing_required_arg_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="name"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "page.goto", "mode": "trigger", "args": {}})
    assert eng._learn_armed is None


async def test_bind_learn_trigger_unknown_extra_arg_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="bogus"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "page.next", "mode": "trigger", "args": {"bogus": "x"}})


async def test_bind_learn_trigger_valid_args_arms_successfully():
    eng = Engine(Config())
    result = await eng.actions.dispatch(
        "bind.learn", {"action": "page.goto", "mode": "trigger", "args": {"name": "harmony"}})
    assert result["armed"] is True
    assert eng._learn_armed.args_template == {"name": "harmony"}


async def test_bind_learn_trigger_rejects_a_none_valued_arg_with_clean_action_error():
    # Review fix (Minor): validate_binding's own new None-in-trigger check
    # (test_bindings.py), surfaced here through the real bind.learn arm
    # path -- a clean ActionError at ARM time, never a silently-wrong
    # persisted binding.
    eng = Engine(Config())
    with pytest.raises(ActionError, match="[Nn]one"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "page.goto", "mode": "trigger", "args": {"name": None}})
    assert eng._learn_armed is None


async def test_bind_learn_continuous_requires_exactly_one_float_arg_when_none_declared():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="float"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "page.goto", "mode": "continuous", "args": {}})
    assert eng._learn_armed is None


async def test_bind_learn_continuous_requires_exactly_one_float_arg_when_two_declared():
    eng = Engine(Config())
    eng.actions.register("test.twofloat", lambda a, b: {"a": a, "b": b},
                         args={"a": "float", "b": "float"})
    with pytest.raises(ActionError, match="float"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "test.twofloat", "mode": "continuous", "args": {}})


async def test_bind_learn_continuous_auto_detects_the_declared_float_arg_as_fill_key():
    eng = Engine(Config())
    result = await eng.actions.dispatch(
        "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {}})
    assert result["armed"] is True
    assert eng._learn_armed.args_template == {"level": None}


async def test_bind_learn_continuous_rejects_the_fill_key_if_passed_explicitly():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="level"):
        await eng.actions.dispatch(
            "bind.learn",
            {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {"level": "0.5"}})
    assert eng._learn_armed is None


async def test_bind_learn_rearm_replaces_previous_arm_and_emits_learn_armed_again():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    assert eng._learn_armed.action == "page.prev"   # replaced, not stacked
    armed_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_armed"]
    assert len(armed_events) == 2
    assert armed_events[0]["data"]["action"] == "page.next"
    assert armed_events[1]["data"]["action"] == "page.prev"


# -- bind.cancel --------------------------------------------------------------

async def test_bind_cancel_clears_armed_slot_and_emits_learn_cancelled():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    result = await eng.actions.dispatch("bind.cancel", {})
    assert result == {"cancelled": True}
    assert eng._learn_armed is None
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_cancelled"]
    assert events
    assert events[-1]["data"]["reason"] == "cancelled"


async def test_bind_cancel_with_nothing_armed_is_a_noop_and_emits_nothing():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    result = await eng.actions.dispatch("bind.cancel", {})
    assert result == {"cancelled": False}
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_cancelled"]
    assert events == []


# -- tick-driven 30s timeout (injected now, no real sleep) -------------------

async def test_tick_learn_does_not_cancel_before_the_timeout():
    eng = Engine(Config())
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    armed_at = eng._learn_armed.armed_at
    eng._tick_learn(armed_at + LEARN_TIMEOUT_S - 0.01)
    assert eng._learn_armed is not None


async def test_tick_learn_cancels_at_the_timeout_boundary_and_emits_learn_cancelled():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    armed_at = eng._learn_armed.armed_at
    eng._tick_learn(armed_at + LEARN_TIMEOUT_S)
    assert eng._learn_armed is None
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_cancelled"]
    assert events
    assert events[-1]["data"]["reason"] == "timeout"


def test_tick_learn_is_a_noop_when_nothing_armed():
    eng = Engine(Config())
    eng._tick_learn(time.time() + 100000)   # must not raise
    assert eng._learn_armed is None


# -- capture: the full arm -> real MidiEvent -> Binding cycle ----------------
#
# Real `Engine.run()` loop, real `eng.queue.put(...)` -- same style as the
# zero-client-firing bindings-dispatch tests above.

async def test_learn_capture_note_on_builds_binding_persists_and_emits_learn_bound(tmp_path):
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    got = []
    eng.add_listener(got.append)
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100, channel=3,
                           source="Midi Through:Midi Through Port-0 14:0"))
    await asyncio.sleep(0.1)
    eng.stop()
    await task

    assert eng._learn_armed is None   # slot cleared
    assert len(eng._bindings_file.bindings) == 1
    b = eng._bindings_file.bindings[0]
    assert b.action == "page.next"
    assert b.mode == "trigger"
    assert b.match.type == "note_on"
    assert b.match.number == 60
    assert b.match.channel == 3
    # Exact source string, not a wildcard/fnmatch pattern (task brief).
    assert b.match.port_pattern == "Midi Through:Midi Through Port-0 14:0"

    from midicrt.engine.bindings import BindingsFile
    assert BindingsFile.load(str(p)).bindings == [b]   # persisted atomically

    bound_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert bound_events
    assert bound_events[-1]["data"]["binding"]["id"] == b.id
    assert bound_events[-1]["data"]["binding"]["action"] == "page.next"
    assert bound_events[-1]["data"]["binding"]["valid"] is True


async def test_learn_capture_control_change_builds_a_cc_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="control_change", data1=20, data2=64, channel=1))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    b = eng._bindings_file.bindings[0]
    assert b.match.type == "control_change"
    assert b.match.number == 20
    assert b.match.channel == 1


async def test_learn_capture_continuous_stores_none_fill_marker_and_persists_as_sentinel(tmp_path):
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="control_change", data1=22, data2=64))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    b = eng._bindings_file.bindings[0]
    assert b.mode == "continuous"
    assert b.args == {"level": None}
    assert b.range == (0.0, 1.0)

    from midicrt.engine.bindings import CONTINUOUS_FILL_TOKEN, BindingsFile
    assert CONTINUOUS_FILL_TOKEN in p.read_text()
    assert BindingsFile.load(str(p)).bindings == [b]


async def test_bind_learn_successive_captures_get_distinct_binding_ids(tmp_path):
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(tmp_path / "bindings.toml"))
    task = asyncio.create_task(eng.run())

    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    await asyncio.sleep(0.05)

    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    await eng.queue.put(ev(type="note_on", data1=61, data2=100))
    await asyncio.sleep(0.05)

    eng.stop()
    await task
    ids = [b.id for b in eng._bindings_file.bindings]
    assert len(ids) == 2
    assert len(set(ids)) == 2


# -- disqualified events never capture ---------------------------------------

async def test_learn_ignores_every_disqualified_event_type_and_leaves_the_arm_intact(tmp_path):
    """Every type `is_learnable_event` (engine/bindings.py) rejects must
    leave the arm exactly as it was: no binding created, still armed, no
    learn_bound/learn_cancelled event -- proven here against the real
    `Engine._handle` path, not just the pure predicate
    test_bindings.py already covers."""
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(tmp_path / "bindings.toml"))
    got = []
    eng.add_listener(got.append)
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())

    disqualified = [
        ev(type="note_on", data1=60, data2=0),
        ev(type="note_off", data1=60, data2=100),
        ev(type="clock_tick", channel=None, data1=24, data2=None, clock_batch_start=None),
        ev(type="program_change", data1=5, data2=None),
        ev(type="start", channel=None, data1=None, data2=None),
        ev(type="stop", channel=None, data1=None, data2=None),
        ev(type="continue", channel=None, data1=None, data2=None),
        ev(type="songpos", channel=None, data1=None, data2=None),
        ev(type="sysex", channel=None, data1=None, data2=None, sysex_data=(1, 2, 3)),
    ]
    for e in disqualified:
        await eng.queue.put(e)
    await asyncio.sleep(0.1)

    eng.stop()
    await task
    assert eng._learn_armed is not None   # still armed the whole time
    assert eng._bindings_file.bindings == []
    fired = [m for m in got if m.get("kind") == "event"
             and m.get("name") in {"learn_bound", "learn_cancelled"}]
    assert fired == []


# -- capture-consumption: withheld from bindings, not from the rest of the
# pipeline --------------------------------------------------------------------

async def test_learn_captured_event_is_not_also_dispatched_to_an_existing_matching_binding(
        tmp_path):
    """Design decision (docs/phase4-notes.md, task-3 amplification): the
    SAME MidiEvent that completes a learn capture must not also fire a
    pre-existing binding matching that identical note -- see
    `Engine._handle`'s own comment for why capture is checked BEFORE
    `self._binding_dispatcher.handle(ev)` runs for that event."""
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, binding_id="existing", action="page.next", number=60)
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    assert eng.current_page == "eventlog"
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.goto", "mode": "trigger", "args": {"name": "harmony"}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    await asyncio.sleep(0.1)
    eng.stop()
    await task

    # The existing "note 60 -> page.next" binding must NOT have fired even
    # though this event matches it exactly.
    assert eng.current_page == "eventlog"
    assert len(eng._bindings_file.bindings) == 2
    learned = next(b for b in eng._bindings_file.bindings if b.id != "existing")
    assert learned.action == "page.goto"


async def test_learn_captured_event_still_reaches_pages_and_analyzers_normally(tmp_path):
    """Consumption is scoped to the BINDING dispatcher only -- the
    captured event is a real, currently-arriving MIDI message and must
    still update every other engine consumer (eventlog etc) exactly as if
    no learn were in progress. Only a pre-existing binding's own action
    is withheld (see the test right above)."""
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(tmp_path / "bindings.toml"))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    before = eng.pages["eventlog"].view_model()["count"]
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    assert eng.pages["eventlog"].view_model()["count"] == before + 1


# -- replace-on-relearn (review Critical, live-reproduced) -------------------
#
# Without this, re-learning an already-bound physical control just ADDS a
# second binding alongside the first -- both then fire on every future
# trigger, silently accumulating forever. This breaks the primary DAW-learn
# use case (remapping a control you already bound something to) the task-3
# report itself names. Fix: at capture time, remove any existing binding(s)
# whose `match` is EXACTLY equal (dataclass `==` on type/number/channel/
# port_pattern) to the one just captured, in the SAME atomic save as the
# new binding's addition, and report the replaced binding(s) in the
# `learn_bound` event's new `"replaced"` field.

async def test_learn_capture_relearning_the_same_exact_match_replaces_the_old_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    got = []
    eng.add_listener(got.append)
    task = asyncio.create_task(eng.run())
    note60 = ev(type="note_on", data1=60, data2=100, channel=0, source="Midi Through:0")

    # First learn: note 60 -> page.next. The capturing event itself is
    # CONSUMED (never dispatched, see the capture-consumption tests above)
    # -- a SECOND, fresh injection is what actually proves it fires.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    await eng.queue.put(note60)
    await asyncio.sleep(0.05)
    assert eng.current_page == "eventlog"   # capture consumed, no dispatch yet
    await eng.queue.put(note60)
    await asyncio.sleep(0.05)
    assert eng.current_page == "voices"     # page.next really fired

    # Relearn the SAME exact match to page.prev.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    await eng.queue.put(note60)   # captures + consumes + REPLACES the old binding
    await asyncio.sleep(0.05)
    assert eng.current_page == "voices"     # still consumed, unchanged by this event

    assert len(eng._bindings_file.bindings) == 1
    only = eng._bindings_file.bindings[0]
    assert only.action == "page.prev"

    from midicrt.engine.bindings import BindingsFile
    assert BindingsFile.load(str(p)).bindings == [only]   # atomic: one binding on disk, not two

    bound_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert len(bound_events) == 2
    assert bound_events[0]["data"]["replaced"] == []             # nothing to replace the first time
    assert len(bound_events[1]["data"]["replaced"]) == 1
    assert bound_events[1]["data"]["replaced"][0]["action"] == "page.next"

    # The real proof: a FRESH injection now fires ONLY page.prev, not both
    # (if the old page.next binding had leaked, this event would fire
    # page.next THEN page.prev in sequence and net back to "voices",
    # unchanged -- a single page.prev fire from "voices" lands on
    # "eventlog", which is what distinguishes "fixed" from "still buggy").
    await eng.queue.put(note60)
    await asyncio.sleep(0.05)
    eng.stop()
    await task
    assert eng.current_page == "eventlog"


async def test_learn_capture_a_different_match_does_not_replace_unrelated_bindings(tmp_path):
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(tmp_path / "bindings.toml"))
    task = asyncio.create_task(eng.run())

    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    await eng.queue.put(ev(type="note_on", data1=60, data2=100, channel=0, source="A"))
    await asyncio.sleep(0.05)

    # Different NUMBER -- must coexist, not replace.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    await eng.queue.put(ev(type="note_on", data1=61, data2=100, channel=0, source="A"))
    await asyncio.sleep(0.05)

    # Different CHANNEL, same number -- must ALSO coexist.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.goto", "mode": "trigger", "args": {"name": "harmony"}})
    await eng.queue.put(ev(type="note_on", data1=60, data2=100, channel=5, source="A"))
    await asyncio.sleep(0.05)

    eng.stop()
    await task
    assert len(eng._bindings_file.bindings) == 3


async def test_learn_capture_does_not_remove_an_overlapping_wildcard_port_binding(tmp_path):
    """Documented contract (review): replace-on-relearn is EXACT-match
    only (`BindingMatch` dataclass `==`, no `fnmatch`) -- a pre-existing
    WILDCARD-port binding that would ALSO match the newly captured
    physical event (via `BindingDispatcher._matches`'s own fnmatch
    semantics) is left alone here, even though the two will now both
    genuinely fire on that event going forward. Simple, honest, disclosed
    limitation: only a binding whose match is byte-for-byte identical to
    the freshly captured one is ever replaced."""
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, binding_id="wild", action="page.next",
                          number=60, port_pattern="Midi Through*")
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    got = []
    eng.add_listener(got.append)
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100, channel=0,
                           source="Midi Through:Midi Through Port-0 14:0"))
    await asyncio.sleep(0.1)
    eng.stop()
    await task

    ids = {b.id for b in eng._bindings_file.bindings}
    assert "wild" in ids
    assert len(eng._bindings_file.bindings) == 2
    bound_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert bound_events[-1]["data"]["replaced"] == []


# -- event-sourced capture (Phase 5 Task 1, docs/phase5-notes.md) -----------
#
# `engine/capture.py::CaptureSink`'s own contract (writer queueing, header
# versioning, retention, malformed-index recovery) is tested directly, in
# isolation, in test_capture.py -- everything below is registry/engine-aware
# WIRING: the four dispatch-site provenance origins, the `rec` chrome flag,
# `capture.*` actions, `capture_started`/`capture_stopped` events, and
# `config.capture_auto_start`. "client" origin is exercised again, over a
# real wire connection, in test_server.py.

def _read_session_lines(eng, session_id):
    with open(eng._capture.session_path(session_id), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _read_capture_index(eng):
    with open(f"{eng._capture.dir}/index.json", encoding="utf-8") as f:
        return json.load(f)


def test_capture_auto_start_defaults_off_matching_v1s_deployed_behavior():
    # v1's OWN deployed `~/codex/midicrt/config/settings.json` has no
    # arm-at-boot flag at all for its memory-capture system -- see
    # config.py's own comment on `capture_auto_start`.
    eng = Engine(Config())
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False


def test_capture_auto_start_true_arms_recording_at_construction():
    eng = Engine(Config(capture_auto_start=True))
    assert eng._capture.is_recording is True
    assert eng.analyzers["status"].view_model()["rec"] is True


def test_capture_start_action_sets_rec_flag_marks_dirty_and_emits_event():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    result = eng._capture_start_action()
    assert result["recording"] is True
    assert eng.analyzers["status"].view_model()["rec"] is True
    assert "overlay.status" in eng._dirty
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "capture_started"]
    assert events and events[-1]["data"]["id"] == result["session_id"]


def test_capture_stop_action_clears_rec_flag_and_emits_event_with_counts():
    eng = Engine(Config())
    eng._capture_start_action()
    eng._handle(ev(type="note_on"))
    got = []
    eng.add_listener(got.append)
    result = eng._capture_stop_action()
    assert eng.analyzers["status"].view_model()["rec"] is False
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "capture_stopped"]
    assert events
    assert events[-1]["data"]["id"] == result["session_id"]
    assert events[-1]["data"]["counts"] == result["counts"]


def test_capture_stop_action_when_nothing_recording_is_a_noop_no_event():
    eng = Engine(Config())
    got = []
    eng.add_listener(got.append)
    result = eng._capture_stop_action()
    assert result["session_id"] is None
    events = [m for m in got if m.get("kind") == "event" and m.get("name") == "capture_stopped"]
    assert events == []


# -- mark completeness (fix wave: Important findings) ------------------------

async def test_capture_stop_records_its_own_action_mark_with_the_calling_origin():
    # Bug: `_capture_stop_action` flipped `_recording` False (inside
    # `CaptureSink.stop()`) BEFORE the normal post-dispatch hook fires --
    # `record_action`'s own guard then silently swallowed the mark. Fixed
    # by recording it explicitly, before stopping, with whatever origin
    # actually triggered this dispatch (here: "client", via
    # `ActionRegistry.dispatch`'s register-time origin injection).
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})
    result = await eng.actions.dispatch("capture.stop", {}, origin="client")
    lines = _read_session_lines(eng, result["session_id"])
    stop_marks = [line for line in lines if line["kind"] == "action" and line["name"] == "capture.stop"]
    assert len(stop_marks) == 1
    assert stop_marks[0]["origin"] == "client"
    # It's the LAST line -- nothing else was recorded after the session's
    # own stop mark.
    assert lines[-1] is stop_marks[0]


async def test_engine_stop_records_a_shutdown_origin_stop_mark():
    eng = Engine(Config())
    result = eng._capture_start_action()
    eng.stop()
    lines = _read_session_lines(eng, result["session_id"])
    stop_marks = [line for line in lines if line["kind"] == "action" and line["name"] == "capture.stop"]
    assert stop_marks and stop_marks[0]["origin"] == "shutdown"


def test_capture_auto_start_records_its_own_start_mark_with_origin_auto():
    # Bug: __init__'s auto-start branch calls `_capture_start_action()`
    # directly (never through `ActionRegistry.dispatch`), so the normal
    # post-dispatch hook that stamps every OTHER `capture.start` origin
    # never fires for it. Fixed by recording the mark explicitly with
    # `origin="auto"` right after starting.
    eng = Engine(Config(capture_auto_start=True))
    result = eng._capture_stop_action()
    lines = _read_session_lines(eng, result["session_id"])
    start_marks = [line for line in lines if line["kind"] == "action" and line["name"] == "capture.start"]
    assert len(start_marks) == 1
    assert start_marks[0]["origin"] == "auto"
    assert lines[0]["kind"] == "header"
    assert lines[1] is start_marks[0]   # the very first thing after the header


# -- cold-path OSError containment (fix wave: Important finding) -------------

async def test_capture_start_action_raises_action_error_on_oserror(monkeypatch):
    eng = Engine(Config())

    def broken_start():
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(eng._capture, "start", broken_start)
    with pytest.raises(ActionError, match="Permission denied"):
        await eng.actions.dispatch("capture.start", {})
    assert eng._capture.is_recording is False


async def test_capture_stop_action_raises_action_error_on_oserror_and_disables_capture(
        monkeypatch):
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})
    assert eng._capture.is_recording is True

    def broken_stop():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(eng._capture, "stop", broken_stop)
    got = []
    eng.add_listener(got.append)

    with pytest.raises(ActionError, match="No space left"):
        await eng.actions.dispatch("capture.stop", {})

    # Cleanly disabled, not left half-recording.
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False
    alerts = [m for m in got if m.get("kind") == "event" and m.get("name") == "alert"]
    assert alerts and alerts[-1]["data"]["source"] == "capture"


async def test_capture_status_action_reports_live_recording_state():
    eng = Engine(Config())
    assert (await eng.actions.dispatch("capture.status", {}))["recording"] is False
    await eng.actions.dispatch("capture.start", {})
    assert (await eng.actions.dispatch("capture.status", {}))["recording"] is True


async def test_capture_pin_unknown_id_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="unknown capture session"):
        await eng.actions.dispatch("capture.pin", {"id": "nope"})


async def test_capture_pin_a_stopped_session_over_the_action_registry():
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})
    stop_result = await eng.actions.dispatch("capture.stop", {})
    result = await eng.actions.dispatch("capture.pin", {"id": stop_result["session_id"]})
    assert result == {"pinned": True, "id": stop_result["session_id"]}


def test_capture_header_carries_engine_version_and_configured_instruments():
    import midicrt
    eng = Engine(Config(instruments=["Foo", "Bar"]))
    result = eng._capture_start_action()
    header = _read_session_lines(eng, result["session_id"])[0]
    assert header["kind"] == "header"
    assert header["instruments"] == ["Foo", "Bar"]
    assert header["engine_version"] == midicrt.__version__


async def test_capture_records_page_changed_mark_on_page_transition():
    eng = Engine(Config())
    eng._capture_start_action()
    await eng.actions.dispatch("page.next", {}, origin="client")
    landed_on = eng.current_page
    result = eng._capture_stop_action()
    lines = _read_session_lines(eng, result["session_id"])
    marks = [line for line in lines if line["kind"] == "page_changed"]
    assert marks and marks[-1]["page"] == landed_on


async def test_engine_stop_finalizes_an_active_capture_session():
    eng = Engine(Config())
    result = eng._capture_start_action()
    eng._handle(ev(type="note_on"))
    eng.stop()
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False
    rows = _read_capture_index(eng)
    assert any(r["id"] == result["session_id"] for r in rows)


async def test_capture_records_no_action_mark_for_a_binding_dispatch_that_raises_actionerror(
        tmp_path):
    # Marks are stamped ONLY on a SUCCESSFUL dispatch (docs/phase5-notes.md:
    # "capture stamps marks at DISPATCH time") -- a binding referencing an
    # action absent from this roster is the SAME graceful-skip case
    # test_dispatch_bindings_skips_roster_absent_action_and_logs_without_crashing
    # already covers; it must not leave an action mark behind either. The
    # session's OWN `capture.stop` mark (fix wave: `_capture_stop_action`
    # now records itself explicitly) is expected here too -- that's a
    # SEPARATE, deliberate mark, not the skipped binding's.
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="sendnotes.key", args_toml='key = "z"')
    eng = Engine(Config(pages=["eventlog"]), bindings_path=str(p))
    eng._capture_start_action()
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()
    result = eng._capture_stop_action()
    lines = _read_session_lines(eng, result["session_id"])
    action_names = [line["name"] for line in lines if line["kind"] == "action"]
    assert "sendnotes.key" not in action_names
    assert action_names == ["capture.stop"]


async def test_capture_records_provenance_for_all_four_dispatch_origins(tmp_path):
    """The headline proof (task brief): fire a binding, an idle behavior, a
    client action, and a sysex command while capturing, then confirm all
    FOUR origins land as distinct `"kind": "action"` marks in the same
    session file -- `binding:<id>`/`behavior`/`client`/`sysex`."""
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, binding_id="b1", action="page.next", number=60)
    eng = Engine(Config(pagecycle_enabled=True, screensaver_enabled=False),
                bindings_path=str(p))
    eng._capture_start_action()

    # 1. binding origin
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()

    # 2. behavior origin -- force a huge idle gap so PageCycleBehavior fires.
    # Two calls needed (same shape as test_tick_behaviors_pagecycle_
    # dispatch_is_observable_via_a_spy above): the FIRST tick only ARMS
    # `_idle_since` (a freshly constructed behavior always treats its very
    # first tick as "activity moved forward", see behaviors/pagecycle.py's
    # own `tick()` docstring); the SECOND tick, far enough past `idle_s`,
    # actually fires `page.next`. No real wall-clock wait needed -- `now`
    # is injected, same "inject now" precedent every other behavior/
    # analyzer tick test uses.
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(now=0.0)
    await eng._tick_behaviors(now=eng.config.pagecycle_idle_s + 100.0)

    # 3. client origin -- exactly what engine/server.py's own action
    # dispatch branch does.
    await eng.actions.dispatch("page.prev", {}, origin="client")

    # 4. sysex origin -- CMD_SWITCH_PAGE to page id 13 ("voices"), version
    # None so no real MIDI reply send is attempted.
    eng._sysex_switch_page(version=None, args=(13,))

    result = eng._capture_stop_action()
    lines = _read_session_lines(eng, result["session_id"])
    origins = {line["origin"] for line in lines if line["kind"] == "action"}
    assert any(o == "binding:b1" for o in origins)
    assert "behavior" in origins
    assert "client" in origins
    assert "sysex" in origins


async def test_run_loop_flushes_capture_to_disk_on_its_own_cadence():
    # Integration proof that `Engine.run()` actually calls `CaptureSink.
    # maybe_flush` each tick (unit-level cadence/gating logic itself is
    # test_capture.py's job) -- speeds the cadence up via the private
    # attribute so this doesn't need a real ~1s wall-clock wait.
    eng = Engine(Config(tick_hz=200.0))
    eng._capture_start_action()
    eng._capture._flush_interval_s = 0.01
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    await asyncio.sleep(0.2)
    session_id = eng._capture.status()["session_id"]
    lines = _read_session_lines(eng, session_id)
    eng.stop()
    await task
    assert any(line["kind"] == "event" and line["type"] == "note_on" for line in lines)


# -- capture write-failure containment (fix wave: Critical finding) --------
#
# `CaptureSink.flush()` can raise `OSError` (ENOSPC/EIO) from `os.fsync` --
# BEFORE this fix, `Engine.run()` called `self._capture.maybe_flush(now)`
# completely unguarded, outside any try/except, so that exception escaped
# the WHOLE `while self._running:` loop: the `run()` asyncio task died
# silently (no log line at all), the daemon stayed "active" to systemd (no
# crash, no restart), and MIDI processing stopped forever. This is the
# reviewer-reproduced headline regression test for that fix -- mirrors
# `_dispatch_bindings`'s own "never let a single failure escape and kill
# the loop" precedent.

async def test_flush_write_failure_does_not_kill_the_run_loop_and_disables_capture(
        tmp_path, caplog):
    eng = Engine(Config(tick_hz=200.0))
    eng._capture_start_action()
    eng._capture._flush_interval_s = 0.01

    def failing_flush():
        raise OSError(errno.ENOSPC, "No space left on device")

    real_flush = eng._capture.flush
    eng._capture.flush = failing_flush

    got = []
    eng.add_listener(got.append)

    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    with caplog.at_level("ERROR"):
        await asyncio.sleep(0.15)   # long enough for maybe_flush to fire and raise

    # 1. The run() loop must still be ALIVE -- a subsequent event is still
    # processed (events_total advances), proving the task never died.
    events_before = eng.events_total
    await eng.queue.put(ev(type="note_on", data1=61, data2=100))
    await asyncio.sleep(0.05)
    assert eng.events_total == events_before + 1
    assert not task.done()   # the run() task itself is still running

    # 2. Capture was stopped cleanly (not left half-alive/half-broken).
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False

    # 3. Exactly ONE alert event, describing the write failure.
    alerts = [m for m in got if m.get("kind") == "event" and m.get("name") == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["data"]["source"] == "capture"
    assert "write failed" in alerts[0]["data"]["message"]

    # 4. Exactly ONE error logged (not spammed every tick).
    error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_logs) == 1
    assert "capture" in error_logs[0].message.lower()

    # 5. A fresh capture.start works once writes succeed again.
    eng._capture.flush = real_flush
    result = await eng.actions.dispatch("capture.start", {})
    assert result["recording"] is True

    eng.stop()
    await task


# -- Phase 5 Task 2 (session replay, docs/phase5-notes.md): the `_handle`
# suppression seam -- `Engine(..., replay=True)`. Full replay-driver
# behavior (the offline engine builder, the JSONL streaming loop, mark
# application, the end-of-replay summary) is covered in test_replay.py;
# this section is ONLY the engine-owned gate itself (`_handle`'s
# learn/dispatch `if self._replay:` branch), tested the same
# feed-`_handle`-directly way every other `_handle` behavior in this file
# already is -- no session file, no `engine/replay.py` import needed.

def test_engine_replay_flag_defaults_false():
    eng = Engine(Config())
    assert eng._replay is False


def test_engine_replay_flag_is_settable_at_construction():
    eng = Engine(Config(), replay=True)
    assert eng._replay is True


def test_replay_engine_never_collects_a_binding_dispatch_intent(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    live = Engine(Config(), bindings_path=str(p))
    replaying = Engine(Config(), bindings_path=str(p), replay=True)
    matching_event = ev(type="note_on", data1=60, data2=100)

    live._handle(matching_event)
    replaying._handle(matching_event)

    # Sanity: the binding really does match this event for a live engine
    # (otherwise this test would trivially pass for the wrong reason).
    assert live._pending_binding_dispatches != []
    # The replaying engine collects NOTHING at all -- the whole
    # learn/dispatch branch is a no-op under `self._replay`.
    assert replaying._pending_binding_dispatches == []


def test_replay_engine_leaves_current_page_unmoved_by_a_matching_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    eng = Engine(Config(), bindings_path=str(p), replay=True)
    eng._handle(ev(type="note_on", data1=60, data2=100))
    # Even collecting nothing (proven above) is one thing -- this confirms
    # there is also no OTHER path (e.g. a stray direct dispatch) that moved
    # the page as a side effect of this same event.
    assert eng.current_page == next(iter(eng.pages))


def test_replay_engine_armed_learn_slot_never_consumes_a_replayed_event(tmp_path):
    # A learn slot has no legitimate way to be armed on a real replay
    # engine (nothing ever calls `bind.learn` offline) -- this proves the
    # SUPPRESSION itself, by force-arming one directly and confirming
    # `_handle` never reaches `_capture_learn` for it, matching the
    # `_replay` branch's own "an armed slot must never consume a REPLAYED
    # event" reasoning (docs/phase5-notes.md decided design, point 2).
    eng = Engine(Config(), replay=True)
    arm = _LearnArm(action="page.next", mode="trigger", args_template={},
                    range=(0.0, 1.0), armed_at=0.0)
    eng._learn_armed = arm
    eng._handle(ev(type="note_on", data1=60, data2=100))
    assert eng._learn_armed is arm   # untouched -- _capture_learn never ran
    assert eng._bindings_file.bindings == []   # nothing got persisted either


def test_non_replay_engine_still_arms_learn_normally_control():
    # Control for the test above: the SAME force-armed slot, on a
    # non-replaying engine, DOES get consumed by a qualifying event --
    # proves the fixture is meaningful (the arm is real, capturable state),
    # not just inert regardless of the replay flag.
    eng = Engine(Config())
    eng._learn_armed = _LearnArm(action="page.next", mode="trigger", args_template={},
                                 range=(0.0, 1.0), armed_at=0.0)
    eng._handle(ev(type="note_on", data1=60, data2=100))
    assert eng._learn_armed is None
    assert len(eng._bindings_file.bindings) == 1


def test_replay_engine_still_processes_analyzers_and_pages_normally():
    # The suppression is scoped EXACTLY to the learn/dispatch branch --
    # analyzers/pages must see a replayed event exactly like a live one
    # (docs/phase5-notes.md: "MIDI events -> _handle (analyzers/pages
    # consume normally)").
    eng = Engine(Config(), replay=True)
    eng._handle(ev(type="note_on", data1=60, data2=100))
    assert eng.pages["voices"].view_model()["total"] == 1
    assert eng.events_total == 1
