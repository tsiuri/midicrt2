"""Phase 9 Task 2b regression tests: the 2026-08-10 production shutdown
hang under an ALSA MIDI-input error storm (`systemctl stop midicrtd`
needing SIGKILL after `TimeoutStopSec`, twice in one night).

Root cause (full diagnosis narrative + reproduction transcript:
.superpowers/sdd/2026-08-09-midicrt2-phase9-instruments/task-2b-report.md):
`loop.add_signal_handler` -- the daemon's ONLY shutdown mechanism before
this fix -- makes `signal.set_wakeup_fd()` point at asyncio's own INTERNAL
self-pipe (`loop._csock`, a `socket.socketpair()`), the EXACT SAME fd every
single `loop.call_soon_threadsafe()` call from ANY thread also writes a
wakeup byte to -- and `MidiInput._enqueue` (engine/midi_in.py) calls
exactly that, from the RtMidi callback thread, for every translated MIDI
event. Under a sustained callback-thread flood (an ALSA input-error storm
has none) combined with ANY stretch where the loop thread itself isn't
back at `epoll_wait()` (`Engine.run()`'s own per-tick burst-drain loop,
pre-fix, processed the WHOLE queued backlog synchronously with no `await`
inside it -- an unbounded stretch under a real storm), that shared pipe's
kernel send buffer can genuinely saturate (measured 212992 bytes on the
production Pi; reproduced live saturating within single-digit seconds at
realistic flood rates). Once full, ANY write to it -- including the raw OS
signal trampoline's own wakeup-byte write for a REAL SIGTERM -- gets
`EWOULDBLOCK` and is SILENTLY DROPPED, no retry (CPython prints "Exception
ignored when trying to write to the signal wakeup fd" from
`asyncio/unix_events.py`'s `_sighandler_noop` -- exactly the incident's own
log line). `stop.set()` was only ever wired to fire via that exact dropped
byte, so shutdown hung until systemd's SIGKILL.

Fixed by two independent, complementary changes, both exercised below:

  1. `daemon.ShutdownWatchdog` -- delivers SIGTERM/SIGINT through a
     PRIVATE `os.pipe()` nothing else on the process ever writes to,
     using a raw `signal.signal()` handler (bypasses `set_wakeup_fd`
     entirely) instead of `loop.add_signal_handler`. A `call_soon_
     threadsafe` flood, however large, can never contend for this fd's
     buffer.
  2. `Engine._MAX_BURST_PER_TICK` (engine/core.py) -- bounds how many
     already-queued events `run()`'s own burst-drain loop
     (`_drain_queue_burst`) processes synchronously in one step, so the
     loop thread always returns to `epoll_wait()` (and therefore services
     the watchdog's private-pipe reader) within a bounded number of
     `_handle()` calls, regardless of backlog size.

No real ALSA/RtMidi anywhere in these tests -- a background thread flooding
`loop.call_soon_threadsafe()` stands in for MidiInput's RtMidi-callback-
thread wakeup (the real production flood source), and a synthetic queue
backlog of plain `MidiEvent`s stands in for a real MIDI storm. Standalone
reproduction against the OLD `loop.add_signal_handler` mechanism (same
flood, same synchronous-stretch shape) was run manually during diagnosis
and DID reproduce the exact "Exception ignored... BlockingIOError" hang,
timing out with the signal permanently lost -- see the task report for
that transcript; it is not itself committed here since it's a proof
against code this repo no longer has, not code this repo defends.
"""
import asyncio
import os
import signal
import threading
import time

from midicrt.config import Config
from midicrt.daemon import ShutdownWatchdog
from midicrt.engine.core import _MAX_BURST_PER_TICK, Engine, MidiEvent


def _note_on(i: int) -> MidiEvent:
    return MidiEvent(ts=float(i), source="USB", type="note_on", channel=0,
                     data1=60, data2=100, summary="note_on ch1 n60 v100")


