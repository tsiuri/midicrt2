"""TDD for PageCycleBehavior -- Phase 8 Task 5's wholesale semantics
reversal (docs/superpowers/sdd/2026-08-08-midicrt2-phase8-gui/task-5-brief.md):
v1's `plugins/pagecycle.py` semantics are RESTORED verbatim (per the
2026-08-08 decisions doc's explicit ruling "TURN BACK ON with v1
semantics", `docs/visual-audit.md` §20e), replacing the Task-9 idle-
triggered/whole-roster re-interpretation this file used to test. See
`behaviors/pagecycle.py`'s own module docstring for the full v1 ground
truth, the ID->name mapping evidence, and the origin ruling. Every test
below is either NEW (the human-origin-pause/cycle-set/missing-page
contracts v1 actually had and the old idle-gated version never modeled)
or a REWRITE of a same-named T9-era test whose assertions no longer apply
under the new "wall-clock interval, no idle gate" mechanism -- each
rewritten test says so inline.

Fake-clock injection throughout -- `now`/`last_activity_ts` are always
explicit float args, never a real clock read.
"""
from midicrt.behaviors.pagecycle import PageCycleBehavior

# -- basic enable/bootstrap ---------------------------------------------

def test_disabled_never_fires():
    b = PageCycleBehavior(enabled=False, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=60.0)
    for now in (0.0, 5.0, 100.0, 1000.0):
        assert b.tick(now=now, last_activity_ts=0.0, current_page="eventlog") is None


def test_first_tick_bootstraps_without_firing():
    # A fresh behavior must not treat "no prior switch recorded" as an
    # already-elapsed interval and fire on its very first tick.
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=60.0)
    assert b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog") is None


# -- v1's actual mechanism: pure wall-clock interval, NO idle gate ------
#
# This is the headline semantics change this task restores: the T9-era
# suite this replaces proved the OPPOSITE contract ("any MIDI activity
# resets the idle clock") -- see behaviors/pagecycle.py's module docstring
# for why that was a disclosed re-interpretation, not a literal port.

def test_fires_page_goto_to_the_first_configured_page_once_interval_elapses():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog", "pianoroll"],
                          user_pause=60.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")   # bootstrap
    assert b.tick(now=4.9, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})


def test_keeps_rotating_through_the_configured_pages_every_interval():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog", "pianoroll"],
                          user_pause=60.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "eventlog"})
    assert b.tick(now=15.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "pianoroll"})
    # Wraps back to the first configured page.
    assert b.tick(now=20.0, last_activity_ts=0.0, current_page="pianoroll") == \
        ("page.goto", {"name": "harmony"})


def test_rotates_on_a_fixed_wall_clock_interval_even_while_midi_is_continuously_active():
    # REPLACES test_any_midi_activity_resets_the_idle_clock (T9-era, now
    # deleted -- it asserted the exact opposite of what this task restores).
    # v1's `draw()` never looks at MIDI activity at all; it only checks
    # elapsed wall-clock time since the last switch, gated purely by a
    # recent KEYPRESS (modeled by `notify_page_action`, tested separately
    # below), never by MIDI traffic. Simulating `last_activity_ts`
    # advancing on literally every tick (a continuous flood of incoming
    # notes) must NOT push the rotation out even one tick -- proving there
    # is no idle gate left in this mechanism at all.
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=3600.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    for t in range(1, 10):
        assert b.tick(now=float(t), last_activity_ts=float(t), current_page="eventlog") is None
    assert b.tick(now=10.0, last_activity_ts=10.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})


def test_does_not_refire_every_tick_once_fired():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=60.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})
    assert b.tick(now=5.01, last_activity_ts=0.0, current_page="harmony") is None
    assert b.tick(now=6.0, last_activity_ts=0.0, current_page="harmony") is None


# -- cycles only within the configured subset, never the whole roster --

