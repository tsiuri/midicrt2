"""Non-gating performance tripwire benchmarks (Minor/cheap, finding 5,
2026-08-07 fix wave) -- the regression tripwire for phases 4/5.

These are NOT correctness tests -- they exist to catch a gross FUTURE
regression (e.g. someone re-introducing an eager per-tick materialization
a la finding 1's Img2TxtVizAnalyzer.tick()-always-dirty bug, or an eager
duplicate-analyzer cost a la finding 2's two-HarmonyAnalyzer-instances bug)
without pinning brittle exact timings against the Pi's own variable load.
Bounds are deliberately generous -- multiples of the ACTUAL measured
post-fix numbers below, comfortably below the ACTUAL measured pre-fix
numbers, so ordinary Pi load jitter never fails CI while a real regression
of either fixed bug still trips the bound. The measured value is always
printed (via `capsys.disabled()`, so it shows even without `-s`) so a real
regression is easy to diagnose, not just "it went red".

Baseline numbers (see docs/phase3-parity.md's "Performance at phase-3
close" section for the full pre-fix/post-fix table, measured on the real
Pi, HEAD 8a44deb pre-fix / this commit post-fix):
- note_on (warm, repeated-triad play pattern): ~3.86ms pre-fix -> ~0.8ms
  post-fix (findings 1 unrelated, finding 2's shared+cached
  HarmonyAnalyzer is the fix).
- idle tick cycle, zero subscribers: previously not gate-able at all (no
  refcount concept existed) -- the OLD `run()` loop always materialized
  every dirty topic including Img2TxtVizAnalyzer's ~6.94ms view_model()
  every tick; post-fix (finding 1's subscriber-aware materialization)
  measures ~0.2ms/tick with a wired zero-subscriber refcount provider.
"""
import statistics
import time

from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent


def _note_on(ts, note, ch=0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch,
                      data1=note, data2=100, summary=f"note_on ch1 n{note} v100")


def _note_off(ts, note, ch=0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_off", channel=ch,
                      data1=note, data2=0, summary=f"note_off ch1 n{note} v0")


def test_warm_note_on_handle_is_fast(capsys):
    """Regression tripwire for finding 2 (memoize + share HarmonyAnalyzer).
    Plays a repeating C-major triad (a representative "typical playing"
    pattern, not a degenerate always-different-pitch-classes worst case)
    to warm up every page/analyzer's state AND the theory.py LRU cache
    before timing."""
    eng = Engine(Config())
    triad = (60, 64, 67)
    t = 0.0
    for i in range(60):
        n = triad[i % 3]
        eng._handle(_note_on(t, n)); t += 0.01
        eng._handle(_note_off(t, n)); t += 0.01

    N = 300
    t0 = time.perf_counter()
    for i in range(N):
        n = triad[i % 3]
        eng._handle(_note_on(t, n)); t += 0.01
    elapsed = time.perf_counter() - t0
    per_call_ms = (elapsed / N) * 1000

    with capsys.disabled():
        print(f"\n[perf] warm _handle(note_on): {per_call_ms:.4f} ms/call "
              f"(N={N}, total {elapsed * 1000:.1f} ms)")

    # Generous tripwire: measured ~0.8ms warm on the real Pi post-fix
    # (finding 2); this bound sits comfortably below the pre-fix ~3.86ms
    # regression it guards against, with real headroom above the measured
    # value for ordinary Pi load jitter.
    assert per_call_ms < 3.0, (
        f"note_on warm-path regressed to {per_call_ms:.3f} ms/call "
        "(tripwire bound 3.0 ms) -- see docs/phase3-parity.md's "
        "'Performance at phase-3 close' section for the last known-good numbers"
    )


def test_idle_tick_cycle_is_fast_with_no_subscribers(capsys):
    """Regression tripwire for finding 1 (subscriber-aware snapshot
    materialization). A running Engine with a refcount provider reporting
    ZERO subscribers for every topic (the realistic idle-daemon shape once
    a real ProtocolServer is attached with no connected clients) must
    never pay img2txtviz's ~6.94ms/tick view_model() cost (or any other
    page's) -- that's the whole point of the finding-1 fix. Feeds one
    note_on first so every page/analyzer carries real, non-empty state
    (exercising the SKIP path for genuinely dirty topics, not just
    perpetually-clean ones)."""
    eng = Engine(Config(tick_hz=30.0))
    eng.set_topic_refcount_provider(lambda topic: 0)
    eng._handle(_note_on(0.0, 60))

    N = 120
    times = []
    for i in range(N):
        now = 1.0 + i * (1.0 / 30.0)
        t0 = time.perf_counter()
        eng._tick_analyzers(now)
        eng._tick_pages(now)
        eng._flush_dirty()
        times.append((time.perf_counter() - t0) * 1000)

    per_tick_ms = statistics.mean(times)
    with capsys.disabled():
        print(f"\n[perf] idle tick cycle, zero subscribers: {per_tick_ms:.4f} ms/tick "
              f"avg over {N} ticks")

    # Generous tripwire: measured ~0.2ms/tick on the real Pi post-fix
    # (finding 1). A regression of the original bug (img2txtviz's
    # view_model() materializing every tick regardless of subscribers)
    # would show up around 6-7ms/tick -- this bound catches that with
    # plenty of margin either side.
    assert per_tick_ms < 3.0, (
        f"idle (zero-subscriber) tick cycle regressed to {per_tick_ms:.3f} ms/tick "
        "(tripwire bound 3.0 ms) -- see finding 1 in docs/phase3-parity.md's "
        "'Performance at phase-3 close' section"
    )