def _flood_call_soon_threadsafe(loop, stop_flood: threading.Event, counter: list[int]) -> None:
    """Mimics `MidiInput._enqueue`'s RtMidi-callback-thread wakeup call --
    the real flood source in production -- as fast as the interpreter will
    go, with no throttling (an ALSA error storm has none either)."""
    while not stop_flood.is_set():
        try:
            loop.call_soon_threadsafe(lambda: None)
        except RuntimeError:
            break
        counter[0] += 1


def _kill_self_after(delay: float, sig: signal.Signals = signal.SIGTERM) -> threading.Thread:
    """Sends `sig` to this process from an INDEPENDENT OS thread, not
    scheduled through asyncio at all -- so it genuinely arrives while the
    loop thread may be synchronously busy, not merely queued behind it
    (an `asyncio.sleep`-scheduled sender would never fire until the loop
    is free anyway, which would defeat the whole point of this test)."""
    t = threading.Thread(target=lambda: (time.sleep(delay), os.kill(os.getpid(), sig)),
                         daemon=True)
    t.start()
    return t


# -- ShutdownWatchdog: survives the exact saturation mechanism that hung
# production --------------------------------------------------------------

async def test_shutdown_watchdog_delivers_sigterm_through_a_concurrent_flood_and_a_busy_loop():
    """A sustained `call_soon_threadsafe` flood runs concurrently while the
    loop thread itself goes synchronously unavailable for a stretch
    (`time.sleep` inside this very coroutine -- mirrors `Engine.run()`'s
    pre-fix unbounded burst-drain, which had no `await` inside it either),
    with a REAL SIGTERM landing mid-stretch from an independent thread.
    `ShutdownWatchdog` must still resolve `stop_event` -- proving the fix
    doesn't depend on the shared self-pipe staying unsaturated, because it
    never touches that pipe at all."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    watchdog = ShutdownWatchdog(loop, stop_event)
    stop_flood = threading.Event()
    counter = [0]
    flooder = threading.Thread(target=_flood_call_soon_threadsafe,
                               args=(loop, stop_flood, counter), daemon=True)
    flooder.start()
    _kill_self_after(0.5)

    try:
        # The loop thread synchronously unavailable for a full second --
        # long enough, at realistic flood rates, to plausibly saturate the
        # shared self-pipe several times over (it does not matter to THIS
        # fix whether it actually does; see module docstring). Deliberately
        # a REAL blocking call, not `await asyncio.sleep` -- the whole
        # point is to occupy the loop thread the same way the pre-fix
        # unbounded burst-drain did.
        time.sleep(1.0)  # noqa: ASYNC251 -- deliberate, see comment above
        await asyncio.wait_for(stop_event.wait(), timeout=5.0)
    finally:
        stop_flood.set()
        flooder.join(timeout=2)
        watchdog.close()

    assert stop_event.is_set()
    assert counter[0] > 1000, (
        f"flood thread only got {counter[0]} calls in -- not exercising a "
        "realistic concurrent-flood scenario"
    )


async def test_shutdown_watchdog_restores_the_previous_signal_handler_on_close():
    """`close()` must hand SIGTERM back to whatever handler owned it
    before -- verified against a distinguishable custom handler rather
    than assuming SIG_DFL, so this also proves `close()` doesn't just
    blindly reset to default."""
    loop = asyncio.get_running_loop()
    marker = []
    prior = signal.signal(signal.SIGTERM, lambda signum, frame: marker.append(True))
    try:
        stop_event = asyncio.Event()
        watchdog = ShutdownWatchdog(loop, stop_event)
        watchdog.close()
        assert signal.getsignal(signal.SIGTERM) is not watchdog._on_signal
        os.kill(os.getpid(), signal.SIGTERM)
        # A raw signal.signal() handler runs on the very next bytecode
        # safepoint -- a tiny REAL sleep (not `await asyncio.sleep`, which
        # would yield to the loop instead of proving the handler already
        # ran on plain synchronous main-thread execution) is just margin
        # against scheduler noise.
        time.sleep(0.05)  # noqa: ASYNC251 -- deliberate, see comment above
        assert marker == [True]
    finally:
        signal.signal(signal.SIGTERM, prior)


# -- Engine._drain_queue_burst: bounds the loop thread's own synchronous
# stretch -------------------------------------------------------------------

def test_drain_queue_burst_is_bounded_even_with_a_much_larger_backlog():
    """Pre-fix, `run()`'s burst-drain loop drained the WHOLE queue
    synchronously regardless of size -- an unbounded stretch on the loop
    thread during which nothing else (a shutdown signal howsoever
    delivered, included) gets a turn. `_drain_queue_burst` is a plain
    synchronous method (extracted from `run()`'s own loop body for exactly
    this kind of direct test, same "no real asyncio run() task needed"
    shape `_flush_dirty()` already established) -- feeds a backlog several
    times `_MAX_BURST_PER_TICK` directly onto `eng.queue` and asserts a
    single call only ever processes the capped amount, leaving the rest
    for the next tick."""
    eng = Engine(Config())
    backlog_total = _MAX_BURST_PER_TICK * 3
    first = _note_on(0)
    for i in range(1, backlog_total):
        eng.queue.put_nowait(_note_on(i))

    handled = eng._drain_queue_burst(first)

    assert handled == _MAX_BURST_PER_TICK
    assert eng.queue.qsize() == backlog_total - _MAX_BURST_PER_TICK


def test_drain_queue_burst_stays_fast_even_capped_at_a_large_backlog():
    """Timing tripwire, same spirit as test_engine_perf.py's own
    non-gating benchmarks: a single capped burst-drain (the worst-case
    synchronous stretch the loop thread can now ever be stuck in, per
    tick) must stay well under a second even at the cap, so a storm can
    delay -- but never hang -- shutdown."""
    eng = Engine(Config())
    first = _note_on(0)
    for i in range(1, _MAX_BURST_PER_TICK * 2):
        eng.queue.put_nowait(_note_on(i))

    t0 = time.perf_counter()
    handled = eng._drain_queue_burst(first)
    elapsed = time.perf_counter() - t0

    assert handled == _MAX_BURST_PER_TICK
    assert elapsed < 2.0, (
        f"capped burst-drain took {elapsed:.3f}s for {_MAX_BURST_PER_TICK} "
        "events -- shutdown could stall this long behind ONE tick"
    )


# -- Combined: a real Engine.run() task + ShutdownWatchdog, under a
# simulated storm, must stop promptly ----------------------------------------

async def test_engine_run_with_watchdog_survives_a_simulated_storm_and_stops_promptly():
    """The full combined regression: a REAL `Engine.run()` task, fed a
    genuinely storm-sized backlog of synthetic `MidiEvent`s (several
    ticks' worth even at the bounded cap -- no real ALSA/RtMidi anywhere)
    plus a background thread flooding `call_soon_threadsafe` concurrently
    (mimicking `MidiInput._enqueue`'s own wakeup calls, the real
    production flood source), with `ShutdownWatchdog` wired exactly the
    way `daemon.run()` wires it. A real SIGTERM sent while the storm is
    still actively draining must resolve `stop_event` within a small
    bound -- "seconds, not 90s+kill" -- proving the bounded burst-drain
    and the private shutdown pipe work TOGETHER, not just in isolation."""
    eng = Engine(Config())
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    watchdog = ShutdownWatchdog(loop, stop_event)

    for i in range(_MAX_BURST_PER_TICK * 5):
        eng.queue.put_nowait(_note_on(i))

    stop_flood = threading.Event()
    counter = [0]
    flooder = threading.Thread(target=_flood_call_soon_threadsafe,
                               args=(loop, stop_flood, counter), daemon=True)
    flooder.start()
    engine_task = asyncio.create_task(eng.run())
    _kill_self_after(0.2)

    try:
        t0 = time.monotonic()
        await asyncio.wait_for(stop_event.wait(), timeout=8.0)
        elapsed = time.monotonic() - t0
        eng.stop()
        await asyncio.wait_for(engine_task, timeout=5.0)
    finally:
        stop_flood.set()
        flooder.join(timeout=2)
        watchdog.close()

    assert stop_event.is_set()
    assert elapsed < 8.0, (
        f"shutdown took {elapsed:.2f}s under a simulated storm -- "
        "expected a small, bounded delay, not systemd's SIGKILL-territory wait"
    )
