import asyncio
import errno
import fnmatch
import json
import logging
import time

import pytest

from midicrt.config import Config, ConfigError
from midicrt.engine import capture as capture_mod
from midicrt.engine import keymap as keymap_mod
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


class _FakeCapturePage:
    """Page double for `_tick_audio_gate` tests (fix round, review finding
    1) -- exposes `start_capture`/`stop_capture` (the two NEW optional
    `PageHooks` fields `_discover_page_hooks` looks for via `hasattr`,
    same discovery mechanism `tick`/`drain_outputs`/`bind_info` already
    use) with call counts/order instrumented instead of touching any real
    `AudioCapture`/hardware -- mirrors `tests/test_pages_spectrum.py`'s
    own `test_start_capture_and_stop_capture_delegate_to_the_capture_object`
    convention of swapping in instrumented callables, one level up (a
    whole fake PAGE here, not a swapped method on a real one, since these
    tests exercise the ENGINE's gating decision, not any one page's own
    delegation)."""

    def __init__(self):
        self.calls: list[str] = []

    def handle(self, ev) -> bool:
        return False

    def view_model(self) -> dict:
        return {}

    def start_capture(self) -> None:
        self.calls.append("start")

    def stop_capture(self) -> None:
        self.calls.append("stop")


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
    # "screensaver"; task 10 appends "img2txtviz" (same "no unbuilt
    # dependency" precedent, NOT because v1's own cycle_pages happened to
    # include it -- see pages/img2txtviz.py's own module docstring) and
    # "config" (the task-10 brief's explicit ask, a zero-dependency
    # read-only viewer) -- all live by default (config.py's Config.pages
    # default) so they're reachable with no config.toml on a stock deploy.
    # Phase 9 Task 3 appends "tuner" last (live pitch detection finally
    # wired -- see config.py's own comment for the full history of why it
    # was excluded before). "eventlog" stays first/current -- order is
    # preserved from config.pages.
    eng = Engine(Config())
    assert list(eng.pages) == [
        "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
        "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
        "chordkey", "sendnotes", "tuner",
    ]
    assert eng.current_page == "eventlog"


def test_register_page_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()
    eng.register_page("second", fake)
    assert list(eng.pages) == [
        "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
        "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
        "chordkey", "sendnotes", "tuner", "second",
    ]
    assert eng.pages["second"] is fake


def test_engine_topics_reflects_roster_order():
    eng = Engine(Config())
    eng.register_page("second", _FakePage())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges",
        "page.ccmonitor", "page.ccdashboard", "page.chordkey", "page.sendnotes", "page.tuner",
        "page.second",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress", "overlay.marquee",
        # Phase 9 Task 2: "polylimit" is registered post-hoc, right after
        # "voices" is built -- see Engine.__init__'s own comment.
        "overlay.polylimit",
        # Phase 9 Task 5: "sysex" (_SysexStatusOverlay) is registered even
        # later, right after `self._midi_out` is constructed -- lands last.
        "overlay.sysex",
    ]


def test_handle_marks_dirty_only_for_pages_reporting_true():
    # `ev()` is a real note_on on channel 1 -- the real "voices"/"harmony"/
    # "pianoroll"/"img2txtviz" pages (all live by default) genuinely react
    # to it too, so they're dirty here alongside eventlog, same as any
    # other real page would be. Phase-3 task 6: "alerts" (StuckNotesAnalyzer)
    # is a real analyzer too, and a fresh note-on genuinely starts tracking
    # it -- dirty for the same reason. Phase 9 Task 2: "polylimit" wraps the
    # SAME VoiceMonitorAnalyzer "voices" owns, so it reacts to a real
    # note-on too (the poly-limit CHECK might not exceed anything, but the
    # underlying `_note_on` call still returns dirty=True either way).
    # Phase-3 task 12: "chordkey" wraps its own HarmonyAnalyzer instance
    # (same class as "harmony"'s), so it reacts to a real note-on too.
    # "timesig" does NOT react (TimesigAnalyzer gates note_on on transport
    # being "running", and no "start" was ever sent here). "config" never
    # reacts to MIDI events at all (a read-only viewer, pages/configview.py's
    # own `handle()`).
    eng = Engine(Config())
    quiet = _FakePage(dirty=False)
    eng.register_page("quiet", quiet)
    eng._handle(ev())
    assert eng._dirty == {
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll",
        "page.img2txtviz", "page.chordkey", "overlay.alerts", "overlay.polylimit",
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
        "page.img2txtviz", "page.chordkey", "page.loud", "overlay.alerts", "overlay.polylimit",
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
    # config-gated, see engine/core.py's module docstring. Phase 8 Task 4
    # adds "marquee" (MarqueeAnalyzer) the same way once more -- the header
    # page-title scrolling marquee, v1's own primary anti-burn-in device.
    eng = Engine(Config())
    assert list(eng.analyzers) == [
        # Phase 9 Task 2: "polylimit" -- registered post-hoc (Engine.
        # __init__, right after "voices" is built), not via
        # _ANALYZER_FACTORIES like the other five.
        # Phase 9 Task 5: "sysex" (_SysexStatusOverlay) lands LAST --
        # registered even later, right after `self._midi_out` is built.
        "status", "alerts", "timesig", "beatflash", "loopprogress", "marquee", "polylimit",
        "sysex",
    ]


def test_register_analyzer_appends_to_live_roster():
    eng = Engine(Config())
    fake = _FakePage()  # same handle()/view_model() shape as an Analyzer
    eng.register_analyzer("second", fake)
    assert list(eng.analyzers) == [
        "status", "alerts", "timesig", "beatflash", "loopprogress", "marquee", "polylimit",
        "sysex", "second",
    ]
    assert eng.analyzers["second"] is fake


def test_topics_include_overlay_after_page_topics():
    eng = Engine(Config())
    assert eng.topics == [
        "page.eventlog", "page.voices", "page.harmony", "page.pianoroll", "page.spectrum",
        "page.screensaver", "page.img2txtviz", "page.config", "page.help", "page.progchanges",
        "page.ccmonitor", "page.ccdashboard", "page.chordkey", "page.sendnotes", "page.tuner",
        "overlay.status", "overlay.alerts", "overlay.timesig",
        "overlay.beatflash", "overlay.loopprogress", "overlay.marquee", "overlay.polylimit",
        "overlay.sysex",
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
    # Phase 9 Task 3: "tuner" now sits between "sendnotes" and the
    # dynamically-registered "second" -- it's the newest default-roster
    # append (config.py).
    assert eng.current_page == "tuner"
    await eng.actions.dispatch("page.next", {})
    assert eng.current_page == "second"
    await eng.actions.dispatch("page.prev", {})
    assert eng.current_page == "tuner"

    # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
    # revamp): `_set_current_page` now ALSO emits `keymap_changed` on every
    # transition (recomputed for the new page's own `[keys.<page>]`
    # section) -- reusing the SAME event both clients already refetch the
    # full keymap on (Phase 4's `config.reload` path) means neither
    # client needed a single line of new event-handling code to pick up
    # per-page keymap sections on page nav. See engine/core.py::
    # Engine._set_current_page's own docstring.
    names = [e["name"] for e in events]
    assert names == ["page_changed", "keymap_changed"] * 16
    page_events = [e for e in events if e["name"] == "page_changed"]
    assert [e["data"]["page"] for e in page_events] == [
        "voices", "harmony", "pianoroll", "spectrum", "screensaver", "img2txtviz",
        "config", "help", "progchanges", "ccmonitor", "ccdashboard", "chordkey",
        "sendnotes", "tuner", "second", "tuner",
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


# -- panic-send (Phase 9 Task 2, config.panic_on_crit) -----------------------
#
# v1 evidence (`~/codex/midicrt/plugins/zstucknotes.py`): `PANIC_ON_CRIT`
# gates a channel-scoped "All Notes Off" CC (control=123) sent through a
# dedicated MIDI output the moment a note's level transitions to "crit"
# (zstucknotes.py:209-216, inside the SAME `if level != prev:` transition
# block `analyzers/stucknotes.py::tick()`'s own `_pending_alerts.append`
# mirrors) -- ported here as an engine-level `_tick_analyzers` side effect
# on each DRAINED "crit" alert, gated on `config.panic_on_crit` (v2 default
# False, see config.py). `PANIC_COOLDOWN=3.0` (zstucknotes.py:26) is
# ported as a per-channel cooldown to bound the same "sustain-toggle
# storm" vector analyzers/stucknotes.py's own docstring discloses
# (repeated none->crit transitions on one channel must not spam real MIDI
# output).

def test_panic_on_crit_sends_channel_scoped_all_notes_off_on_a_crit_alert():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert fake.control_change_calls == [(123, 0, 5)]


def test_panic_on_crit_ignores_warn_level_alerts():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "warn", "held_s": 2.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert fake.control_change_calls == []


def test_panic_on_crit_defaults_off_sends_nothing():
    eng = Engine(Config())   # panic_on_crit default False
    fake = _FakeMidiOut()
    eng._midi_out = fake
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert fake.control_change_calls == []


def test_panic_on_crit_still_emits_the_ordinary_alert_event_too():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert [e["name"] for e in events if e["name"] == "alert"] == ["alert"]


def test_panic_on_crit_is_cooled_down_per_channel():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert len(fake.control_change_calls) == 1
    analyzer._alerts = [{"ch": 5, "note": 60, "level": "crit", "held_s": 11.5}]
    eng._tick_analyzers(1.5)   # 0.5s later -- well under the 3.0s cooldown
    assert len(fake.control_change_calls) == 1   # still just the one send
    analyzer._alerts = [{"ch": 5, "note": 60, "level": "crit", "held_s": 15.0}]
    eng._tick_analyzers(4.5)   # 3.5s later -- past cooldown
    assert len(fake.control_change_calls) == 2


def test_panic_on_crit_cooldown_is_independent_per_channel():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 1, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    analyzer._alerts = [{"ch": 2, "note": 64, "level": "crit", "held_s": 11.0}]
    eng._tick_analyzers(1.1)   # a DIFFERENT channel, well under ch1's cooldown
    assert fake.control_change_calls == [(123, 0, 1), (123, 0, 2)]


def test_panic_on_crit_records_a_provenance_marked_action():
    eng = Engine(Config(panic_on_crit=True, capture_dir=None))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._capture_start_action()
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    marks = [line for line in eng._capture._buffer
             if line.get("kind") == "action" and line.get("name") == "panic.notes_off"]
    assert len(marks) == 1
    assert marks[0]["origin"] == "alert"
    assert marks[0]["args"] == {"ch": 5}


# -- panic RELEASE (review fix, Critical): v1 parity requires clearing
# engine-tracked state, not just sending the external CC123 -----------------
#
# v1 evidence: `~/codex/midicrt/plugins/zstucknotes.py:209-225` -- v1's OWN
# comment explains why: "the engine ignores CC123" and a lossy MIDI
# loopback under overload can strand phantom held notes even after the
# external device silences. v1 additionally calls `eng.request_release
# (ch - 1)`, which synthesizes a real note_off for EVERY note the engine's
# ledger believes is held on that channel and re-ingests each one through
# the SAME path real MIDI takes (`~/codex/midicrt/engine/core.py:410-433`),
# so every module -- not just zstucknotes -- clears its own tracking.
# `Engine._maybe_panic` mirrors this: after the CC123 send, it synthesizes
# note_off `MidiEvent`s for `self.analyzers["alerts"].held_notes(ch)` and
# routes each through the SAME analyzer/page fan-out `_handle()` uses
# (`_dispatch_to_state`), NOT through `_handle()` itself -- these are
# honestly-provenanced synthetic releases (a `panic.release` action mark,
# origin="alert"), not fake wire events (no `record_event`/`events_total`
# bump, matching docs/phase5-capture.md's "action marks record what
# fired... not a replacement for the raw trace" contract).

def test_panic_release_clears_the_real_alerts_analyzers_held_note():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))   # ch5 (1-based)
    assert eng.analyzers["alerts"].held_notes(5) == [60]
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert fake.control_change_calls == [(123, 0, 5)]
    assert eng.analyzers["alerts"].held_notes(5) == []


def test_panic_release_clears_every_held_note_on_the_channel_not_just_the_crit_one():
    # v1's channel-wide release scope (§ module docstring) -- a SECOND,
    # not-yet-alerting note on the same channel must ALSO clear, matching
    # what a real device receiving CC123 on that channel would do.
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    eng._handle(ev(type="note_on", channel=4, data1=64, data2=100, ts=0.9))   # fresh, not alerting
    assert eng.analyzers["alerts"].held_notes(5) == [60, 64]
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert eng.analyzers["alerts"].held_notes(5) == []


def test_panic_release_also_clears_the_voices_pages_own_active_count():
    # The release must reach EVERY analyzer/page that tracks held notes,
    # not just the one that triggered the alert -- proven here against the
    # REAL "voices" page (a genuinely different analyzer instance,
    # VoiceMonitorAnalyzer, not StuckNotesAnalyzer).
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    assert eng.pages["voices"].view_model()["rows"][4]["active"] == 1
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert eng.pages["voices"].view_model()["rows"][4]["active"] == 0


def test_panic_release_is_a_noop_when_nothing_is_held_on_that_channel():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    before_total = eng.events_total
    eng._maybe_panic({"ch": 3, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert fake.control_change_calls == [(123, 0, 3)]   # CC still sent
    marks = [line for line in eng._capture._buffer if line.get("name") == "panic.release"]
    assert marks == []   # nothing to release -- no mark, matches v1's own no-op
    assert eng.events_total == before_total


def test_panic_release_records_a_provenance_marked_action():
    eng = Engine(Config(panic_on_crit=True, capture_dir=None))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._capture_start_action()
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    marks = [line for line in eng._capture._buffer
             if line.get("kind") == "action" and line.get("name") == "panic.release"]
    assert len(marks) == 1
    assert marks[0]["origin"] == "alert"
    assert marks[0]["args"] == {"ch": 5, "notes": [60]}


def test_panic_release_does_not_record_a_raw_wire_event_for_the_synthetic_release():
    # "not fake wire events" (review's own wording): the synthetic release
    # must NOT show up as a kind="event" capture line (which would make it
    # indistinguishable from real incoming MIDI) -- only the kind="action"
    # mark above represents it.
    eng = Engine(Config(panic_on_crit=True, capture_dir=None))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._capture_start_action()
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    events_before = [line for line in eng._capture._buffer if line.get("kind") == "event"]
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    events_after = [line for line in eng._capture._buffer if line.get("kind") == "event"]
    assert events_after == events_before   # no new raw event line


# -- panic-release REPLAY parity (Phase 9 close-out fix wave, reviewer-
# verified regression) -------------------------------------------------------
#
# `_release_for_panic` routes synthetic releases through `_dispatch_to_
# state` only -- no `kind="event"` line, just the `panic.release` mark
# (proven above). `engine/replay.py::stream_session` used to COUNT every
# action mark (the generic, deliberate "never re-executed" safety rule) but
# never APPLY `panic.release` specifically -- meaning a captured panic cycle
# replayed with the panicked note STILL showing held, even though the LIVE
# session had already released it. Fixed: replay now applies `panic.release`
# as a direct state mutation, the SAME "state change with zero MIDI trace"
# category `page_changed` marks already are (see replay.py's own "Mark
# application semantics" docstring section).

def test_replay_applies_a_captured_panic_release_so_voices_total_reaches_zero(tmp_path):
    """The reviewer's own reproduction, automated: capture a real note_on
    then a real panic release (via `_maybe_panic`, so the mark's `args`
    shape is genuine, not hand-rolled), stop, and replay the file -- the
    panicked note must NOT still show held."""
    eng = Engine(Config(panic_on_crit=True, capture_dir=str(tmp_path)))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._capture_start_action()
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=1.0))   # ch5 (1-based)
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 2.0)
    # Confirm the LIVE state really did clear (the baseline this replay must reproduce).
    assert eng.pages["voices"].view_model()["rows"][4]["active"] == 0
    result = eng._capture_stop_action()
    path = eng._capture.session_path(result["session_id"])

    from midicrt.engine.replay import replay_session
    summary = replay_session(path, instant=True)
    assert summary["marks_by_kind"].get("action", 0) >= 1
    assert summary["final_state"]["voices"]["total"] == 0


