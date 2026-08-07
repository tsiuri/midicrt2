"""TDD for SpectrumPage: a thin wrapper around analyzers.spectrum.
SpectrumAnalyzer + the AudioCapture lifecycle it owns. See pages/
spectrum.py's and analyzers/spectrum.py's own module docstrings for why
this page owns (constructs, but never auto-starts) the capture thread, and
why merely constructing a page here never touches real hardware.

`start_capture()`/`stop_capture()` are tested here purely as DELEGATION
(swap in a stub object and assert it was called) -- AudioCapture's own
real threading/device-resolution/graceful-degradation behavior is already
fully covered by tests/test_analyzers_spectrum.py's FakeBackend tests; this
file does not re-test it.
"""
from midicrt.engine.core import MidiEvent
from midicrt.pages.spectrum import SpectrumPage


def test_view_model_shape_and_idle_defaults():
    page = SpectrumPage()
    vm = page.view_model()
    assert vm["title"] == "SPECTRUM"
    assert vm["available"] is False
    assert vm["device"] is None
    assert len(vm["bins"]) == 96
    assert len(vm["peak_hold"]) == 96


def test_bins_config_is_forwarded_to_the_analyzer():
    page = SpectrumPage(bins=32)
    vm = page.view_model()
    assert len(vm["bins"]) == 32
    assert len(vm["peak_hold"]) == 32


def test_handle_is_a_true_noop_for_midi_events():
    page = SpectrumPage()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False
    assert page.view_model()["available"] is False


def test_tick_delegates_to_the_analyzer():
    page = SpectrumPage()
    assert page.tick(0.0) is False   # nothing has ever played
    page._analyzer.mark_available("Test Device")  # sanity: analyzer is reachable
    assert page.view_model()["available"] is True


def test_each_page_instance_gets_its_own_independent_analyzer():
    page_a = SpectrumPage()
    page_b = SpectrumPage()
    page_a._analyzer.mark_available("Test Device")
    assert page_a.view_model()["available"] is True
    assert page_b.view_model()["available"] is False   # untouched


def test_constructing_a_page_never_touches_real_hardware():
    # No AudioCapture thread exists until start_capture() is called --
    # merely constructing the page (as every other test in this file does)
    # must never spawn one.
    page = SpectrumPage()
    assert page._capture._thread is None


def test_start_capture_and_stop_capture_delegate_to_the_capture_object():
    page = SpectrumPage()
    calls = []
    page._capture.start = lambda: calls.append("start")
    page._capture.stop = lambda: calls.append("stop")
    page.start_capture()
    page.stop_capture()
    assert calls == ["start", "stop"]


def test_audio_backend_injection_reaches_the_capture_object():
    sentinel = object()
    page = SpectrumPage(audio_backend=sentinel)
    assert page._capture._backend is sentinel
