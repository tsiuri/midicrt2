"""Engine: owns MIDI event flow, pages, actions; publishes snapshots (spec §2).

Page roster
-----------
`Engine.pages` is an ordered dict (`name -> page instance`) built from
`config.pages` (default `["eventlog", "voices"]` as of phase-3 task 4),
filtered against `_PAGE_FACTORIES`
-- the module-level registry of known page constructors. Dict insertion
order is preserved and IS the cycle order used by the `page.next`/
`page.prev` actions. Later phase-3 tasks add real pages by adding an entry
to `_PAGE_FACTORIES`; tests (or, later, dynamically-arriving overlays) that
need a page with no production factory yet can call `Engine.register_page()`
to append one directly to the live roster.

Dirty tracking
--------------
Each page's `handle(ev)` returns `True` when the event changed that page's
state (falsy means "no-op for this page", e.g. a future page that filters
by channel). `Engine._handle` marks `page.<name>` dirty for every page that
reports True, not just the current page -- this was the phase-2 latent bug:
`_handle` used to mark only `page.<current_page>` dirty even though every
page consumed every event, so a client subscribed to a non-current page's
topic would never see it update. Chosen over an injected `mark_dirty()`
callback because it keeps pages pure functions of (state, event) -> bool,
with no back-reference to the engine.

Event topics
------------
`Engine.topics` is the single source of truth for "all subscribable
topics" -- `page.<name>` for each page roster entry AND `overlay.<name>`
for each analyzer (phase-3 task 3 is the first of the latter) -- `describe`
reports it verbatim so it can never drift from what `snapshot_now` can
actually resolve.

Analyzers (phase-3 task 3)
--------------------------
`Engine.analyzers` mirrors `Engine.pages`: an ordered dict (`name ->
analyzer instance`) built from `_ANALYZER_FACTORIES`, published under
`overlay.<name>` topics instead of `page.<name>`. Unlike pages, analyzers
are not config-gated (no `config.overlays` list exists yet) -- every
registered analyzer is always live, since there is currently exactly one
(`"status"` -> `TransportAnalyzer`) and it is meant to be visible
regardless of which page is current (a status bar, not a page). Analyzers
are wired BEFORE pages in `__init__`/`_handle` on the (currently latent,
future-proofing) assumption that a page's own view_model could one day
read an analyzer's derived state within the same event tick; today the two
sets are independent and order has no observable effect.
`Engine.register_analyzer()` mirrors `register_page()` for the same
test/dynamically-arriving-overlay reasons.

Analyzer wall-clock tick + alert events (phase-3 task 6)
--------------------------------------------------------
Every analyzer to date derives all timing from `MidiEvent.ts` deltas alone
-- but `analyzers/stucknotes.py`'s escalation (a note going "stuck" after
N seconds with no note-off) can cross a threshold with NO new MIDI event
at all, and a pure `handle(ev)` can only ever react to events. `run()`
therefore calls `_tick_analyzers(now)` once per `tick_hz` period (using
`time.time()` -- the SAME clock domain as `MidiEvent.ts`, see
`engine/midi_in.py`), which: (1) calls `analyzer.tick(now)` for any
analyzer exposing that OPTIONAL method (duck-typed via `hasattr`, not
added to the `Analyzer` Protocol itself since most analyzers don't need
it -- `tick` marks its own topic dirty exactly like `handle()`'s bool
convention), and (2) drains any analyzer exposing an OPTIONAL
`drain_alerts() -> list[dict]` and turns each drained dict into its own
`emit_event("alert", ...)` -- reusing task-1's event-broadcast path
(`ProtocolServer._on_engine_message`'s slow-client high-water check
applies here unchanged, no new backpressure code needed). Analyzers
themselves stay "no I/O": `now` is always INJECTED by this method, never
read by an analyzer internally -- see analyzers/stucknotes.py's own
docstring for the full rationale.

Page wall-clock tick (phase-3 task 7)
--------------------------------------
`_tick_pages(now)` is the same mechanism, one level up: `pages/pianoroll.py`'s
scrolling roll must keep advancing purely from wall-clock progress (a
sustained chord sliding toward the window's edge) even with zero new MIDI
events, which no page previously needed (every other page is fully
event-driven). `run()` calls it right after `_tick_analyzers(now)`, same
injected `now`, same duck-typed OPTIONAL `tick(now) -> bool` convention,
minus `drain_alerts()` (analyzer-only, no page needs it).

Behaviors + activity tracking (phase-3 task 9)
------------------------------------------------
`behaviors/pagecycle.py` and `behaviors/screensaver.py` are a THIRD
roster kind, structurally different from analyzers/pages: a behavior
publishes no topic and holds no view model at all -- it is a pure idle-
timer state machine whose `tick(now, last_activity_ts, current_page) ->
(action_name, args) | None` either does nothing or returns ONE action
intent for the engine to dispatch (see `behaviors/__init__.py`'s module
docstring for why this is stricter than "no I/O": a behavior cannot even
call `dispatch` itself, only ask the engine to). `_tick_behaviors(now)`
(called from `run()` right after `_tick_pages(now)`) is the only place
that ever turns one of those intents into a real `await
self.actions.dispatch(...)` call -- an `ActionError` there (e.g. a custom
config removed the "screensaver" page a behavior tries to `page.goto`) is
swallowed rather than propagated, since an internal, unattended behavior
tick crashing the whole `run()` loop would be far worse than one skipped
auto-action.

Both behaviors need "has anything happened recently" as their idle
reference point, which -- like `_tick_analyzers`'s `now` -- only the
engine is positioned to track: `_handle(ev)` stamps `self._last_activity_ts
= ev.ts` for `note_on`/`note_off`/`control_change` events ONLY, mirroring
v1's own `plugins/zscreensaver.py::handle()` activity filter exactly (a
running MIDI clock alone must not count as "activity", or a transport left
running with nobody playing would never idle out -- see behaviors/
screensaver.py's module docstring). `self._last_activity_ts` is seeded to
`time.time()` at construction (NOT epoch 0), so a freshly booted engine
starts its idle clocks from boot time rather than looking infinitely idle
on its very first tick.

`pagecycle_idle_s` and `screensaver_after_s` are two INDEPENDENT clocks
measured off this SAME `_last_activity_ts` -- with `Config()`'s own
shipped defaults (300s/60s, both enabled) a fully idle engine crosses BOTH
thresholds, not just the first one reached. `behaviors/pagecycle.py`'s own
`tick()` is what prevents its later threshold from un-blanking the
screensaver (see that module's "Arbitration with the screensaver" docstring
section for the full failure mode a reviewer once reproduced here, and the
fix) -- `Engine.__init__`'s own comment next to `self._behaviors` restates
this at the wiring site.
"""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from midicrt import proto
from midicrt.analyzers.beatflash import BeatFlashAnalyzer
from midicrt.analyzers.loopprogress import LoopProgressAnalyzer
from midicrt.analyzers.stucknotes import StuckNotesAnalyzer
from midicrt.analyzers.timesig import TimesigAnalyzer
from midicrt.analyzers.transport import TransportAnalyzer
from midicrt.behaviors.pagecycle import PageCycleBehavior
from midicrt.behaviors.screensaver import ScreensaverBehavior
from midicrt.config import Config
from midicrt.engine.actions import ActionError, ActionRegistry
from midicrt.pages.ccdashboard import CCDashboardPage
from midicrt.pages.ccmonitor import CCMonitorPage
from midicrt.pages.chordkey import ChordKeyPage
from midicrt.pages.configview import ConfigPage
from midicrt.pages.eventlog import EventLogPage
from midicrt.pages.harmony import HarmonyPage
from midicrt.pages.help import HelpPage
from midicrt.pages.img2txtviz import Img2TxtVizPage
from midicrt.pages.pianoroll import PianorollPage
from midicrt.pages.progchanges import ProgChangesPage
from midicrt.pages.screensaver import ScreensaverPage
from midicrt.pages.spectrum import SpectrumPage
from midicrt.pages.tuner import TunerPage
from midicrt.pages.voices import VoicesPage