def test_replay_without_the_panic_release_fix_would_show_the_note_still_held(tmp_path):
    """Negative control, pinning the BUG this fix closes: replaying ONLY
    the note_on (no panic.release mark at all, e.g. a session captured
    before a panic ever fired) correctly shows the note STILL held --
    proving the zero-total result above comes from actually APPLYING the
    release mark, not from some unrelated reason total always reads 0."""
    eng = Engine(Config(capture_dir=str(tmp_path)))
    eng._capture_start_action()
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=1.0))
    result = eng._capture_stop_action()
    path = eng._capture.session_path(result["session_id"])

    from midicrt.engine.replay import replay_session
    summary = replay_session(path, instant=True)
    assert summary["final_state"]["voices"]["total"] == 1   # still held, no release mark present


def test_replay_ignores_a_malformed_panic_release_mark_instead_of_crashing(tmp_path, caplog):
    eng = Engine(Config(capture_dir=str(tmp_path)))
    eng._capture_start_action()
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=1.0))
    # Hand-inject a malformed mark directly into the buffer (missing "ch")
    # -- record_action itself always produces a well-formed one live, so
    # this simulates a hand-edited/corrupted file, not a reachable live
    # shape.
    eng._capture._buffer.append({"kind": "action", "ts": 1.5, "name": "panic.release",
                                 "args": {"notes": [60]}, "origin": "alert"})
    result = eng._capture_stop_action()
    path = eng._capture.session_path(result["session_id"])

    from midicrt.engine.replay import replay_session
    with caplog.at_level("WARNING"):
        summary = replay_session(path, instant=True)
    assert summary["final_state"]["voices"]["total"] == 1   # malformed mark skipped, not applied
    assert any("panic.release" in r.message for r in caplog.records)


def test_panic_release_is_gated_by_the_same_cooldown_as_the_cc_send():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert eng.analyzers["alerts"].held_notes(5) == []
    eng._handle(ev(type="note_on", channel=4, data1=62, data2=100, ts=1.1))   # new note, same ch
    eng._maybe_panic({"ch": 5, "note": 62, "level": "crit", "held_s": 11.0}, 1.1)   # within cooldown
    assert fake.control_change_calls == [(123, 0, 5)]   # still just the one send
    assert eng.analyzers["alerts"].held_notes(5) == [62]   # NOT released -- cooldown blocked it too


