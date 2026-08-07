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