# `_handle()` stamps `Engine._last_activity_ts` only for these event types --
# matches v1's `plugins/zscreensaver.py::handle()` activity filter exactly
# (see the module docstring's "Behaviors + activity tracking" section and
# behaviors/screensaver.py's own docstring for why a free-running MIDI
# clock must NOT count as activity).
_ACTIVITY_EVENT_TYPES = {"note_on", "note_off", "control_change"}


@dataclass
class MidiEvent:
    ts: float
    source: str
    type: str
    channel: int | None
    data1: int | None
    data2: int | None
    summary: str
    # `type == "clock_tick"` only: timestamp of the PREVIOUS 24-raw-clock
    # aggregation boundary (see engine/midi_in.py's module docstring for why
    # clock is batched instead of queued per-pulse). `ts - clock_batch_start`
    # spans exactly 24 clock pulses (one quarter note), letting
    # analyzers/transport.py derive bpm without any smoothing of its own.
    # None for every other event type, and for the first batch after a
    # start/stop/continue reset (no prior boundary exists yet).
    clock_batch_start: float | None = None


class Page(Protocol):
    def handle(self, ev: MidiEvent) -> bool: ...
    def view_model(self) -> dict: ...


class Analyzer(Protocol):
    """Same shape as `Page` (see analyzers/__init__.py) -- kept as a
    separate Protocol rather than reusing `Page` so the two roster kinds
    stay independently evolvable even though today they're structurally
    identical."""

    def handle(self, ev: MidiEvent) -> bool: ...
    def view_model(self) -> dict: ...