def test_panic_release_dispatch_marks_the_affected_topics_dirty():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._handle(ev(type="note_on", channel=4, data1=60, data2=100, ts=0.0))
    eng._dirty.clear()
    eng._maybe_panic({"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}, 1.0)
    assert "overlay.alerts" in eng._dirty
    assert "page.voices" in eng._dirty


async def test_panic_release_clears_the_alert_end_to_end_then_lingers_then_expires():
    # The REQUIRED integration test (review's Important finding): real
    # StuckNotesAnalyzer + real Engine, no _FakeTickingAnalyzer -- proves
    # the release actually reaches the escalation path that created the
    # alert in the first place, not just a directly-invoked _maybe_panic.
    import midicrt.analyzers.stucknotes as stucknotes_mod

    eng = Engine(Config(panic_on_crit=True, stuck_hold_after=1.0, capture_dir=None))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    eng._capture_start_action()

    eng._handle(ev(type="note_on", channel=0, data1=60, data2=100, ts=0.0))
    assert eng.pages["voices"].view_model()["rows"][0]["active"] == 1

    crit_after = stucknotes_mod.CRIT_AFTER
    eng._tick_analyzers(crit_after + 0.1)   # escalates to crit -- panic fires + release synthesized

    assert fake.control_change_calls == [(123, 0, 1)]
    # Handle()-time-equivalent clear already visible: the release was
    # dispatched synchronously inside THIS SAME _tick_analyzers call.
    assert eng.analyzers["alerts"].view_model()["alerts"] == []
    assert eng.pages["voices"].view_model()["rows"][0]["active"] == 0

    # Next tick arms the stuck-linger window (tick()-time bookkeeping,
    # analyzers/stucknotes.py's own documented one-tick gap).
    eng._tick_analyzers(crit_after + 0.2)
    cleared = eng.analyzers["alerts"].view_model()["cleared"]
    assert cleared and cleared[0]["note"] == 60

    # After stuck_hold_after (1.0s here) elapses, the linger expires.
    eng._tick_analyzers(crit_after + 0.2 + 1.0 + 0.1)
    assert eng.analyzers["alerts"].view_model()["cleared"] == []

    release_marks = [line for line in eng._capture._buffer
                     if line.get("kind") == "action" and line.get("name") == "panic.release"]
    assert len(release_marks) == 1
    assert release_marks[0]["origin"] == "alert"
    assert release_marks[0]["args"] == {"ch": 1, "notes": [60]}


async def test_panic_on_crit_re_arms_after_cooldown_for_a_genuinely_new_stuck_note():
    """T2b warm-up (re-review follow-up 1): pins the cooldown RE-ARM case
    with a real analyzer/engine round trip, not a directly-invoked
    `_maybe_panic` (every cooldown test above already covers that
    shortcut). Reviewer verified live that a first stuck note escalating
    to crit, being released, and THEN a genuinely new stuck note on the
    SAME channel escalating again well outside the 3.0s PANIC_COOLDOWN
    window fires panic a second time -- two real sends on channel 3,
    `[3, 3]` -- but nothing pinned it with an injected clock, so a future
    regression (e.g. cooldown keyed wrong, or never reset) could slip
    through silently. Mirrors
    test_panic_release_clears_the_alert_end_to_end_then_lingers_then_expires's
    real-StuckNotesAnalyzer shape above, just carried one note further."""
    import midicrt.analyzers.stucknotes as stucknotes_mod

    eng = Engine(Config(panic_on_crit=True, capture_dir=None))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    crit_after = stucknotes_mod.CRIT_AFTER

    # First stuck note on channel 3 (1-based; channel=2 is 0-based) --
    # held past CRIT_AFTER escalates to crit and fires panic (CC123 +
    # internal release, clearing the analyzer's own held-note state).
    eng._handle(ev(type="note_on", channel=2, data1=60, data2=100, ts=0.0))
    first_fire_at = crit_after + 0.1
    eng._tick_analyzers(first_fire_at)
    assert [c[2] for c in fake.control_change_calls] == [3]
    assert eng.analyzers["alerts"].held_notes(3) == []

    # A genuinely NEW stuck note (different pitch, fresh hold) on the SAME
    # channel, started well after the first release and escalating to
    # crit well outside the 3.0s cooldown measured from the first panic.
    second_note_start = first_fire_at + 1.0
    eng._handle(ev(type="note_on", channel=2, data1=64, data2=100, ts=second_note_start))
    second_fire_at = second_note_start + crit_after + 0.1
    assert second_fire_at - first_fire_at > 3.0   # sanity: genuinely past cooldown
    eng._tick_analyzers(second_fire_at)

    assert [c[2] for c in fake.control_change_calls] == [3, 3]   # panic fired AGAIN
    assert eng.analyzers["alerts"].held_notes(3) == []   # released again


# -- panic-send must never feed back as a new inbound event (CRITICAL,
# brief's own callout -- the P3 self-subscription runaway is the ancestor
# bug here) -----------------------------------------------------------------
#
# The panic CC goes out through the SAME shared `self._midi_out` port every
# other real send already uses (sendnotes note-on, sysex replies) -- the
# TWO existing self-subscription defense layers (engine/midi_in.py's own
# `exclude_names`, and `Engine._handle`'s own source filter, both proven
# above by test_handle_drops_an_event_whose_source_is_our_own_output_port/
# test_handle_drops_an_event_with_the_real_prefixed_alsa_source_form)
# therefore already cover it structurally -- this test pins the piece that
# is genuinely NEW here: firing panic never itself synthesizes/queues a
# fake inbound MidiEvent, only ever calls the OUTPUT method.

def test_panic_send_never_synthesizes_a_new_inbound_event():
    eng = Engine(Config(panic_on_crit=True))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    before_total = eng.events_total
    before_dirty = set(eng._dirty)
    analyzer = _FakeTickingAnalyzer(alerts=[{"ch": 5, "note": 60, "level": "crit", "held_s": 11.0}])
    eng.register_analyzer("fake", analyzer)
    eng._tick_analyzers(1.0)
    assert fake.control_change_calls == [(123, 0, 5)]
    # The panic send itself must never be counted as (or trigger) a real
    # inbound MIDI event -- events_total only moves through _handle(), which
    # _tick_analyzers never calls.
    assert eng.events_total == before_total
    assert eng._dirty - before_dirty == {"overlay.fake"}   # only the drained analyzer's own topic


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


# -- stuck-linger config wiring (Phase 9 Task 2, config.stuck_hold_after) ---

def test_config_stuck_hold_after_is_wired_into_the_real_alerts_analyzer():
    eng = Engine(Config(stuck_hold_after=5.0))
    assert eng.analyzers["alerts"]._hold_after == 5.0


def test_config_stuck_hold_after_default_matches_v1s_hold_after():
    eng = Engine(Config())
    assert eng.analyzers["alerts"]._hold_after == 15.0


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


# -- tuner page (phase-3 task 6; live-wired Phase 9 Task 3) ------------------

def test_tuner_is_in_the_default_roster():
    # Phase 9 Task 3: the tuner page now runs real pitch detection (a
    # dependency-free numpy YIN, see analyzers/tuner.py's module docstring
    # for the aubio-vs-YIN investigation) and degrades gracefully to a
    # "no audio input" state exactly like spectrum when no input device is
    # present -- so it joins config.py's default `pages` list the same way
    # voices/harmony did in tasks 4/5. (Previously excluded: it could only
    # ever show v1's idle state before pitch detection existed to feed it
    # -- see git history / config.py's own comment for that prior
    # reasoning.)
    eng = Engine(Config())
    assert "tuner" in eng.pages
    assert eng.pages["tuner"].view_model()["title"] == "TUNER"


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


# -- page.goto graceful no-op for a known-v1-ID page absent from the       --
# -- roster (Phase 9 Task 0, docs/gui-phase-decisions-2026-08-08.md        --
# -- digit-nav reconciliation) -----------------------------------------------

async def test_page_goto_known_v1_id_page_absent_from_roster_is_a_graceful_noop(caplog):
    # "tuner" has a real v1 page ID (marquee.PAGE_IDS) -- Phase 9 Task 3
    # put it IN the stock default roster, so this test now synthesizes a
    # deliberately narrowed roster (a custom config.toml could do the
    # same) to keep exercising the graceful no-op path: dispatching
    # page.goto at a known-v1-ID name that's absent from THIS PARTICULAR
    # build's roster (as DEFAULT_KEYMAP's own shift+0 binding could hit,
    # engine/keymap.py) must not raise, must not move current_page, and
    # must log once -- the SAME "ordinary, expected" treatment
    # `_page_jump`'s out-of-range position already gets (see
    # Engine._page_goto's own docstring).
    narrowed = [p for p in Config().pages if p != "tuner"]
    eng = Engine(Config(pages=narrowed))
    assert "tuner" not in eng.pages
    with caplog.at_level(logging.INFO):
        r = await eng.actions.dispatch("page.goto", {"name": "tuner"})
    assert r == {}
    assert eng.current_page == "eventlog"
    assert "tuner" in caplog.text


async def test_page_goto_unknown_name_with_no_v1_id_still_raises():
    # Narrowing check: a name that's neither a live page NOR a known v1-ID
    # page name (a genuine typo) keeps raising loudly -- the graceful
    # no-op above only ever makes MORE names succeed silently, never
    # fewer error out.
    eng = Engine(Config())
    with pytest.raises(ActionError):
        await eng.actions.dispatch("page.goto", {"name": "nonexistent"})


async def test_pagecycle_v1_id_digit_bound_page_goto_pauses_rotation():
    # Mirrors Phase 8 Task 6's own `test_pagecycle_page_jump_client_
    # origin_pauses_rotation` -- proves the NEW default digit binding
    # (page.goto, baked from marquee.PAGE_IDS, engine/keymap.py) still
    # arms `pagecycle_user_pause` when dispatched exactly as a real
    # keypress would send it. `page.goto` was already in
    # `_PAGE_NAV_ACTIONS` before this task (see that set's own comment),
    # so this is an integration/regression proof for the new binding
    # shape, not a new engine-side wire.
    fake_now = [0.0]
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        screensaver_enabled=False))
    eng._clock = lambda: fake_now[0]
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(fake_now[0])   # bootstrap
    # "#" is shift+3 -> v1 ID 13 -> "voices" (engine/keymap.py's own
    # digit<->v1-ID formula) -- pulled from the REAL DEFAULT_KEYMAP, not
    # hand-typed, so this actually exercises what a physical keypress
    # sends.
    entry = keymap_mod.DEFAULT_KEYMAP["#"]
    assert entry == {"action": "page.goto", "args": {"name": "voices"}}
    await eng.actions.dispatch(entry["action"], entry["args"], origin="client")
    assert eng.current_page == "voices"
    fake_now[0] = 5.0
    await eng._tick_behaviors(fake_now[0])   # interval elapsed, but paused
    assert eng.current_page == "voices"
    fake_now[0] = 29.9
    await eng._tick_behaviors(fake_now[0])
    assert eng.current_page == "voices"
    fake_now[0] = 30.0
    await eng._tick_behaviors(fake_now[0])   # pause expired -- interval long since elapsed too
    assert eng.current_page == "harmony"


# -- page.jump (Phase 8 Task 6, docs/gui-phase-decisions-2026-08-08.md      --
# -- keymap revamp: roster-POSITIONAL number-key page jumps) ----------------

async def test_page_jump_dispatches_to_the_nth_page_in_roster():
    eng = Engine(Config())
    order = eng._page_order()
    r = await eng.actions.dispatch("page.jump", {"position": 3})
    assert eng.current_page == order[2]   # 1-indexed
    assert r["page"] == order[2]


async def test_page_jump_position_one_is_the_first_page():
    eng = Engine(Config())
    order = eng._page_order()
    await eng.actions.dispatch("page.goto", {"name": order[-1]})   # move away from position 1
    await eng.actions.dispatch("page.jump", {"position": 1})
    assert eng.current_page == order[0]


async def test_page_jump_out_of_range_position_is_a_silent_no_op_not_an_error(caplog):
    eng = Engine(Config())
    order = eng._page_order()
    starting_page = eng.current_page
    with caplog.at_level(logging.INFO):
        r = await eng.actions.dispatch("page.jump", {"position": len(order) + 5})
    assert eng.current_page == starting_page   # unchanged -- no exception, no page switch
    assert r == {}
    assert "out of range" in caplog.text.lower()


async def test_page_jump_position_zero_is_out_of_range():
    eng = Engine(Config())
    starting_page = eng.current_page
    await eng.actions.dispatch("page.jump", {"position": 0})
    assert eng.current_page == starting_page


async def test_page_jump_negative_position_is_out_of_range():
    eng = Engine(Config())
    starting_page = eng.current_page
    await eng.actions.dispatch("page.jump", {"position": -1})
    assert eng.current_page == starting_page


async def test_page_jump_emits_page_changed_and_keymap_changed_like_page_goto():
    eng = Engine(Config())
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    await eng.actions.dispatch("page.jump", {"position": 2})
    assert [e["name"] for e in events] == ["page_changed", "keymap_changed"]


# -- marquee reset-on-page-change (Phase 10 Task B, docs/demo-feedback-      --
# -- 2026-08-12.md item 7) ---------------------------------------------------

async def test_page_change_resets_the_marquee_to_the_new_pages_own_entry():
    # `_set_current_page` is the SINGLE funnel every page-nav action goes
    # through (module docstring) -- one representative action (page.goto)
    # is enough to prove the wiring; `MarqueeAnalyzer.reset_to_page`'s own
    # tests (test_analyzers_marquee.py) cover the offset math itself.
    fake_now = [10.0]
    eng = Engine(Config())
    eng._clock = lambda: fake_now[0]
    marquee = eng.analyzers["marquee"]
    marquee.tick(fake_now[0])
    fake_now[0] = 13.0
    marquee.tick(fake_now[0])   # scroll away from offset 0 first
    before_offset = marquee.view_model()["offset"]
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"})
    vm = marquee.view_model()
    assert vm["offset"] != before_offset
    assert vm["doubled"][vm["offset"]:vm["offset"] + len("[8:PIANOROLL]")] == "[8:PIANOROLL]"


async def test_page_change_marks_overlay_marquee_dirty_when_the_reset_actually_moves_it():
    eng = Engine(Config())
    eng._clock = lambda: 20.0
    eng.analyzers["marquee"].tick(20.0)
    eng._dirty.clear()
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"})
    assert "overlay.marquee" in eng._dirty


