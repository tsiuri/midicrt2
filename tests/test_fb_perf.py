"""Benchmark for `Surface.to_rgb565()` at the real device resolution
(800x475 -- see surface.py's module docstring for the Pi hardware geometry
and the before/after numbers this task recorded).

This is NOT a tight regression gate: CI runners have wildly different CPUs
than the Pi Zero/3/4-class hardware this client actually targets, so the
bound here is deliberately generous (10x-plus the ~26ms/frame measured on
the Pi for the numpy pack) purely to catch a catastrophic regression (e.g.
someone reverting to the ~1.2s/frame pure-Python per-pixel loop) without
ever flaking on a slow/loaded CI box. The measured number is printed
(`-s`/pytest capture=no, or just read from a failure) for humans tracking
real performance -- see task-2-report.md for the authoritative benchmark
run on the actual target hardware.
"""
import time

from midicrt.clients.fb.surface import Surface

WIDTH, HEIGHT = 800, 475
ITERATIONS = 30
GENEROUS_BOUND_MS = 200.0  # non-gating in spirit; just catches a catastrophic regression


def test_to_rgb565_stays_well_under_frame_budget():
    surf = Surface(WIDTH, HEIGHT)
    surf.clear((0, 180, 50))
    surf.rect(100, 100, 300, 200, (0, 255, 80))

    times_ms = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        surf.to_rgb565()
        t1 = time.perf_counter()
        times_ms.append((t1 - t0) * 1000.0)

    avg_ms = sum(times_ms) / len(times_ms)
    print(
        f"\nto_rgb565 @ {WIDTH}x{HEIGHT}: "
        f"avg={avg_ms:.3f}ms min={min(times_ms):.3f}ms max={max(times_ms):.3f}ms "
        f"over {ITERATIONS} iterations"
    )
    assert avg_ms < GENEROUS_BOUND_MS, (
        f"to_rgb565 averaged {avg_ms:.1f}ms/frame over {ITERATIONS} runs -- "
        f"that's over the generous {GENEROUS_BOUND_MS}ms bound, which likely means "
        f"a fast-path regression (e.g. back to a pure-Python per-pixel pack)"
    )