PageFactory = Callable[[Config], Page]
AnalyzerFactory = Callable[[Config], Analyzer]

# Known production pages, keyed by the name used in config.pages / topics.
# Add an entry here as each phase-3 parity page lands.
_PAGE_FACTORIES: dict[str, PageFactory] = {
    "eventlog": lambda config: EventLogPage(capacity=config.eventlog_capacity),
    # Phase-3 task 4: v1's main screen, the first second page -- see
    # pages/voices.py + analyzers/voices.py. `config.pages` now defaults to
    # ["eventlog", "voices"] (config.py) so it's live without a config.toml.
    "voices": lambda config: VoicesPage(instruments=config.instruments),
    # Phase-3 task 5: v1's Notes-page harmony fields (chord/scale/key/
    # tension/harmonic-rhythm/motif) -- see pages/harmony.py +
    # analyzers/harmony.py. `config.pages` now defaults to
    # ["eventlog", "voices", "harmony"] (config.py).
    "harmony": lambda config: HarmonyPage(),
    # Phase-3 task 6: v1's audio tuner page (pages/tuner.py) -- see
    # pages/tuner.py's + analyzers/tuner.py's module docstrings for why
    # this is registered here (reachable via config.toml/register_page)
    # but deliberately NOT in config.py's default `pages` list: it can
    # only ever show v1's idle state until a separate, not-yet-built audio-
    # capture task feeds it real pitch samples.
    "tuner": lambda config: TunerPage(),
    # Phase-3 task 7: v1's flagship two-projection-mode scrolling note
    # display -- see pages/pianoroll.py's module docstring for the full
    # port. `config.pages` now defaults to [..., "pianoroll"] (config.py):
    # unlike "tuner", it shows real data with just a running daemon + MIDI
    # input, matching the "voices"/"harmony" precedent for default-roster
    # inclusion.
    "pianoroll": lambda config: PianorollPage(),
    # Phase-3 task 8: v1's audio spectrum analyzer (pages/audiospectrum.py)
    # -- see pages/spectrum.py's + analyzers/spectrum.py's module
    # docstrings for the full port (real USB audio hardware found on the
    # Pi, v1's log-band FFT mapping, the v2-only peak-hold addition).
    # `config.pages` now defaults to [..., "spectrum"] (config.py): unlike
    # "tuner", this page degrades gracefully (`available: false` -> "no
    # audio input" placeholder) instead of being permanently idle, so it
    # joins the default roster despite also depending on audio hardware.
    # The actual capture thread is NOT started here -- `daemon.py`'s
    # `run()` calls `SpectrumPage.start_capture()` explicitly, after the
    # roster is built, guarded by a `--no-audio` opt-out mirroring
    # `--no-midi`.
    "spectrum": lambda config: SpectrumPage(device=config.audio_device, bins=config.spectrum_bins),
    # Phase-3 task 9: the page `behaviors/screensaver.py` switches to via
    # `page.goto screensaver` -- see pages/screensaver.py's module
    # docstring for the v1 comparison. In `config.pages`' default list
    # (config.py) so it is already instantiated (and therefore goto-able)
    # on a stock deploy, matching v1's screensaver being always-live too.
    "screensaver": lambda config: ScreensaverPage(),
    # Phase-3 task 10: v1's real-time MIDI-reactive ASCII generator
    # (pages/img2txtviz.py, "MIDI IMG2TXT") -- see that module's docstring
    # for the investigation (no image-bank loading in v1 despite the name)
    # and the disclosed wave-field adaptation. `config.pages` now defaults
    # to [..., "img2txtviz"] (config.py): a self-contained animation with
    # no unbuilt dependency, matching the "voices"/"harmony"/"pianoroll"/
    # "spectrum" precedent, not "tuner"'s.
    "img2txtviz": lambda config: Img2TxtVizPage(),
    # Phase-3 task 10: spec §5's read-only config viewer -- see
    # pages/configview.py's module docstring. `config.pages` now defaults
    # to [..., "config"] (config.py, the task dispatch's explicit ask).
    # Constructed here from `config` alone like every other factory;
    # `Engine.__init__` wires its LIVE engine-info callback separately
    # right after `self.pages` is built (see the "config page" section
    # below) since no page factory here has access to the `Engine` being
    # built yet.
    "config": lambda config: ConfigPage(config),
    # Phase-3 task 12 (gap ports, v1 page 0 "Help / Keys"): see
    # pages/help.py's own module docstring for why this is a
    # v2-appropriate-equivalent (describe-data reference), not a literal
    # port of v1's static keybinding list. `config.pages` now defaults to
    # [..., "help"] (config.py): a self-contained page with no unbuilt
    # dependency, matching the "img2txtviz"/"config" precedent.
    "help": lambda config: HelpPage(),
    # Phase-3 task 12 (gap ports, v1 page 7 "Program Changes"): see
    # pages/progchanges.py's own module docstring for the full v1
    # (`pages/proglog.py`) behavioral synthesis -- a rolling program-change
    # log reusing eventlog's own `{title, count, lines}` VM shape.
    "progchanges": lambda config: ProgChangesPage(),
    # Phase-3 task 12 (gap ports, v1 pages 4 "CC Monitor" + 5 "CC
    # Dashboard"): both wrap analyzers/ccmonitor.py::CCMonitorAnalyzer, each
    # a SEPARATE instance (see pages/ccmonitor.py's own module docstring
    # for why -- no cross-page analyzer-sharing infrastructure exists yet,
    # same precedent as pages/chordkey.py below).
    "ccmonitor": lambda config: CCMonitorPage(),
    "ccdashboard": lambda config: CCDashboardPage(),
    # Phase-3 task 12 (gap ports, v1 page 11 "Chord+Key"): a second,
    # compact chord/key display distinct from "harmony" (v1 page 1's own
    # port, Task 5) -- see pages/chordkey.py's own module docstring for
    # what's genuinely new here vs already covered by "harmony". Owns its
    # OWN HarmonyAnalyzer instance, same "no cross-page analyzer sharing
    # yet" precedent as "ccmonitor"/"ccdashboard" above.
    "chordkey": lambda config: ChordKeyPage(),
}