async def test_page_change_to_a_page_with_no_marquee_entry_does_not_mark_it_dirty_solely_for_that():
    # "screensaver" has no v1 page ID (analyzers/marquee.py) -- resetting
    # to it is a no-op on the marquee itself; `overlay.marquee` may still
    # be marked dirty by the marquee's own independent wall-clock tick
    # (unrelated to this transition), but the page-change reset call
    # itself contributes nothing extra. Proven by comparing to a page.goto
    # dispatched with the analyzer's tick() never having been called at
    # all between the two -- if the reset itself were marking it dirty,
    # THIS dispatch would too, even with a screensaver-bound roster.
    eng = Engine(Config(pages=["eventlog", "screensaver"]))
    eng._clock = lambda: 20.0
    eng._dirty.clear()
    await eng.actions.dispatch("page.goto", {"name": "screensaver"})
    assert "overlay.marquee" not in eng._dirty


async def test_page_change_still_resumes_scrolling_normally_after_a_reset():
    fake_now = [0.0]
    eng = Engine(Config())
    eng._clock = lambda: fake_now[0]
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"}, )
    reset_offset = eng.analyzers["marquee"].view_model()["offset"]
    fake_now[0] = 1.0
    eng.analyzers["marquee"].tick(fake_now[0])
    # Some real time passed post-reset -- the marquee must have kept
    # advancing from the reset point (not frozen there, not reset to 0).
    assert eng.analyzers["marquee"].view_model()["offset"] != reset_offset


# -- per-page keymap sections (Phase 8 Task 6) -------------------------------

def test_keymap_page_reflects_the_current_pages_own_default_section():
    from midicrt.engine import keymap as keymap_mod

    eng = Engine(Config())   # boots on "eventlog" -- no page-specific keys
    assert eng.keymap_page == {}
    assert eng.keymap_global == keymap_mod.DEFAULT_KEYMAP
    assert eng.keymap == eng.keymap_global


async def test_keymap_page_recomputes_on_page_change_to_a_page_with_its_own_section():
    from midicrt.engine import keymap as keymap_mod

    eng = Engine(Config())
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"})
    assert eng.keymap_page == keymap_mod.DEFAULT_PAGE_KEYMAPS["pianoroll"]
    assert eng.keymap_global == keymap_mod.DEFAULT_KEYMAP
    # The effective table is the union, page winning on any overlap
    # (structurally none exist between global/pianoroll defaults today).
    assert eng.keymap == {**eng.keymap_global, **eng.keymap_page}


async def test_keymap_page_goes_back_to_empty_when_leaving_a_page_with_its_own_section():
    eng = Engine(Config())
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"})
    assert eng.keymap_page
    await eng.actions.dispatch("page.goto", {"name": "eventlog"})
    assert eng.keymap_page == {}


async def test_config_reload_recomputes_per_page_keymap_sections(tmp_path):
    keymap_path = tmp_path / "keymap.toml"
    eng = Engine(Config(), keymap_path=str(keymap_path))
    await eng.actions.dispatch("page.goto", {"name": "pianoroll"})
    keymap_path.write_text('[keys.pianoroll]\n"9" = "pianoroll.projection_toggle"\n')
    r = await eng.actions.dispatch("config.reload", {})
    assert eng.keymap_page["9"] == "pianoroll.projection_toggle"
    # Every other pianoroll default survives the merge (module docstring's
    # own "override what you name, keep everything else" contract).
    assert eng.keymap_page["p"] == "pianoroll.projection_toggle"
    assert r["keymap"] == eng.keymap


# -- pianoroll page (phase-3 task 7) -----------------------------------------

def test_pianoroll_is_in_the_default_roster():
    eng = Engine(Config())
    assert "pianoroll" in eng.pages
    assert eng.pages["pianoroll"].view_model()["title"] == "PIANOROLL"


def test_pianoroll_actions_are_registered_when_the_page_is_present():
    eng = Engine(Config())
    described = eng.actions.describe()
    assert {"pianoroll.zoom", "pianoroll.zoom_level", "pianoroll.pan", "pianoroll.projection",
            "pianoroll.channels"} <= set(described)


def test_pianoroll_actions_are_absent_when_the_page_is_not_in_the_roster():
    # Mirrors "eventlog.clear" always assuming eventlog exists -- guarded
    # registration means a build without "pianoroll" in config.pages simply
    # never advertises these actions (no KeyError at dispatch time either).
    eng = Engine(Config(pages=["eventlog"]))
    described = eng.actions.describe()
    assert not ({"pianoroll.zoom", "pianoroll.zoom_level", "pianoroll.pan",
                "pianoroll.projection", "pianoroll.channels"} & set(described))


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


async def test_pianoroll_pan_action_mutates_and_marks_dirty():
    # Phase 10 Task A (docs/demo-feedback-2026-08-12.md item 11).
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.pan", {"delta": "1"})
    assert r["range"] == {"lo": 37, "hi": 84}
    assert eng.pages["pianoroll"].view_model()["range"] == {"lo": 37, "hi": 84}
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


# -- pianoroll.channel_toggle / .projection_toggle (Phase 8 Task 6) ---------

async def test_pianoroll_channel_toggle_removes_a_visible_channel():
    eng = Engine(Config())
    r = await eng.actions.dispatch("pianoroll.channel_toggle", {"channel": 10})
    assert 10 not in r["channels"]
    assert "page.pianoroll" in eng._dirty


async def test_pianoroll_channel_toggle_is_a_true_toggle_leaving_others_untouched():
    eng = Engine(Config())
    await eng.actions.dispatch("pianoroll.channel_toggle", {"channel": 10})
    r = await eng.actions.dispatch("pianoroll.channel_toggle", {"channel": 10})
    assert 10 in r["channels"]
    assert set(r["channels"]) == set(range(1, 17))   # every other channel still visible


async def test_pianoroll_projection_toggle_flips_wallclock_to_tempo_and_back():
    # Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9): default
    # flipped from "wallclock" to "tempo" -- v1-parity fix, see pages/
    # pianoroll.py's own __init__ comment for the file:line evidence. The
    # toggle itself is unchanged -- still just flips between the two modes
    # -- so this test now starts from "tempo" and asserts the same
    # round-trip in the other direction.
    eng = Engine(Config())
    assert eng.pages["pianoroll"].view_model()["window"]["mode"] == "tempo"
    r = await eng.actions.dispatch("pianoroll.projection_toggle", {})
    assert r["mode"] == "wallclock"
    r = await eng.actions.dispatch("pianoroll.projection_toggle", {})
    assert r["mode"] == "tempo"


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


async def test_spectrum_bins_action_adjusts_and_marks_dirty():
    # Phase 8 Task 6 -- the one representative v1 retuning key this task
    # adds real mutable state for (see engine/keymap.py's own
    # DEFAULT_PAGE_KEYMAPS docstring for the disclosed-partial-coverage
    # rationale).
    eng = Engine(Config())
    r = await eng.actions.dispatch("spectrum.bins", {"delta": -16})
    assert r["bins"] == 80   # DEFAULT_BINS(96) - 16
    assert "page.spectrum" in eng._dirty


async def test_spectrum_bins_action_clamps_to_the_documented_range():
    eng = Engine(Config())
    r = await eng.actions.dispatch("spectrum.bins", {"delta": -1000})
    assert r["bins"] == 8    # MIN_BINS
    r = await eng.actions.dispatch("spectrum.bins", {"delta": 1000})
    assert r["bins"] == 256  # MAX_BINS


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


async def test_tick_behaviors_dispatches_page_goto_when_pagecycle_behavior_fires():
    # Phase 8 Task 5 (v1-semantics restoration): REPLACES the T9-era
    # `..._dispatches_page_next_...` test -- the new behavior dispatches
    # `page.goto` to the FIRST configured `pagecycle_pages` entry
    # ("harmony", Config()'s own default), never the roster-wide
    # `page.next` the idle-triggered version used.
    eng = Engine(Config(pagecycle_interval=5.0, screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(0.0)   # bootstraps -- no fire yet
    assert eng.current_page == "eventlog"
    await eng._tick_behaviors(5.0)
    assert eng.current_page == "harmony"


async def test_tick_behaviors_pagecycle_dispatch_is_observable_via_a_spy():
    eng = Engine(Config(pagecycle_interval=5.0, screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    calls = []
    original_dispatch = eng.actions.dispatch

    async def spy(name, args, **kwargs):
        calls.append((name, args))
        return await original_dispatch(name, args, **kwargs)

    eng.actions.dispatch = spy
    await eng._tick_behaviors(0.0)
    await eng._tick_behaviors(5.0)
    assert calls == [("page.goto", {"name": "harmony"})]


async def test_pagecycle_rotates_on_a_fixed_interval_even_while_midi_is_continuously_active():
    # Phase 8 Task 5's headline contract, proven end to end (not just in
    # behaviors/pagecycle.py's own unit tests): a real `note_on` arriving
    # on EVERY tick -- continuously "active" by any idle-gate definition --
    # must not push pagecycle's rotation out by even one tick. The T9-era
    # idle-triggered version would never have fired at all under this
    # traffic pattern (activity resets its idle clock every tick).
    eng = Engine(Config(pagecycle_interval=5.0, screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(0.0)   # bootstrap
    for t in range(1, 5):
        eng._handle(ev(type="note_on", ts=float(t)))
        await eng._tick_behaviors(float(t))
        assert eng.current_page == "eventlog"
    eng._handle(ev(type="note_on", ts=5.0))
    await eng._tick_behaviors(5.0)
    assert eng.current_page == "harmony"


def test_pagecycle_disabled_by_config_never_fires():
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_enabled=False, screensaver_enabled=False))
    eng._last_activity_ts = 0.0

    async def run_ticks():
        await eng._tick_behaviors(0.0)
        await eng._tick_behaviors(5.0)
        await eng._tick_behaviors(1000.0)

    asyncio.run(run_ticks())
    assert eng.current_page == "eventlog"


async def test_pagecycle_client_origin_page_action_pauses_rotation():
    # Origin ruling (behaviors/pagecycle.py's own docstring): a real client
    # action is a human touching page navigation directly -- must pause
    # rotation for `pagecycle_user_pause`.
    #
    # Fix round 1 (Important reviewer finding): `eng._clock` is pinned to
    # this test's own fake timeline (`Engine.__init__`'s injectable clock
    # source, wired through `_on_action_dispatched` -- see its own
    # comment) so this assertion is actually dispositive. Before this fix,
    # `notify_page_action`'s `now` came from a REAL `time.time()` while
    # `_tick_behaviors` ran on these small FAKE `now` values -- a clock-
    # domain mismatch that made "still paused" pass regardless of
    # `pagecycle_user_pause`'s configured value (a real epoch timestamp
    # trivially exceeds any small fake `now`). See
    # test_pagecycle_honors_a_tiny_user_pause_at_fake_clock_scale below for
    # the extreme-scale version of this same proof.
    fake_now = [0.0]
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        screensaver_enabled=False))
    eng._clock = lambda: fake_now[0]
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(fake_now[0])   # bootstrap
    await eng.actions.dispatch("page.goto", {"name": "voices"}, origin="client")
    fake_now[0] = 5.0
    await eng._tick_behaviors(fake_now[0])   # interval elapsed, but paused
    assert eng.current_page == "voices"
    fake_now[0] = 29.9
    await eng._tick_behaviors(fake_now[0])
    assert eng.current_page == "voices"
    fake_now[0] = 30.0
    await eng._tick_behaviors(fake_now[0])   # pause expired -- interval long since elapsed too
    assert eng.current_page == "harmony"


async def test_pagecycle_page_jump_client_origin_pauses_rotation():
    # Review finding (Critical, live-reproduced): `page.jump` (Phase 8
    # Task 6) was missing from `_PAGE_NAV_ACTIONS`, so a number-key jump
    # never armed `notify_page_action` at all -- pagecycle rotated the
    # user away after just one `pagecycle_interval` instead of honoring
    # `pagecycle_user_pause`, unlike `page.goto`/`page.next`/`page.prev`
    # (all of which DO arm it, `test_pagecycle_client_origin_page_action_
    # pauses_rotation` above). Same fake-clock-injection shape as that
    # test (dispositive for the same reason -- see its own comment).
    fake_now = [0.0]
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        screensaver_enabled=False))
    eng._clock = lambda: fake_now[0]
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(fake_now[0])   # bootstrap
    order = eng._page_order()
    await eng.actions.dispatch("page.jump", {"position": order.index("voices") + 1},
                              origin="client")
    assert eng.current_page == "voices"
    fake_now[0] = 5.0
    await eng._tick_behaviors(fake_now[0])   # interval elapsed, but paused
    assert eng.current_page == "voices"
    fake_now[0] = 29.9
    await eng._tick_behaviors(fake_now[0])
    assert eng.current_page == "voices"
    fake_now[0] = 30.0
    await eng._tick_behaviors(fake_now[0])   # pause expired -- interval long since elapsed too
    assert eng.current_page == "harmony"