def test_cycles_only_within_the_configured_pages_not_the_whole_roster():
    # v1's CYCLE_PAGES=[1,6,8,9] is a CURATED subset of a much larger page
    # roster (18 pages in v1) -- it never visits any page outside that
    # list. `known_pages` here stands in for a real roster with MORE pages
    # than are configured for cycling (mirrors config.pages having 14
    # entries while pagecycle_pages only names 4, see config.py).
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=60.0,
                          known_pages={"harmony", "eventlog", "spectrum", "pianoroll", "voices"})
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    seen = set()
    now = 0.0
    for _ in range(6):
        now += 5.0
        _, args = b.tick(now=now, last_activity_ts=0.0, current_page="eventlog")
        seen.add(args["name"])
    assert seen == {"harmony", "eventlog"}


def test_missing_configured_page_is_skipped_and_logged_once(caplog):
    # A page named in `pagecycle_pages` that isn't actually in the current
    # roster (e.g. a custom config.pages omits it) must be skipped, not
    # crash and not dispatch a goto to a page the engine doesn't have --
    # `_page_goto` would raise ActionError for an unknown page name, which
    # `Engine._tick_behaviors` swallows, but that's a wasted/confusing
    # dispatch; skipping in the behavior itself is the correct fix. Logged
    # ONCE per missing name, not on every tick that skips over it.
    import logging
    caplog.set_level(logging.WARNING)
    b = PageCycleBehavior(enabled=True, interval=5.0,
                          pages=["harmony", "ghost", "eventlog"], user_pause=60.0,
                          known_pages={"harmony", "eventlog"})
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "harmony"})
    # Next fire must skip "ghost" and land on "eventlog".
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "eventlog"})
    # A full second lap re-encounters "ghost" again (still skipped) without
    # a second warning.
    assert b.tick(now=15.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})
    assert b.tick(now=20.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "eventlog"})
    ghost_warnings = [r for r in caplog.records if "ghost" in r.getMessage()]
    assert len(ghost_warnings) == 1


def test_all_configured_pages_missing_returns_none_without_crashing():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["ghost1", "ghost2"],
                          user_pause=60.0, known_pages={"eventlog"})
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") is None
    # Must keep trying (not wedge) rather than raising on a later tick.
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") is None


def test_empty_configured_page_list_never_fires():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=[], user_pause=60.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") is None


# -- human-origin pause (notify_page_action) -----------------------------
#
# REPLACES the old idle-gated suite's activity-reset tests -- v1's actual
# pause trigger is a recent KEYPRESS, not MIDI activity. `notify_page_action`
# is the new engine->behavior hook (see behaviors/pagecycle.py's module
# docstring's "Origin ruling" section for why only "client"/"sysex" count).

def test_client_origin_page_action_pauses_rotation():
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("client", now=2.0)   # pauses until t=22.0
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=21.9, last_activity_ts=0.0, current_page="eventlog") is None


def test_sysex_origin_page_action_pauses_rotation():
    # A real hardware SysEx page-switch command is just as much "a human
    # pressed something" as a client keypress -- see the origin ruling.
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("sysex", now=2.0)
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=21.9, last_activity_ts=0.0, current_page="eventlog") is None


def test_binding_origin_page_action_does_not_pause():
    # A learned MIDI binding driving page changes IS the sequencer
    # performing, not a user asking for a page -- must NOT pause rotation.
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("binding:b1", now=2.0)
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})


def test_behavior_origin_page_action_does_not_pause():
    # Includes pagecycle's OWN auto-advance and screensaver's restore --
    # neither is a human touching anything.
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("behavior", now=2.0)
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})


def test_pause_expiry_resumes_immediately_since_the_interval_already_elapsed():
    # Faithful v1 port: v1's `draw()` checks the keypress-pause BEFORE ever
    # touching `_last_switch`, so `_last_switch` stays frozen at whatever it
    # was when the pause began. Since USER_PAUSE (typically 3600s) is far
    # longer than INTERVAL (typically 300s), by the time a pause naturally
    # expires the interval has always already elapsed many times over --
    # so rotation resumes with an IMMEDIATE fire on the tick the pause ends,
    # not a fresh interval wait. This is a real, if easy-to-miss, v1 quirk
    # faithfully reproduced here (see module docstring) rather than
    # "smoothed over" with a fresh-interval grace period.
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("client", now=2.0)   # pauses until t=22.0
    assert b.tick(now=15.0, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=22.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})
    # Normal cadence resumes from the fire instant.
    assert b.tick(now=31.9, last_activity_ts=0.0, current_page="harmony") is None
    assert b.tick(now=32.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "eventlog"})


