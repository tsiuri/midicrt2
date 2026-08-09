"""Spectrum page: v1's `pages/audiospectrum.py` (PAGE_ID 9, "Audio
Spectrum"), wrapping `analyzers.spectrum.SpectrumAnalyzer` -- see that
module's docstring for the full investigation (v1's `sounddevice` backend,
the real USB Audio Device present on the Pi, v1's log-band/max-pooling
defaults, the v2-only `peak_hold` addition).

Ownership split: this page owns the `AudioCapture` background thread
(constructed here, started/stopped only via `start_capture()`/
`stop_capture()` below) rather than the analyzer owning it or the engine
core reaching into it -- mirrors `daemon.py`'s existing `MidiInput`
lifecycle (`build()` constructs it, `run()` calls `.start()`/`.stop()`
around the event loop), applied one layer down since audio capture is
page-specific rather than engine-wide. `start_capture()`/`stop_capture()`
are NEVER called by this module or by any test -- only `daemon.py`'s
production `run()` calls them (guarded on `"spectrum" in engine.pages` and
a `--no-audio` opt-out, mirroring `--no-midi`) -- so constructing a
`SpectrumPage` (as every test here does) never touches real hardware; see
`analyzers/spectrum.py`'s module docstring for the full "CI-safe by
construction" rationale.

`audio_backend` is a test-only injection point, forwarded straight to the
`AudioCapture` this page constructs (mirrors `AudioCapture.__init__`'s own
`backend` parameter, one level up) -- production code never passes it,
leaving `AudioCapture`'s own default lazy `import sounddevice` path
untouched.
"""
from midicrt.analyzers.spectrum import DEFAULT_BINS, AudioCapture, SpectrumAnalyzer


class SpectrumPage:
    name = "spectrum"

    def __init__(self, device: str | None = None, bins: int = DEFAULT_BINS,
                 audio_backend=None) -> None:
        self._analyzer = SpectrumAnalyzer(bins=bins)
        self._capture = AudioCapture(self._analyzer, device=device, backend=audio_backend)

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def tick(self, now: float) -> bool:
        return self._analyzer.tick(now)

    def view_model(self) -> dict:
        return {"title": "SPECTRUM", **self._analyzer.view_model()}

    def start_capture(self) -> None:
        """Start the real (or injected-fake) audio thread -- see module
        docstring for why this is opt-in and never called by tests."""
        self._capture.start()

    def stop_capture(self) -> None:
        self._capture.stop()

    # -- page-declared actions (Phase 8 Task 6, keymap revamp) ---------------
    #
    # See `analyzers.spectrum.SpectrumAnalyzer.adjust_bins`'s own docstring
    # for why bin-count is the one v1 retuning control this task adds real
    # mutable state for; the other 21 v1 keys stay disclosed-deferred.

    def _action_bins(self, delta: int) -> dict:
        return {"bins": self._analyzer.adjust_bins(delta)}

    def actions(self) -> list[tuple[str, object, str, dict[str, str]]]:
        return [
            ("spectrum.bins", self._action_bins,
             "Adjust the spectrum's bin count (clamped to [8, 256])", {"delta": "int"}),
        ]