async def test_pagecycle_honors_a_tiny_user_pause_at_fake_clock_scale():
    # Fix round 1 (Important reviewer finding, the specific probe that
    # caught the clock-domain bug): `pagecycle_user_pause=0.001` is
    # deliberately tiny and `eng._clock` is pinned to this test's own fake
    # timeline. Under the OLD wiring (`notify_page_action` stamped a REAL
    # `time.time()`, ~1.7 billion), `paused_until` would land near that
    # real epoch value regardless of the configured 0.001s -- so even a
    # fake `now` as generous as 1.5 (long past both the tiny pause AND the
    # 1.0s interval) would still look "paused," making this test fail
    # against the unfixed wiring and pass falsely-confident under any
    # `pagecycle_user_pause` value if it DIDN'T check for eventual
    # resumption. This test's final assertion is what makes it dispositive.
    fake_now = [0.0]
    eng = Engine(Config(pagecycle_interval=1.0, pagecycle_user_pause=0.001,
                        screensaver_enabled=False))
    eng._clock = lambda: fake_now[0]
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(fake_now[0])   # bootstrap at t=0
    await eng.actions.dispatch("page.goto", {"name": "voices"}, origin="client")
    # paused_until = clock()(0.0) + user_pause(0.001) = 0.001
    fake_now[0] = 0.0005
    await eng._tick_behaviors(fake_now[0])
    assert eng.current_page == "voices"   # still inside the tiny pause window
    fake_now[0] = 1.5
    await eng._tick_behaviors(fake_now[0])
    # Both the 1.0s interval (measured from _last_switch, set at bootstrap)
    # and the 0.001s pause have long elapsed by t=1.5 -- rotation must have
    # resumed.
    assert eng.current_page != "voices"


async def test_pagecycle_binding_origin_page_action_does_not_pause(tmp_path):
    # A learned MIDI binding driving page.next is the sequencer performing,
    # not a user -- must NOT pause pagecycle's own rotation. No clock-domain
    # concern here (unlike the "pauses" tests above): a binding origin never
    # reaches `_HUMAN_ORIGINS` at all, so no `paused_until` of any kind gets
    # set regardless of which clock stamped it.
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, binding_id="b1", action="page.next", number=60)
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        screensaver_enabled=False), bindings_path=str(p))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(0.0)   # bootstrap
    eng._handle(ev(type="note_on", data1=60, data2=100))
    await eng._dispatch_bindings()   # fires page.next via origin="binding:b1"
    await eng._tick_behaviors(5.0)   # must still fire -- no pause was armed
    assert eng.current_page == "harmony"


async def test_pagecycle_sysex_page_switch_does_not_pause_rotation():
    # Fix round 1 (Important, reviewer-found reversal): an earlier version
    # of this test asserted the OPPOSITE ("a real hardware SysEx page-
    # switch command is a human pressing a physical control -- ruled a
    # human origin alongside client") without verifying it against v1
    # source. It doesn't hold: v1's own `plugins/sysex.py::_dispatch()`
    # CMD_SWITCH_PAGE branch calls `midicrt.switch_page()` directly and
    # never touches `notify_keypress()` -- only a literal physical
    # keystroke does (`midicrt.py`'s `keyboard_listener()`). See
    # behaviors/pagecycle.py's "Origin ruling" docstring section for the
    # full evidence trail. No clock-domain concern here either (see the
    # binding test above) -- `_sysex_switch_page` no longer calls
    # `notify_page_action` at all.
    eng = Engine(Config(pagecycle_interval=5.0, pagecycle_user_pause=30.0,
                        screensaver_enabled=False))
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(0.0)   # bootstrap
    eng._sysex_switch_page(version=None, args=(13,), ts=0.0)   # page id 13 = "voices"
    assert eng.current_page == "voices"
    await eng._tick_behaviors(5.0)   # interval elapsed -- rotation proceeds, unpaused
    assert eng.current_page == "harmony"


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
    eng = Engine(Config(pagecycle_interval=0.05, screensaver_enabled=False, tick_hz=200.0))
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.25)
    eng.stop()
    await task
    assert eng.current_page != "eventlog"


async def test_pagecycle_does_not_unblank_screensaver_with_shipped_defaults():
    # Guard KEPT verbatim from the task-9 review (task brief's explicit
    # requirement) even though pagecycle's OWN gating mechanism changed
    # completely (Phase 8 Task 5: no longer idle-triggered off
    # `last_activity_ts` at all -- a pure wall-clock `pagecycle_interval`,
    # see behaviors/pagecycle.py's own "Arbitration with the screensaver"
    # docstring section). If anything the guard matters MORE now: pagecycle
    # no longer shares any clock with the screensaver, so a fully idle
    # engine's pagecycle timer keeps counting down completely obliviously
    # to `last_activity_ts` -- the screensaver-page check in
    # `PageCycleBehavior.tick()` is the ONLY thing standing between that and
    # un-blanking a just-activated screensaver. Reproduced against
    # Config()'s actual shipped defaults (pagecycle_interval=300,
    # screensaver_after_s=60, both enabled): a fully idle engine must reach
    # the screensaver at t=60 and STAY there through at least t=900 (three
    # full pagecycle intervals' worth of idle time).
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
    # Guard-interaction test carried over from the task-9 review, updated
    # for Phase 8 Task 5's new re-arm mechanism: `PageCycleBehavior.tick()`
    # re-arms `_last_switch = now` the instant `current_page` stops being
    # the screensaver page (see that module's own "Arbitration with the
    # screensaver" docstring section) -- a manual escape must NOT be
    # immediately overridden by a stale elapsed-interval check. Unlike the
    # T9-era version of this test, the old "was it activity-driven or
    # manual" distinction no longer applies (this module's re-arm no
    # longer keys off `last_activity_ts` at all) -- see
    # test_behaviors_pagecycle.py::
    # test_rearms_with_a_fresh_interval_once_the_screensaver_block_ends for
    # the isolated timing contract this engine-level smoke test exercises
    # against real dual-behavior wiring instead.
    #
    # This deliberately does NOT project all the way out to pagecycle's own
    # full re-armed interval -- with the shipped defaults, screensaver's own
    # SHORTER after_s (60s) will legitimately reclaim the display again well
    # before pagecycle's longer interval (300s) could ever elapse
    # uninterrupted (a real, disclosed, and arguably correct consequence of
    # screensaver taking priority), which would make asserting a specific
    # later page value here brittle and would actually be testing
    # screensaver's OWN timer, not pagecycle's.
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
    assert calls == [], "no behavior should have dispatched anything on this tick"


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


def test_help_page_renders_the_live_keymap(tmp_path):
    """Phase 5 Task 3 (docs/phase5-notes.md cheap-wins bundle): `_help_info`
    now also feeds the engine's real, live `self.keymap` through --
    `DEFAULT_KEYMAP`'s own `n -> page.next` entry (engine/keymap.py) should
    be visible in the help page's rendered keymap section with no
    keymap.toml on disk at all."""
    eng = Engine(Config(), keymap_path=str(tmp_path / "nope.toml"))
    vm = eng.pages["help"].view_model()
    keymap_rows = {r["label"]: r["value"] for r in vm["keymap_rows"]}
    assert keymap_rows["n"] == "page.next"


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
        # Phase 9 Task 2 (panic-send): mirrors note_on_calls/note_off_calls'
        # own convention -- (control, value, channel), matching
        # MidiOutput.all_notes_off's real `control_change(control=123,
        # value=0, channel=...)` send.
        self.control_change_calls: list[tuple[int, int, int]] = []
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

    def all_notes_off(self, channel):
        self.control_change_calls.append((123, 0, channel))
        self.call_order.append(("all_notes_off", channel))

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


# -- topic_refcount() public accessor (fix round, review finding 1) ---------
#
# New public wrapper around `_topic_refcount_provider` for consumers that
# only need the net "is anyone plausibly listening" signal and are fine
# treating "unwired" the same as "wired but zero" -- unlike `_flush_dirty`'s
# own internal `is not None` check (which must NOT skip when unwired, see
# that method's own docstring), `_tick_audio_gate` below (the audio-capture
# demand gate) wants the SAFE-for-an-expensive-resource default: unwired
# means "assume no subscribers", not "assume subscribed".

def test_topic_refcount_is_zero_when_unwired():
    eng = Engine(Config())
    assert eng.topic_refcount("page.tuner") == 0


def test_topic_refcount_reflects_the_wired_provider():
    eng = Engine(Config())
    eng.set_topic_refcount_provider(lambda topic: 3 if topic == "page.tuner" else 0)
    assert eng.topic_refcount("page.tuner") == 3
    assert eng.topic_refcount("page.spectrum") == 0


