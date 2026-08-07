"""TDD for ScreensaverBehavior: a pure idle-timer state machine (no I/O, no
action dispatch of its own -- mirrors behaviors/pagecycle.py's "returns an
action intent" contract, see behaviors/screensaver.py's module docstring).
"""
from midicrt.behaviors.screensaver import ScreensaverBehavior


def test_disabled_never_fires():
    b = ScreensaverBehavior(enabled=False, after_s=60.0)
    assert b.tick(now=1000.0, last_activity_ts=0.0, current_page="eventlog") is None


def test_activates_after_idle_threshold():
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    assert b.tick(now=59.9, last_activity_ts=0.0, current_page="eventlog") is None
    assert b.tick(now=60.0, last_activity_ts=0.0, current_page="eventlog") == (
        "page.goto", {"name": "screensaver"})


def test_does_not_reactivate_every_tick_once_active():
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    assert b.tick(now=60.0, last_activity_ts=0.0, current_page="eventlog") == (
        "page.goto", {"name": "screensaver"})
    assert b.tick(now=61.0, last_activity_ts=0.0, current_page="screensaver") is None
    assert b.tick(now=1000.0, last_activity_ts=0.0, current_page="screensaver") is None


def test_does_not_activate_if_already_on_the_screensaver_page():
    # e.g. a user manually navigated there -- must not re-trigger the same
    # transition (and, more importantly, must not then think ITS OWN
    # activation caused it, which would corrupt the remembered previous page).
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    assert b.tick(now=60.0, last_activity_ts=0.0, current_page="screensaver") is None


def test_activity_after_activation_restores_the_previous_page():
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    assert b.tick(now=60.0, last_activity_ts=0.0, current_page="voices") == (
        "page.goto", {"name": "screensaver"})
    # No new activity yet -- stays put.
    assert b.tick(now=65.0, last_activity_ts=0.0, current_page="screensaver") is None
    # Activity ts advances -- restore.
    assert b.tick(now=65.1, last_activity_ts=65.0, current_page="screensaver") == (
        "page.goto", {"name": "voices"})


def test_after_restore_the_idle_timer_starts_fresh_from_the_new_activity():
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    b.tick(now=60.0, last_activity_ts=0.0, current_page="voices")
    b.tick(now=65.1, last_activity_ts=65.0, current_page="screensaver")
    # Immediately after restoring, must not re-activate until ANOTHER
    # full after_s of idleness measured from the restoring activity.
    assert b.tick(now=65.2, last_activity_ts=65.0, current_page="voices") is None
    assert b.tick(now=124.9, last_activity_ts=65.0, current_page="voices") is None
    assert b.tick(now=125.0, last_activity_ts=65.0, current_page="voices") == (
        "page.goto", {"name": "screensaver"})


def test_remembers_whatever_page_was_current_at_activation_time():
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    b.tick(now=60.0, last_activity_ts=0.0, current_page="harmony")
    result = b.tick(now=60.1, last_activity_ts=60.0, current_page="screensaver")
    assert result == ("page.goto", {"name": "harmony"})


def test_manual_page_change_while_active_is_treated_as_user_override_no_restore():
    # IMPORTANT fix (task-9 review): while active, a manual page.next/goto
    # from ANY client (not MIDI activity) must be treated as the user
    # taking over -- not silently overridden later by a stale restore.
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    assert b.tick(now=60.0, last_activity_ts=0.0, current_page="voices") == (
        "page.goto", {"name": "screensaver"})
    # Manual navigation to a DIFFERENT page than the one remembered
    # ("voices") -- current_page just changes, no activity involved.
    assert b.tick(now=61.0, last_activity_ts=0.0, current_page="harmony") is None
    # Later MIDI activity arrives -- must NOT restore "voices" (the stale
    # pre-activation page); the manual choice must already have won.
    assert b.tick(now=62.0, last_activity_ts=62.0, current_page="harmony") is None


def test_manual_override_without_new_activity_reactivates_on_the_next_tick():
    # Disclosed, intended consequence of the fix above: idle time is
    # measured PURELY from last_activity_ts (real MIDI traffic), which a
    # manual page override does not touch -- there is no engine-side
    # channel for a behavior to bump that shared clock itself. If
    # last_activity_ts never advances, the screensaver is free to reclaim
    # the display again on the very next tick; a manual escape buys no
    # grace period unless real MIDI activity actually happens. See
    # behaviors/screensaver.py's own module docstring.
    b = ScreensaverBehavior(enabled=True, after_s=60.0)
    b.tick(now=60.0, last_activity_ts=0.0, current_page="voices")
    b.tick(now=61.0, last_activity_ts=0.0, current_page="harmony")   # manual override
    assert b.tick(now=61.1, last_activity_ts=0.0, current_page="harmony") == (
        "page.goto", {"name": "screensaver"})
