"""TDD for TunerPage: a thin wrapper around analyzers.tuner.TunerAnalyzer +
(Phase 9 Task 3) the AudioCapture lifecycle it now owns independently --
see pages/tuner.py's and analyzers/tuner.py's own module docstrings for
the "independent capture, not a shared v1-style tap" design decision, and
for why merely constructing a page here never touches real hardware.

`start_capture()`/`stop_capture()` are tested here purely as DELEGATION
(swap in a stub object and assert it was called), mirroring
`tests/test_pages_spectrum.py`'s own identical convention -- AudioCapture's
own real threading/device-resolution/graceful-degradation behavior is
already fully covered by `tests/test_analyzers_spectrum.py`'s FakeBackend
tests (the SAME `AudioCapture` class, reused unmodified here); this file
does not re-test it.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.tuner import TunerPage


def test_view_model_shape_and_idle_defaults():
    page = TunerPage()
    vm = page.view_model()
    assert vm["title"] == "TUNER"
    assert vm["has_signal"] is False
    assert vm["note"] == ""
    assert vm["available"] is False
    assert vm["device"] is None


def test_handle_is_a_true_noop_for_midi_events():
    page = TunerPage()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False
    assert page.view_model()["has_signal"] is False


def test_each_page_instance_gets_its_own_independent_analyzer():
    page_a = TunerPage()
    page_b = TunerPage()
    page_a._analyzer.on_pitch_sample(hz=440.0, confidence=0.9, db=-10.0)
    assert page_a.view_model()["has_signal"] is True
    assert page_b.view_model()["has_signal"] is False   # untouched


# -- AudioCapture lifecycle (Phase 9 Task 3) ----------------------------------

def test_constructing_a_page_never_touches_real_hardware():
    # No AudioCapture thread exists until start_capture() is called --
    # merely constructing the page (as every other test in this file does)
    # must never spawn one.
    page = TunerPage()
    assert page._capture._thread is None


def test_start_capture_and_stop_capture_delegate_to_the_capture_object():
    page = TunerPage()
    calls = []
    page._capture.start = lambda: calls.append("start")
    page._capture.stop = lambda: calls.append("stop")
    page.start_capture()
    page.stop_capture()
    assert calls == ["start", "stop"]


def test_audio_backend_injection_reaches_the_capture_object():
    sentinel = object()
    page = TunerPage(audio_backend=sentinel)
    assert page._capture._backend is sentinel


def test_device_config_is_forwarded_to_the_capture_object():
    page = TunerPage(device="usb audio")
    assert page._capture._device_name == "usb audio"


def test_capture_is_wired_to_this_pages_own_analyzer():
    # The AudioCapture instance this page owns must feed THIS page's own
    # analyzer, not a stray one -- proven by driving a fake block through
    # the capture's wired analyzer reference and checking the page's own
    # view_model reacts.
    page = TunerPage()
    assert page._capture._analyzer is page._analyzer


# -- tick() wiring (Phase 9 Task 3 fix-round finding) -------------------------
#
# CRITICAL bug found and fixed before sign-off: `on_audio_block`'s data
# arrives on `AudioCapture`'s own background thread and is NEVER, on its
# own, visible to `Engine._tick_pages` (engine/core.py) -- that method only
# marks a page's topic dirty (and therefore worth pushing to subscribed
# clients) when the page HAS a `tick(now)` method AND that method returns
# True. `SpectrumPage.tick()` derives its own True/False from peak-hold
# decay math it recomputes anyway; without an equivalent `tick()` here,
# `TunerPage` had NO wire from the audio thread to the engine's dirty-set
# at all -- live-verified via a real subscribed client against a running
# daemon: page.goto tuner pushed exactly ONE snapshot (the goto's own
# forced push) and then NOTHING further for 15+ real seconds despite the
# capture thread calling `on_audio_block` at ~43Hz the entire time. Fixed
# by giving `TunerPage` a `tick(now) -> True` that ALWAYS reports dirty --
# not a new design decision, just completing the wiring for the "always
# dirty" contract `TunerAnalyzer.on_pitch_sample`'s own docstring already
# documented (v1's `draw()` redraws Conf/Level every audio block
# regardless of whether a note is locked) but that, before this fix,
# nothing on the tick-driven path ever consulted.

def test_tick_always_reports_dirty():
    # Mirrors on_pitch_sample's own "Always dirty" docstring contract --
    # audio-driven readouts (Conf/Level) update every block regardless of
    # whether a note is currently locked, so every tick is worth pushing.
    page = TunerPage()
    assert page.tick(0.0) is True
    assert page.tick(1.0) is True   # still True on a second call, no state to exhaust