# -- audio-capture demand gate (fix round, review finding 1: "Audio        --
# -- capture is unconditional for daemon lifetime") --------------------------
#
# Root cause (live-measured by the reviewer): daemon.py started spectrum's
# AND tuner's AudioCapture unconditionally at boot and never stopped either
# until shutdown -- identical ~39-40% of one core whether the current page
# was screensaver or tuner. Fix: `Engine._tick_audio_gate(now)` (called
# from `run()` every tick, alongside `_tick_pages`/`_tick_analyzers`) starts
# a page's capture only while it's `current_page` OR its `page.<name>`
# topic has subscribers (`topic_refcount()` above), and stops it after
# `_AUDIO_CAPTURE_STOP_DEBOUNCE_S` of neither holding -- so ordinary
# page-flipping/pagecycle rotation doesn't thrash capture threads open and
# closed. Applies to ANY page with `start_capture`/`stop_capture` hooks
# (discovered the same `hasattr`-at-construction way `tick`/`drain_outputs`/
# `bind_info` already are, see `PageHooks`'s own docstring) -- today that's
# both "spectrum" and "tuner", gated by the exact same mechanism, not two
# separate ones.

def test_audio_gate_does_not_start_capture_when_page_not_current_and_no_subscribers():
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "eventlog"   # NOT "capture_test"
    eng._tick_audio_gate(0.0)
    assert cap.calls == []


def test_audio_gate_starts_capture_when_page_becomes_current():
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "capture_test"
    eng._tick_audio_gate(0.0)
    assert cap.calls == ["start"]


def test_audio_gate_start_is_idempotent_while_page_stays_current():
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "capture_test"
    eng._tick_audio_gate(0.0)
    eng._tick_audio_gate(0.1)
    eng._tick_audio_gate(0.2)
    assert cap.calls == ["start"]   # never re-started


def test_audio_gate_stops_capture_after_debounce_once_page_leaves_and_no_subscribers():
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "capture_test"
    eng._tick_audio_gate(0.0)
    assert cap.calls == ["start"]

    eng.current_page = "eventlog"   # left the page -- unwanted-since = 1.0
    eng._tick_audio_gate(1.0)       # well within the debounce window
    assert cap.calls == ["start"]   # not stopped yet
    eng._tick_audio_gate(5.9)
    assert cap.calls == ["start"]   # still not stopped (< 1.0+5.0s since unwanted)
    eng._tick_audio_gate(6.1)       # debounce elapsed (>= 1.0+5.0)
    assert cap.calls == ["start", "stop"]


def test_audio_gate_debounce_resets_if_page_becomes_current_again_before_elapsing():
    # Thrash protection -- the whole point of the debounce: ordinary
    # page-flipping back and forth must never toggle the capture thread.
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "capture_test"
    eng._tick_audio_gate(0.0)
    assert cap.calls == ["start"]

    eng.current_page = "eventlog"
    eng._tick_audio_gate(1.0)       # unwanted-since = 1.0
    eng.current_page = "capture_test"
    eng._tick_audio_gate(2.0)       # wanted again, well before 1.0+5.0
    assert cap.calls == ["start"]   # still just the one start, no stop ever fired

    eng.current_page = "eventlog"
    eng._tick_audio_gate(7.0)       # a FRESH unwanted-since = 7.0, not 1.0
    eng._tick_audio_gate(11.9)      # < 7.0+5.0 -- must not have stopped yet
    assert cap.calls == ["start"]
    eng._tick_audio_gate(12.1)      # >= 7.0+5.0 -- now it stops
    assert cap.calls == ["start", "stop"]


def test_audio_gate_topic_subscription_alone_keeps_capture_alive():
    # Not the current page at all, ever -- only a subscriber (e.g. the web
    # client rendering it in a background tab) -- capture still runs, and
    # never gets debounce-stopped while the subscription holds.
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "eventlog"
    eng.set_topic_refcount_provider(lambda topic: 1 if topic == "page.capture_test" else 0)
    eng._tick_audio_gate(0.0)
    assert cap.calls == ["start"]
    eng._tick_audio_gate(100.0)   # long past any debounce window
    assert cap.calls == ["start"]   # never stopped -- the subscription alone holds it


def test_audio_gate_stops_once_subscriber_refcount_drops_and_debounce_elapses():
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "eventlog"
    refcount = {"page.capture_test": 1}
    eng.set_topic_refcount_provider(lambda topic: refcount.get(topic, 0))
    eng._tick_audio_gate(0.0)
    assert cap.calls == ["start"]

    refcount["page.capture_test"] = 0
    eng._tick_audio_gate(1.0)
    assert cap.calls == ["start"]
    eng._tick_audio_gate(6.1)
    assert cap.calls == ["start", "stop"]


def test_audio_gate_disabled_never_starts_capture_even_when_page_is_current():
    # Mirrors the `--no-audio` opt-out -- daemon.py calls
    # set_audio_capture_enabled(False) once, before run() ever ticks.
    eng = Engine(Config())
    cap = _FakeCapturePage()
    eng.register_page("capture_test", cap)
    eng.current_page = "capture_test"
    eng.set_audio_capture_enabled(False)
    eng._tick_audio_gate(0.0)
    assert cap.calls == []


def test_audio_gate_applies_independently_per_page():
    eng = Engine(Config())
    cap_a = _FakeCapturePage()
    cap_b = _FakeCapturePage()
    eng.register_page("capture_a", cap_a)
    eng.register_page("capture_b", cap_b)
    eng.current_page = "capture_a"
    eng._tick_audio_gate(0.0)
    assert cap_a.calls == ["start"]
    assert cap_b.calls == []


def test_audio_gate_ignores_pages_with_no_capture_hooks():
    # A page with no start_capture/stop_capture (e.g. "eventlog") must
    # never raise or be touched by the gate at all.
    eng = Engine(Config())
    eng.current_page = "eventlog"
    eng._tick_audio_gate(0.0)   # must not raise


# -- the real default-roster spectrum/tuner pages, not just fakes -----------

def test_audio_gate_wires_the_real_spectrum_and_tuner_pages():
    eng = Engine(Config())   # default roster includes both
    spectrum_calls: list[str] = []
    tuner_calls: list[str] = []
    eng.pages["spectrum"].start_capture = lambda: spectrum_calls.append("start")
    eng.pages["spectrum"].stop_capture = lambda: spectrum_calls.append("stop")
    eng.pages["tuner"].start_capture = lambda: tuner_calls.append("start")
    eng.pages["tuner"].stop_capture = lambda: tuner_calls.append("stop")
    # Re-discover hooks so the gate picks up the swapped-in instrumented
    # methods above -- PageHooks binds the callables at discovery time
    # (Engine.__init__), before this test ever gets a chance to monkeypatch
    # the page instances it already built.
    eng._discover_page_hooks("spectrum", eng.pages["spectrum"])
    eng._discover_page_hooks("tuner", eng.pages["tuner"])

    eng.current_page = "eventlog"
    eng._tick_audio_gate(0.0)
    assert spectrum_calls == []
    assert tuner_calls == []

    eng.current_page = "tuner"
    eng._tick_audio_gate(0.1)
    assert tuner_calls == ["start"]
    assert spectrum_calls == []

    eng.current_page = "spectrum"
    eng._tick_audio_gate(0.2)
    assert spectrum_calls == ["start"]
    # tuner is no longer current and has no subscriber -- still within the
    # debounce window at t=0.2 (unwanted-since=0.2), so not stopped yet.
    assert tuner_calls == ["start"]

    eng._tick_audio_gate(5.3)   # past tuner's debounce (0.2 + 5.0)
    assert tuner_calls == ["start", "stop"]
    assert spectrum_calls == ["start"]   # spectrum still current, untouched


async def test_audio_gate_starts_capture_via_a_real_page_goto_dispatch():
    # End-to-end through the real action-dispatch path (not a direct
    # current_page assignment) -- the brief's own named regression case
    # ("started on page.goto to tuner").
    eng = Engine(Config())
    calls: list[str] = []
    eng.pages["tuner"].start_capture = lambda: calls.append("start")
    eng.pages["tuner"].stop_capture = lambda: calls.append("stop")
    eng._discover_page_hooks("tuner", eng.pages["tuner"])
    await eng.actions.dispatch("page.goto", {"name": "tuner"})
    eng._tick_audio_gate(0.0)
    assert calls == ["start"]


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


# -- voices/polylimit shared VoiceMonitorAnalyzer (Phase 9 Task 2) ---------
#
# Same "share one instance across two roster entries, protected by that
# analyzer's own dedup guard" precedent as harmony/chordkey above -- here
# the SECOND consumer is a NEW analyzer/overlay entry (`overlay.polylimit`,
# `_PolyLimitOverlay`), not a second page, so poly-limit tracking is
# computed exactly once per event while the chrome flash still gets its own
# minimal, separately-subscribable topic (see analyzers/voices.py's own
# module docstring for why a full channels-array duplicate onto chrome
# would be wasteful).

def test_voices_page_and_polylimit_overlay_share_one_analyzer_instance():
    eng = Engine(Config())
    assert eng.pages["voices"].analyzer is eng.analyzers["polylimit"]._shared


def test_polylimit_topic_is_registered():
    eng = Engine(Config())
    assert "overlay.polylimit" in eng.topics


def test_config_poly_limits_are_wired_into_the_real_voices_analyzer():
    eng = Engine(Config(poly_limit_global=4, poly_limit_ch=2))
    assert eng.pages["voices"].analyzer._limit_global == 4
    assert eng.pages["voices"].analyzer._limit_ch == 2


def test_voices_and_polylimit_do_not_double_count_a_shared_event():
    eng = Engine(Config(poly_limit_global=100, poly_limit_ch=1))
    shared = eng.pages["voices"].analyzer
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0))
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0))   # exceeds ch limit 1
    assert shared.view_model()["channels"][0]["active"] == 2   # counted once each, not twice
    assert len(shared.view_model()["events"]) == 1


def test_polylimit_overlay_view_model_is_the_minimal_flash_shape():
    eng = Engine(Config())
    assert eng.analyzers["polylimit"].view_model() == {"flashing": False}


def test_polylimit_flash_reaches_the_overlay_topic_via_tick_analyzers():
    eng = Engine(Config(poly_limit_global=100, poly_limit_ch=1))
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0, ts=1.0))
    eng._handle(ev(type="note_on", data1=60, data2=100, channel=0, ts=1.0))
    eng._tick_analyzers(1.1)
    assert "overlay.polylimit" in eng._dirty
    assert eng.analyzers["polylimit"].view_model() == {"flashing": True}


def test_a_solo_voices_page_still_gets_its_own_working_analyzer_no_polylimit_overlay():
    # Boundary case: a custom roster without "voices" -- nothing to wrap,
    # no "polylimit" overlay registered at all (mirrors the harmony-solo
    # boundary test above).
    eng = Engine(Config(pages=["eventlog"]))
    assert "voices" not in eng.pages
    assert "polylimit" not in eng.analyzers
    assert "overlay.polylimit" not in eng.topics


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


# -- bind.list port_present (Phase 5 Task 3, docs/phase5-notes.md cheap-wins
# bundle: "annotate bind.list with port-present status") -----------------------

async def test_bind_list_port_present_true_with_no_provider_wired():
    """Unwired (the default for every bare `Engine()` in this whole test
    suite, and for a real `--no-midi` daemon) means "unknown", not
    "absent" -- see `set_open_ports_provider`'s own docstring."""
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is True


