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

`ScreensaverBehavior` needs "has anything happened recently" as its idle
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

Phase 8 Task 5 (`behaviors/pagecycle.py`'s own docstring has the full
story -- v1-semantics restoration, ruled by the 2026-08-08 decisions doc):
`PageCycleBehavior` no longer reads `_last_activity_ts` for its OWN gating
at all -- it rotates on a pure wall-clock `pagecycle_interval`, paused only
by a recent HUMAN page action (`notify_page_action`, wired below and at
the sysex page-switch call sites), exactly like v1's real "unconditional
interval timer, gated only by a recent keypress" mechanism. It still takes
`last_activity_ts` as a `tick()` parameter (signature parity with
`ScreensaverBehavior`, since both are called identically from the loop
below) but ignores it. The practical consequence: pagecycle's timer keeps
counting down completely obliviously to whether the engine is idle or
not, so `behaviors/pagecycle.py`'s own screensaver-page guard (skip acting
whenever `current_page` IS the screensaver page) is now the ONLY thing
preventing a long idle stretch from having pagecycle un-blank the
screensaver -- see that module's own "Arbitration with the screensaver"
docstring section for the full mechanism and the re-arm-on-block-end fix
carried over from the original Task-9 fix round.
"""
import asyncio
import contextlib
import fnmatch
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from midicrt import config as config_mod
from midicrt import proto
from midicrt.analyzers.beatflash import BeatFlashAnalyzer
from midicrt.analyzers.loopprogress import LoopProgressAnalyzer
from midicrt.analyzers.marquee import PAGE_IDS as _MARQUEE_PAGE_IDS
from midicrt.analyzers.marquee import MarqueeAnalyzer
from midicrt.analyzers.stucknotes import StuckNotesAnalyzer
from midicrt.analyzers.timesig import TimesigAnalyzer
from midicrt.analyzers.transport import TransportAnalyzer
from midicrt.behaviors.pagecycle import PageCycleBehavior
from midicrt.behaviors.screensaver import ScreensaverBehavior
from midicrt.config import Config, ConfigError
from midicrt.engine import bindings as bindings_mod
from midicrt.engine import capture as capture_mod
from midicrt.engine import keymap as keymap_mod
from midicrt.engine import sysex as sysex_mod
from midicrt.engine.actions import ActionError, ActionRegistry
from midicrt.engine.midi_out import MidiOutput
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
from midicrt.pages.sendnotes import SendNotesPage
from midicrt.pages.spectrum import SpectrumPage
from midicrt.pages.tuner import TunerPage
from midicrt.pages.voices import VoicesPage

_LOG = logging.getLogger(__name__)

# `_handle()` stamps `Engine._last_activity_ts` only for these event types --
# matches v1's `plugins/zscreensaver.py::handle()` activity filter exactly
# (see the module docstring's "Behaviors + activity tracking" section and
# behaviors/screensaver.py's own docstring for why a free-running MIDI
# clock must NOT count as activity).
_ACTIVITY_EVENT_TYPES = {"note_on", "note_off", "control_change"}

# Phase 8 Task 5 (behaviors/pagecycle.py's own "notify_page_action" +
# "Origin ruling" docstring sections): every action name that moves
# `current_page` -- `_on_action_dispatched` (below) and the two sysex
# call sites that bypass the dispatch registry (`_sysex_switch_page`,
# `_sysex_screensaver`) all check membership here before notifying
# `_pagecycle_behavior`, so a completely unrelated action (e.g.
# `sendnotes.key`, `capture.start`) never resets pagecycle's user-pause
# window.
#
# Review finding (Critical, live-reproduced, Phase 8 Task 6 follow-up):
# `page.jump` (the roster-positional number-key action that task added)
# was missing from this set at first landing -- a number-key jump moved
# `current_page` exactly like `page.goto` but never armed `notify_page_
# action`, so pagecycle yanked the user away after one `pagecycle_
# interval` instead of honoring `pagecycle_user_pause` the way every
# OTHER page-nav action already does. Fixed by simply adding it here --
# `page.jump` is a page-navigation action in exactly the same sense
# `page.goto` is (both move `current_page` via `_set_current_page`),
# it just resolves its target differently.
_PAGE_NAV_ACTIONS = frozenset({"page.next", "page.prev", "page.goto", "page.jump"})


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
    # `type == "sysex"` only (phase-3 task 12, gap ports): the raw SysEx
    # payload bytes WITHOUT the F0/F7 framing (mido's own `msg.data`
    # convention) -- `data1`/`data2` (single-byte ints) can't carry an
    # arbitrary-length byte sequence, so this is a new field rather than
    # overloading either. None for every other event type. See
    # engine/sysex.py's module docstring for the frame format this feeds
    # (`Engine._handle_sysex`'s own parse/dispatch).
    sysex_data: tuple[int, ...] | None = None


@dataclass
class PageHooks:
    """A page's OPTIONAL protocol hooks, discovered ONCE via `hasattr` right
    here at `Engine.__init__` construction time (see that method's own
    "Formalized optional Page hooks" comment) -- Phase 4 Task 0's engine
    consolidation (docs/phase4-notes.md), replacing what had accreted by
    phase-3's close into hasattr checks scattered across `_tick_pages` (a
    name-keyed `self.pages.get("sendnotes")` special case for the one hook
    that needed draining) AND three separate, near-identically-shaped
    bind-wiring blocks in `__init__` (config/help/sendnotes, each guarding
    on its own page name and calling a DIFFERENTLY-named bind method).
    Every field is `None` for a page that doesn't implement the
    corresponding optional method -- every call site checks for that
    before calling, exactly like the old hasattr checks did, just computed
    once instead of on every `_tick_pages` call.

    - `tick(now: float) -> bool`: wall-clock progress hook (phase-3 task
      7), unchanged in shape -- `pages/pianoroll.py`, `pages/img2txtviz.py`,
      `pages/spectrum.py`, `pages/ccdashboard.py` all implement it.
    - `drain_outputs(now: float) -> list[tuple[int, int]]`: the formalized,
      GENERIC name for the one hook `_tick_pages` used to reach only via
      `self.pages.get("sendnotes")` -- any future output-producing page
      implementing this same name is now picked up automatically, no new
      name-keyed branch needed. `pages/sendnotes.py::SendNotesPage` keeps
      its own more descriptive `drain_expired(now)` as the real,
      directly-tested method; `drain_outputs` there is a thin delegate
      (see that page's own docstring for why the rename wasn't worth
      touching test_pages_sendnotes.py's whole "expiry" test section).
    - `bind_info(provider: Callable[[], dict]) -> None`: called ONCE here,
      right after this dataclass is built for each page, with a
      page-specific ENGINE-facts provider (see `page_info_providers` in
      `__init__` below) -- generalizes what used to be THREE differently
      named methods (`ConfigPage.bind_engine_info`, `SendNotesPage.
      bind_device_info`, and `HelpPage.bind_info` -- which already used
      this exact name, unchanged). The other two pages keep their own
      real, directly-tested methods and add a thin `bind_info` delegate
      (see each page's own docstring) for this generic discovery to find.
    """
    tick: Callable[[float], bool] | None
    drain_outputs: Callable[[float], list[tuple[int, int]]] | None
    bind_info: Callable[[Callable[[], dict]], None] | None


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
    # Phase-3 task 12 (gap ports, v1 page 2 "Send Notes"): the only v1
    # page/plugin that sends real MIDI output -- see pages/sendnotes.py's
    # own module docstring for why this needed a new engine-level I/O
    # surface (engine/midi_out.py::MidiOutput) rather than fitting the
    # existing "pure page" contract. `Engine.__init__` wires this page's
    # LIVE device-open callback separately (mirrors the "config"/"help"
    # pages' own bind_*_info() wiring) right after `self.pages` is built.
    "sendnotes": lambda config: SendNotesPage(),
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
    # Phase 8 Task 4 (docs/visual-audit.md §20b): v1's header page-title
    # scrolling marquee -- v1's own primary anti-burn-in device, "always
    # visible regardless of page" like every other analyzer/overlay here.
    # `config.pages` filtered to `_PAGE_FACTORIES` (the SAME filter
    # `Engine.__init__` applies below to build `self.pages`) rather than
    # `self.pages` itself -- analyzers are constructed BEFORE pages in
    # `__init__` (see this module's own docstring, "Analyzers" section), so
    # `self.pages` does not exist yet at this factory's call time.
    "marquee": lambda config: MarqueeAnalyzer(
        [name for name in config.pages if name in _PAGE_FACTORIES],
        speed_cps=config.header_scroll_speed_cps,
    ),
}

# Phase-3 task 12 (gap ports): v1 numeric page ID (per README's Pages
# table, IDs 0-17) -> v2 page NAME, for `engine/sysex.py`'s CMD_SWITCH_PAGE
# (`Engine._sysex_switch_page`). v2 has no page-ID concept at all (pages
# are named, not numbered) -- this is the ONE place that vocabulary needs
# to be bridged, so a Cirklon operator can keep using the numeric IDs
# their existing patch/sequencer config already sends. Every v2 page this
# phase-3 pass has ever built is covered; the four IDs with no v2 home at
# all (3 Transport -- folded into chrome, no page; 12 Stuck Heatmap, 15
# TimeSig Exp -- not ported; 16 Piano Roll Exp -- merged into "pianoroll")
# are deliberately absent, matching docs/phase3-parity.md's own
# ported/folded/dropped dispositions -- `_sysex_switch_page` replies with
# an error status for any ID not in this map, exactly like v1's own
# `switch_page()` returning `ok=False` for an invalid page.
#
# Phase 8 Task 4 anti-drift cleanup: this used to be the one and only
# place v1's page-ID numbering lived. `analyzers/marquee.py::PAGE_IDS`
# needs the SAME numbering (v1's header marquee is `"[pid:TITLE]"` text,
# see that module's own docstring) -- rather than keep two independently-
# maintained copies of the same 14 numbers, this map is now DERIVED from
# `PAGE_IDS` (inverted), making that module the single source of truth.
# Values are unchanged (still every ID `analyzers/marquee.py` documents,
# same four gaps -- 3/12/15/16 -- for the same reasons); this is a pure
# refactor, not a behavior change, and `test_engine_sysex_dispatch.py`'s
# existing ID-based tests are the regression coverage that proves it.
_SYSEX_PAGE_ID_MAP: dict[int, str] = {pid: name for name, pid in _MARQUEE_PAGE_IDS.items()}


@dataclass
class _LearnArm:
    """Phase 4 Task 3 (DAW-style MIDI learn, docs/phase4-notes.md): the
    engine's single armed learn slot (`Engine._learn_armed`, `None` when
    nothing is armed -- see `Engine._bind_learn`'s own docstring for how
    this is built and validated, and `Engine._capture_learn` for how it
    turns into a real, persisted `bindings_mod.Binding` once a qualifying
    `MidiEvent` arrives). `args_template` already carries the mode ==
    "continuous" fill marker (a real Python `None` on the ONE
    auto-detected float arg) -- everything needed to build the eventual
    `Binding` except the `match`, which only the CAPTURED event itself can
    supply."""
    action: str
    mode: str
    args_template: dict[str, Any]
    range: tuple[float, float]
    armed_at: float


def _parse_learn_range(spec: str) -> tuple[float, float]:
    """Parse `bind.learn`'s optional `range` wire arg (Phase 5 Task 3,
    docs/phase5-notes.md cheap-wins bundle: `clients/cli.py`'s `--range
    lo,hi` flag) -- a plain `"lo,hi"` string, matching this codebase's
    established `--arg k=v`-style "everything over the wire is a string"
    convention (`clients/cli.py::_parse_args`) rather than adding a new
    non-scalar `ActionRegistry` coercer type just for two floats. Raises
    `ActionError` (not `ValueError`) on anything else -- this runs INSIDE
    `Engine._bind_learn`, which is itself the arm-time validation step the
    task brief requires (see that method's own docstring's "Validation
    happens ENTIRELY here, at ARM time" section): a malformed `--range`
    must fail the SAME way a bad `mode`/`action` does, not raise a raw
    `ValueError` that would escape `ActionRegistry.dispatch`'s own
    `except ActionError`-only narrowing and tear down the requesting
    client connection."""
    parts = spec.split(",")
    if len(parts) != 2:
        raise ActionError(f"bind.learn: range must be 'lo,hi', got {spec!r}")
    try:
        lo, hi = (float(p) for p in parts)
    except ValueError as exc:
        raise ActionError(f"bind.learn: range must be 'lo,hi' floats, got {spec!r}") from exc
    return (lo, hi)


class Engine:
    def __init__(self, config: Config, *, keymap_path: str | None = None,
                 config_path: str | None = None, bindings_path: str | None = None,
                 replay: bool = False):
        self.config = config
        # Phase 5 Task 2 (session replay, docs/phase5-notes.md): `False` for
        # every real, live-running engine (`daemon.py::build()` never passes
        # this) -- `True` only for the offline `Engine` `engine/replay.py::
        # build_offline_engine()` constructs. See `_handle`'s own comment at
        # the gate site for exactly what this suppresses and why (the
        # decided design's three named reasons: double-firing against
        # replayed action marks, non-determinism against whatever
        # bindings.toml happens to be on disk, and learn arming that isn't
        # actually "learning" anything real). Stored as a plain attribute
        # (not threaded through as a per-call argument) because `_handle`
        # is called once per event, at MIDI rates, by both live and replay
        # code paths -- a per-instance flag set once at construction is
        # cheaper and simpler than a parallel `_handle(ev, replay=...)`
        # signature every call site would need to thread through.
        self._replay = replay
        # Phase 4 Task 1 (config-served keymap, docs/phase4-notes.md):
        # `self._keymap_path` is eagerly resolved to a concrete path (never
        # `None`) so it always reads back as the truth ("what file does
        # THIS engine's config.reload re-read?") -- see
        # test_engine_core.py::test_engine_default_keymap_path_is_the_real_
        # config_dir_path. `self._config_path` is kept as whatever was
        # passed (including `None`) since `config_mod.load()` already does
        # its own `path or DEFAULT_PATH` resolution internally every time
        # it's called -- eagerly resolving it here would be redundant, not
        # wrong, just extra state to keep in sync with that function's own
        # fallback. `_config_reload` (below) is the one place either is
        # ever read again; `daemon.py::build()` is the one production call
        # site that passes `config_path=args.config` through (mirrors how
        # `args.config` already flows into `config_mod.load()` at daemon
        # startup) so a reload re-reads the SAME file the daemon was
        # actually started with, not always the hardcoded default path.
        self._keymap_path = keymap_path or keymap_mod.DEFAULT_PATH
        self._config_path = config_path
        # Phase 4 Task 2 (MIDI bindings, docs/phase4-notes.md): eagerly
        # resolved to a concrete path, same reasoning as `self._keymap_path`
        # right above -- `_config_reload` and `bind.list`/`bind.remove`'s
        # own `self._bindings_file.save()` calls both need "what file does
        # THIS engine actually read/write" to be unambiguous.
        self._bindings_path = bindings_path or bindings_mod.DEFAULT_PATH
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
        # Must-fix (2026-08-07 fix wave): fail LOUDLY, at startup, before
        # anything else in this method runs -- an empty resolved roster
        # (config.pages == [] outright, or every listed name unknown to
        # _PAGE_FACTORIES, e.g. a typo'd config.toml) used to silently fall
        # through to `next(iter(self.pages), "eventlog")` below, seeding
        # `self.current_page` with a page name that does NOT actually exist
        # in `self.pages`. That was harmless until the first autonomous
        # `page.next` (behaviors/pagecycle.py's own idle-triggered dispatch,
        # `_tick_behaviors`) called `_page_next` -> `order.index(self.
        # current_page)` against an EMPTY `order` list -- a bare ValueError
        # that escapes `_tick_behaviors`'s `except ActionError` guard
        # entirely (see that method's own docstring: it only ever expects
        # ActionError there) and kills the whole `run()` loop, live, with no
        # client ever having done anything wrong. Raising here instead means
        # `daemon.py` never gets an Engine to hand to ProtocolServer/run()
        # at all -- the daemon fails at boot with a readable message instead
        # of dying later, silently, mid-idle.
        if not self.pages:
            raise ConfigError(
                f"config.pages resolved to an empty page roster "
                f"(config.pages={config.pages!r}); known page names: "
                f"{sorted(_PAGE_FACTORIES)}. The engine cannot start with no "
                "pages to serve -- fix config.toml's `pages` list."
            )
        # Important perf fix (2026-08-07 fix wave, finding 2b): "harmony"
        # and "chordkey" each independently constructed their OWN
        # HarmonyAnalyzer above (via _PAGE_FACTORIES) -- a live review
        # measured that duplication as ~90% of note_on's 3.86ms cost (two
        # instances computing byte-identical chord/scale detections from
        # the same event stream). When BOTH make the roster, replace
        # chordkey's independently-constructed analyzer with harmony's
        # SAME instance -- mirrors the "config"/"help"/"sendnotes"
        # post-construction wiring pattern below (no page factory has
        # visibility into the REST of the roster being built, only
        # __init__ does). Safe, not just cheaper, only because
        # analyzers/harmony.py::HarmonyAnalyzer.handle() has its own
        # shared-instance dedup guard (see that method's docstring) --
        # Engine._handle() still calls EVERY page's handle(ev) once per
        # event, so both pages' calls land on this one instance.
        if "harmony" in self.pages and "chordkey" in self.pages:
            self.pages["chordkey"] = ChordKeyPage(self.pages["harmony"].analyzer)
        self.current_page = next(iter(self.pages))
        # Phase-3 task 12: a single lazily-opened MIDI output, shared by
        # "sendnotes" (real note sends) and, when the roster reaches it,
        # engine/sysex.py's versioned reply frames -- see engine/
        # midi_out.py's own module docstring for why ONE port covers both
        # v1 capabilities. Constructed unconditionally (mirrors
        # `_pagecycle_behavior`/`_screensaver_behavior` below): no I/O
        # happens until a real send is attempted. Built BEFORE the
        # PageHooks discovery pass right below so `_sendnotes_device_info`
        # (wired into "sendnotes" via that pass) can read its state once
        # actually CALLED later -- the bound-method reference itself
        # doesn't need `self._midi_out` to exist yet, just `self`.
        self._midi_out = MidiOutput()
        # Phase 4 Task 0 (engine consolidation, docs/phase4-notes.md):
        # `self._page_info_providers` is the one genuinely irreducible
        # per-page bit left after formalizing PageHooks -- each of these
        # three pages needs a DIFFERENT shape of engine fact
        # (version/uptime/roster for config, the action registry for help,
        # MIDI-output open state for sendnotes). `_discover_page_hooks`
        # (below) is the ONE hasattr-based discovery pass this dict feeds
        # into -- see `PageHooks`' own docstring for the full "what this
        # replaces" writeup (three near-duplicated bind-wiring blocks, one
        # per page name, each calling a differently-named bind method).
        self._page_info_providers: dict[str, Callable[[], dict]] = {
            "config": self._config_engine_info,
            "help": self._help_info,
            "sendnotes": self._sendnotes_device_info,
        }
        self._page_hooks: dict[str, PageHooks] = {}
        for name, page in self.pages.items():
            self._discover_page_hooks(name, page)
        self.events_total = 0
        self.started_at = time.monotonic()
        self._listeners: list[Callable[[dict], None]] = []
        self._seq: dict[str, int] = {}
        self._dirty: set[str] = set()
        # Phase 4 Task 2 (MIDI bindings): every `(action_name, args)` intent
        # `_handle` collects from `self._binding_dispatcher.handle(ev)`,
        # awaiting dispatch by `_dispatch_bindings` (called from `run()`
        # right after `_handle` returns -- see the module docstring's
        # "dispatch context decision" and `_dispatch_bindings`'s own
        # docstring for why `_handle` itself can never dispatch directly).
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
        # widened from `(name, args)` to `(binding_id, name, args)` --
        # `_handle` now calls `self._binding_dispatcher.handle_with_origin(ev)`
        # (an ADDITIVE method, `handle()` itself is untouched -- see
        # engine/bindings.py's own docstring) so `_dispatch_bindings` can
        # stamp a capture action mark's `origin` as `f"binding:{binding_id}"`.
        self._pending_binding_dispatches: list[tuple[str, str, dict]] = []
        self._running = False
        # Important perf fix (2026-08-07 fix wave, finding 1): see
        # set_topic_refcount_provider's own docstring and _flush_dirty's.
        self._topic_refcount_provider: Callable[[str], int] | None = None
        # Phase 5 Task 3 (bind.list port_present, docs/phase5-notes.md's
        # bundled cheap-wins list, Minor #2 from the phase-4 final review):
        # same PULL-seam shape as `_topic_refcount_provider` right above --
        # `Engine` has no `MidiInput` reference of its own at all
        # (`daemon.py::build()` constructs one separately and feeds only
        # `engine.queue`, see that module's own docstring), so `_bind_list`
        # needs a way to ask "what ports are actually open right now"
        # without the engine owning MIDI I/O itself. See
        # `set_open_ports_provider`'s own docstring for the full contract.
        self._open_ports_provider: Callable[[], list[str]] | None = None
        # Phase-3 task 9: seeded to "now", NOT epoch 0 -- see the module
        # docstring's "Behaviors + activity tracking" section for why a
        # freshly booted engine must not look infinitely idle on its very
        # first `_tick_behaviors` call.
        self._last_activity_ts: float = time.time()
        # Phase 8 Task 5, fix round 1 (Important reviewer finding):
        # `_on_action_dispatched` (below) needs a "now" for
        # `PageCycleBehavior.notify_page_action`, but it is called from
        # deep inside `ActionRegistry.dispatch()` -- a context `run()`'s own
        # tick loop has no way to thread an injected `now` INTO (unlike
        # `_tick_analyzers`/`_tick_pages`/`_tick_behaviors`, which all
        # receive `now` as a plain argument). Calling `time.time()` directly
        # there put `notify_page_action` on a DIFFERENT clock domain than
        # whatever fake `now` a test drives through `_tick_behaviors()` --
        # a test asserting "still paused" a few fake-seconds later would
        # pass regardless of the CONFIGURED `pagecycle_user_pause` value
        # (even 0.001s), since a real epoch timestamp (~1.7 billion) trivially
        # exceeds any small fake `now`, making the assertion non-dispositive
        # (reviewer-reproduced: `tests/test_engine_core.py::
        # test_pagecycle_honors_a_tiny_user_pause_at_fake_clock_scale`).
        # `self._clock` is the fix: a swappable clock SOURCE (defaults to
        # real `time.time`, exactly today's production behavior -- nothing
        # changes for the real daemon) that a test can override
        # (`eng._clock = lambda: fake_now[0]`) to put every clock read
        # `_on_action_dispatched` makes on the SAME timeline as whatever
        # fake `now` values that same test feeds `_tick_behaviors()`
        # directly. Scoped to this one non-tick-loop-injected "now" read --
        # not a sweeping replacement of every `time.time()` call in this
        # file, which would be unrelated scope creep for this fix.
        self._clock: Callable[[], float] = time.time
        # Phase 8 Task 5: `known_pages=set(self.pages)` -- built above, at
        # line ~516, never reassigned after `__init__` -- lets
        # `PageCycleBehavior` skip a configured `pagecycle_pages` entry
        # that isn't actually in THIS deploy's roster (see that module's
        # own "Missing-page skip" docstring section) instead of dispatching
        # a `page.goto` `_tick_behaviors` would just swallow as an
        # ActionError.
        self._pagecycle_behavior = PageCycleBehavior(
            enabled=config.pagecycle_enabled, interval=config.pagecycle_interval,
            pages=config.pagecycle_pages, user_pause=config.pagecycle_user_pause,
            known_pages=set(self.pages))
        self._screensaver_behavior = ScreensaverBehavior(
            enabled=config.screensaver_enabled, after_s=config.screensaver_after_s)
        # Phase 8 Task 5 (behaviors/pagecycle.py's own docstring has the
        # full story -- v1-semantics restoration): `PageCycleBehavior` no
        # longer shares ANY clock with `ScreensaverBehavior` -- it rotates
        # on its own wall-clock `pagecycle_interval`, completely oblivious
        # to `_last_activity_ts`/idle state. The screensaver-page guard in
        # `PageCycleBehavior.tick()` (refuse to act while `current_page` IS
        # the screensaver page, checked first, however that page was
        # reached) is carried over UNCHANGED from the Phase-3 Task 9
        # arbitration fix this replaces -- it is now the ONLY thing
        # preventing a long idle stretch from having pagecycle un-blank a
        # just-activated screensaver, since there is no shared idle-clock
        # math left to reason about. On block-end, `tick()` re-arms its own
        # `_last_switch` to `now` (a full fresh `pagecycle_interval` before
        # the next auto-advance) -- the direct descendant of the Task-9
        # fix round's "re-arm after a manual escape" fix, now needed for
        # every block-end path uniformly (see that module's own "Arbitration
        # with the screensaver" docstring section for why the OLD two-path
        # distinction collapsed into one). List order below remains
        # inconsequential for the same reason it always was: pagecycle is a
        # no-op whenever screensaver owns the display, regardless of which
        # behavior's `tick()` runs first within a given `_tick_behaviors`
        # call.
        self._behaviors: list = [self._pagecycle_behavior, self._screensaver_behavior]
        # Phase-3 task 12 (gap ports): registered UNCONDITIONALLY --
        # `_pagecycle_behavior` always exists regardless of `config.pages`
        # (unlike page-declared actions below, which are only advertised
        # when their owning page is actually in the roster). Gives
        # `engine/sysex.py`'s CMD_PAGE_CYCLE (v1's own `03 <0|1>` command)
        # a real action to dispatch through, and is independently reachable
        # via `midicrt action pagecycle.enable` too -- one capability, two
        # entry points, matching "page.goto"'s own dual reachability (a
        # normal client action AND CMD_SWITCH_PAGE's own dispatch target).
        self.actions.register("pagecycle.enable", self._pagecycle_enable,
                              description="Enable or disable idle page cycling",
                              args={"enabled": "bool"})
        # Roster-wide navigation -- no single page owns "the next/previous/
        # named page in the WHOLE cycle order", so these three stay
        # registered directly here rather than through any one page's
        # `actions()` (see the page-declared-actions loop's own comment
        # below for the full engine-owned-vs-page-declared split).
        self.actions.register("page.next", self._page_next,
                              description="Advance to the next page in the roster")
        self.actions.register("page.prev", self._page_prev,
                              description="Go back to the previous page in the roster")
        self.actions.register("page.goto", self._page_goto,
                              description="Jump to a named page", args={"name": "str"})
        # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
        # revamp): the roster-POSITIONAL jump action number keys bind to --
        # see engine/keymap.py's own module docstring ("Why page.jump, not
        # page.goto") for why this is a SEPARATE action rather than
        # baking resolved page NAMES into DEFAULT_KEYMAP. Registered
        # unconditionally, like page.next/.prev/.goto above -- it always
        # exists structurally, regardless of how many pages THIS build's
        # roster actually has (`_page_jump`'s own docstring covers the
        # out-of-range case).
        self.actions.register("page.jump", self._page_jump,
                              description="Jump to the Nth page in the roster (1-indexed)",
                              args={"position": "int"})
        # Phase-3 task 12: sendnotes' one interactive entry point -- ported
        # from v1's real per-keystroke `keypress()` (see pages/sendnotes.py's
        # own module docstring for the full v1 branch-order/KEYMAP-shadowing
        # notes). Reachable via `midicrt action sendnotes.key --arg key=z`,
        # same "no client-side keyboard binding yet" precedent as the
        # page-declared actions below (Phase 4's key->action table,
        # phase3-notes.md's own known-latent item). Stays registered
        # directly here rather than through `SendNotesPage.actions()` --
        # docs/phase4-notes.md's own binding-architecture notes call this
        # exact shape out as "the template for key-driven engine actions":
        # it needs to read the wall clock ONCE (the engine's job) and send
        # a real note through `self._midi_out`, both engine-level I/O a
        # page is architecturally never given (see this file's module
        # docstring) -- so unlike eventlog.clear/pianoroll.*/img2txtviz.*
        # below, this one genuinely isn't page-scoped state alone.
        if "sendnotes" in self.pages:
            self.actions.register("sendnotes.key", self._sendnotes_key,
                                  description="Send one Send-Notes keystroke "
                                              "(note or control key)",
                                  args={"key": "str"})
        # Phase 4 Task 0 (engine consolidation, docs/phase4-notes.md):
        # PAGE-DECLARED actions -- replaces what used to be `eventlog.clear`
        # registered unconditionally plus two more guarded blocks here
        # (`if "pianoroll" in self.pages: ...` x3 actions, `if "img2txtviz"
        # in self.pages: ...` x2 actions), each hand-marking its own page's
        # topic dirty and (pianoroll only) hand-translating a ValueError
        # into ActionError. Any page exposing `actions() -> [(name, handler,
        # description, args_schema), ...]` (see pages/eventlog.py,
        # pages/pianoroll.py, pages/img2txtviz.py for the concrete
        # examples) gets every action it declares registered here, in ONE
        # loop, with the roster itself as the ONLY guard -- a page not in
        # `self.pages` is never asked for its actions at all, so there is
        # no more per-page `if` to keep in sync as new pages grow their own
        # actions. `_wrap_page_action` (below) is what now does the
        # dirty-marking and ValueError->ActionError translation, uniformly,
        # for every action registered through this loop.
        for page_name, page in self.pages.items():
            declare_actions = getattr(page, "actions", None)
            if declare_actions is None:
                continue
            for action_name, handler, description, args_schema in declare_actions():
                self.actions.register(
                    action_name, self._wrap_page_action(page_name, handler),
                    description=description, args=args_schema)
        # Phase 4 Task 2 (MIDI bindings, docs/phase4-notes.md): `bind.list`/
        # `bind.remove` are registered BEFORE the keymap-filtering block
        # below for the exact same reason `config.reload` already is (its
        # own comment right below) -- `self.keymap`'s construction needs
        # `self.actions.describe()` to already include every action this
        # build will ever have, and a user COULD (if oddly) bind a key to
        # `bind.list`. `bindings_mod.load_or_warn` never raises (mirrors
        # `keymap_mod.load_keymap_or_warn` exactly, same live-reproduced-
        # incident rationale: a malformed OPTIONAL file must never prevent
        # boot) -- an empty `BindingsFile` is the natural "last-good" at
        # construction time, since there is no earlier value to fall back
        # to (mirrors the keymap block's own `DEFAULT_KEYMAP` fallback).
        bindings_file, bindings_warning = bindings_mod.BindingsFile.load_or_warn(
            self._bindings_path)
        if bindings_warning:
            _LOG.warning("startup: %s; starting with no bindings", bindings_warning)
            bindings_file = bindings_mod.BindingsFile(path=self._bindings_path)
        self._bindings_file = bindings_file
        self._binding_dispatcher = bindings_mod.BindingDispatcher(self._bindings_file.bindings)
        self.actions.register("bind.list", self._bind_list,
                              description="List MIDI bindings and their validity")
        self.actions.register("bind.remove", self._bind_remove,
                              description="Remove a MIDI binding by id", args={"id": "str"})
        # Phase 4 Task 3 (DAW-style MIDI learn, docs/phase4-notes.md):
        # `self._learn_armed` is the engine's single armed learn slot (see
        # `_LearnArm`'s own docstring) -- `None` whenever nothing is armed,
        # which is also the initial state (a freshly booted engine has
        # never had `bind.learn` called). Registered here, right alongside
        # `bind.list`/`bind.remove`, for the exact same "before the
        # keymap-filtering block below" reason that section's own comment
        # already gives: a user COULD (if oddly) bind a key to `bind.
        # cancel` (never `bind.learn` itself -- its `args` schema entry is
        # a nested dict a plain keymap.toml key->action mapping has no way
        # to supply, see `clients/base.py::dispatch_key`'s own no-args
        # `client.action(action)` call), so both must exist before
        # `self.keymap`'s own construction computes THIS build's complete
        # action vocabulary.
        self._learn_armed: _LearnArm | None = None
        # Phase 5 Task 3 (CLI `--range lo,hi` for continuous learn, docs/
        # phase5-notes.md cheap-wins bundle): `range` joins the schema as
        # a genuinely OPTIONAL wire arg (`defaults={"range": ""}` --
        # `ActionRegistry.register`'s own new opt-in mechanism, see its
        # docstring) -- an empty string means "not supplied", matching
        # `_bind_learn`'s pre-existing hardcoded `(0.0, 1.0)` fallback
        # exactly, so every one of this codebase's dozens of pre-existing
        # `bind.learn` call sites (none of which pass `range` at all)
        # keeps working completely unchanged.
        self.actions.register(
            "bind.learn", self._bind_learn,
            description="Arm the engine to learn a new MIDI binding from the next "
                        "qualifying MIDI event",
            args={"action": "str", "mode": "str", "args": "dict", "range": "str"},
            defaults={"range": ""})
        self.actions.register("bind.cancel", self._bind_cancel,
                              description="Cancel the currently armed learn slot, if any")
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
        # registered here (before `config.reload`, same "must exist before
        # `self.keymap`'s construction computes THIS build's complete
        # action vocabulary" reasoning `bind.learn`/`bind.cancel`'s own
        # comment above already gives). `CaptureSink` itself is
        # constructed first -- see engine/capture.py's own module
        # docstring for the full design (writer queueing/loss window,
        # index/retention, storage resolution). `self.analyzers["status"]`
        # already exists by this point (built at the very top of
        # `__init__`), which `_capture_start_action`/`_capture_stop_action`
        # below need for the `rec` chrome flag.
        self._capture = capture_mod.CaptureSink(
            capture_dir=config.capture_dir,
            retention=config.capture_retention,
            engine_version=__import__("midicrt").__version__,
            instruments=list(config.instruments),
        )
        # The dispatch-hook seam (docs/phase5-notes.md's decided design,
        # point 1: "record raw MIDI + action marks WITH PROVENANCE") --
        # covers 3 of the 4 origins (bindings/behaviors/client) uniformly;
        # the 4th (sysex) never reaches `ActionRegistry.dispatch` at all
        # (see `_handle_sysex`'s own sysex handlers, which call
        # `self._capture.record_action(..., origin="sysex")` directly).
        self.actions.set_dispatch_hook(self._on_action_dispatched)
        self.actions.register("capture.start", self._capture_start_action,
                              description="Start a new event-sourced capture session")
        self.actions.register("capture.stop", self._capture_stop_action,
                              description="Stop the active capture session")
        self.actions.register("capture.status", self._capture_status_action,
                              description="Report the active capture session's status")
        self.actions.register("capture.pin", self._capture_pin_action,
                              description="Pin a stopped capture session so retention "
                                          "never deletes it",
                              args={"id": "str"})
        # Fix-wave fix (2026-08-07, Important finding, replay isolation):
        # `and not self._replay` -- an offline `engine/replay.py::
        # build_offline_engine()` engine has no real I/O of its own, but
        # honoring `capture_auto_start` here would still (a) sweep
        # retention against the REAL sessions directory before this
        # `Engine` even exists to know it's "just" a replay (a misconfigured
        # config.toml could delete the very session file about to be
        # replayed) and (b) open a brand-new REAL capture session that
        # re-records the replayed stream right back into it. `self._replay`
        # is already set (top of this method) by the time this line runs.
        if config.capture_auto_start and not self._replay:
            # v1's OWN deployed default is manual-start-only (verified,
            # not invented -- see config.py's own comment on
            # `capture_auto_start`) -- this branch only ever fires when a
            # config.toml explicitly opts in.
            self._capture_start_action()
            # Fix-wave addition (Important finding: an auto-started
            # session got NO `capture.start` mark at all) -- this call is
            # a direct Python method call, not an `ActionRegistry.
            # dispatch(...)`, so the normal post-dispatch hook (which is
            # what stamps a mark for every OTHER `capture.start` origin --
            # client/binding/behavior all go through the registry) never
            # fires here. `"auto"` joins the documented origin set
            # (`binding:<id>` / `client` / `behavior` / `sysex` / `auto` /
            # `shutdown` -- see `_capture_stop_action`'s own docstring for
            # the last one) as the one origin that can ONLY ever appear on
            # a session's very first action mark, at construction time.
            self._capture.record_action("capture.start", {}, "auto")
        # Phase 4 Task 1 (config-served keymap, docs/phase4-notes.md):
        # registered LAST, after every other action above (engine-owned
        # AND page-declared) -- both because `config.reload` itself is a
        # perfectly legitimate keymap target (a user COULD bind a key to
        # it) and because `self.keymap`'s own construction right below
        # needs `self.actions.describe()` to already reflect THIS build's
        # complete, roster-dependent action vocabulary (see
        # `keymap_mod.filter_known_actions`'s own docstring for why that
        # order matters: an action name valid in a full-roster build but
        # absent here must be dropped, not just left in as a landmine for
        # the first keypress/dispatch to hit).
        self.actions.register("config.reload", self._config_reload,
                              description="Re-read keymap.toml (and, best-effort, "
                                          "config.toml's instruments) without a restart")
        # Bindings review fix (live-reproduced Critical finding): a
        # malformed keymap.toml must never prevent boot -- `load_keymap_
        # or_warn` (engine/keymap.py) never raises; a parse failure here
        # falls back to `DEFAULT_KEYMAP` (there is no earlier "last-good"
        # keymap to keep at construction time, unlike `_config_reload`
        # below) and is logged loudly rather than silently swallowed.
        initial_keymap, warning = keymap_mod.load_keymap_or_warn(self._keymap_path)
        if warning:
            _LOG.warning("startup: %s; falling back to built-in defaults", warning)
            initial_keymap = dict(keymap_mod.DEFAULT_KEYMAP)
        # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
        # revamp): the per-page counterpart, same never-raises/fall-back-
        # to-defaults discipline as the global load right above.
        initial_page_keymaps, page_warning = keymap_mod.load_page_keymaps_or_warn(
            self._keymap_path)
        if page_warning:
            _LOG.warning("startup: %s; falling back to built-in per-page defaults",
                         page_warning)
            initial_page_keymaps = dict(keymap_mod.DEFAULT_PAGE_KEYMAPS)
        self._raw_global_keymap = initial_keymap
        self._raw_page_keymaps = initial_page_keymaps
        # `self.keymap_global`/`self.keymap_page`/`self.keymap` (effective,
        # for THIS build's initial `self.current_page`) all come from this
        # one call -- see `_recompute_keymap`'s own docstring.
        self._recompute_keymap()
        # Phase 4 Task 2 (MIDI bindings): log-only, "kept-but-inert" pass
        # (see `validate_binding`'s own docstring for why this is NOT a
        # filter -- unlike `keymap_mod.filter_known_actions` right above,
        # nothing is ever dropped from `self._bindings_file.bindings` here).
        # Placed at the very END of `__init__`, after every action
        # (including `config.reload` right above) is registered, so this
        # check runs against THIS build's truly complete action registry.
        self._log_invalid_bindings()

    def _log_invalid_bindings(self) -> None:
        """Warn (never drop -- see `validate_binding`'s own docstring for
        the "kept-but-inert" disposition) about any binding whose action is
        absent from this build's roster or whose `args` template doesn't
        satisfy that action's schema. Called at construction and again
        after every successful `config.reload` bindings.toml re-read (see
        `_config_reload`) so a newly-introduced problem is always logged
        loudly, not just at boot."""
        for binding in self._bindings_file.bindings:
            error = bindings_mod.validate_binding(binding, self.actions.describe())
            if error:
                _LOG.warning("bindings: binding %r may not fire correctly: %s",
                             binding.id, error)

    def _config_reload(self) -> dict:
        """`config.reload` action handler (Phase 4 Task 1, docs/phase4-
        notes.md): the ONE live-reload entry point.

        Unconditionally re-reads `keymap.toml` (`self._keymap_path`) --
        this is the actual point of the task -- re-validates it against
        THIS build's current action registry exactly like construction
        time did, and always emits a `keymap_changed` event (even when the
        freshly-loaded keymap is byte-identical to the old one -- simplest
        correct behavior: a client's "refetch on keymap_changed" path
        stays one unconditional code path with no "did it actually change"
        check of its own to get wrong).

        Then, as a cheap best-effort CONVENIENCE (not the task's core
        deliverable -- see docs/phase4-notes.md's own task-1 framing):
        re-reads `config.toml` (`self._config_path`) and
          - pushes an `instruments` change live into the "voices" page
            (see `VoicesPage.set_instruments`'s own docstring for why
            that's safe/cheap: plain data, no analyzer state involved),
          - but only WARNS (never applies) a `pages` roster change -- the
            roster is built once, at `__init__`, and every page/analyzer
            below it (harmony's shared-instance dedup, sendnotes' real
            `MidiOutput`, every topic's subscriber refcount) has no tested
            migration path for being re-parented live. A restart is the
            honest way to apply a roster change, matching
            docs/phase4-notes.md's own "action vocabulary is roster-
            dependent" architecture fact.
        A `config.toml` that fails to parse (or doesn't exist -- tolerated
        silently, matching `config_mod.load()`'s own "no file -> defaults"
        contract) never blocks the keymap half above: this method's return
        value's `warnings` list is the only place either failure surfaces,
        never an exception.

        Bindings review fix (live-reproduced Critical finding): a
        malformed `keymap.toml` at reload time used to raise straight out
        of this handler -- an uncaught exception escaping
        `ActionRegistry.dispatch` here tears down the REQUESTING
        CONNECTION (`ProtocolServer._dispatch`'s own `except ActionError`
        narrowing does not catch it). `load_keymap_or_warn` never raises;
        a parse failure here keeps `self.keymap` exactly as it was BEFORE
        this call (the real "last-good" value, unlike startup) and adds a
        `warnings` entry instead.

        Phase 4 Task 2 (MIDI bindings): also unconditionally re-reads
        `bindings.toml` (`self._bindings_path`), same "last-good on failure,
        never raise" discipline as the keymap half right above -- a
        malformed `bindings.toml` at reload time appends a `warnings` entry
        and leaves `self._bindings_file`/`self._binding_dispatcher`
        untouched rather than tearing down the connection. Unlike the
        keymap half, there is no `bindings_changed` event (docs/phase4-
        notes.md's own task-2 scope: "no new events this task" -- learn,
        Task 3, is what actually needs clients to react live to a bindings
        change; nothing consumes one yet). A successful reload re-runs
        `_log_invalid_bindings` so a newly-stale binding is logged again,
        matching construction time's own behavior.
        """
        warnings: list[str] = []
        new_keymap, keymap_warning = keymap_mod.load_keymap_or_warn(self._keymap_path)
        new_page_keymaps, page_keymap_warning = keymap_mod.load_page_keymaps_or_warn(
            self._keymap_path)
        if keymap_warning:
            warnings.append(f"{keymap_warning}; keeping last-good keymap")
            _LOG.warning("config.reload: %s; keeping last-good keymap", keymap_warning)
        else:
            self._raw_global_keymap = new_keymap
        if page_keymap_warning:
            warnings.append(f"{page_keymap_warning}; keeping last-good per-page keymap")
            _LOG.warning("config.reload: %s; keeping last-good per-page keymap",
                         page_keymap_warning)
        else:
            self._raw_page_keymaps = new_page_keymaps
        if not keymap_warning or not page_keymap_warning:
            # Recompute even if only ONE half actually changed -- `self.
            # keymap`/`keymap_global`/`keymap_page` are all derived
            # together (`_recompute_keymap`'s own docstring), so a global-
            # only or page-only successful re-read still needs a fresh
            # merge against whichever half DIDN'T change.
            self._recompute_keymap()
        new_bindings_file, bindings_warning = bindings_mod.BindingsFile.load_or_warn(
            self._bindings_path)
        if bindings_warning:
            warnings.append(f"{bindings_warning}; keeping last-good bindings")
            _LOG.warning("config.reload: %s; keeping last-good bindings", bindings_warning)
        else:
            self._bindings_file = new_bindings_file
            self._binding_dispatcher.set_bindings(self._bindings_file.bindings)
            self._log_invalid_bindings()
        try:
            new_config = config_mod.load(self._config_path)
        except (OSError, ValueError) as exc:
            warnings.append(f"config.toml reload failed: {exc}")
            new_config = None
        if new_config is not None:
            if new_config.pages != self.config.pages:
                msg = ("config.toml 'pages' changed but the live page roster is "
                      "fixed at startup -- restart midicrtd to apply")
                warnings.append(msg)
                _LOG.warning("config.reload: %s (old=%r new=%r)",
                             msg, self.config.pages, new_config.pages)
            voices = self.pages.get("voices")
            if (voices is not None and hasattr(voices, "set_instruments")
                    and new_config.instruments != self.config.instruments):
                voices.set_instruments(new_config.instruments)
                self.config.instruments = new_config.instruments
                self._dirty.add("page.voices")
        if "config" in self.pages:
            self._dirty.add("page.config")   # cheap; the viewer's own facts may have shifted
        self.emit_event("keymap_changed", {"keymap": self.keymap, "warnings": warnings})
        return {"keymap": self.keymap, "warnings": warnings}

    def _bind_list(self) -> dict:
        """`bind.list` action handler (Phase 4 Task 2): every persisted
        binding, serialized, each carrying a live `valid`/`error` pair
        computed FRESH against THIS call's `self.actions.describe()` --
        never cached, so a binding that was valid at load time but went
        stale after this (there is no live roster-change path today, but
        `validate_binding` costs nothing to just always recompute) is
        always reported accurately. See `validate_binding`'s own docstring
        for why an invalid binding is listed here rather than silently
        absent -- "kept-but-inert" only means something if a human can
        actually SEE the inert entry."""
        actions = self.actions.describe()
        return {"bindings": [self._serialize_binding(b, actions)
                             for b in self._bindings_file.bindings]}

    def _serialize_binding(self, b: bindings_mod.Binding, actions: dict) -> dict:
        """Shared wire-shape builder for one `Binding` (Phase 4 Task 3
        extraction, docs/phase4-notes.md) -- `bind.list` (above, every
        persisted binding) and `_capture_learn`'s own `learn_bound` event
        payload (below, exactly one binding, freshly created) both need
        the IDENTICAL shape, so this is the one place that shape is
        actually written."""
        error = bindings_mod.validate_binding(b, actions)
        return {
            "id": b.id,
            "match": {"type": b.match.type, "number": b.match.number,
                     "channel": b.match.channel, "port_pattern": b.match.port_pattern},
            "action": b.action,
            "args": dict(b.args),
            "mode": b.mode,
            "threshold": b.threshold,
            "range": list(b.range),
            "valid": error is None,
            "error": error,
            "port_present": self._port_present(b.match.port_pattern),
        }

    def _port_present(self, port_pattern: str | None) -> bool:
        """Phase 5 Task 3 (`bind.list` diagnostics, docs/phase5-notes.md
        cheap-wins bundle, Minor #2 from the phase-4 final review): is
        `port_pattern` currently matched by ANY open MIDI input port? Pure
        diagnostic sugar for `bind list` (§6 of docs/phase4-bindings.md's
        own troubleshooting section already tells a human to eyeball
        `port_pattern` against "the real incoming event" by hand -- this
        computes that check FOR them) -- never gates whether the binding
        actually fires; `BindingDispatcher._matches` is untouched and is
        the only place matching semantics that matter for real dispatch
        live.

        `port_pattern is None` ("any port") is trivially, always present
        -- there is no specific port to be missing. No provider wired
        (`set_open_ports_provider`'s own docstring: `--no-midi`, or the
        overwhelming majority of this test suite's bare `Engine()`
        instances) means "unknown", not "absent" -- reported `True` so a
        binding is never spuriously flagged as port-missing just because
        nothing ever told this engine what's open. A provider call that
        itself raises (defensive only -- `MidiInput.open_ports` is a
        plain `sorted(dict)` today and should never raise) is treated the
        same way: this is a diagnostic nicety, not something worth letting
        crash `bind.list` over."""
        if port_pattern is None or self._open_ports_provider is None:
            return True
        try:
            open_ports = self._open_ports_provider()
        except Exception:  # noqa: BLE001 -- diagnostic-only; never let this crash bind.list
            return True
        return any(fnmatch.fnmatch(p, port_pattern) for p in open_ports)

    def _bind_remove(self, id: str) -> dict:
        """`bind.remove` action handler: drops the binding by id, persists
        the file (atomic save, `BindingsFile.save`), and immediately
        disarms the live dispatcher (`set_bindings`) -- a removed binding
        must never fire again even if a MIDI event matching it is already
        in flight when this dispatches. Raises `ActionError` for an unknown
        id, matching `_page_goto`'s own "unknown named resource" precedent
        (engine/core.py) rather than silently reporting `{"removed":
        False}` -- `bind.remove` is always issued BY NAME (a CLI/TUI
        operator picking an id off `bind.list`), so an unknown id here is a
        genuine caller error worth surfacing loudly, unlike e.g.
        `sendnotes.key`'s own "unrecognized key -> applied: false"
        convention for input that's allowed to just not match anything."""
        if self._bindings_file.get(id) is None:
            raise ActionError(f"unknown binding id: {id!r}")
        self._bindings_file.remove(id)
        self._bindings_file.save()
        self._binding_dispatcher.set_bindings(self._bindings_file.bindings)
        return {"removed": True}

    # -- bind.learn / bind.cancel: DAW-style MIDI learn (Phase 4 Task 3,
    # docs/phase4-notes.md) ----------------------------------------------
    #
    # `bind.learn` arms `self._learn_armed`; the next qualifying MidiEvent
    # (`bindings_mod.is_learnable_event`, checked in `_handle` BEFORE the
    # binding dispatcher sees the event -- see that method's own comment)
    # is captured by `_capture_learn` into a real, persisted `Binding`.
    # `_tick_learn` (called from `run()` once per tick, same injected-`now`
    # convention as `_tick_analyzers`/`_tick_pages`/`_tick_behaviors`)
    # auto-cancels an arm that's sat unanswered past `bindings_mod.
    # LEARN_TIMEOUT_S`. `bind.cancel` is the manual equivalent.

    def _bind_learn(self, action: str, mode: str, args: dict, range: str = "") -> dict:
        """`bind.learn` action handler: arms the engine's single learn
        slot. A second `bind.learn` call while already armed REPLACES the
        arm outright (no stacking, no error) -- `learn_armed` is emitted
        again for the new arm; there is no separate "the old arm was
        discarded" event, since the old arm was never itself observable
        externally as anything but "armed" (docs/phase4-notes.md's task-3
        amplification: document this choice, don't build a queue of one).

        Validation happens ENTIRELY here, at ARM time, never at capture
        time -- the task brief's own requirement, and the only way a human
        arming a learn from the CLI ever SEES a validation failure (a
        capture-time failure would only ever surface in the daemon's log,
        arriving whenever a MIDI event happens to land, disconnected from
        the arm command that caused it):

        - `mode` must be "trigger" or "continuous".
        - `action` must be a real, currently-registered action.
        - For `mode == "continuous"`: the target action's schema must
          declare EXACTLY ONE `"float"` arg (ambiguous otherwise -- which
          one would the lerped CC value even fill?); that arg is
          AUTO-DETECTED as the fill target -- `args` must NOT also name
          it, since there is nothing left to statically supply for a key
          learn is about to fill from live MIDI.
        - Every other check (missing/unknown args, a continuous fill arg
          not declared float) is delegated to `bindings_mod.
          validate_binding` itself, run here against a throwaway PROBE
          `Binding` (its `match` is never consulted by that function at
          all -- only `action`/`args`/`mode` are) -- one validator, shared
          with `bind.list`'s own post-hoc check, rather than re-deriving
          the same rules twice.

        `range` (Phase 5 Task 3, docs/phase5-notes.md cheap-wins bundle:
        "CLI --range lo,hi for continuous learn -- stuck at [0,1] today, a
        learned zoom knob only reaches the bottom quarter"): an OPTIONAL
        `"lo,hi"` string (`clients/cli.py`'s own `--range` flag), parsed by
        `_parse_learn_range` below. Empty string (the wire-level default,
        `ActionRegistry.register`'s own `defaults={"range": ""}` -- see
        that registration's own comment) means "not supplied", falling
        back to the pre-existing hardcoded `(0.0, 1.0)`. Meaningful ONLY
        for `mode == "continuous"` -- silently ignored for `mode ==
        "trigger"` (mirrors `Binding.threshold`'s own "always present,
        ignored when the mode doesn't use it" precedent, engine/
        bindings.py) rather than an error, since a trigger binding has no
        `range` concept at all to reject a value FOR."""
        if mode not in ("trigger", "continuous"):
            raise ActionError(
                f"bind.learn: unknown mode {mode!r} (must be 'trigger' or 'continuous')")
        actions = self.actions.describe()
        info = actions.get(action)
        if info is None:
            raise ActionError(f"bind.learn: cannot learn unknown action: {action!r}")
        schema = info.get("args", {})
        args_template = dict(args)
        range_ = (0.0, 1.0)
        if mode == "continuous":
            if range:
                range_ = _parse_learn_range(range)
            float_args = sorted(k for k, t in schema.items() if t == "float")
            if len(float_args) != 1:
                raise ActionError(
                    f"bind.learn: continuous mode requires {action!r} to declare exactly "
                    f"one 'float' arg to auto-fill from the MIDI value, found "
                    f"{len(float_args)}: {float_args}")
            fill_key = float_args[0]
            if fill_key in args_template:
                raise ActionError(
                    f"bind.learn: {fill_key!r} is auto-filled from the MIDI value in "
                    f"continuous mode and must not be passed in args")
            args_template[fill_key] = None
        probe = bindings_mod.Binding(
            id="_probe", match=bindings_mod.BindingMatch(type="note_on", number=0),
            action=action, args=args_template, mode=mode, range=range_)
        error = bindings_mod.validate_binding(probe, actions)
        if error:
            raise ActionError(f"bind.learn: {error}")
        self._learn_armed = _LearnArm(action=action, mode=mode, args_template=args_template,
                                      range=range_, armed_at=time.time())
        self.emit_event("learn_armed", {"action": action, "mode": mode, "args": args_template})
        return {"armed": True, "action": action, "mode": mode}

    def _bind_cancel(self) -> dict:
        """`bind.cancel` action handler: disarms the learn slot if one is
        armed. A no-op (not an `ActionError`) when nothing is armed --
        unlike `bind.remove`'s "unknown id is a genuine caller error"
        precedent above, `bind.cancel` names no resource at all; "cancel
        whatever might be armed" is naturally idempotent, so calling it
        with nothing armed is a harmless confirmation, not a mistake worth
        surfacing loudly. No event is emitted for the no-op case either --
        `learn_cancelled` means "an arm that existed just ended", which
        isn't true here."""
        was_armed = self._learn_armed is not None
        self._learn_armed = None
        if was_armed:
            self.emit_event("learn_cancelled", {"reason": "cancelled"})
        return {"cancelled": was_armed}

    # -- capture.start / .stop / .status / .pin (Phase 5 Task 1,
    # docs/phase5-notes.md) -------------------------------------------------

    def _on_action_dispatched(self, name: str, args: dict, origin: str, result: dict) -> None:
        """`ActionRegistry`'s dispatch-hook (wired in `__init__` right
        after `self._capture` is constructed) -- fires after every
        SUCCESSFUL dispatch through the registry (bindings/behaviors/
        client; sysex bypasses the registry entirely, see
        `_handle_sysex`'s own handlers). `CaptureSink.record_action` is
        itself a no-op when not recording, so this is cheap even outside a
        capture session.

        Does NOT cover `capture.stop`'s own mark (see
        `_capture_stop_action`'s own docstring for why that one records
        itself explicitly, before this hook would even see it) --
        harmless overlap either way, since by the time this hook fires for
        `capture.stop`, recording has already ended and `record_action`'s
        own guard makes this a no-op.

        Phase 8 Task 5: also the seam `PageCycleBehavior.notify_page_action`
        is wired through for every REGISTRY-dispatched page-navigation
        action (client/binding/behavior/auto -- see `_PAGE_NAV_ACTIONS`'s
        own comment and `behaviors/pagecycle.py`'s "Origin ruling" docstring
        section for which of those actually pause rotation; filtering to
        "client" only happens INSIDE `notify_page_action`, not here). Sysex
        bypasses this hook entirely (same as `record_action` above) and,
        per that same "Origin ruling" section's fix-round-1 evidence,
        deliberately never calls `notify_page_action` at all -- v1's own
        SysEx dispatch never pauses the page cycler either.

        `time.time()` is read via `self._clock()`, not directly -- see
        `Engine.__init__`'s own comment next to `self._clock` for why a
        raw `time.time()` here silently broke two fake-clock engine tests
        (fix round 1, Important reviewer finding)."""
        self._capture.record_action(name, args, origin)
        if name in _PAGE_NAV_ACTIONS:
            self._pagecycle_behavior.notify_page_action(origin, self._clock())

    def _capture_write_failed(self, exc: OSError) -> dict:
        """Shared write-failure containment (fix wave, Critical finding:
        see `CaptureSink.fail`'s own docstring for the full incident and
        "why disable instead of retry" rationale) -- called from BOTH
        `_tick_capture_flush` (the background per-tick flush call site in
        `run()`) and `_capture_stop_action`'s own `except OSError` branch
        (a foreground `capture.stop` whose `CaptureSink.stop()` raised
        synchronously). Flips the `rec` chrome flag off, logs exactly ONE
        error, and emits an `alert` event -- the log line is invisible to
        anyone not tailing `journalctl`; the alert is what a connected
        client actually sees. Also emits `capture_stopped` (with an
        `error` field) when a session genuinely was active, matching the
        normal `capture.stop` action's own event shape as closely as
        possible so a client doesn't need a special code path to notice
        "the session ended, just abnormally"."""
        result = self._capture.fail(exc)
        self.analyzers["status"].set_rec(False)
        self._dirty.add("overlay.status")
        _LOG.error("capture: write failed (%s); capture disabled", exc)
        self.emit_event("alert", {
            "source": "capture", "level": "crit",
            "message": f"capture write failed: {exc}; capture disabled",
        })
        if result.get("session_id"):
            self.emit_event("capture_stopped", {
                "id": result["session_id"], "counts": result.get("counts", {}),
                "error": result.get("error"),
            })
        return result

    def _tick_capture_flush(self, now: float) -> None:
        """Wraps `CaptureSink.maybe_flush(now)` -- called from `run()`
        once per tick. Critical fix (docs/phase5-notes.md fix wave): this
        call used to be `self._capture.maybe_flush(now)` directly inline
        in `run()`, completely UNGUARDED and outside `run()`'s own
        try/except -- an `OSError` from `os.fsync` inside `flush()`
        (ENOSPC/EIO) propagated straight out of the WHOLE `while self.
        _running:` loop, killing the `run()` asyncio task with NO log
        line at all (the daemon stayed "active" to systemd -- no crash,
        no restart -- and MIDI processing simply stopped forever;
        reproduced live by test_engine_core.py::
        test_flush_write_failure_does_not_kill_the_run_loop_and_disables_capture).
        Mirrors `_dispatch_bindings`'s own "never let a single failure
        escape and kill the loop" precedent exactly."""
        try:
            self._capture.maybe_flush(now)
        except OSError as exc:
            self._capture_write_failed(exc)

    def _capture_start_action(self) -> dict:
        """`capture.start` action handler (also called directly from
        `__init__` for `config.capture_auto_start` -- see that call
        site's own comment for why it must ALSO explicitly record an
        `origin="auto"` mark, since a direct method call bypasses the
        dispatch hook entirely). Stamps the `rec` chrome flag on the
        ALWAYS-present "status" analyzer and marks `overlay.status` dirty
        directly (mirrors `_sendnotes_key`'s own "engine-level state
        change marks its page dirty itself" precedent -- there is no
        `MidiEvent` here for `TransportAnalyzer.handle()` to react to).

        Fix wave (Important finding): `CaptureSink.start()` can raise
        `OSError` (an unwritable/unreachable sessions directory) --
        converted here into a clean `ActionError` rather than letting the
        raw `OSError` escape `ActionRegistry.dispatch` past its own
        `except ActionError` narrowing and tear down the requesting
        client connection (`engine/server.py::ProtocolServer._dispatch`'s
        own `action` branch only ever catches `ActionError`)."""
        try:
            result = self._capture.start()
        except OSError as exc:
            _LOG.error("capture.start failed: %s", exc)
            raise ActionError(f"capture.start failed: {exc}") from exc
        self.analyzers["status"].set_rec(True)
        self._dirty.add("overlay.status")
        self.emit_event("capture_started", {"id": result["session_id"]})
        return {"recording": True, **result}

    def _capture_stop_action(self, *, origin: str = "unknown") -> dict:
        """`capture.stop` action handler. A no-op-shaped result (mirrors
        `CaptureSink.stop`'s own "stopping nothing is harmless" precedent)
        when nothing was recording -- no `capture_stopped` event in that
        case either, matching `bind.cancel`'s own "no event for a no-op"
        precedent right above.

        `origin` (fix wave, Important finding) -- INJECTED by
        `ActionRegistry.dispatch` (register-time introspection over this
        method's own signature, see `ActionRegistry.register`'s own
        docstring), never a client-suppliable arg. Needed because this
        handler must record its OWN `capture.stop` action mark EXPLICITLY,
        BEFORE actually stopping: `CaptureSink.stop()` flips `is_recording`
        False as its very first observable effect, and `record_action`'s
        own guard (`if not self._recording: return`) would otherwise
        silently swallow a stop mark reported through the NORMAL
        post-dispatch hook (which only ever fires AFTER this handler
        returns, by which point recording has already stopped) -- this is
        the asymmetry fix versus `capture.start` (which happens to work
        without any special handling: `_capture.start()` flips recording
        ON before the hook fires). `Engine.stop()` (a clean daemon
        shutdown, bypassing the registry entirely like the auto-start
        case) calls this directly with `origin="shutdown"` -- both
        `"auto"` and `"shutdown"` join the documented origin set
        (`binding:<id>` / `client` / `behavior` / `sysex` / `auto` /
        `shutdown`).

        Fix wave (Important finding): `CaptureSink.stop()` can also raise
        `OSError` (the final `flush()` inside it fails) -- routed through
        the SAME `_capture_write_failed` containment path a background
        flush failure uses, then re-raised as a clean `ActionError` for
        the requesting client (same "never let a raw OSError past
        ActionRegistry.dispatch" reasoning as `_capture_start_action`)."""
        self._capture.record_action("capture.stop", {}, origin)
        try:
            result = self._capture.stop()
        except OSError as exc:
            result = self._capture_write_failed(exc)
            raise ActionError(f"capture.stop failed: {exc}") from exc
        self.analyzers["status"].set_rec(False)
        self._dirty.add("overlay.status")
        if result.get("session_id"):
            self.emit_event("capture_stopped",
                            {"id": result["session_id"], "counts": result.get("counts", {})})
        return {"recording": False, **result}

    def _capture_status_action(self) -> dict:
        return self._capture.status()

    def _capture_pin_action(self, id: str) -> dict:
        """`capture.pin {id}` action handler -- translates `CaptureSink.
        pin`'s `ValueError` (unknown/not-yet-stopped session id) into
        `ActionError`, same "unknown named resource is a genuine caller
        error" precedent `_bind_remove` already sets."""
        try:
            return self._capture.pin(id)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc

    def _new_binding_id(self) -> str:
        """A short, stable, TOML-table-key-safe id for a freshly learned
        binding (`Binding.id`'s own contract, see that dataclass's
        docstring in engine/bindings.py) -- a millisecond wall-clock
        timestamp, de-duplicated against every currently-persisted binding
        id. A same-millisecond collision (two captures within 1ms of each
        other) is astronomically unlikely given learn always waits for a
        real, human-triggered MIDI event -- but cheap to guard against
        outright rather than merely disclose."""
        base = f"learn_{int(time.time() * 1000)}"
        existing = {b.id for b in self._bindings_file.bindings}
        candidate = base
        suffix = 1
        while candidate in existing:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _replace_bindings_with_exact_match(
            self, match: bindings_mod.BindingMatch) -> list[bindings_mod.Binding]:
        """DAW-standard replace-on-relearn (review Critical finding,
        live-reproduced): without this, re-learning an already-bound
        physical control just ADDS a second binding alongside the first --
        both then fire on every future trigger, silently accumulating
        forever. This is the primary DAW-learn use case (remapping a
        control you already bound something to), so it's a correctness
        bug, not a cosmetic one.

        Removes every currently-persisted binding whose `match` is
        EXACTLY equal to `match` (`BindingMatch` is a plain dataclass, so
        `==` already compares type/number/channel/port_pattern all
        together) -- deliberately EXACT equality, not `BindingDispatcher.
        _matches`'s own `fnmatch`-on-`port_pattern` semantics: a
        pre-existing WILDCARD binding (e.g. `port_pattern="Midi
        Through*"`) that would ALSO match the same physical event going
        forward is left untouched here. This is a disclosed, honest
        simplification -- exact-match relearn is unambiguous (there is
        only ever at most one prior binding it could mean "this one,
        specifically"), whereas removing every WILDCARD binding that
        happens to overlap would be a much more aggressive, surprising
        operation with no single obvious "the one being replaced" target
        (see test_engine_core.py::
        test_learn_capture_does_not_remove_an_overlapping_wildcard_port_binding).

        Returns the removed bindings (for the `learn_bound` event's own
        `"replaced"` field) but does NOT call `BindingsFile.save()` --
        `_capture_learn` adds the new binding to the SAME in-memory list
        right after this returns and saves ONCE, so the file transitions
        directly from "old binding present" to "new binding present" in a
        single atomic write, never through an intermediate state with
        neither on disk."""
        to_remove = [b for b in self._bindings_file.bindings if b.match == match]
        for b in to_remove:
            self._bindings_file.remove(b.id)
        return to_remove

    def _capture_learn(self, ev: MidiEvent) -> None:
        """Turn the currently-armed learn slot + `ev` into a real,
        persisted `Binding` -- called from `_handle` (see that method's
        own comment for why this must run BEFORE `self._binding_
        dispatcher.handle(ev)`).

        Review fix (Phase 5 Task 3, docs/phase5-notes.md carry-over from
        the phase-4 final review's own Important #1): `match.port_pattern`
        used to be `ev.source` written VERBATIM -- an exact-string match
        against a suffix (the ALSA `<client>:<port>` numbering) that
        reliably renumbers across a reboot/replug/rtpmidid session churn,
        silently turning every learned binding into a landmine that stops
        firing the moment its physical port re-enumerates. Now goes
        through `bindings_mod.glob_port_pattern` (see that function's own
        docstring for the exact suffix-stripping + fnmatch-escaping it
        does) -- the durable, glob-style equivalent that still matches the
        SAME physical/logical port under whatever client:port number ALSA
        assigns it next. `BindingDispatcher._matches`'s `fnmatch.fnmatch`
        needs no special-casing to consume either shape (exact or
        globbed) -- both are just patterns to it. Any binding(s) with this
        EXACT match (the globbed pattern, dataclass `==`) are replaced
        first (`_replace_bindings_with_exact_match`, see its own
        docstring) -- both the removal and this new binding's addition are
        persisted in ONE `BindingsFile.save()` call, and `learn_bound`'s
        payload discloses whatever was replaced."""
        arm = self._learn_armed
        self._learn_armed = None
        match = bindings_mod.BindingMatch(
            type=ev.type, number=ev.data1, channel=ev.channel,
            port_pattern=bindings_mod.glob_port_pattern(ev.source))
        replaced = self._replace_bindings_with_exact_match(match)
        binding = bindings_mod.Binding(
            id=self._new_binding_id(), match=match, action=arm.action,
            args=dict(arm.args_template), mode=arm.mode, range=arm.range)
        self._bindings_file.add(binding)
        self._bindings_file.save()
        self._binding_dispatcher.set_bindings(self._bindings_file.bindings)
        actions = self.actions.describe()
        self.emit_event("learn_bound", {
            "binding": self._serialize_binding(binding, actions),
            "replaced": [self._serialize_binding(b, actions) for b in replaced],
        })

    def _tick_learn(self, now: float) -> None:
        """Called from `run()` once per tick (mirrors `_tick_analyzers`/
        `_tick_pages`/`_tick_behaviors`'s own injected-`now` convention) --
        auto-cancels an armed learn slot that has sat unanswered past
        `bindings_mod.LEARN_TIMEOUT_S`, so a human who armed a learn and
        then walked away (or hit the wrong key/knob first) doesn't leave
        the slot armed forever, silently capturing whatever MIDI arrives
        next no matter how much later."""
        if self._learn_armed is None:
            return
        if now - self._learn_armed.armed_at >= bindings_mod.LEARN_TIMEOUT_S:
            self._learn_armed = None
            self.emit_event("learn_cancelled", {"reason": "timeout"})

    async def _dispatch_bindings(self) -> None:
        """Dispatch every binding-triggered action intent collected by
        `_handle` (via `self._binding_dispatcher.handle_with_origin(ev)`)
        since the last call -- the async half of the dispatch-context split
        docs/phase4-notes.md calls for (`_handle` is synchronous and
        cannot itself await `ActionRegistry.dispatch`; `behaviors/
        pagecycle.py`'s own `tick()` -> `_tick_behaviors` split is the
        identical precedent this reuses -- see the module docstring's
        "dispatch context decision"). Called once per `run()` iteration,
        unconditionally (a cheap no-op when nothing fired this tick).

        Exception containment (task-2 brief: "never crash the loop", "log
        + skip, NOT an alert storm"): an `ActionError` -- the EXPECTED
        failure mode for a persisted binding whose action doesn't exist in
        THIS build's roster (docs/phase4-notes.md's "action vocabulary is
        roster-dependent ... fail gracefully at dispatch" architecture
        fact) or whose stored `args` no longer satisfy that action's
        schema -- is logged at WARNING and swallowed, mirroring
        `_tick_behaviors`'s own `except ActionError: pass` (this adds the
        log line that one doesn't have -- see below for why). Any OTHER
        exception (a genuine bug in an action handler) is ALSO swallowed
        here, logged at ERROR instead -- a STRONGER guarantee than
        `_tick_behaviors` gives itself, justified because a binding's
        trigger is arbitrary EXTERNAL MIDI input arriving at MIDI rates,
        not an internal idle timer: one misbehaving handler must never be
        able to kill the whole engine `run()` loop from a single crafted or
        unlucky MIDI message. Neither case emits an `alert` event -- an
        `alert` is chrome meant for a human to notice on screen; a stale
        binding failing every time a knob moves would spam that channel
        into uselessness. The log is the record of this, not the UI (and
        is why this logs at all, unlike `_tick_behaviors`'s fully silent
        swallow -- a MIDI-triggered failure is much harder to notice/
        diagnose otherwise than an idle timer's occasional skip)."""
        pending, self._pending_binding_dispatches = self._pending_binding_dispatches, []
        for binding_id, name, args in pending:
            try:
                # Phase 5 Task 1 (event-sourced capture, docs/phase5-
                # notes.md): `origin=f"binding:{binding_id}"` -- picked up
                # by `ActionRegistry`'s dispatch hook (`Engine.
                # _on_action_dispatched`) ONLY on a successful dispatch,
                # never for the `ActionError` case caught right below (a
                # skipped/stale binding never fired, so it must not leave
                # an action mark behind).
                await self.actions.dispatch(name, args, origin=f"binding:{binding_id}")
            except ActionError as exc:
                _LOG.warning("binding dispatch skipped: action %r: %s", name, exc)
            except Exception:  # noqa: BLE001 -- see docstring: MIDI-driven, must never kill run()
                _LOG.exception("binding dispatch: unexpected error dispatching action %r", name)

    def _sendnotes_device_info(self) -> dict:
        """Bound into `pages/sendnotes.py`'s `SendNotesPage` at construction
        (see `__init__` above) -- exposes `self._midi_out`'s own open/closed
        state without handing the page a reference to the I/O object
        itself (pages never own MidiOutput -- see that page's module
        docstring)."""
        return {"is_open": self._midi_out.is_open, "port_name": self._midi_out.port_name}

    def _sendnotes_key(self, key: str) -> dict:
        """Handler for the `sendnotes.key` action: reads the wall clock
        ONCE here (the engine's job, mirroring `_tick_pages(time.time())`'s
        own call site in `run()`) and hands it to `SendNotesPage.apply_key`
        as an injected value -- the page itself never reads a clock. Any
        real note trigger the page reports is sent through `self._midi_out`
        immediately; a control-key adjustment (or an unrecognized key) never
        touches MIDI output at all."""
        now = time.time()
        page = self.pages["sendnotes"]
        result = page.apply_key(key, now)
        self._dirty.add("page.sendnotes")
        if result and result.get("note_on"):
            self._midi_out.note_on(result["note"], result["vel"], result["ch"])
        return {"applied": result is not None}

    def _wrap_page_action(self, page_name: str, handler: Callable[..., dict]) -> Callable[..., dict]:
        """Phase 4 Task 0 (engine consolidation, docs/phase4-notes.md): the
        ONE place that now does what used to be hand-written, per-action,
        inside each of the removed `_pianoroll_zoom`/`_pianoroll_projection`/
        `_pianoroll_channels`/`_img2txtviz_charset`/`_img2txtviz_invert`/
        `_clear_eventlog` methods -- called by the page-declared-actions
        loop in `__init__` to wrap every `(name, handler, description,
        args_schema)` a page's own `actions()` declares, so the page's
        handler itself can stay a small, pure dict-shaping method (see
        pages/pianoroll.py's/pages/img2txtviz.py's own `actions()`
        docstrings) with no idea an `Engine` or a dirty-set even exists:

        1. Marks `page.<page_name>` dirty after a SUCCESSFUL call --
           exactly the "this page-scoped action always dirties its own
           page" rule every removed handler above hand-coded individually.
        2. Translates a plain `ValueError` the handler raises (a page's own
           input-validation failure -- today only `pianoroll.projection`/
           `.channels`' `set_projection`/`set_channels`) into the
           client-facing `ActionError`, matching what `_pianoroll_projection`/
           `_pianoroll_channels` used to do by hand. Order matters: a raise
           here skips the dirty-marking below entirely, exactly like the
           old try/except-then-mark-dirty shape did (state didn't actually
           change, so it must not be reported dirty).

        Engine-owned actions that need real engine state (I/O, cross-
        roster navigation) -- `sendnotes.key`, `page.next`/`.prev`/`.goto`,
        `pagecycle.enable` -- are registered directly in `__init__` instead
        and never pass through here; see that method's own comments for
        why each one doesn't fit "one page's own pure state" alone.
        """
        def wrapped(**kwargs):
            try:
                result = handler(**kwargs)
            except ValueError as exc:
                raise ActionError(str(exc)) from exc
            self._dirty.add(f"page.{page_name}")
            return result
        return wrapped

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
        `__init__` above) -- the live page roster (cycle order), the full
        action registry, and (Phase 5 Task 3, docs/phase5-notes.md
        cheap-wins bundle: "help page renders live keymap") the live
        key->action `keymap` -- exactly what `describe` already reports
        over the wire (see `engine/server.py`), just rendered on-screen.
        `self.keymap` is read live (a fresh attribute lookup on every
        call, not a value frozen at `bind_info()` wiring time) so a
        `config.reload` that changes the keymap is reflected the very
        next time this page's `view_model()` runs, with no extra wiring
        needed -- same "read the engine's current attribute" contract
        `pages`/`actions.describe()` right beside it already have."""
        return {"pages": list(self.pages), "actions": self.actions.describe(),
                "keymap": self.keymap}

    def _discover_page_hooks(self, name: str, page: Page) -> None:
        """The ONE hasattr-based `PageHooks` discovery pass (Phase 4 Task
        0, docs/phase4-notes.md) -- called for every page in the initial
        roster (the loop in `__init__`) AND, so a page arriving later via
        `register_page` gets the exact same treatment (not a stale,
        hooks-less entry `_tick_pages` would silently skip forever), from
        `register_page` itself below. Also performs the one-shot
        `bind_info` wiring for whichever page names have a matching entry
        in `self._page_info_providers` -- see `PageHooks`' own docstring."""
        hooks = PageHooks(
            tick=getattr(page, "tick", None),
            drain_outputs=getattr(page, "drain_outputs", None),
            bind_info=getattr(page, "bind_info", None),
        )
        self._page_hooks[name] = hooks
        provider = self._page_info_providers.get(name)
        if hooks.bind_info is not None and provider is not None:
            hooks.bind_info(provider)

    def register_page(self, name: str, page: Page) -> None:
        """Append `page` to the live roster under `name`. Production pages
        register via `_PAGE_FACTORIES` + `config.pages`; this hook exists
        for pages with no factory yet (tests today; dynamically-arriving
        overlays later). Re-discovers `PageHooks` (tick/drain_outputs/
        bind_info) for `page` same as the initial roster build, but does
        NOT register any `actions()` it declares -- that loop only ever
        runs once, over the roster `__init__` builds; a future bindings
        task wiring pages in dynamically after construction will need its
        own call into the same action-registration loop shape if it wants
        those actions to be dispatchable too."""
        self.pages[name] = page
        self._discover_page_hooks(name, page)

    def register_analyzer(self, name: str, analyzer: Analyzer) -> None:
        """Append `analyzer` to the live roster under `name`, publishing it
        under `overlay.<name>`. Mirrors `register_page()` for the same
        no-production-factory-yet reasons (tests today)."""
        self.analyzers[name] = analyzer

    def _page_order(self) -> list[str]:
        return list(self.pages)

    def _recompute_keymap(self) -> None:
        """Recompute `self.keymap_global`/`self.keymap_page`/`self.keymap`
        (Phase 8 Task 6, docs/gui-phase-decisions-2026-08-08.md keymap
        revamp) from `self._raw_global_keymap`/`self._raw_page_keymaps` --
        called at construction, from every `_set_current_page` transition
        (a new current page has a different `[keys.<page>]` section), and
        after a successful `config.reload` keymap re-read.

        Filters the global table and the CURRENT page's own section
        SEPARATELY (each through `keymap_mod.filter_known_actions`
        against THIS build's action registry) rather than merging first
        and filtering once -- that's what lets `keymap_global`/`keymap_
        page` report two genuinely DISTINCT, non-duplicated sections: a
        key the current page overrides shows up in `keymap_page` only, not
        as a stale duplicate under `keymap_global` too. `clients/chrome.py`
        (the on-screen indicator + help-overlay renderers) and `engine/
        server.py`'s `describe` response both read these two fields
        directly for that section split; `self.keymap` (their simple
        union, page winning on a key present in both -- structurally
        impossible today since a key never survives filtering under both
        names at once, but page-wins is the documented tie-break) remains
        the single flat table `dispatch_key`/`bind.learn`/the help PAGE's
        `keymap_rows` all keep using unchanged."""
        actions_desc = self.actions.describe()
        self.keymap_global: dict[str, Any] = keymap_mod.filter_known_actions(
            self._raw_global_keymap, actions_desc)
        page_raw = self._raw_page_keymaps.get(self.current_page, {})
        self.keymap_page: dict[str, Any] = keymap_mod.filter_known_actions(
            page_raw, actions_desc)
        self.keymap: dict[str, Any] = {**self.keymap_global, **self.keymap_page}

    def _set_current_page(self, name: str) -> dict:
        self.current_page = name
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
        # the single funnel point for EVERY page transition regardless of
        # cause (page.next/.prev/.goto/.jump actions, a sysex
        # CMD_SWITCH_PAGE/CMD_SCREENSAVER-force, an idle pagecycle/
        # screensaver behavior) -- a `page_changed` mark here is complete
        # with no per-origin plumbing of its own, unlike action marks.
        # No-op when not recording (`CaptureSink.record_page_changed`'s
        # own guard).
        self._capture.record_page_changed(name)
        # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
        # revamp): `self.keymap_page` (and the effective `self.keymap`) is
        # PAGE-SCOPED now -- every transition through this one funnel point
        # must recompute it for the NEW current page BEFORE either event
        # goes out, so a client reacting to `page_changed` by resubscribing
        # and one reacting to `keymap_changed` by refetching both see the
        # post-transition state, never a stale one from the old page.
        self._recompute_keymap()
        self.emit_event("page_changed", {"page": name})
        # Emitting `keymap_changed` here too (not just from `config.
        # reload`) is the load-bearing design choice that makes per-page
        # keymap sections work with ZERO new client-side event-handling
        # code: `clients/tui.py`/`clients/fb/app.py` already refetch the
        # full keymap bundle on this exact event name (Phase 4 Task 1) --
        # reusing it means "the current page's own key section is live"
        # falls out of plumbing that already existed, rather than needing
        # a second event both clients would have to learn to react to.
        self.emit_event("keymap_changed", {"keymap": self.keymap, "warnings": []})
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
        """`page.goto`'s handler. A `name` genuinely unknown to this whole
        build (a typo, or any other caller's own bad page name) raises
        loudly (`ActionError`), unchanged since Phase-3.

        Phase 9 Task 0 (docs/gui-phase-decisions-2026-08-08.md digit-nav
        reconciliation) narrows that: a `name` that IS one of `marquee.
        PAGE_IDS`' known v1-mapped page names but simply isn't in THIS
        build's roster (e.g. "tuner" -- has a real v1 ID, excluded from
        config.py's default `pages` list) is treated as the SAME kind of
        ordinary, expected situation `_page_jump`'s out-of-range position
        already is below -- logged once and a plain no-op (`{}`), NOT an
        `ActionError`. This is what lets `engine/keymap.py::DEFAULT_
        KEYMAP`'s digit bindings (baked unconditionally from the FULL
        `PAGE_IDS` table, regardless of any one build's roster -- see that
        module's own docstring) stay harmless on a build that doesn't
        happen to include every v1-numbered page, instead of a keypress
        surfacing an "unknown page: tuner" `ClientError` to the user for a
        page name that isn't actually a mistake at all.

        This narrowing only ever makes MORE names succeed silently, never
        fewer error out: `_MARQUEE_PAGE_IDS` is a small, fixed vocabulary
        (14 names today) entirely unrelated to whatever a caller might
        type by accident, so a real typo (anything outside that
        vocabulary too) still raises exactly as before."""
        if name not in self.pages:
            if name in _MARQUEE_PAGE_IDS:
                _LOG.info(
                    "page.goto: %r has a v1 page ID but is not in this build's roster -- "
                    "no-op", name)
                return {}
            raise ActionError(f"unknown page: {name}")
        return self._set_current_page(name)

    def _page_jump(self, position: int) -> dict:
        """`page.jump`'s handler (Phase 8 Task 6) -- 1-indexed against the
        CURRENT roster order (`self._page_order()`, the same order `page.
        next`/`.prev` walk). Deliberately DIFFERENT failure behavior from
        `_page_goto` above: a `position` outside `[1, len(order)]` is an
        ORDINARY, expected situation -- logged once per occurrence and a
        plain no-op (`{}`, no `page_changed`/`keymap_changed` emitted), NOT
        an `ActionError`. A typo'd `page.goto` NAME, by contrast, is a
        genuine user mistake worth failing loudly on -- that distinction is
        the whole reason this is a separate action rather than a thin
        wrapper around `_page_goto`.

        STALE-AS-OF Phase 9 Task 0 update: `DEFAULT_KEYMAP` (engine/
        keymap.py) no longer binds any digit key to `page.jump` at all --
        its digit/shifted-digit bindings are now `page.goto {name: ...}`
        entries baked from `analyzers.marquee.PAGE_IDS` (see that module's
        own docstring). `page.jump` itself, and this out-of-range no-op,
        remain fully registered/dispatchable -- they matter now only for a
        HAND-WRITTEN `keymap.toml` that explicitly binds a key to
        `page.jump {position: N}` for roster-relative behavior no fixed-ID
        scheme can express (docs/phase4-bindings.md §1b)."""
        order = self._page_order()
        if not 1 <= position <= len(order):
            _LOG.info("page.jump: position %d out of range (roster has %d pages) -- no-op",
                      position, len(order))
            return {}
        return self._set_current_page(order[position - 1])

    @property
    def topics(self) -> list[str]:
        """All subscribable topics, roster order: pages first, then overlays."""
        return [f"page.{name}" for name in self.pages] + \
            [f"overlay.{name}" for name in self.analyzers]

    @property
    def midi_output_port_name(self) -> str:
        """Phase-3 task 12 fix: the daemon's own `MidiOutput` port name,
        exposed so `daemon.py::build()` can wire it into `MidiInput`'s
        `exclude_names` -- the layer-1 self-subscription defense (see
        `engine/midi_in.py`'s own module docstring for the full incident).
        A public accessor rather than daemon.py reaching into
        `engine._midi_out.port_name` directly."""
        return self._midi_out.port_name

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _broadcast(self, msg: dict) -> None:
        for cb in list(self._listeners):
            cb(msg)

    def emit_event(self, name: str, data: dict) -> None:
        self._broadcast(proto.event(name, data))

    def set_topic_refcount_provider(self, provider: Callable[[str], int]) -> None:
        """Engine<->server seam for the finding-1 perf fix (2026-08-07 fix
        wave): `ProtocolServer` is the only thing that actually knows which
        topics have live subscribers (its own `conn.topics` sets across all
        connected clients) -- it calls this once, at its own construction,
        with a callable `topic -> subscriber count`. `_flush_dirty()`
        consults it before calling `snapshot_now` for each dirty topic,
        skipping `view_model()` materialization entirely for a topic with a
        refcount of exactly 0 (see that method's own docstring for the
        root-cause measurement: Img2TxtVizAnalyzer.tick() always returns
        True, so `page.img2txtviz` was dirty every tick at 30Hz and paying
        its 6.94ms view_model() cost even with zero clients ever attached).

        No provider wired (the default -- e.g. a bare `Engine` in a unit
        test with no `ProtocolServer` attached at all, or any of this
        file's own `add_listener()`-based tests) means "nobody has ever
        told me who's subscribed", which is NOT the same claim as "nobody
        is subscribed" -- the unwired default therefore always
        materializes every dirty topic, preserving every such test's
        existing behavior unchanged. This is deliberately a PULL seam (the
        engine asks, on its own schedule) rather than the server pushing
        refcount updates into the engine -- the engine already owns the
        single `_flush_dirty()` call site that needs the answer, and a
        pull avoids a second synchronization surface between the two.

        Note what this does NOT gate: `snapshot_now()` itself (the
        subscribe-time initial-delivery path in `ProtocolServer._dispatch`
        calls it directly) and `emit_event()` (events, unlike snapshots,
        are never gated by subscriber presence at the engine level --
        server.py's own slow-client high-water check is the only
        backpressure events ever get) are both untouched by this seam --
        see test_engine_core.py::
        test_snapshot_now_is_never_gated_by_the_refcount_provider."""
        self._topic_refcount_provider = provider

    def set_open_ports_provider(self, provider: Callable[[], list[str]]) -> None:
        """Engine<->`MidiInput` seam (Phase 5 Task 3, docs/phase5-notes.md
        cheap-wins bundle) -- mirrors `set_topic_refcount_provider` right
        above exactly (same PULL shape, same "engine asks on its own
        schedule" rationale): `daemon.py::build()` calls this once, right
        after constructing its `MidiInput`, with a callable returning that
        `MidiInput.open_ports`'s CURRENT list (a live property read at
        provider-call time, not a snapshot frozen at wiring time -- a port
        vanishing/appearing between `bind.list` calls must be reflected on
        the very next call). `_bind_list`/`_serialize_binding` are the only
        consumers, computing each binding's `port_present` field (see
        `_serialize_binding`'s own docstring).

        No provider wired (the default -- `--no-midi`, or any test
        constructing a bare `Engine()` with no daemon/MidiInput at all,
        which is the overwhelming majority of this test suite) means
        "there is no live port roster to check against at all", NOT "no
        ports are open" -- `_port_present` treats that case as
        `True` (never spuriously reports a binding as port-absent just
        because nothing ever told this engine what's open), matching
        `_topic_refcount_provider`'s own "unwired means preserve prior
        behavior, don't guess a stricter answer" precedent."""
        self._open_ports_provider = provider

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
        # Phase-3 task 12 fix (Critical, live-reproduced self-subscription
        # feedback loop): drop ANY event whose `source` refers to our own
        # `MidiOutput` port, before touching anything else -- events_total,
        # activity, analyzers, pages, all of it. This is LAYER 2 of a
        # two-layer defense (layer 1: `engine/midi_in.py::MidiInput`'s own
        # `exclude_names`, which should already prevent this port from ever
        # being opened as an input at all -- see that module's own
        # docstring for the full incident writeup). Layer 2 exists as cheap
        # insurance against ANY future code path that opens a port without
        # going through that scan, or a bug there: a self-subscription loop
        # is catastrophic enough (a SysEx reply re-parses as a brand-new
        # valid command with no origin field anywhere in the wire format,
        # so it would otherwise loop forever) that a second, independent
        # check here is cheap. Substring match (not exact), matching layer
        # 1's own reasoning -- the real ALSA-enumerated source name wraps
        # our configured port name in backend-specific client/port framing.
        if ev.source and self._midi_out.port_name in ev.source:
            return
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
        # raw MIDI is ground truth (decided design, point 1) -- recorded
        # unconditionally here, before any learn/binding/analyzer/page
        # branching below, so a capture reflects EXACTLY what the rest of
        # the engine saw, regardless of what (if anything) later consumed
        # it. `record_event` is a no-op when not recording.
        self._capture.record_event(ev)
        # Phase 4 Task 3 (DAW-style MIDI learn, docs/phase4-notes.md): an
        # armed learn slot CONSUMES the next qualifying event -- it must
        # NOT also reach the binding dispatcher below, or a learn capture
        # on a note that already has an existing binding would fire that
        # stale binding's action in the same instant it's being remapped
        # (task brief: "a learned-this-instant event must not also fire
        # existing bindings" -- see test_engine_core.py::
        # test_learn_captured_event_is_not_also_dispatched_to_an_existing_
        # matching_binding for the live-shaped proof). Deliberately scoped
        # to the BINDING dispatcher only, via this if/else -- analyzers/
        # pages further down still see this event completely normally (it
        # IS a real, currently-arriving MIDI message; only a pre-existing
        # binding's own action is withheld -- see test_engine_core.py::
        # test_learn_captured_event_still_reaches_pages_and_analyzers_
        # normally). Checked BEFORE the self-output-filter's sibling
        # concerns below (activity stamp, sysex, analyzers/pages) so a
        # capture always happens on the very same tick the qualifying
        # event arrives, never delayed a tick behind them.
        if self._replay:
            # Phase 5 Task 2 (session replay, docs/phase5-notes.md): THE
            # SEAM -- gates the exact same learn-arming/binding-dispatch-
            # collection branch this `if/else` already was, per the
            # decided design's point 2 ("the seam: the learn/dispatch
            # branch atop `_handle` -- gate on a replay flag"). A replaying
            # engine must never (a) let an armed learn slot consume a
            # REPLAYED event (nothing is actually "learning" from a MIDI
            # message that already happened, possibly hours/days ago), and
            # must never (b) collect a binding-dispatch intent at all --
            # `engine/replay.py`'s driver feeds events straight into
            # `_handle` and NEVER calls `_dispatch_bindings`/`run()`, so an
            # uncollected intent here would otherwise just silently
            # accumulate in `self._pending_binding_dispatches` forever
            # (a real, if slow, memory leak over a long replay) rather than
            # ever firing -- gating the collection itself, not merely
            # leaving it undispatched, is both the documented seam AND the
            # cheaper choice. The three reasons this must be suppressed
            # (not just "happens to never fire" as a side effect of no
            # `run()` loop existing): double-firing (the session's own
            # action marks already record what fired live -- re-deriving
            # and re-dispatching the SAME intent from raw MIDI would double
            # it), non-determinism (dispatching for real would consult
            # WHATEVER `bindings.toml` happens to be loaded on the machine
            # replaying this file, which can differ from -- or have
            # drifted since -- the session's own original recording), and
            # in-memory state that was never captured (a continuous
            # binding's CC-edge baseline lives only in `BindingDispatcher`'s
            # own live memory, never written to the session log, so
            # "replaying" it for real would be replaying against a cold,
            # not the original, baseline). Behaviors need no analogous gate
            # here at all -- they only ever run from `_tick_behaviors`,
            # which only `run()` ever calls, and replay never calls `run()`.
            pass
        elif self._learn_armed is not None and bindings_mod.is_learnable_event(ev):
            self._capture_learn(ev)
        else:
            # Phase 4 Task 2 (MIDI bindings, docs/phase4-notes.md): bindings
            # consume every event BEFORE analyzers/pages get it -- `_handle`
            # is synchronous and `ActionRegistry.dispatch` is a coroutine, so
            # this only ever COLLECTS intents here; `_dispatch_bindings`
            # (called from `run()` right after `_handle` returns, see that
            # method's own docstring) is the one place they actually fire.
            # Collected even for a `clock_tick`/`sysex`/etc event --
            # harmless, since `BindingDispatcher._matches` only ever matches
            # `note_on`/`control_change` types, so every other event type is
            # a guaranteed no-op pass-through here.
            self._pending_binding_dispatches.extend(
                self._binding_dispatcher.handle_with_origin(ev))
        self.events_total += 1
        if ev.type in _ACTIVITY_EVENT_TYPES:
            self._last_activity_ts = ev.ts
        # Phase-3 task 12: sysex is handled BEFORE the generic analyzer/page
        # loop below (mirrors this method's own activity-stamp ordering) --
        # its dispatch can itself change `self.current_page`/mutate
        # behaviors, and the analyzer/page loop right after should see that
        # NEW state for the rest of this same event (harmless either way
        # today, since no analyzer/page branches on ev.type=="sysex", but
        # keeps engine-level side effects grouped at the top of _handle,
        # matching the activity-stamp's own placement).
        if ev.type == "sysex":
            self._handle_sysex(ev)
        for name, analyzer in self.analyzers.items():
            if analyzer.handle(ev):
                self._dirty.add(f"overlay.{name}")
        for name, page in self.pages.items():
            if page.handle(ev):
                self._dirty.add(f"page.{name}")

    # -- sysex remote control (phase-3 task 12, gap ports) -----------------
    #
    # DISPATCH half of v1's `plugins/sysex.py` -- see engine/sysex.py's own
    # module docstring for the PARSE half (pure, no side effects) this
    # calls into. Every method below is a thin, testable wrapper: parse ->
    # (maybe) mutate engine state through the SAME methods a normal client
    # action would use -> (maybe) send a reply through self._midi_out.

    def _handle_sysex(self, ev: MidiEvent) -> None:
        if ev.sysex_data is None:
            return
        parsed = sysex_mod.parse_command(ev.sysex_data)
        if parsed is None:
            return   # not addressed to midicrt at all -- true no-op, matches v1
        # Any matched-prefix frame counts as activity, however it parses
        # from here -- mirrors v1's own unconditional `ss.deactivate()`
        # (screensaver wake) on ANY valid-prefix SysEx command, checked
        # BEFORE marker/version parsing even begins. v2 has no direct
        # "wake" primitive on the screensaver behavior (see
        # `_sysex_screensaver`'s own docstring) -- bumping the SAME
        # activity clock every OTHER wake path already uses lets
        # `ScreensaverBehavior.tick()`'s own real-activity-advance branch
        # do the restore on the very next tick, exactly as if a note had
        # arrived.
        self._last_activity_ts = ev.ts
        # v2 addition (disclosed): v1's only observability for arriving
        # commands is a transient footer message + file logging
        # (`_log()`/`sysex.log`/`sysex.d/`, none of which this task ports
        # -- see engine/sysex.py's module docstring). An `alert`-shaped
        # engine EVENT (the same `emit_event` path `_tick_analyzers`'s own
        # `drain_alerts()` already uses) gives any connected client
        # real-time visibility for free, with no new transport machinery.
        self.emit_event("sysex_command", {
            "version": parsed["version"], "cmd": parsed["cmd"],
            "args": list(parsed["args"]), "error": parsed["error"],
        })
        if parsed["error"] == "missing-cmd":
            return   # no cmd byte to reply about -- matches v1 (logs, no reply)
        if parsed["error"] == "unsupported-version":
            # v1's own reply quirk, preserved: the REPLY's version byte is
            # the highest SUPPORTED version, not the (unsupported) one that
            # was requested.
            reply = sysex_mod.build_reply(
                max(sysex_mod.SUPPORTED_PROTOCOL_VERSIONS), parsed["cmd"], 0x01,
                sysex_mod.SUPPORTED_PROTOCOL_VERSIONS)
            self._midi_out.send_sysex(reply)
            return
        self._dispatch_sysex_command(parsed["version"], parsed["cmd"], parsed["args"])

    def _dispatch_sysex_command(self, version: int | None, cmd: int, args: tuple[int, ...]) -> None:
        if cmd == sysex_mod.CMD_SWITCH_PAGE:
            self._sysex_switch_page(version, args)
        elif cmd == sysex_mod.CMD_SCREENSAVER:
            self._sysex_screensaver(version, args)
        elif cmd == sysex_mod.CMD_PAGE_CYCLE:
            self._sysex_page_cycle(version, args)
        elif cmd == sysex_mod.CMD_CAPTURE_RECENT:
            self._sysex_capture_recent(version)
        elif cmd == sysex_mod.CMD_CAPABILITIES:
            self._sysex_capabilities(version)
        elif version is not None:
            self._midi_out.send_sysex(sysex_mod.build_reply(version, cmd, 0x01))

    def _sysex_switch_page(self, version: int | None, args: tuple[int, ...]) -> None:
        """CMD_SWITCH_PAGE (`01 <page>`): v1 numeric page ID -> v2 page
        name via `_SYSEX_PAGE_ID_MAP`, dispatched through the SAME
        `_page_goto` a normal `page.goto` action uses (so it emits the
        same `page_changed` event, marks the same dirty topic -- no
        parallel page-switch code path)."""
        if not args:
            if version is not None:
                self._midi_out.send_sysex(
                    sysex_mod.build_reply(version, sysex_mod.CMD_SWITCH_PAGE, 0x01))
            return
        page_id = args[0]
        name = _SYSEX_PAGE_ID_MAP.get(page_id)
        if name is None or name not in self.pages:
            if version is not None:
                self._midi_out.send_sysex(
                    sysex_mod.build_reply(version, sysex_mod.CMD_SWITCH_PAGE, 0x01, (page_id,)))
            return
        self._page_goto(name)
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
        # sysex never goes through `ActionRegistry.dispatch` (it calls
        # `_page_goto` directly, above) so it can't rely on the dispatch
        # hook every OTHER origin uses -- this is the 4th of the "four
        # dispatch sites" instrumented directly, calling the SAME
        # `CaptureSink.record_action` primitive the hook itself calls,
        # with the equivalent action name/args a `page.goto` action call
        # would have carried.
        self._capture.record_action("page.goto", {"name": name}, "sysex")
        # Phase 8 Task 5 (fix round 1, reviewer-found reversal): deliberately
        # does NOT call `PageCycleBehavior.notify_page_action` here -- v1's
        # own `plugins/sysex.py::_dispatch()` CMD_SWITCH_PAGE branch calls
        # `midicrt.switch_page()` directly and never touches
        # `notify_keypress()` either (verified by reading v1 source; only a
        # literal physical keystroke does, `midicrt.py`'s
        # `keyboard_listener()`). A SysEx page switch is remote-control/
        # automation territory in v1 ITSELF, the same category as a learned
        # binding -- see behaviors/pagecycle.py's "Origin ruling" docstring
        # section for the full evidence trail.
        if version is not None:
            self._midi_out.send_sysex(
                sysex_mod.build_reply(version, sysex_mod.CMD_SWITCH_PAGE, 0x00, (page_id,)))

    def _sysex_screensaver(self, version: int | None, args: tuple[int, ...]) -> None:
        """CMD_SCREENSAVER (`02 <0|1>`): `0`=wake, `1`=force on immediately.

        "Wake" needs no code here at all -- `_handle_sysex`'s own
        unconditional activity-bump (see its docstring) already lets
        `ScreensaverBehavior.tick()`'s real restore path fire on the very
        next tick, exactly like a note arriving would.

        "Force on immediately" dispatches `page.goto screensaver` directly,
        bypassing `ScreensaverBehavior`'s own idle-timer state machine
        entirely -- a disclosed limitation this INHERITS, not introduces:
        `ScreensaverBehavior.tick()`'s "not active" branch already treats
        `current_page == screensaver_page` as "already there, nothing to
        latch" without ever setting its own `_active`/`_previous_page`
        bookkeeping (see that module's own `tick()`), so ANY direct
        `page.goto screensaver` -- from this command, from a raw `midicrt
        action` call, from any other client -- leaves automatic restore-on-
        activity unarmed until a later idle/active cycle re-latches it
        normally. Fixing that general interaction is out of this task's
        scope (porting sysex control, not auditing every existing
        `page.goto screensaver` call site); v1 has no equivalent case at
        all (no page concept to leave "unrestored").
        """
        if not args:
            if version is not None:
                self._midi_out.send_sysex(
                    sysex_mod.build_reply(version, sysex_mod.CMD_SCREENSAVER, 0x01))
            return
        if args[0] != 0 and "screensaver" in self.pages:
            self._page_goto("screensaver")
            # Phase 5 Task 1: see `_sysex_switch_page`'s own comment --
            # same "sysex bypasses the dispatch hook, call the primitive
            # directly" reasoning.
            self._capture.record_action("page.goto", {"name": "screensaver"}, "sysex")
            # Phase 8 Task 5 (fix round 1): same origin-ruling reasoning as
            # `_sysex_switch_page` -- no `notify_page_action` call here
            # either, v1's own SysEx dispatch never pauses the page cycler.
        if version is not None:
            self._midi_out.send_sysex(sysex_mod.build_reply(
                version, sysex_mod.CMD_SCREENSAVER, 0x00, (int(args[0] != 0),)))

    def _sysex_page_cycle(self, version: int | None, args: tuple[int, ...]) -> None:
        """CMD_PAGE_CYCLE (`03 <0|1>`): dispatches through the SAME
        `_pagecycle_enable` the `pagecycle.enable` action uses (see its own
        registration comment)."""
        if not args:
            if version is not None:
                self._midi_out.send_sysex(
                    sysex_mod.build_reply(version, sysex_mod.CMD_PAGE_CYCLE, 0x01))
            return
        self._pagecycle_enable(bool(args[0]))
        # Phase 5 Task 1: see `_sysex_switch_page`'s own comment -- same
        # "sysex bypasses the dispatch hook, call the primitive directly"
        # reasoning.
        self._capture.record_action("pagecycle.enable", {"enabled": bool(args[0])}, "sysex")
        if version is not None:
            self._midi_out.send_sysex(sysex_mod.build_reply(
                version, sysex_mod.CMD_PAGE_CYCLE, 0x00, (int(bool(args[0])),)))

    def _sysex_capture_recent(self, version: int | None) -> None:
        """CMD_CAPTURE_RECENT (`04 [bars]`): NOT ported -- capture/replay
        is Phase 5, unbuilt (docs/phase3-parity.md's sign-off section).
        A versioned frame gets an honest error reply (status 0x01) rather
        than silently claiming success; a legacy frame has no reply
        channel at all, matching v1 exactly."""
        if version is not None:
            self._midi_out.send_sysex(
                sysex_mod.build_reply(version, sysex_mod.CMD_CAPTURE_RECENT, 0x01))

    def _sysex_capabilities(self, version: int | None) -> None:
        """CMD_CAPABILITIES (`10`, versioned-only -- matches v1's own
        "requires-versioned-frame" rule; a legacy frame has no reply
        channel to answer on regardless)."""
        if version is None:
            return
        payload = self._sysex_capabilities_payload(version)
        self._midi_out.send_sysex(
            sysex_mod.build_reply(version, sysex_mod.CMD_CAPABILITIES, 0x00, payload))

    def _sysex_capabilities_payload(self, version: int) -> tuple[int, ...]:
        """Adapted from v1's `_capabilities_payload` -- v1's `profile`/
        `backend` bytes have no v2 analog (fb/tui are separate client
        BINARIES here, not engine-side profiles the way v1's `run_tui`/
        `run_pixel` were), disclosed as `0` (unknown/not-applicable)
        rather than guessing a mapping. `feature_flags` reports v2's REAL
        capabilities (capture is Phase 5, unbuilt; screensaver/pagecycle
        are always-available behaviors here, not optional plugins).
        `pages` reports the v1 numeric IDs THIS build can actually reach
        (`_SYSEX_PAGE_ID_MAP` filtered to `self.pages`), letting a Cirklon
        operator's existing numeric-ID vocabulary double as a capability
        probe."""
        reachable_ids = sorted(pid for pid, name in _SYSEX_PAGE_ID_MAP.items()
                               if name in self.pages)
        feature_flags = [
            0,   # capture -- Phase 5, unbuilt
            1,   # screensaver -- always available (behaviors/screensaver.py)
            1,   # pagecycle -- always available (behaviors/pagecycle.py)
        ]
        payload = [
            version,
            0,   # profile -- no v2 analog, disclosed 0
            0,   # backend -- no v2 analog, disclosed 0
            len(sysex_mod.SUPPORTED_PROTOCOL_VERSIONS), *sysex_mod.SUPPORTED_PROTOCOL_VERSIONS,
            len(feature_flags), *feature_flags,
            len(reachable_ids), *reachable_ids,
        ]
        return tuple(max(0, min(127, int(v))) for v in payload)

    def _pagecycle_enable(self, enabled: bool) -> dict:
        self._pagecycle_behavior.enabled = enabled
        return {"enabled": enabled}

    def stop(self) -> None:
        self._running = False
        # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md): a
        # clean shutdown (SIGTERM/SIGINT -- see daemon.py's own
        # `run()`/signal handling) finalizes any still-active capture
        # session (flush + write its index.json row) rather than silently
        # orphaning it -- see engine/capture.py's module docstring's
        # "Writer design" section for what an UNCLEAN shutdown (a crash)
        # still loses (up to ~flush_interval_s of buffered events/marks,
        # plus the index row itself). A no-op when nothing is recording
        # (`CaptureSink.stop`'s own guard). Reuses `_capture_stop_action`
        # itself (not a hand-rolled duplicate) so shutdown gets the exact
        # same `rec`-flag/dirty-marking/`capture_stopped` event behavior a
        # normal `capture.stop` action gets. `origin="shutdown"` -- this
        # call bypasses `ActionRegistry.dispatch` entirely (a direct
        # method call, like the auto-start case in `__init__`), so it
        # must supply its own origin explicitly (see
        # `_capture_stop_action`'s own docstring for the full origin-
        # injection mechanism this is the one exception to).
        if self._capture.is_recording:
            # Fix-wave fix (2026-08-07, Important finding): `_capture_stop_
            # action` can raise `ActionError` when `CaptureSink.stop()`'s own
            # final flush hits an `OSError` (ENOSPC/EIO) -- but the
            # CONTAINMENT for that (rec flag off, alert emitted, session
            # disabled -- see `_capture_write_failed`) already ran INSIDE
            # that call, before it re-raised. That re-raise exists so a
            # normal `capture.stop` DISPATCH reports the failure to the
            # requesting client; `Engine.stop()` is not a dispatch caller
            # (a direct method call, same as the auto-start case in
            # `__init__`), and letting it propagate here would abort this
            # method's OWN shutdown ordering right below -- skipping the
            # sendnotes `flush_all()` note-off flush and `_midi_out.close()`
            # -- on nothing more than a full disk, live-reproducing the
            # exact phase-3 "stuck notes on real hardware" bug class this
            # code was originally written to prevent. Suppressed, not
            # logged again: `_capture_write_failed` already logged ONE
            # error and emitted an `alert` for this exact incident.
            with contextlib.suppress(ActionError):
                self._capture_stop_action(origin="shutdown")
        # Phase-3 task 12 fix (Important, live-reproduced): flush any
        # still-gated Send Notes BEFORE closing midi_out -- a routine
        # restart mid-note used to just close the port out from under
        # `drain_expired`'s future note_off, leaving a real downstream
        # synth holding a stuck note. `flush_all()` needs no injected
        # clock (see that method's own docstring), so this is safe to call
        # synchronously right here, while the port is still open/usable.
        sendnotes = self.pages.get("sendnotes")
        if sendnotes is not None:
            for note, ch in sendnotes.flush_all():
                self._midi_out.note_off(note, ch)
        # Phase-3 task 12: unlike MidiInput (owned/started/stopped by
        # daemon.py, injected via self.queue), MidiOutput is constructed
        # entirely inside Engine.__init__ with no external owner -- so
        # Engine.stop() closes it directly rather than adding a second
        # daemon.py-level shutdown call site. A safe no-op if the port was
        # never actually opened (see MidiOutput.close()'s own docstring).
        self._midi_out.close()

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
        describes for `analyzers/stucknotes.py`'s escalation).

        Phase 4 Task 0 (engine consolidation, docs/phase4-notes.md): both
        passes below now read `self._page_hooks` (built once, at
        construction, by the `PageHooks` discovery pass in `__init__` --
        see that dataclass's own docstring) instead of re-checking
        `hasattr(page, "tick")` on every single tick, and `drain_outputs`
        is looked up the SAME generic way rather than the old
        `self.pages.get("sendnotes")` name-keyed special case this method
        used to have (the one page needing `drain_outputs()` today is
        still "sendnotes" -- `SendNotesPage.drain_outputs` delegates to its
        own real, directly-tested `drain_expired`, see that page's own
        docstring -- but this loop no longer needs to know that by name).
        Each expired `(note, ch)` pair still gets a REAL `MidiOutput.
        note_off` here -- the one place in this whole method that does
        I/O, mirroring `_tick_analyzers`'s own `drain_alerts()` ->
        `emit_event` split (pure module reports, engine acts).

        Ordering contract (fix, code-review finding): this is deliberately
        TWO SEPARATE passes over `self._page_hooks` -- every page's
        `tick()` runs first, THEN every page's `drain_outputs()` runs --
        not one merged per-page loop. The shipped default roster happens
        to have "sendnotes" (the only `drain_outputs()` implementer today)
        LAST, so a single merged loop (tick-then-drain for each page
        before moving to the next) would look identical to this for as
        long as that stays true -- but it would silently interleave a
        LATER page's tick with an EARLIER page's drain the moment any
        roster puts a draining page before a ticking page (e.g. a custom
        `config.pages` order, or a future page registered via
        `register_page` -- see
        test_tick_pages_drains_strictly_after_all_ticks_even_when_drain_page_precedes_ticker_in_roster
        in test_engine_core.py, which reproduces exactly that). Two passes
        make the ordering guarantee hold regardless of roster order, at no
        real cost (`self._page_hooks` is small, already materialized).
        """
        for name, hooks in self._page_hooks.items():
            if hooks.tick is not None and hooks.tick(now):
                self._dirty.add(f"page.{name}")
        for name, hooks in self._page_hooks.items():
            if hooks.drain_outputs is not None:
                for note, ch in hooks.drain_outputs(now):
                    self._dirty.add(f"page.{name}")
                    self._midi_out.note_off(note, ch)

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
                # Phase 5 Task 1 (event-sourced capture, docs/phase5-
                # notes.md): origin="behavior" -- an idle-triggered
                # page.next/page.goto has no more specific provenance to
                # report (unlike a binding's own id).
                await self.actions.dispatch(name, args, origin="behavior")
            except ActionError:
                # e.g. a custom config removed the "screensaver" page a
                # behavior tries to `page.goto` -- an unattended internal
                # tick crashing the whole run() loop would be far worse
                # than one skipped auto-action; see module docstring.
                pass

    def _flush_dirty(self) -> None:
        """Broadcast a snapshot for every topic marked dirty this tick --
        extracted from `run()`'s own loop body (finding-1 fix, 2026-08-07
        fix wave) so it's directly callable from a synchronous test with no
        real asyncio `run()` task needed. Skips `snapshot_now`
        materialization entirely for any topic a wired
        `_topic_refcount_provider` reports as having zero subscribers --
        see that setter's own docstring for the full root-cause writeup and
        why the unwired default (no provider) must never skip anything."""
        for topic in sorted(self._dirty):
            if self._topic_refcount_provider is not None and self._topic_refcount_provider(topic) <= 0:
                continue
            snap = self.snapshot_now(topic)
            if snap:
                self._broadcast(snap)
        self._dirty.clear()

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
            # Phase 4 Task 2 (MIDI bindings): dispatch every intent this
            # iteration's `_handle` call(s) collected, right after the
            # event-draining block above and before the wall-clock ticks
            # below -- see `_dispatch_bindings`'s own docstring for why
            # this can't happen synchronously inside `_handle` itself.
            await self._dispatch_bindings()
            now = time.time()
            self._tick_analyzers(now)
            self._tick_pages(now)
            await self._tick_behaviors(now)
            # Phase 4 Task 3 (DAW-style MIDI learn): auto-cancel an armed
            # learn slot past its timeout -- see `_tick_learn`'s own
            # docstring. Placed after `_tick_behaviors` (an armed learn has
            # no interaction with pagecycle/screensaver) and before
            # `_flush_dirty` (a `learn_cancelled` event this call emits
            # should reach clients in the same tick it fires).
            self._tick_learn(now)
            # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
            # the writer's flush cadence gate -- see `CaptureSink.
            # maybe_flush`'s own docstring for why this tick-driven cadence
            # (piggybacking on `run()`'s existing per-iteration wall clock,
            # not a dedicated thread/asyncio task) is the deliberate,
            # disclosed choice here: `run()` already drains a whole queued
            # burst of events before reaching this point (the `while not
            # self.queue.empty()` loop above), so a MIDI storm doesn't
            # starve the flush cadence, and `tick_hz`'s default 30Hz
            # (~33ms) comfortably services a ~1s cadence without a second
            # concurrency domain to reason about. Critical fix
            # (docs/phase5-notes.md fix wave): calls `_tick_capture_flush`
            # (NOT `self._capture.maybe_flush(now)` directly anymore) --
            # see that method's own docstring for the write-failure
            # containment this now guards against.
            self._tick_capture_flush(now)
            self._flush_dirty()