# Known production analyzers, keyed by the name used in the `overlay.<name>`
# topic. Unlike pages, not config-gated -- see the module docstring.
_ANALYZER_FACTORIES: dict[str, AnalyzerFactory] = {
    "status": lambda config: TransportAnalyzer(),
    # Phase-3 task 6: v1's plugins/zstucknotes.py (long-held-note alerts)
    # and plugins/ztimesig.py (time-signature estimate) -- both are v1
    # "always visible regardless of page" chrome-class features (see
    # analyzers/stucknotes.py's/analyzers/timesig.py's own module
    # docstrings for the v1 layout evidence), so both are registered here
    # as analyzers/overlays, not pages, matching "status"'s own precedent.
    "alerts": lambda config: StuckNotesAnalyzer(),
    "timesig": lambda config: TimesigAnalyzer(),
    # Phase-3 task 9: v1's plugins/beatflash.py (beat-synced flash pulse)
    # and plugins/loopprogress.py (8-bar cyclic position bar) -- both v1
    # "always visible regardless of page" chrome-class features (see
    # analyzers/beatflash.py's/analyzers/loopprogress.py's own module
    # docstrings for the v1 row-offset evidence), registered as overlays
    # here matching "status"'s own precedent.
    "beatflash": lambda config: BeatFlashAnalyzer(),
    "loopprogress": lambda config: LoopProgressAnalyzer(),
}


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.actions = ActionRegistry()
        self.analyzers: dict[str, Analyzer] = {
            name: factory(config) for name, factory in _ANALYZER_FACTORIES.items()
        }
        self.pages: dict[str, Page] = {
            name: _PAGE_FACTORIES[name](config)
            for name in config.pages
            if name in _PAGE_FACTORIES
        }
        self.current_page = next(iter(self.pages), "eventlog")
        # Phase-3 task 10: wire the config page's LIVE engine-info callback
        # (version/uptime/current_page/live pages+analyzers roster) --
        # see pages/configview.py's own "Engine-info wiring" docstring
        # section for why this is the one page that needs a reference back
        # into the engine at all. Guarded on presence like every other
        # page-specific wiring here (a custom config could drop "config"
        # from the roster entirely).
        if "config" in self.pages:
            self.pages["config"].bind_engine_info(self._config_engine_info)
        # Phase-3 task 12: same "engine facts no page can derive from its
        # own constructor args alone" wiring as the config page above --
        # see pages/help.py's own "Engine-info wiring" docstring section.
        if "help" in self.pages:
            self.pages["help"].bind_info(self._help_info)
        self.events_total = 0
        self.started_at = time.monotonic()
        self._listeners: list[Callable[[dict], None]] = []
        self._seq: dict[str, int] = {}
        self._dirty: set[str] = set()
        self._running = False
        # Phase-3 task 9: seeded to "now", NOT epoch 0 -- see the module
        # docstring's "Behaviors + activity tracking" section for why a
        # freshly booted engine must not look infinitely idle on its very
        # first `_tick_behaviors` call.
        self._last_activity_ts: float = time.time()
        self._pagecycle_behavior = PageCycleBehavior(
            enabled=config.pagecycle_enabled, idle_s=config.pagecycle_idle_s)
        self._screensaver_behavior = ScreensaverBehavior(
            enabled=config.screensaver_enabled, after_s=config.screensaver_after_s)
        # CORRECTED (task-9 review): an earlier version of this comment
        # claimed "order rarely matters under sane configs" because
        # screensaver_after_s=60 fires before pagecycle_idle_s=300 ever
        # could -- that reasoning was WRONG and a reviewer reproduced the
        # failure against the actual shipped defaults: pagecycle_idle_s and
        # screensaver_after_s are two INDEPENDENT clocks measured from the
        # SAME last_activity_ts, so a fully idle engine crosses 60s
        # (screensaver activates) and then, inevitably, ALSO crosses 300s
        # (pagecycle's own threshold) with no new activity in between --
        # order of activation is irrelevant to that. The actual fix lives
        # in `behaviors/pagecycle.py`'s `tick()` (see its own "Arbitration
        # with the screensaver" docstring section): it refuses to act at
        # all while `current_page` is the screensaver page, however that
        # page was reached. List order below is therefore now genuinely
        # inconsequential (pagecycle is a no-op whenever screensaver owns
        # the display, regardless of which behavior's `tick()` runs first
        # within a given `_tick_behaviors` call) -- verified by
        # `test_engine_core.py::
        # test_pagecycle_does_not_unblank_screensaver_with_shipped_defaults`,
        # which sweeps a fake clock from t=0 to t=900 against Config()'s own
        # shipped defaults (both behaviors enabled) and asserts the engine
        # reaches the screensaver at t=60 and STAYS there.
        #
        # 2nd review pass: that fix alone introduced a SIBLING gap -- a
        # MANUAL escape from the screensaver (no MIDI activity) left
        # pagecycle's own idle timer stale, firing `page.next` on the very
        # next tick and discarding the manual choice. Both
        # `behaviors/pagecycle.py` AND `behaviors/screensaver.py` now
        # re-arm their respective idle references to `now` when a block/
        # activation ends WITHOUT a real activity advance -- see each
        # module's own docstring ("Re-arming after a manual escape" /
        # "A manual override now buys a FRESH after_s grace period").
        self._behaviors: list = [self._pagecycle_behavior, self._screensaver_behavior]
        self.actions.register("eventlog.clear", self._clear_eventlog,
                              description="Clear the event log")
        self.actions.register("page.next", self._page_next,
                              description="Advance to the next page in the roster")
        self.actions.register("page.prev", self._page_prev,
                              description="Go back to the previous page in the roster")
        self.actions.register("page.goto", self._page_goto,
                              description="Jump to a named page", args={"name": "str"})
        # Phase-3 task 7: pianoroll-specific actions, mirroring the
        # "eventlog.clear" precedent above (a page-specific action
        # registered directly here, its handler reaching into
        # self.pages[...] and hand-marking that page's topic dirty since
        # these mutations happen outside the normal handle(ev) dirty-flow).
        # Guarded on the page actually being in the roster (like "tuner",
        # "pianoroll" is reachable via config.toml/register_page even when
        # not in the default roster) so dispatching these against a build
        # with pianoroll disabled fails with a clear "unknown action"
        # rather than a KeyError.
        if "pianoroll" in self.pages:
            self.actions.register("pianoroll.zoom", self._pianoroll_zoom,
                                  description="Adjust the pianoroll's zoom level",
                                  args={"delta": "float"})
            self.actions.register("pianoroll.projection", self._pianoroll_projection,
                                  description="Switch the pianoroll's projection mode "
                                              "(wallclock|tempo)",
                                  args={"mode": "str"})
            self.actions.register("pianoroll.channels", self._pianoroll_channels,
                                  description="Set the pianoroll's visible-channel filter "
                                              "(comma/range spec, empty = all)",
                                  args={"spec": "str"})
        # Phase-3 task 10: img2txtviz's runtime-adjustable controls (spec
        # §5) -- ported from v1's real keypress()-driven 'c' (charset
        # cycle) and 'i' (invert toggle), see pages/img2txtviz.py's own
        # module docstring. Same guarded-registration/hand-marked-dirty
        # shape as the pianoroll actions above.
        if "img2txtviz" in self.pages:
            self.actions.register("img2txtviz.charset", self._img2txtviz_charset,
                                  description="Cycle the img2txtviz ASCII charset")
            self.actions.register("img2txtviz.invert", self._img2txtviz_invert,
                                  description="Toggle the img2txtviz invert flag")

    def _clear_eventlog(self):
        self.pages["eventlog"].clear()
        self._dirty.add("page.eventlog")

    def _img2txtviz_charset(self) -> dict:
        charset = self.pages["img2txtviz"].cycle_charset()
        self._dirty.add("page.img2txtviz")
        return {"charset": charset}

    def _img2txtviz_invert(self) -> dict:
        invert = self.pages["img2txtviz"].toggle_invert()
        self._dirty.add("page.img2txtviz")
        return {"invert": invert}

    def _config_engine_info(self) -> dict:
        """Bound into `pages/configview.py`'s `ConfigPage` at construction
        (see `__init__` above) -- reuses `status()`'s already-computed
        version/uptime/current-page facts and layers the live roster names
        on top, rather than re-deriving any of it a second time."""
        status = self.status()
        return {
            "version": status["engine_version"],
            "proto_version": status["proto_version"],
            "uptime_s": status["uptime_s"],
            "current_page": status["page"],
            "pages": list(self.pages),
            "analyzers": list(self.analyzers),
        }

    def _help_info(self) -> dict:
        """Bound into `pages/help.py`'s `HelpPage` at construction (see
        `__init__` above) -- the live page roster (cycle order) plus the
        full action registry, exactly what `describe` already reports over
        the wire (see `engine/server.py`), just rendered on-screen."""
        return {"pages": list(self.pages), "actions": self.actions.describe()}

    def _pianoroll_zoom(self, delta: float) -> dict:
        zoom = self.pages["pianoroll"].zoom_by(delta)
        self._dirty.add("page.pianoroll")
        return {"zoom": zoom}

    def _pianoroll_projection(self, mode: str) -> dict:
        try:
            applied = self.pages["pianoroll"].set_projection(mode)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        self._dirty.add("page.pianoroll")
        return {"mode": applied}

    def _pianoroll_channels(self, spec: str) -> dict:
        try:
            channels = self.pages["pianoroll"].set_channels(spec)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        self._dirty.add("page.pianoroll")
        return {"channels": channels}

    def register_page(self, name: str, page: Page) -> None:
        """Append `page` to the live roster under `name`. Production pages
        register via `_PAGE_FACTORIES` + `config.pages`; this hook exists
        for pages with no factory yet (tests today; dynamically-arriving
        overlays later)."""
        self.pages[name] = page

    def register_analyzer(self, name: str, analyzer: Analyzer) -> None:
        """Append `analyzer` to the live roster under `name`, publishing it
        under `overlay.<name>`. Mirrors `register_page()` for the same
        no-production-factory-yet reasons (tests today)."""
        self.analyzers[name] = analyzer

    def _page_order(self) -> list[str]:
        return list(self.pages)

    def _set_current_page(self, name: str) -> dict:
        self.current_page = name
        self.emit_event("page_changed", {"page": name})
        return {"page": name}

    def _page_next(self) -> dict:
        order = self._page_order()
        idx = order.index(self.current_page)
        return self._set_current_page(order[(idx + 1) % len(order)])

    def _page_prev(self) -> dict:
        order = self._page_order()
        idx = order.index(self.current_page)
        return self._set_current_page(order[(idx - 1) % len(order)])

    def _page_goto(self, name: str) -> dict:
        if name not in self.pages:
            raise ActionError(f"unknown page: {name}")
        return self._set_current_page(name)

    @property
    def topics(self) -> list[str]:
        """All subscribable topics, roster order: pages first, then overlays."""
        return [f"page.{name}" for name in self.pages] + \
            [f"overlay.{name}" for name in self.analyzers]

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _broadcast(self, msg: dict) -> None:
        for cb in list(self._listeners):
            cb(msg)

    def emit_event(self, name: str, data: dict) -> None:
        self._broadcast(proto.event(name, data))

    def snapshot_now(self, topic: str) -> dict | None:
        if topic.startswith("page."):
            obj = self.pages.get(topic.removeprefix("page."))
        elif topic.startswith("overlay."):
            obj = self.analyzers.get(topic.removeprefix("overlay."))
        else:
            obj = None
        if obj is None:
            return None
        seq = self._seq.get(topic, 0) + 1
        self._seq[topic] = seq
        return proto.snapshot(topic, seq, obj.view_model())

    def status(self) -> dict:
        return {
            "page": self.current_page,
            "events_total": self.events_total,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "engine_version": __import__("midicrt").__version__,
            "proto_version": proto.PROTO_VERSION,
        }

    def _handle(self, ev: MidiEvent) -> None:
        self.events_total += 1
        if ev.type in _ACTIVITY_EVENT_TYPES:
            self._last_activity_ts = ev.ts
        for name, analyzer in self.analyzers.items():
            if analyzer.handle(ev):
                self._dirty.add(f"overlay.{name}")
        for name, page in self.pages.items():
            if page.handle(ev):
                self._dirty.add(f"page.{name}")

    def stop(self) -> None:
        self._running = False

    def _tick_analyzers(self, now: float) -> None:
        """Inject wall-clock progress into any analyzer that needs it, and
        drain/emit any resulting `alert` events -- see the module
        docstring's "Analyzer wall-clock tick + alert events" section.
        `now` is read ONCE here (the engine's job); analyzers only ever
        receive it as a parameter, never read a clock themselves."""
        for name, analyzer in self.analyzers.items():
            tick_fn = getattr(analyzer, "tick", None)
            if tick_fn is not None and tick_fn(now):
                self._dirty.add(f"overlay.{name}")
            drain = getattr(analyzer, "drain_alerts", None)
            if drain is not None:
                for alert in drain():
                    self.emit_event("alert", alert)

    def _tick_pages(self, now: float) -> None:
        """Mirrors `_tick_analyzers` above but for PAGES (phase-3 task 7):
        added so `pages/pianoroll.py`'s scrolling display keeps advancing
        from wall-clock progress alone -- a sustained chord must visibly
        slide toward the window's left edge even with ZERO new MIDI events,
        which a pure `handle(ev)` can never produce on its own (same
        "needs an injected clock" problem `_tick_analyzers`'s own docstring
        describes for `analyzers/stucknotes.py`'s escalation). No page
        needs `drain_alerts()` (an analyzer-only, phase-3 task 6 concept),
        so this is the strict `tick()`-only subset of `_tick_analyzers`,
        applied to `self.pages` instead of `self.analyzers`."""
        for name, page in self.pages.items():
            tick_fn = getattr(page, "tick", None)
            if tick_fn is not None and tick_fn(now):
                self._dirty.add(f"page.{name}")

    async def _tick_behaviors(self, now: float) -> None:
        """Give each behavior (phase-3 task 9) a chance to act, and
        dispatch any action intent it returns -- see the module docstring's
        "Behaviors + activity tracking" section. Unlike `_tick_analyzers`/
        `_tick_pages` (which only ever set dirty flags directly), this is
        the ONLY place a behavior's decision turns into a real
        `ActionRegistry.dispatch` call -- the behaviors themselves stay
        synchronous and side-effect-free (see `behaviors/__init__.py`).
        `await`ed (dispatch is async) so this method itself must be
        awaited by `run()`'s loop, unlike its two siblings."""
        for behavior in self._behaviors:
            intent = behavior.tick(now, self._last_activity_ts, self.current_page)
            if intent is None:
                continue
            name, args = intent
            try:
                await self.actions.dispatch(name, args)
            except ActionError:
                # e.g. a custom config removed the "screensaver" page a
                # behavior tries to `page.goto` -- an unattended internal
                # tick crashing the whole run() loop would be far worse
                # than one skipped auto-action; see module docstring.
                pass

    async def run(self) -> None:
        self._running = True
        tick = 1.0 / max(self.config.tick_hz, 1.0)
        while self._running:
            try:
                ev = await asyncio.wait_for(self.queue.get(), timeout=tick)
                self._handle(ev)
                while not self.queue.empty():          # coalesce a burst
                    self._handle(self.queue.get_nowait())
            except TimeoutError:
                pass
            now = time.time()
            self._tick_analyzers(now)
            self._tick_pages(now)
            await self._tick_behaviors(now)
            for topic in sorted(self._dirty):
                snap = self.snapshot_now(topic)
                if snap:
                    self._broadcast(snap)
            self._dirty.clear()