async def test_bind_list_port_present_true_for_any_port_binding_even_with_no_matching_open_port():
    eng = Engine(Config())
    eng.set_open_ports_provider(lambda: ["USB MIDI Interface 20:0"])
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern=None),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is True


async def test_bind_list_port_present_true_when_an_open_port_matches_the_pattern():
    eng = Engine(Config())
    eng.set_open_ports_provider(
        lambda: ["Midi Through:Midi Through Port-0 14:0", "USB MIDI Interface 20:0"])
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is True


async def test_bind_list_port_present_false_when_no_open_port_matches_the_pattern():
    eng = Engine(Config())
    eng.set_open_ports_provider(lambda: ["USB MIDI Interface 20:0"])
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is False


async def test_bind_list_port_present_false_when_provider_reports_no_ports_open_at_all():
    eng = Engine(Config())
    eng.set_open_ports_provider(list)
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is False


async def test_bind_list_port_present_true_when_provider_itself_raises():
    """Defensive-only: `MidiInput.open_ports` should never raise, but a
    diagnostic-only field must not be able to crash `bind.list` if a
    future provider implementation ever does."""
    def _boom():
        raise RuntimeError("boom")

    eng = Engine(Config())
    eng.set_open_ports_provider(_boom)
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["port_present"] is True


# -- bind.list device_present (Phase 9 Task 1, device-identity bindings) -----
# Mirrors the port_present suite right above exactly -- same PULL-seam
# shape (`set_open_device_ids_provider`), same "unknown means present,
# only a genuine known-absent reports False" precedent.

async def test_bind_list_device_present_true_with_no_provider_wired():
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["device_present"] is True


async def test_bind_list_device_present_true_when_device_is_none():
    """No device identity at all -- trivially "present", same "no
    constraint to be missing" reasoning as port_pattern=None."""
    eng = Engine(Config())
    eng.set_open_device_ids_provider(list)
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(type="note_on", number=60, device=None),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["device_present"] is True


async def test_bind_list_device_present_true_when_an_open_device_matches():
    eng = Engine(Config())
    eng.set_open_device_ids_provider(lambda: ["usb:1234:5678:SN1", "virt:Midi Through"])
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["device_present"] is True


async def test_bind_list_device_present_false_when_no_open_device_matches():
    eng = Engine(Config())
    eng.set_open_device_ids_provider(lambda: ["virt:Midi Through"])
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["device_present"] is False


async def test_bind_list_device_present_true_when_provider_itself_raises():
    def _boom():
        raise RuntimeError("boom")

    eng = Engine(Config())
    eng.set_open_device_ids_provider(_boom)
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["device_present"] is True


async def test_bind_list_serializes_the_device_field_in_match():
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    result = await eng.actions.dispatch("bind.list", {})
    assert result["bindings"][0]["match"]["device"] == "usb:1234:5678:SN1"


# -- device identity is PRIMARY over port_pattern at real dispatch time -------
# (Phase 9 Task 1) -- the actual "differentiate different devices, recognize
# the same device even in a different port" capability, proven at the full
# Engine._handle/_dispatch_bindings level (test_bindings.py already proves
# the pure BindingDispatcher._matches unit in isolation).

async def test_same_device_different_port_still_fires():
    """The headline capability: a binding learned/bound against one
    device_id fires for an event carrying an entirely DIFFERENT `source`
    string (simulating the same physical device replugged into a
    different USB port) as long as the resolved device_id is unchanged."""
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Port A*",
            device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    eng._handle(ev(type="note_on", data1=60, data2=100,
                   source="Port B:Totally Different Name 30:1",
                   device_id="usb:1234:5678:SN1"))
    await eng._dispatch_bindings()
    assert eng.current_page == "voices"


async def test_distinct_serials_do_not_cross_fire():
    """Two identical-model devices WITH serials must stay disambiguated
    -- an event from the second device must not fire a binding learned
    against the first, even though both would match the same
    port_pattern."""
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="USB Midi*",
            device="usb:1234:5678:SN1"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    eng._handle(ev(type="note_on", data1=60, data2=100,
                   source="USB Midi:USB Midi MIDI 1 21:0",
                   device_id="usb:1234:5678:SN2"))
    await eng._dispatch_bindings()
    assert eng.current_page == "eventlog"   # did NOT fire


async def test_no_serial_same_model_still_cross_fires_documented_honestly():
    """Pinned, not silently fixed (docs/phase4-bindings.md /
    docs/phase5-capture.md §7): two serial-less units of the same model
    resolve to the identical device_id, so a binding learned against one
    STILL fires for the other."""
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, device="usb:1234:5678"),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    eng._handle(ev(type="note_on", data1=60, data2=100, source="second unit, different port",
                   device_id="usb:1234:5678"))
    await eng._dispatch_bindings()
    assert eng.current_page == "voices"   # collided, as documented


async def test_migration_pattern_only_binding_still_fires_via_port_pattern():
    """A binding with `device=None` (every binding persisted before this
    task) must keep matching purely on port_pattern -- even against a
    real, fully-upgraded event that itself DOES carry a resolved
    device_id (the old binding simply never asked for one)."""
    eng = Engine(Config())
    eng._bindings_file.add(_binding_module().Binding(
        id="b1", match=_binding_module().BindingMatch(
            type="note_on", number=60, port_pattern="Midi Through*", device=None),
        action="page.next"))
    eng._binding_dispatcher.set_bindings(eng._bindings_file.bindings)
    eng._handle(ev(type="note_on", data1=60, data2=100,
                   source="Midi Through:Midi Through Port-0 14:0",
                   device_id="virt:Midi Through:Midi Through Port-0"))
    await eng._dispatch_bindings()
    assert eng.current_page == "voices"


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
    assert actions["bind.learn"]["args"] == {
        "action": "str", "mode": "str", "args": "dict", "range": "str"}
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


# -- bind.learn: `range` (Phase 5 Task 3, docs/phase5-notes.md cheap-wins
# bundle: "CLI --range lo,hi for continuous learn -- stuck at [0,1] today") --

async def test_bind_learn_continuous_defaults_to_zero_one_range_when_omitted():
    eng = Engine(Config())
    await eng.actions.dispatch(
        "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {}})
    assert eng._learn_armed.range == (0.0, 1.0)


async def test_bind_learn_continuous_parses_a_custom_range():
    eng = Engine(Config())
    await eng.actions.dispatch(
        "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {},
                       "range": "0.25,4.0"})
    assert eng._learn_armed.range == (0.25, 4.0)


async def test_bind_learn_continuous_range_can_be_inverted():
    eng = Engine(Config())
    await eng.actions.dispatch(
        "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {},
                       "range": "1.0,0.0"})
    assert eng._learn_armed.range == (1.0, 0.0)


async def test_bind_learn_range_is_ignored_for_trigger_mode():
    eng = Engine(Config())
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {},
                       "range": "0.25,4.0"})
    assert eng._learn_armed.range == (0.0, 1.0)   # unused by trigger mode; unaffected


async def test_bind_learn_rejects_a_malformed_range_with_clean_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="range"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {},
                           "range": "not-a-range"})
    assert eng._learn_armed is None


async def test_bind_learn_rejects_a_range_with_the_wrong_number_of_parts():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="range"):
        await eng.actions.dispatch(
            "bind.learn", {"action": "pianoroll.zoom_level", "mode": "continuous", "args": {},
                           "range": "0.25,4.0,8.0"})
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
    # Durable, suffix-globbed pattern (Phase 5 Task 3 review fix, docs/
    # phase5-notes.md) -- NOT the raw verbatim source string anymore: the
    # trailing ALSA "14:0" client:port suffix is stripped and replaced with
    # a trailing "*" so the binding survives that suffix renumbering (see
    # bindings.py::glob_port_pattern's own docstring). It still matches the
    # exact capturing source string, proven right below.
    assert b.match.port_pattern == "Midi Through:Midi Through Port-0*"
    assert fnmatch.fnmatch("Midi Through:Midi Through Port-0 14:0", b.match.port_pattern)

    from midicrt.engine.bindings import BindingsFile
    assert BindingsFile.load(str(p)).bindings == [b]   # persisted atomically

    bound_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert bound_events
    assert bound_events[-1]["data"]["binding"]["id"] == b.id
    assert bound_events[-1]["data"]["binding"]["action"] == "page.next"
    assert bound_events[-1]["data"]["binding"]["valid"] is True


async def test_learn_capture_still_fires_after_the_port_re_enumerates(tmp_path):
    """The actual durability proof (Phase 5 Task 3, docs/phase5-notes.md):
    ALSA renumbers a port's `<client>:<port>` suffix across a reboot/
    replug/rtpmidid session restart -- simulated here by feeding a SECOND,
    real dispatch event whose `source` carries a DIFFERENT trailing suffix
    than the one the binding was learned against. A pre-fix exact-string
    `port_pattern` would never match this second event at all (the
    physical/logical port is the same, but the string isn't); the durable
    glob pattern (`bindings_mod.glob_port_pattern`) must still fire."""
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100,
                           source="Midi Through:Midi Through Port-0 14:0"))
    await asyncio.sleep(0.05)
    assert eng.current_page == "eventlog"   # capturing event itself is consumed, not dispatched

    # Same physical note, but ALSA re-enumerated the port to a DIFFERENT
    # client:port pair (e.g. after a reboot) -- the suffix changed, the
    # rest of the port name did not.
    await eng.queue.put(ev(type="note_on", data1=60, data2=100,
                           source="Midi Through:Midi Through Port-0 23:1"))
    await asyncio.sleep(0.05)
    eng.stop()
    await task
    assert eng.current_page == "voices"   # fired despite the renumbered suffix


async def test_learn_capture_records_the_capturing_events_device_id(tmp_path):
    """Phase 9 Task 1 (device-identity bindings): `_capture_learn` must
    stamp `match.device` from the capturing event's own `MidiEvent.
    device_id` -- `port_pattern` is STILL populated too (documented
    fallback, see BindingMatch's own docstring), not replaced."""
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100,
                           source="USB Midi:USB Midi MIDI 1 20:0",
                           device_id="usb:1234:5678:SN1"))
    await asyncio.sleep(0.1)
    eng.stop()
    await task

    b = eng._bindings_file.bindings[0]
    assert b.match.device == "usb:1234:5678:SN1"
    assert b.match.port_pattern == "USB Midi:USB Midi MIDI 1*"

    from midicrt.engine.bindings import BindingsFile
    reloaded = BindingsFile.load(str(p)).bindings[0]
    assert reloaded.match.device == "usb:1234:5678:SN1"   # persisted, round-trips


async def test_learn_capture_with_no_device_id_leaves_match_device_none(tmp_path):
    """Backward-compat: a capturing event with no resolved identity at
    all (`device_id=None` -- the default for every MidiEvent built
    without an identity resolver behind it, matching this whole test
    suite's overwhelming default) must not invent a device string."""
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))   # device_id defaults to None
    await asyncio.sleep(0.1)
    eng.stop()
    await task
    assert eng._bindings_file.bindings[0].match.device is None


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


