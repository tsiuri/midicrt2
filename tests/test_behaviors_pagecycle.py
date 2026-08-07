"""TDD for PageCycleBehavior: a pure idle-timer state machine (no I/O, no
action dispatch of its own -- see behaviors/pagecycle.py's module docstring
for why `tick()` RETURNS an action intent instead of dispatching it, and
for the v1-vs-v2 semantics comparison). Fake-clock injection throughout --
`now`/`last_activity_ts` are always explicit float args, never a real
clock read.
"""
from midicrt.behaviors.pagecycle import PageCycleBehavior


def test_disabled_never_fires():
    b = PageCycleBehavior(enabled=False, idle_s=5.0)
    for now in (0.0, 5.0, 100.0, 1000.0):
        assert b.tick(now=now, last_activity_ts=0.0, current_page="eventlog") is None


def test_first_tick_bootstraps_without_firing():
    # A fresh behavior must not treat "no activity recorded yet" as
    # infinitely idle and fire on its very first tick.
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    assert b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog") is None


def test_fires_page_next_once_idle_s_elapses_with_no_activity():
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=4.9, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == ("page.next", {})


def test_does_not_refire_every_tick_once_fired():
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == ("page.next", {})
    assert b.tick(now=5.01, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=6.0, last_activity_ts=0.0, current_page="eventlog") is None


def test_keeps_cycling_every_idle_s_while_idleness_persists():
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=5.0, last_activity_ts=0.0, current_page="eventlog") == ("page.next", {})
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="eventlog") == ("page.next", {})
    assert b.tick(now=15.0, last_activity_ts=0.0, current_page="eventlog") == ("page.next", {})


def test_any_midi_activity_resets_the_idle_clock():
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=4.0, last_activity_ts=0.0, current_page="eventlog") is None
    # Activity at t=4 (last_activity_ts advances) -- resets the window.
    assert b.tick(now=4.1, last_activity_ts=4.0, current_page="eventlog") is None
    assert b.tick(now=8.9, last_activity_ts=4.0, current_page="eventlog") is None
    assert b.tick(now=9.0, last_activity_ts=4.0, current_page="eventlog") == ("page.next", {})


def test_activity_reset_mid_would_be_fire_prevents_that_fire():
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    # Activity arrives right at the instant it would have fired -- must not fire.
    assert b.tick(now=5.0, last_activity_ts=5.0, current_page="eventlog") is None


# -- arbitration with the screensaver (Critical fix, task-9 review) ---------

def test_blocked_while_on_the_screensaver_page_never_fires():
    b = PageCycleBehavior(enabled=True, idle_s=5.0, screensaver_page="screensaver")
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=100.0, last_activity_ts=0.0, current_page="screensaver") is None
    assert b.tick(now=1000.0, last_activity_ts=0.0, current_page="screensaver") is None


def test_rearms_after_a_manual_screensaver_escape_instead_of_firing_immediately():
    # REQUIRED test (2nd review pass): must fail against the code that only
    # fixed the Critical arbitration bug (blocks while on the screensaver
    # page, but falls straight into the STALE elapsed check the instant it
    # leaves) -- see module docstring's "Re-arming after a manual escape"
    # section. Uses the shipped default idle_s (Config().pagecycle_idle_s
    # == 300.0) to mirror the real deployed timeline.
    b = PageCycleBehavior(enabled=True, idle_s=300.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")   # bootstrap
    # Idle t=0->60: the real screensaver would activate around here --
    # simulate it blocking pagecycle from t=60 through t=500, no activity
    # (last_activity_ts stays 0.0 throughout).
    for now in (60.0, 200.0, 500.0):
        assert b.tick(now=now, last_activity_ts=0.0, current_page="screensaver") is None
    # t=500: a MANUAL escape (some other client's page.goto/next) -- NOT
    # via any MIDI activity; current_page simply changes.
    assert b.tick(now=500.5, last_activity_ts=0.0, current_page="voices") is None
    # Must keep NOT firing all the way up to just short of a FULL FRESH
    # idle_s measured from the re-arm point (500.5 + 300 = 800.5) --
    # proving the fix doesn't just delay-by-one-tick.
    assert b.tick(now=600.0, last_activity_ts=0.0, current_page="voices") is None
    assert b.tick(now=800.4, last_activity_ts=0.0, current_page="voices") is None
    # Proving pagecycle isn't permanently dead after a manual escape: it
    # DOES act again, exactly a fresh idle_s after the re-arm point.
    assert b.tick(now=800.5, last_activity_ts=0.0, current_page="voices") == ("page.next", {})


def test_block_ending_via_real_activity_rearms_from_the_activity_instant():
    # The OTHER way a block can end: the real ScreensaverBehavior's own
    # activity-triggered restore (last_activity_ts genuinely advances in
    # the SAME tick the page changes back). Must fall through to the
    # ordinary "activity moved forward" re-arm (anchored to the activity
    # instant), NOT the manual-escape "anchored to now" re-arm -- both
    # happen to look similar here but must use different anchors.
    b = PageCycleBehavior(enabled=True, idle_s=5.0)
    b.tick(now=0.0, last_activity_ts=0.0, current_page="eventlog")
    assert b.tick(now=10.0, last_activity_ts=0.0, current_page="screensaver") is None
    assert b.tick(now=10.1, last_activity_ts=10.0, current_page="eventlog") is None
    # Re-armed from the ACTIVITY instant (10.0), not `now` (10.1) -- fires
    # at 10.0+5.0=15.0.
    assert b.tick(now=14.9, last_activity_ts=10.0, current_page="eventlog") is None
    assert b.tick(now=15.0, last_activity_ts=10.0, current_page="eventlog") == ("page.next", {})
