"""TDD for the Phase 9 Task 6 SECOND review round (Important finding,
live-reproduced by the reviewer): `capture.start`/`.stop`/`.pin` used to
call into `engine/capture.py::CaptureSink`'s `index.json` lock with a
plain BLOCKING `fcntl.flock` -- since `ActionRegistry.dispatch()` runs
every action handler SYNCHRONOUSLY on the daemon's one asyncio event
loop, that blocking wait stalled the WHOLE loop (MIDI draining, ticks,
rendering, every OTHER client's request), not just the one action
waiting on the lock. Reviewer's own reproduction: an unrelated `status`
request stalled 2227ms while a separate process held the lock for 2.0s.

Fix (engine/capture.py + engine/core.py): `_try_index_write_lock`
(`LOCK_NB` + a bounded poll-retry, `capture_mod.ENGINE_LOCK_TIMEOUT_S`)
plus `asyncio.to_thread` at the three engine-reachable action call sites
(`_capture_start_action_dispatch`/`_capture_stop_action_dispatch`/
`_capture_pin_action`) -- see each of those methods' own docstrings.
`capture.start`/`.stop` degrade GRACEFULLY on a busy lock (skip the
retention sweep / leave the session as an orphan, both self-healing,
never fail the action); `capture.pin` has no honest degraded outcome and
reports a clean, distinctly-worded busy `ActionError` instead.

This file is fed a real, in-process `Engine` (no daemon/socket needed --
`eng.actions.dispatch(...)` alone exercises the exact code path
`ProtocolServer._dispatch` uses in production) and a background THREAD
holding the SAME `.index.lock` file via a raw `os.open`+`flock` call to
simulate "another process" (`fcntl.flock`'s exclusion is per open-file-
description, not per-process, so a thread's own `os.open()` call
genuinely contends against the engine's lock exactly like a separate
process would -- see test_sessions.py's own concurrency tests for the
same technique already established in this phase)."""
import asyncio
import fcntl
import os
import threading
import time

import pytest

from midicrt.config import Config
from midicrt.engine import capture as capture_mod
from midicrt.engine import sessions as sessions_mod
from midicrt.engine.actions import ActionError
from midicrt.engine.core import Engine


def _hold_lock_in_background(capture_dir: str, hold_seconds: float) -> threading.Thread:
    """Starts a background thread that acquires `<capture_dir>/.index.lock`
    (blocking flock) and holds it for `hold_seconds` before releasing --
    simulates a concurrently-running `midicrt sessions repair-index` (or
    any other lock holder) from a separate process. Returns the started
    thread; caller must `.join()` it. Blocks until the lock is CONFIRMED
    held (a `threading.Event`) before returning, so callers never race
    the holder's own acquisition."""
    os.makedirs(capture_dir, exist_ok=True)
    lock_path = os.path.join(capture_dir, capture_mod._INDEX_LOCK_FILE)
    ready = threading.Event()

    def hold():
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        time.sleep(hold_seconds)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    t = threading.Thread(target=hold)
    t.start()
    assert ready.wait(timeout=2), "lock holder thread never acquired the lock"
    return t


# -- the reviewer's exact scenario, automated ---------------------------------

# Timing methodology (important, and the reason an earlier draft of these
# tests almost shipped a false-negative-prone version): measuring ONLY
# the unrelated call's own elapsed time, after a FIXED `await asyncio.
# sleep(0.1)` "warm-up", is NOT robust -- a plain synchronous handler with
# no internal `await` (the PRE-FIX shape) can swallow the ENTIRE blocking
# wait inside that warm-up sleep itself (a coroutine with no internal
# `await` points runs the event loop's single thread captive from the
# moment it's scheduled until it returns; verified live while writing
# this fix: `asyncio.sleep(0.1)` measured 1.023s wall-clock against the
# pre-fix handler shape, not ~0.1s -- by the time execution reached the
# "unrelated" call afterward, the lock had already been released and
# THAT call alone looked instant, hiding the bug). Every test below
# instead measures TOTAL elapsed wall-clock time from BEFORE creating the
# lock-needing task to AFTER the unrelated call returns -- wherever the
# stall actually manifests (a warm-up yield, the unrelated dispatch
# itself, or `asyncio.wait_for`'s own timeout firing as an uncaught
# exception), this window catches it.

