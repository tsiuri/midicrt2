"""PageCycleBehavior: v1's `~/codex/midicrt/plugins/pagecycle.py` semantics,
RESTORED VERBATIM. Phase 8 Task 5 (docs/superpowers/sdd/
2026-08-08-midicrt2-phase8-gui/task-5-brief.md) is an explicit, user-ruled
reversal of Phase 3 Task 9's re-interpretation this module used to
implement -- the 2026-08-08 decisions doc's own words: **"get page cycling
turned back on; it should do the same pages it used to do."**
`docs/visual-audit.md` §20e names this exact redo ("DIFFERENT, explicitly
slated for a redo") and `docs/phase3-parity.md`'s own Sign-off DEFERRED
list carries the composition bug this fixes. This docstring is the full
record of what changed and why -- read it before assuming any T9-era
assumption about this module still holds.

v1 ground truth (re-verified this session, read from source, not memory)
---------------------------------------------------------------------------
`~/codex/midicrt/plugins/pagecycle.py::draw(state)` runs on every UI-loop
pass (`midicrt.py`'s main loop calls every plugin's `draw()` at
`_runtime_tuning["plugin_overlay_interval"]` cadence, completely
independent of whether the transport is running or MIDI is flowing) and:

    if not ENABLED: return
    if _last_keypress is not None and now - _last_keypress < USER_PAUSE: return
    if now - _last_switch >= INTERVAL:
        _page_index = (_page_index + 1) % len(CYCLE_PAGES)
        midicrt.current_page = CYCLE_PAGES[_page_index]
        _last_switch = now

i.e. an UNCONDITIONAL wall-clock interval timer rotating through a fixed,
curated page-ID list, gated ONLY by a recent KEYPRESS -- never by MIDI
activity, never by whether the transport is playing. `notify_keypress()`
(stamps `_last_keypress = time.time()`) is called from `midicrt.py`'s
`keyboard_listener()` thread on literally ANY key the physical terminal
receives (`midicrt.py:1765`, inside the `with term.cbreak():` loop) --
this IS v1's screen, so any key means a human is right there.

Deployed v1 config (`~/codex/midicrt/config/settings.json`'s `pagecycle`
section, verified on the Pi, not invented):

    {"enabled": true, "cycle_pages": [1, 6, 8, 9], "interval": 300.0,
     "user_pause": 3600.0}

ID -> v2 name mapping (evidence, not guesswork)
---------------------------------------------------------------------------
Each v1 page module declares its own `PAGE_ID` (grepped directly,
`~/codex/midicrt/pages/*.py`):

    pages/notes.py:        PAGE_ID = 1
    pages/eventlog.py:     PAGE_ID = 6
    pages/pianoroll.py:    PAGE_ID = 8
    pages/audiospectrum.py: PAGE_ID = 9

Cross-checked against `docs/phase3-parity.md` §1's own v1->v2 page table:
ID 1 (`notes.py`) -> `pages/harmony.py` ("harmony", Task 5 -- v1's Notes
page IS `zharmony.py`'s primary UI surface); ID 6 (`eventlog.py`) ->
`pages/eventlog.py` ("eventlog", 1:1 name match, Task 1/3); ID 8
(`pianoroll.py`) -> `pages/pianoroll.py` ("pianoroll", Task 7); ID 9
(`audiospectrum.py`) -> `pages/spectrum.py` ("spectrum", Task 8). So
`cycle_pages=[1,6,8,9]` becomes `pagecycle_pages=["harmony", "eventlog",
"pianoroll", "spectrum"]` (`config.py`'s default, same order as v1's
list -- order matters, see "Cursor mechanics" below).

Origin ruling: which page actions count as "a human touched it" (judgment
call, flagged for review per the task brief)
---------------------------------------------------------------------------
v1's "activity" signal is a literal physical keypress on the one console
this software runs on -- there is no other actor in v1's world at all. v2
has several: a real client (fb/TUI/CLI/keymap-bound key/web -- ALL of
these funnel through the ONE `origin="client"` dispatch call in
`engine/server.py:236`, see `engine/actions.py`'s own "four dispatch
origins" comment -- there is no separate "keymap"/"web" origin string
anywhere in this codebase to distinguish them), a real hardware SysEx
command from the Cirklon control surface (`origin="sysex"`, bypasses the
dispatch registry entirely -- see `engine/core.py`'s `_sysex_switch_page`/
`_sysex_screensaver`), a LEARNED MIDI BINDING (`origin="binding:<id>"`),
and an unattended internal BEHAVIOR tick (`origin="behavior"`, this
module's own auto-advance included).

Ruling: `_HUMAN_ORIGINS = {"client", "sysex"}` pause rotation;
`"binding:*"`/`"behavior"`/anything else does NOT. Rationale -- a client
action is a human at a keyboard/TUI/CLI/web control, exactly v1's keypress
substrate. A SysEx command is a human pressing a physical button on the
Cirklon, just arriving over a different wire -- still a person, still
"stop moving the display out from under me." A learned BINDING firing
`page.goto` means a NOTE OR CC the sequencer is playing is driving the
page -- that is the SEQUENCER performing, not a user asking for a page;
pausing rotation because the song itself hit a bound note would be
backwards (the whole point of pagecycle is ambient variety DURING
playback). A BEHAVIOR-origin page action (this module's own auto-advance,
or `ScreensaverBehavior`'s activate/restore) is unattended machinery, not
a person -- and critically, this module's own dispatched `page.goto` must
NOT pause ITSELF (an origin-blind pause would make every second rotation
immediately re-pause the first).

What v2 keeps unconditionally from v1: rotation is NOT gated by MIDI/idle
state at all
---------------------------------------------------------------------------
This is the actual headline behavior change from the Task-9 version:
that version idle-*triggered* off `last_activity_ts` (fired only after N
seconds of NO MIDI, reset by any note) -- the literal opposite of v1,
which rotates on a fixed wall-clock cadence regardless of whether MIDI is
flowing, and is silent about MIDI entirely. `tick()` below still accepts
`last_activity_ts` as a parameter (kept for signature parity with
`ScreensaverBehavior.tick()` -- `Engine._tick_behaviors` calls every
behavior in its list identically: `tick(now, last_activity_ts,
current_page)`) but never reads it. Only `notify_page_action` (the new
v1-`notify_keypress`-equivalent hook, see below) can pause rotation.

`notify_page_action`: the second entry point v1's plugin also had
---------------------------------------------------------------------------
v1's plugin was never JUST `draw(state)` -- it exposed a SECOND function,
`notify_keypress()`, that `midicrt.py`'s keyboard-input thread calls
independently of the draw loop (see `_pagecycle_module()`/
`keyboard_listener()` in `midicrt.py`). `behaviors/__init__.py`'s "a
behavior's only channel to affect anything is `tick()`'s return value"
framing predates this need; this module's own `notify_page_action(origin,
now)` mirrors v1's shape as a second, additive, side-effect-free (no I/O,
no dispatch, just an internal timestamp write) entry point -- NOT a
violation of the "returns an intent, never acts" contract, since it never
returns an action itself, only changes what a LATER `tick()` call decides.
The engine wires this at the ONE seam that sees every successful page
navigation regardless of entry point (bindings/behaviors/client all
dispatch through `ActionRegistry`, whose post-dispatch hook is
`Engine._on_action_dispatched`; sysex bypasses the registry, so its two
`page.goto`-equivalent call sites -- `_sysex_switch_page`,
`_sysex_screensaver`'s force-on branch -- call it directly, right next to
the `record_action(..., "sysex")` call each already makes for capture
provenance. Same shape, reused, not a new seam invented).

Cursor mechanics: independent of `current_page`, in configured order
---------------------------------------------------------------------------
Like v1, this module's own rotation index is COMPLETELY independent of
whatever `current_page` happens to be when it fires -- it always advances
to the NEXT entry in `pages` (wrapping), the same as v1's
`_page_index = (_page_index + 1) % len(CYCLE_PAGES)`. A disclosed,
deliberate simplification from a literal port: v1 initializes
`_page_index = 0` but PRE-increments before ever reading it, so v1's very
first automatic rotation actually lands on `CYCLE_PAGES[1]` (eventlog),
not `CYCLE_PAGES[0]` (notes/harmony) -- a real but purely cosmetic v1
quirk (self-corrects after the first fire; every later rotation is
identical either way). This module instead starts its cursor so the FIRST
fire lands on `pages[0]` -- the unsurprising, "starts at the beginning of
the configured list" behavior a reader of `config.pagecycle_pages` would
expect, and avoids the first auto-advance target depending on exactly how
many pages happen to precede it in the list. Noted here rather than
silently "fixed" per this codebase's own disclosure convention.

Missing-page skip (new: v1's CYCLE_PAGES could never contain a dead ID)
---------------------------------------------------------------------------
v1's `cycle_pages` is a flat, load-time-fixed list of page IDs -- v1 never
has to consider "what if one of these isn't loaded" because `PAGES` is
built from a full page-ID-keyed dict.  v2 can (a custom `config.pages`
that omits, say, "pianoroll" while `pagecycle_pages` still names it, or a
future page rename), and dispatching `page.goto` to a page the engine
doesn't have raises `ActionError` (swallowed by `_tick_behaviors`, but
that is a silently-wasted tick, not a fix). `known_pages` (an optional
constructor arg -- `Engine` passes the real roster; standalone unit tests
omit it and get no filtering at all) lets `_next_target()` skip any
configured name absent from the current roster, logging a WARNING exactly
ONCE per missing name (not every time it's skipped) so a misconfigured
`pagecycle_pages` is discoverable without spamming the log every
`interval` seconds forever.

Arbitration with the screensaver: guard KEPT, per task brief
---------------------------------------------------------------------------
`behaviors/screensaver.py` is explicitly UNCHANGED by this task and still
turns "idle" into a real `page.goto screensaver` -- the ONE piece of the
Task-9 architecture this module still has to coexist with (v1 has no
analog: its screensaver blanks the physical framebuffer directly, never
touching `current_page`, so v1 never needed this arbitration at all).
`tick()` still refuses to act while `current_page` IS the screensaver
page, checked first, exactly like the Task-9 version. What's DIFFERENT
under the new mechanism: because rotation is no longer idle-gated at all,
a fully idle engine's pagecycle timer keeps counting down completely
obliviously to activity -- the screensaver-page guard is now the ONLY
thing standing between a long idle stretch and pagecycle un-blanking it
(there is no shared "both measured off the same activity timestamp" idle
math anymore, since this module no longer measures anything off activity).
On block-end (`current_page` stops being the screensaver page, whatever
the reason), `tick()` re-arms `_last_switch = now` -- giving a full FRESH
`interval` before the next auto-advance, mirroring the Task-9 fix-round's
own "re-arm after a manual escape" precedent (`tests/
test_behaviors_pagecycle.py`'s prior `test_rearms_after_a_manual_
screensaver_escape_...`/`test_block_ending_via_real_activity_rearms_...`
pair). Those two tests collapse into ONE here (`test_rearms_with_a_fresh_
interval_once_the_screensaver_block_ends`): the old pair existed only
because the OLD re-arm anchor was `last_activity_ts`, which the two paths
advance differently; this module's re-arm anchor is simply `now` at the
tick the block ends, so there is only one path left to test. A pending
`_paused_until` from a human page action survives a screensaver block
untouched -- the two gates are independent (see
`test_screensaver_block_does_not_consume_a_pending_user_pause`).

`_last_switch` is NOT re-armed by a pause -- verbatim v1 quirk (kept)
---------------------------------------------------------------------------
v1's pause check (`if ... now - _last_keypress < USER_PAUSE: return`)
happens BEFORE `_last_switch` is ever read or written -- so `_last_switch`
sits frozen at its last real value for the ENTIRE pause. Since
`USER_PAUSE` (3600s deployed) vastly exceeds `INTERVAL` (300s deployed),
by the time a pause naturally expires the interval has already elapsed
many times over, so v1's real, observed behavior is: rotation resumes
with an IMMEDIATE fire on the very tick the pause ends, not a fresh
interval wait. This module reproduces that literally (see
`test_pause_expiry_resumes_immediately_since_the_interval_already_elapsed`)
rather than "smoothing" it into a fresh-interval grace period the way the
screensaver-block re-arm above deliberately DOES get -- the screensaver
case needs that extra fix because it is a genuinely NEW v2-only
interaction (v1 never left the page it drew); the user-pause case has a
real, faithfully-portable v1 answer already, so this module doesn't
invent a different one.
"""
from __future__ import annotations