def test_a_second_pause_during_an_active_pause_extends_it():
    # Each notify_page_action call re-stamps the pause window from ITS OWN
    # `now`, matching v1's `_last_keypress = time.time()` (unconditional
    # overwrite, no "only if later" guard).
    b = PageCycleBehavior(enabled=True, interval=10.0, pages=["harmony", "eventlog"],
                          user_pause=20.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("client", now=2.0)    # pause until 22.0
    b.notify_page_action("client", now=15.0)   # re-stamped -- pause until 35.0
    assert b.tick(now=22.0, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=34.9, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=35.0, last_activity_ts=0.0, current_page="eventlog") == \
        ("page.goto", {"name": "harmony"})


# -- arbitration with the screensaver (guard kept, per task brief) ------

def test_blocked_while_on_the_screensaver_page_never_fires():
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=60.0, screensaver_page="screensaver")
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=100.0, last_activity_ts=0.0, current_page="screensaver") is None
    assert b.tick(now=1000.0, last_activity_ts=0.0, current_page="screensaver") is None


def test_rearms_with_a_fresh_interval_once_the_screensaver_block_ends():
    # REPLACES the T9-era pair test_rearms_after_a_manual_screensaver_
    # escape_instead_of_firing_immediately / test_block_ending_via_real_
    # activity_rearms_from_the_activity_instant. Those two tests existed
    # to distinguish "block ended via real MIDI activity" from "block ended
    # via a manual escape" because the OLD mechanism's re-arm anchor was
    # `last_activity_ts` itself, and the two cases advance that value
    # differently. The new mechanism no longer references
    # `last_activity_ts` for its own timing AT ALL (see module docstring) --
    # so there is only ONE way a block can end from `tick()`'s point of
    # view: `current_page` simply isn't the screensaver page anymore,
    # whatever the cause. This single test therefore covers what used to
    # be two, and does so correctly (a disclosed simplification, not a
    # dropped case).
    b = PageCycleBehavior(enabled=True, interval=300.0, pages=["harmony", "eventlog"],
                          user_pause=3600.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")   # bootstrap
    for now in (60.0, 200.0, 500.0):
        assert b.tick(now=now, last_activity_ts=0.0, current_page="screensaver") is None
    # Block ends at t=500.5 -- current_page moved off "screensaver".
    assert b.tick(now=500.5, last_activity_ts=0.0, current_page="harmony") is None
    # Must NOT fire again until a FULL FRESH interval from the re-arm point
    # (500.5 + 300 = 800.5), proving this isn't a delay-by-one-tick fix.
    assert b.tick(now=600.0, last_activity_ts=0.0, current_page="harmony") is None
    assert b.tick(now=800.4, last_activity_ts=0.0, current_page="harmony") is None
    # This is the very first fire ANYWHERE in this test (every prior call
    # returned None) -- so it lands on pages[0] ("harmony"), the same
    # first-target rule every other "first fire" test in this file uses.
    assert b.tick(now=800.5, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "harmony"})


def test_screensaver_block_does_not_consume_a_pending_user_pause():
    # A human pause armed just before the screensaver takes over must still
    # be honored (not silently cleared) once the screensaver lets go --
    # `_paused_until` and the screensaver block are independent gates.
    b = PageCycleBehavior(enabled=True, interval=5.0, pages=["harmony", "eventlog"],
                          user_pause=100.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    b.notify_page_action("client", now=1.0)   # pause until t=101.0
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="screensaver") is None
    assert b.tick(now=50.0, last_activity_ts=0.0, current_page="harmony") is None
    # Re-armed by the screensaver-block-end at t=50 -> next earliest fire
    # would be t=55 by interval alone, but the pause (until t=101) still wins.
    assert b.tick(now=55.0, last_activity_ts=0.0, current_page="harmony") is None
    # First-ever fire in this test -> pages[0] ("harmony"), same rule as
    # every other "first fire" test in this file.
    assert b.tick(now=101.0, last_activity_ts=0.0, current_page="harmony") == \
        ("page.goto", {"name": "harmony"})