async def test_learn_capture_relearning_a_pre_device_binding_replaces_it_not_duplicates(tmp_path):
    """THE Critical review reproduction (live-reproduced): before the
    `should_replace_on_relearn` fix, `BindingMatch.device` joining plain
    dataclass `==` meant a fresh, device-stamped relearn capture could
    NEVER equal an existing device=None binding on the identical physical
    control -- both stayed on disk, both firing, silently accumulating
    forever (exactly the bug class the ORIGINAL Phase-4 replace-on-relearn
    fix eliminated, reopened by a different field). Proof mirrors
    test_learn_capture_relearning_the_same_exact_match_replaces_the_old_
    binding right above almost exactly -- the only difference is the
    FIRST capture's event carries no device_id at all (simulating either
    a binding learned before this task, or one captured during a
    transient identity-resolution outage), while the SECOND (relearn)
    capture's event DOES."""
    p = tmp_path / "bindings.toml"
    eng = Engine(Config(tick_hz=200.0), bindings_path=str(p))
    got = []
    eng.add_listener(got.append)
    task = asyncio.create_task(eng.run())
    note60_no_identity = ev(type="note_on", data1=60, data2=100, channel=0,
                            source="Midi Through:Midi Through Port-0 14:0", device_id=None)
    note60_with_identity = ev(type="note_on", data1=60, data2=100, channel=0,
                              source="Midi Through:Midi Through Port-0 23:1",
                              device_id="virt:Midi Through:Midi Through Port-0")

    # First learn: captured with NO resolved device identity -- the
    # "pre-device" (or transient-outage) binding.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.next", "mode": "trigger", "args": {}})
    await eng.queue.put(note60_no_identity)
    await asyncio.sleep(0.05)
    assert len(eng._bindings_file.bindings) == 1
    assert eng._bindings_file.bindings[0].match.device is None

    # Relearn the SAME physical control -- this time identity resolves.
    await eng.actions.dispatch(
        "bind.learn", {"action": "page.prev", "mode": "trigger", "args": {}})
    await eng.queue.put(note60_with_identity)
    await asyncio.sleep(0.05)
    eng.stop()
    await task

    # Exactly ONE binding remains -- the new, device-stamped one.
    assert len(eng._bindings_file.bindings) == 1
    only = eng._bindings_file.bindings[0]
    assert only.action == "page.prev"
    assert only.match.device == "virt:Midi Through:Midi Through Port-0"

    from midicrt.engine.bindings import BindingsFile
    assert BindingsFile.load(str(p)).bindings == [only]   # persisted atomically, one binding

    bound_events = [m for m in got if m.get("kind") == "event" and m.get("name") == "learn_bound"]
    assert len(bound_events) == 2
    assert bound_events[0]["data"]["replaced"] == []              # nothing to replace the first time
    assert len(bound_events[1]["data"]["replaced"]) == 1          # the pre-device binding WAS replaced
    assert bound_events[1]["data"]["replaced"][0]["action"] == "page.next"


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


# -- shutdown-time capture-stop failure must not abort shutdown ordering
# (fix wave: Important finding) ---------------------------------------------
#
# Bug: `Engine.stop()` calls `_capture_stop_action(origin="shutdown")`
# completely unguarded. `_capture_stop_action` already contains an `OSError`
# from `CaptureSink.stop()` (via `_capture_write_failed` -- rec flag off,
# alert emitted) but then RE-RAISES it as `ActionError` for a normal
# dispatch caller. `Engine.stop()` is not a dispatch caller -- it calls the
# handler directly -- so that re-raise used to escape `stop()` entirely,
# skipping the sendnotes `flush_all()` note-off flush AND `_midi_out.close()`
# right below it: the exact phase-3 "stuck notes on real hardware" bug
# class, now reachable again via a full disk at shutdown time instead of a
# missing flush call.

async def test_engine_stop_completes_and_flushes_notes_when_shutdown_capture_stop_fails(
        monkeypatch):
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("sendnotes.key", {"key": "z"})   # gates a real note
    assert eng._capture.is_recording is True

    def broken_stop():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(eng._capture, "stop", broken_stop)

    eng.stop()   # must NOT raise -- containment already happened inside

    # Shutdown ordering completed past the failed capture-stop: the gated
    # note got its note_off, and midi_out was closed.
    assert fake.note_off_calls == [(60, 1)]
    assert fake.closed is True
    # And capture itself ended up cleanly disabled, not stuck "recording".
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False


async def test_engine_stop_completes_when_shutdown_capture_stop_raises_a_bare_value_error(
        monkeypatch):
    """Phase 9 close-out fix wave (controller ruling): `Engine.stop()`'s
    own `suppress` widened from `(ActionError,)` to `(ActionError,
    ValueError, OSError)` -- a concurrent `capture.stop` DISPATCH (async,
    `asyncio.to_thread`-offloaded, Task 6 second review round) racing this
    SIGTERM-driven direct call reaches the SAME `CaptureSink.stop()` body;
    a race there could in principle raise something `_capture_stop_
    action`'s own narrow `except OSError` never catches at all (unlike
    the pre-existing OSError-only containment test right above this
    file's own `test_engine_stop_completes_and_flushes_notes_when_
    shutdown_capture_stop_fails`) -- a bare `ValueError` escapes THAT
    method's try/except entirely and used to escape `Engine.stop()` too.
    Simulated directly (CaptureSink.stop monkeypatched to raise
    ValueError) since forcing a genuine two-thread race to land on this
    EXACT exception type deterministically isn't practical; the actual
    concurrency proof is test_capture.py's own `test_concurrent_capture_
    stop_calls_are_serialized_by_the_lifecycle_lock`."""
    eng = Engine(Config())
    fake = _FakeMidiOut()
    eng._midi_out = fake
    await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("sendnotes.key", {"key": "z"})   # gates a real note
    assert eng._capture.is_recording is True

    def broken_stop():
        raise ValueError("I/O operation on closed file.")

    monkeypatch.setattr(eng._capture, "stop", broken_stop)

    eng.stop()   # must NOT raise -- the widened suppress catches it

    # Shutdown ordering completed past the failed capture-stop: the gated
    # note got its note_off, and midi_out was closed -- identical proof to
    # the OSError-specific test above, now for a ValueError too.
    assert fake.note_off_calls == [(60, 1)]
    assert fake.closed is True


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

    # 2. behavior origin -- force a huge elapsed-interval gap so
    # PageCycleBehavior fires. Two calls needed (same shape as
    # test_tick_behaviors_pagecycle_dispatch_is_observable_via_a_spy
    # above): the FIRST tick only bootstraps `_last_switch` (a freshly
    # constructed behavior always treats its very first tick as "nothing
    # to measure elapsed time from yet", see behaviors/pagecycle.py's own
    # `tick()` docstring); the SECOND tick, far enough past
    # `pagecycle_interval`, actually fires `page.goto` (Phase 8 Task 5:
    # never `page.next` anymore -- see that module's docstring). No real
    # wall-clock wait needed -- `now` is injected, same "inject now"
    # precedent every other behavior/analyzer tick test uses.
    eng._last_activity_ts = 0.0
    await eng._tick_behaviors(now=0.0)
    await eng._tick_behaviors(now=eng.config.pagecycle_interval + 100.0)

    # 3. client origin -- exactly what engine/server.py's own action
    # dispatch branch does.
    await eng.actions.dispatch("page.prev", {}, origin="client")

    # 4. sysex origin -- CMD_SWITCH_PAGE to page id 13 ("voices"), version
    # None so no real MIDI reply send is attempted.
    eng._sysex_switch_page(version=None, args=(13,), ts=0.0)

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


async def test_flush_value_error_does_not_kill_the_run_loop_either(tmp_path, caplog):
    """THIRD review round (Critical finding) belt-and-suspenders: the SAME
    proof as `test_flush_write_failure_does_not_kill_the_run_loop_and_
    disables_capture` immediately above, but for `ValueError` instead of
    `OSError` -- `_tick_capture_flush`'s `except (OSError, ValueError)`
    addition exists specifically because a closed-file `.write()` raises
    `ValueError`, not `OSError` (see `CaptureSink.stop()`'s own docstring
    for the real incident this defends against; the REAL fix is that
    method's own state-reset reordering, this is the independent backstop
    layer). Proven directly here by forcing `maybe_flush` to raise
    `ValueError`, rather than trying to reproduce the exact closed-file
    race a second time at the Engine level (already proven at the
    CaptureSink level in test_capture.py)."""
    eng = Engine(Config(tick_hz=200.0))
    eng._capture_start_action()
    eng._capture._flush_interval_s = 0.01

    def failing_flush():
        raise ValueError("I/O operation on closed file.")

    real_flush = eng._capture.flush
    eng._capture.flush = failing_flush

    got = []
    eng.add_listener(got.append)

    task = asyncio.create_task(eng.run())
    await eng.queue.put(ev(type="note_on", data1=60, data2=100))
    with caplog.at_level("ERROR"):
        await asyncio.sleep(0.15)   # long enough for maybe_flush to fire and raise

    # The run() loop must still be ALIVE -- a subsequent event is still
    # processed, proving a ValueError from the write path never escapes
    # `_tick_capture_flush` and kills the task the way it used to.
    events_before = eng.events_total
    await eng.queue.put(ev(type="note_on", data1=61, data2=100))
    await asyncio.sleep(0.05)
    assert eng.events_total == events_before + 1
    assert not task.done()

    # Same containment outcome as the OSError case: capture disabled
    # cleanly, one alert, one error log.
    assert eng._capture.is_recording is False
    alerts = [m for m in got if m.get("kind") == "event" and m.get("name") == "alert"]
    assert len(alerts) == 1
    assert "write failed" in alerts[0]["data"]["message"]

    eng._capture.flush = real_flush
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


# -- fix wave (2026-08-07, Important finding): a replaying engine must
# never honor `config.capture_auto_start` -- an offline engine has no real
# I/O of its own, but `_capture_start_action()` would still sweep retention
# against the REAL sessions directory (a config error could delete the very
# session file being replayed) and open a brand-new REAL capture session
# that re-records the replayed stream. Gated the same way as this section's
# own `_handle` seam: `Engine.__init__`'s auto-start branch now checks
# `not self._replay` too.

def test_capture_auto_start_is_suppressed_on_a_replaying_engine(monkeypatch):
    swept = []
    monkeypatch.setattr(capture_mod.CaptureSink, "_sweep_retention",
                        lambda self: swept.append(1))
    eng = Engine(Config(capture_auto_start=True), replay=True)
    assert eng._capture.is_recording is False
    assert eng.analyzers["status"].view_model()["rec"] is False
    assert swept == []   # no retention sweep either -- start() never ran


def test_capture_auto_start_still_works_on_a_normal_non_replay_engine_control():
    # Control for the test above: proves the config flag is still real and
    # the gate is scoped to replay specifically, not a blanket regression.
    eng = Engine(Config(capture_auto_start=True))
    assert eng._capture.is_recording is True


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