import logging

from midicrt.behaviors.screensaver import SCREENSAVER_PAGE

_LOG = logging.getLogger(__name__)

# Origins that count as "a human directly touched page navigation" for
# v1's USER_PAUSE mechanism -- see module docstring's "Origin ruling"
# section for the full rationale. Exact-string membership (not a prefix
# check) is deliberate: "binding:b1" etc. never equals "client"/"sysex" as
# a plain string, so no separate stripping logic is needed to exclude it.
_HUMAN_ORIGINS = frozenset({"client", "sysex"})


class PageCycleBehavior:
    """Pure state machine, PLUS one additive notification hook (see module
    docstring's "notify_page_action" section for why this module -- alone
    among behaviors -- has two entry points, mirroring v1's own
    `draw()`/`notify_keypress()` shape):

    `tick(now, last_activity_ts, current_page) -> (action_name, args) | None`
        Called every engine tick (`Engine._tick_behaviors`). `last_activity_ts`
        is accepted for signature parity with `ScreensaverBehavior.tick()`
        (both behaviors are called identically in a loop) but NOT read --
        v1's real semantics never gate on MIDI activity, only on elapsed
        wall-clock time and a recent human page action.

    `notify_page_action(origin, now) -> None`
        Called by the engine whenever a `page.next`/`page.prev`/`page.goto`
        action successfully lands, from ANY entry point (registry dispatch
        OR the two sysex call sites that bypass it) -- see `engine/core.py`'s
        `_on_action_dispatched`/`_sysex_switch_page`/`_sysex_screensaver`.
        No I/O, no dispatch -- purely an internal bookkeeping write, so this
        does not violate `behaviors/__init__.py`'s "acts only through
        `tick()`'s return value" contract for what a behavior may originate.
    """

    def __init__(self, enabled: bool, interval: float, pages: list[str],
                 user_pause: float, screensaver_page: str = SCREENSAVER_PAGE,
                 known_pages: set[str] | None = None) -> None:
        self.enabled = enabled
        self.interval = interval
        self.pages = list(pages)
        self.user_pause = user_pause
        self._screensaver_page = screensaver_page
        # None (the default, every standalone unit test in this file) means
        # "no roster to check against -- never skip anything." A real
        # `Engine` passes `set(self.pages)` (its live page roster).
        self._known_pages = None if known_pages is None else set(known_pages)
        self._last_switch: float | None = None
        self._paused_until: float | None = None
        self._was_blocked = False
        # Starts "one before the first configured page" -- see module
        # docstring's "Cursor mechanics" section for why this deliberately
        # does NOT reproduce v1's pre-increment-from-0 quirk.
        self._cursor = -1
        self._warned_missing: set[str] = set()

    def notify_page_action(self, origin: str, now: float) -> None:
        if origin in _HUMAN_ORIGINS:
            self._paused_until = now + self.user_pause

    def tick(self, now: float, last_activity_ts: float,
              current_page: str) -> tuple[str, dict] | None:
        if not self.enabled:
            return None
        if current_page == self._screensaver_page:
            # Guard kept from Task 9 (task brief's explicit requirement) --
            # see module docstring's "Arbitration with the screensaver".
            self._was_blocked = True
            return None
        if self._was_blocked:
            # Block just ended, whatever the cause -- re-arm a FULL fresh
            # interval from this instant rather than firing on a
            # `_last_switch` left stale for however long blocking lasted.
            self._was_blocked = False
            self._last_switch = now
            return None
        if self._last_switch is None:
            # Bootstrap: nothing to measure elapsed time FROM yet -- take
            # no action on a freshly constructed behavior's very first tick.
            self._last_switch = now
            return None
        if self._paused_until is not None:
            if now < self._paused_until:
                return None
            # Pause just expired -- deliberately does NOT touch
            # `_last_switch` (see module docstring's own section on this;
            # a verbatim v1 quirk, not an oversight).
            self._paused_until = None
        if now - self._last_switch < self.interval:
            return None
        self._last_switch = now
        target = self._next_target()
        if target is None:
            return None
        return ("page.goto", {"name": target})

    def _next_target(self) -> str | None:
        n = len(self.pages)
        if n == 0:
            return None
        for step in range(1, n + 1):
            idx = (self._cursor + step) % n
            name = self.pages[idx]
            if self._known_pages is None or name in self._known_pages:
                self._cursor = idx
                return name
            if name not in self._warned_missing:
                self._warned_missing.add(name)
                _LOG.warning(
                    "pagecycle: configured page %r is not in the current "
                    "roster -- skipping it", name)
        return None   # every configured page is missing from the roster
