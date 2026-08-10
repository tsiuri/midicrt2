"""Tuner page: v1's `pages/tuner.py` (PAGE_ID 10, "Tuner"), wrapping
`analyzers.tuner.TunerAnalyzer` -- see that module's docstring for the full
Phase 9 Task 3 investigation (aubio build failure on this Pi, the
dependency-free numpy YIN this analyzer now runs, the independent-capture
architecture decision this page implements below).

Ownership split: this page owns the `AudioCapture` background thread
(constructed here, started/stopped only via `start_capture()`/
`stop_capture()` below) -- mirrors `pages/spectrum.py`'s identical
ownership split byte-for-byte (SAME `AudioCapture` class from
`analyzers/spectrum.py`, reused unmodified via duck typing: `TunerAnalyzer`
implements the same `on_audio_block`/`mark_available`/`mark_unavailable`
shape `SpectrumAnalyzer` does). `start_capture()`/`stop_capture()` are
NEVER called by this module or by any test -- only `daemon.py`'s
production `run()` calls them (guarded on `"tuner" in engine.pages` and
the same `--no-audio` opt-out spectrum's own capture already uses) -- so
constructing a `TunerPage` (as every test here does) never touches real
hardware.

Independent from spectrum's OWN `AudioCapture`, not a shared tap
(disclosed architecture decision, see analyzers/tuner.py's module
docstring's "Audio-capture architecture" section for the full tradeoff
writeup): this is a SECOND native PortAudio stream running alongside
spectrum's own, not v1's single-shared-thread-multi-tap design. Chosen for
blast-radius/regression-risk reasons (zero changes needed to
`analyzers/spectrum.py`/`pages/spectrum.py`/the already-passing spectrum
test suite) -- the measured resource cost of running both concurrently is
in task-3-report.md, not assumed free.

`audio_backend` is a test-only injection point, forwarded straight to the
`AudioCapture` this page constructs (mirrors `pages/spectrum.py`'s
identical parameter) -- production code never passes it, leaving
`AudioCapture`'s own default lazy `import sounddevice` path untouched.
`device` defaults to `None` ("system default input", same convention as
spectrum) but production wiring (`engine/core.py`'s `_PAGE_FACTORIES`)
passes `config.audio_device` -- the SAME config knob spectrum reads, since
both pages want the same physical input device, not two independently
configured ones.

Joins `config.py`'s default `pages` roster (Phase 9 Task 3 -- previously
excluded, see git history for the prior "permanently idle, no default-
roster benefit" reasoning, now moot: this page shows real, live pitch
data with just a running daemon + an audio-capable input, matching the
"voices"/"harmony"/"pianoroll"/"spectrum" precedent for default-roster
inclusion, and degrades gracefully to a "no audio input" state exactly
like spectrum when no input device is present).

`tick(now) -> True` (fix-round finding, CRITICAL, found and fixed before
sign-off): `on_audio_block` runs on `AudioCapture`'s own background
thread, and `Engine._tick_pages` (engine/core.py) is the ONLY thing that
turns a page's activity into `self._dirty.add("page.<name>")` -- and only
for pages that HAVE a `tick(now)` method returning True. Without one here,
nothing ever told the engine tuner had new data: live-verified against a
real running daemon (a subscribed client + `page.goto tuner` received
exactly ONE snapshot -- the goto's own forced push -- then nothing further
for 15+ real seconds despite the capture thread calling `on_audio_block`
at ~43Hz throughout). Unlike `SpectrumPage.tick()` (which derives True/
False from peak-hold decay math it recomputes anyway), this method has no
equivalent per-tick computation to derive a real True/False from without
adding a new thread-safe dirty-flag between the capture thread and the
engine's asyncio-loop thread -- SpectrumAnalyzer/TunerAnalyzer's existing
`threading.Lock`-guarded state was never designed to also carry a "changed
since last tick" edge, only the current values. `tick()` here instead
always returns True, unconditionally -- completing the wiring for the
"always dirty" contract `TunerAnalyzer.on_pitch_sample`'s own docstring
already documented (v1's `draw()` redraws Conf/Level every audio block
regardless of whether a note is locked) but that nothing on the
tick-driven path previously consulted. Cost: `page.tuner` is marked dirty
every engine tick (default `tick_hz=30.0`) whenever "tuner" is in the
roster, matching pianoroll's own "ticks fairly often while live" cost
profile -- cheap (a set insertion) unless a client is actually subscribed
to `page.tuner`, in which case the traffic is exactly the live audio
readout this page exists to show.
"""
from midicrt.analyzers.spectrum import AudioCapture
from midicrt.analyzers.tuner import TunerAnalyzer


class TunerPage:
    name = "tuner"

    def __init__(self, device: str | None = None, audio_backend=None) -> None:
        self._analyzer = TunerAnalyzer()
        self._capture = AudioCapture(self._analyzer, device=device, backend=audio_backend)

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def tick(self, now: float) -> bool:
        return True   # see module docstring's "tick(now) -> True" section

    def view_model(self) -> dict:
        return {"title": "TUNER", **self._analyzer.view_model()}

    def start_capture(self) -> None:
        """Start the real (or injected-fake) audio thread -- see module
        docstring for why this is opt-in and never called by tests."""
        self._capture.start()

    def stop_capture(self) -> None:
        self._capture.stop()