async def test_capture_pin_lock_contention_does_not_block_the_event_loop():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    stop_result = await eng.actions.dispatch("capture.stop", {})
    sid = stop_result["session_id"]
    assert sid == start["session_id"]

    hold_seconds = 1.0
    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds)
    try:
        t_start = time.monotonic()
        # `capture.pin` NEEDS the lock -- dispatch it as a background task
        # so it's "in flight" while we issue the unrelated request below.
        pin_task = asyncio.create_task(eng.actions.dispatch("capture.pin", {"id": sid}))
        await asyncio.sleep(0)   # let pin_task get its first scheduling chance
        status_result = await asyncio.wait_for(
            eng.actions.dispatch("capture.status", {}), timeout=0.5)
        unrelated_elapsed = time.monotonic() - t_start

        assert status_result["recording"] is False
        assert unrelated_elapsed < 0.4, (
            f"getting an answer to an unrelated capture.status took "
            f"{unrelated_elapsed:.3f}s while another process held the index "
            f"lock for {hold_seconds}s -- the event loop was blocked (the "
            "exact bug this test guards against)")

        # The pin itself must still land -- once the lock is released.
        pin_result = await asyncio.wait_for(pin_task, timeout=5)
        assert pin_result == {"pinned": True, "id": sid}
    finally:
        holder.join(timeout=5)


async def test_capture_start_lock_contention_does_not_block_the_event_loop():
    # `capture.start`'s own `_sweep_retention` also needs the lock --
    # same proof, different action.
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("capture.stop", {})

    hold_seconds = 1.0
    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds)
    try:
        t_start = time.monotonic()
        start_task = asyncio.create_task(eng.actions.dispatch("capture.start", {}))
        await asyncio.sleep(0)
        status_result = await asyncio.wait_for(
            eng.actions.dispatch("capture.status", {}), timeout=0.5)
        unrelated_elapsed = time.monotonic() - t_start
        assert unrelated_elapsed < 0.4, (
            f"getting an answer to an unrelated capture.status took "
            f"{unrelated_elapsed:.3f}s while another process held the index "
            f"lock for {hold_seconds}s -- the event loop was blocked")

        await asyncio.wait_for(start_task, timeout=5)
        assert status_result is not None
    finally:
        holder.join(timeout=5)
        await eng.actions.dispatch("capture.stop", {})


async def test_capture_stop_lock_contention_does_not_block_the_event_loop():
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})

    hold_seconds = 1.0
    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds)
    try:
        t_start = time.monotonic()
        stop_task = asyncio.create_task(eng.actions.dispatch("capture.stop", {}))
        await asyncio.sleep(0)
        status_result = await asyncio.wait_for(
            eng.actions.dispatch("capture.status", {}), timeout=0.5)
        unrelated_elapsed = time.monotonic() - t_start
        assert unrelated_elapsed < 0.4, (
            f"getting an answer to an unrelated capture.status took "
            f"{unrelated_elapsed:.3f}s while another process held the index "
            f"lock for {hold_seconds}s -- the event loop was blocked")

        await asyncio.wait_for(stop_task, timeout=5)
        assert status_result is not None
    finally:
        holder.join(timeout=5)


# -- bounded wait: busy vs graceful degradation ------------------------------

async def test_capture_pin_fails_busy_when_lock_held_past_the_bound(monkeypatch):
    monkeypatch.setattr(capture_mod, "ENGINE_LOCK_TIMEOUT_S", 0.2)
    eng = Engine(Config())
    await eng.actions.dispatch("capture.start", {})
    stop_result = await eng.actions.dispatch("capture.stop", {})
    sid = stop_result["session_id"]

    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds=1.0)
    try:
        with pytest.raises(ActionError, match="busy"):
            await eng.actions.dispatch("capture.pin", {"id": sid})
    finally:
        holder.join(timeout=5)


async def test_capture_start_degrades_gracefully_when_retention_sweep_lock_is_busy(
        monkeypatch, caplog):
    monkeypatch.setattr(capture_mod, "ENGINE_LOCK_TIMEOUT_S", 0.2)
    eng = Engine(Config())

    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds=1.0)
    try:
        with caplog.at_level("WARNING"):
            result = await eng.actions.dispatch("capture.start", {})
        assert result["recording"] is True   # never fails merely from lock contention
        assert any("retention sweep skipped" in r.message for r in caplog.records)
    finally:
        holder.join(timeout=5)
        await eng.actions.dispatch("capture.stop", {})


async def test_capture_stop_degrades_gracefully_when_index_lock_is_busy_leaving_an_orphan(
        monkeypatch, caplog):
    monkeypatch.setattr(capture_mod, "ENGINE_LOCK_TIMEOUT_S", 0.2)
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    sid = start["session_id"]

    holder = _hold_lock_in_background(eng._capture.dir, hold_seconds=1.0)
    try:
        with caplog.at_level("WARNING"):
            result = await eng.actions.dispatch("capture.stop", {})
        assert result["recording"] is False   # never fails merely from lock contention
        assert result["session_id"] == sid
        assert any("left as an orphan" in r.message for r in caplog.records)
    finally:
        holder.join(timeout=5)

    # The session really did become an orphan (no index row yet) -- the
    # SAME, already-tested drift class `repair-index` heals.
    listing = sessions_mod.list_sessions(eng._capture.dir)
    row = next(r for r in listing["sessions"] if r["id"] == sid)
    assert row["status"] == "orphan"

    report = sessions_mod.repair_index(eng._capture.dir)
    assert report["adopted"] == [sid]
