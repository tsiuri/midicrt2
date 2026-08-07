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
