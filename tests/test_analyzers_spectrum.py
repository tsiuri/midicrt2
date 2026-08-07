"""TDD for SpectrumAnalyzer/AudioCapture: v1's audio spectrum analyzer,
ported from `~/codex/midicrt/pages/audiospectrum.py`. See analyzers/
spectrum.py's own module docstring for the full investigation (v1's
`sounddevice` backend, the real USB Audio Device found on the Pi, v1's
log-band FFT mapping, the v2-only `peak_hold` addition).

Pure-math tests below drive `compute_bins`/`high_pass` with synthetic PCM
(sine waves at known frequencies, silence, white noise) -- never real audio
hardware. `AudioCapture` tests use an injected `FakeBackend` (mirroring
`test_midi_in.py`'s `FakeBackend` for `MidiInput`) -- the real `sounddevice`
import path (`backend=None`) is only exercised by one test that forces the
import itself to fail, proving the graceful-degradation contract without
ever touching real PortAudio/hardware. No test here starts a thread against
`backend=None` with a *working* import -- that path is production-only
(`pages/spectrum.py`'s `start_capture()`, wired from `daemon.py`), per the
task's "audio thread must not run in CI" constraint.
"""
import time

import numpy as np
import pytest

from midicrt.analyzers.spectrum import (
    DEFAULT_BINS,
    DEFAULT_SR,
    FLOOR_DB,
    HPF_HZ,
    MAX_BINS,
    MIN_BINS,
    AudioCapture,
    SpectrumAnalyzer,
    compute_bins,
    high_pass,
)
from midicrt.engine.core import MidiEvent


def _sine(freq_hz: float, sr: int = DEFAULT_SR, n: int = 1024, amp: float = 1.0) -> np.ndarray:
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


# -- pure math: compute_bins --------------------------------------------------


def test_compute_bins_silence_is_all_zero():
    block = np.zeros(1024, dtype=np.float32)
    assert compute_bins(block, DEFAULT_SR, 32) == [0.0] * 32


def test_compute_bins_returns_requested_bin_count():
    bins = compute_bins(_sine(1000.0), DEFAULT_SR, 40)
    assert len(bins) == 40


def test_compute_bins_values_are_normalized_0_to_1():
    bins = compute_bins(_sine(1000.0), DEFAULT_SR, 32)
    assert all(0.0 <= v <= 1.0 for v in bins)


def test_compute_bins_empty_block_returns_zeros_sized_to_bins():
    assert compute_bins(np.array([], dtype=np.float32), DEFAULT_SR, 16) == [0.0] * 16


def test_compute_bins_sine_produces_a_concentrated_peak():
    bins = compute_bins(_sine(2000.0, amp=1.0), DEFAULT_SR, 48)
    peak_idx = max(range(len(bins)), key=lambda i: bins[i])
    assert bins[peak_idx] > 0.5
    others = [v for i, v in enumerate(bins) if abs(i - peak_idx) > 1]
    assert (sum(others) / len(others)) < (bins[peak_idx] / 2)


def test_compute_bins_log_scale_orders_low_and_high_tones_correctly():
    # A low tone must peak in an earlier (lower-index) band than a high
    # tone under v1's log-frequency band mapping -- a coarse sanity check
    # that doesn't require re-deriving the exact band-edge geometry.
    low_bins = compute_bins(_sine(100.0), DEFAULT_SR, 32)
    high_bins = compute_bins(_sine(8000.0), DEFAULT_SR, 32)
    low_peak = max(range(32), key=lambda i: low_bins[i])
    high_peak = max(range(32), key=lambda i: high_bins[i])
    assert low_peak < high_peak


def test_compute_bins_white_noise_spreads_energy_across_many_bins():
    rng = np.random.default_rng(42)
    block = rng.normal(0.0, 0.3, 1024).astype(np.float32)
    bins = compute_bins(block, DEFAULT_SR, 32)
    nonzero = [v for v in bins if v > 0.05]
    assert len(nonzero) > 20   # most bins carry SOME energy, unlike the sine's one peak


def test_compute_bins_floor_db_default_matches_v1():
    assert FLOOR_DB == -110.0


# -- pure math: high_pass ------------------------------------------------------


def test_high_pass_blocks_dc_offset():
    # A step onset (x_prev=0.0 -> a sustained 0.5 DC level) must decay
    # toward zero well within the block -- the DC-blocker settling.
    block = np.full(4096, 0.5, dtype=np.float32)
    y, _x_prev, _y_prev = high_pass(block, DEFAULT_SR, HPF_HZ, x_prev=0.0, y_prev=0.0)
    assert abs(float(y[0])) == pytest.approx(0.5, abs=1e-6)
    assert abs(float(y[-1])) < 1e-3


def test_high_pass_passes_high_frequency_content_mostly_unattenuated():
    block = _sine(2000.0, amp=1.0)   # well above the 30Hz cutoff
    y, _, _ = high_pass(block, DEFAULT_SR, HPF_HZ, x_prev=0.0, y_prev=0.0)
    assert float(np.max(np.abs(y))) > 0.8


def test_high_pass_threads_state_across_calls_toward_zero():
    block = np.full(256, 0.5, dtype=np.float32)
    y1, xp, yp = high_pass(block, DEFAULT_SR, HPF_HZ, 0.0, 0.0)
    y2, _, _ = high_pass(block, DEFAULT_SR, HPF_HZ, xp, yp)
    assert float(np.max(np.abs(y2))) <= float(np.max(np.abs(y1)))


def test_high_pass_empty_block_returns_state_unchanged():
    y, xp, yp = high_pass(np.array([], dtype=np.float32), DEFAULT_SR, HPF_HZ, 1.0, 2.0)
    assert y.size == 0
    assert xp == 1.0
    assert yp == 2.0


# -- SpectrumAnalyzer: shape / defaults ---------------------------------------


def test_initial_view_model_is_idle_and_unavailable():
    a = SpectrumAnalyzer()
    vm = a.view_model()
    assert vm == {
        "bins": [0.0] * DEFAULT_BINS,
        "peak_hold": [0.0] * DEFAULT_BINS,
        "device": None,
        "available": False,
    }


def test_handle_is_a_true_noop_for_any_midi_event():
    a = SpectrumAnalyzer()
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert a.handle(ev) is False
    assert a.view_model()["available"] is False


def test_bins_count_is_clamped_to_v1s_keypress_range():
    assert len(SpectrumAnalyzer(bins=1).view_model()["bins"]) == MIN_BINS
    assert len(SpectrumAnalyzer(bins=9999).view_model()["bins"]) == MAX_BINS
    assert len(SpectrumAnalyzer(bins=40).view_model()["bins"]) == 40


# -- SpectrumAnalyzer: on_audio_block state machine ---------------------------


def test_on_audio_block_reports_dirty_and_updates_bins():
    a = SpectrumAnalyzer(bins=32)
    changed = a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    assert changed is True
    assert max(a.view_model()["bins"]) > 0.0


def test_on_audio_block_smooths_across_calls_not_a_hard_cut():
    # Tracks the TONE'S OWN band specifically (not "the global max"): going
    # silent after a loud block also drives the high-pass DC-blocker's step
    # response (a real, correct filter transient -- see `on_audio_block`'s
    # docstring/`high_pass()`'s), which can transiently show up as new,
    # genuinely louder energy in OTHER (mostly low-frequency) bins. That's
    # real signal, not smoothing -- this test isolates smoothing itself by
    # watching the one band the injected tone actually lives in.
    a = SpectrumAnalyzer(bins=32)
    a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    bins1 = a.view_model()["bins"]
    tone_idx = max(range(32), key=lambda i: bins1[i])
    first_val = bins1[tone_idx]
    a.on_audio_block(np.zeros(1024, dtype=np.float32), DEFAULT_SR)
    second_val = a.view_model()["bins"][tone_idx]
    assert 0.0 < second_val < first_val   # decays smoothly, not straight to 0


def test_on_audio_block_raises_peak_hold_immediately():
    a = SpectrumAnalyzer(bins=32)
    a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    vm = a.view_model()
    peak_idx = max(range(32), key=lambda i: vm["bins"][i])
    assert vm["peak_hold"][peak_idx] >= vm["bins"][peak_idx]
    assert vm["peak_hold"][peak_idx] > 0.0


def test_peak_hold_does_not_fall_just_because_the_level_drops():
    # peak-hold only ever RISES in on_audio_block() -- per-bin, `new_peak =
    # max(old_peak, new_level)`; it falls only via tick()'s injected
    # wall-clock decay (see next section). This does NOT assert every bin
    # stays EXACTLY unchanged: going silent after a loud block also drives
    # a real high-pass filter transient (see the previous test's comment)
    # that can genuinely raise a few (mostly low-frequency) bins' peaks
    # further -- the actual contract under test is just "never decreases".
    a = SpectrumAnalyzer(bins=16)
    a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    peak_before = list(a.view_model()["peak_hold"])
    a.on_audio_block(np.zeros(1024, dtype=np.float32), DEFAULT_SR)
    peak_after = a.view_model()["peak_hold"]
    assert all(after >= before for after, before in zip(peak_after, peak_before))


# -- SpectrumAnalyzer: tick(now) peak-hold decay ------------------------------


def test_tick_returns_false_when_nothing_has_ever_played():
    a = SpectrumAnalyzer()
    assert a.tick(0.0) is False
    assert a.tick(5.0) is False


def test_tick_decays_peak_hold_toward_the_live_level_over_injected_time():
    a = SpectrumAnalyzer(bins=8)
    a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    bins1 = a.view_model()["bins"]
    tone_idx = max(range(8), key=lambda i: bins1[i])   # the injected tone's own band
    a.on_audio_block(np.zeros(1024, dtype=np.float32), DEFAULT_SR)   # live level now < peak
    a.tick(0.0)   # establish the reference "now" -- first call is dt=0, always False
    before = a.view_model()["peak_hold"][tone_idx]
    changed = a.tick(1.0)   # one full injected second later
    after = a.view_model()["peak_hold"][tone_idx]
    live = a.view_model()["bins"][tone_idx]
    assert changed is True
    assert after < before
    assert after >= live   # never decays below the live level


def test_tick_never_reads_a_real_clock_only_the_injected_now():
    # Passing "now" values nowhere near real wall time must work identically
    # -- mirrors test_pages_pianoroll.py's own structural guard for
    # PianorollState.tick().
    a = SpectrumAnalyzer(bins=4)
    a.on_audio_block(_sine(2000.0, amp=1.0), DEFAULT_SR)
    a.on_audio_block(np.zeros(1024, dtype=np.float32), DEFAULT_SR)   # live level now < peak
    a.tick(10_000_000.0)
    changed = a.tick(10_000_000.0 + 2.0)
    assert changed is True


def test_module_never_imports_sounddevice_at_top_level():
    import midicrt.analyzers.spectrum as mod
    assert not hasattr(mod, "sounddevice")


# -- mark_available / mark_unavailable ----------------------------------------


def test_mark_available_sets_device_and_flag():
    a = SpectrumAnalyzer()
    a.mark_available("USB Audio Device")
    vm = a.view_model()
    assert vm["available"] is True
    assert vm["device"] == "USB Audio Device"


def test_mark_unavailable_clears_device_and_flag():
    a = SpectrumAnalyzer()
    a.mark_available("USB Audio Device")
    a.mark_unavailable()
    vm = a.view_model()
    assert vm["available"] is False
    assert vm["device"] is None


# -- AudioCapture: fake-backend integration (never real hardware) ------------


class _FakeStream:
    def __init__(self, callback):
        self.callback = callback

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeBackend:
    """Mirrors `test_midi_in.py`'s `FakeBackend` shape, for the
    `sounddevice` surface AudioCapture actually calls: `query_devices()`,
    `InputStream(...)` (a context manager), and a `CallbackStop` exception
    class the callback can raise to unwind cleanly."""

    class CallbackStop(Exception):
        pass

    def __init__(self, devices=None, fail_open=False):
        self._devices = devices if devices is not None else []
        self.fail_open = fail_open
        self.streams: list[_FakeStream] = []

    def query_devices(self, kind=None):
        if kind == "input":
            return {"name": "System Default Input"}
        return list(self._devices)

    def InputStream(self, **kw):
        if self.fail_open:
            raise RuntimeError("no such device")
        stream = _FakeStream(kw["callback"])
        self.streams.append(stream)
        return stream


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_audio_capture_marks_available_and_wires_callback_to_analyzer():
    analyzer = SpectrumAnalyzer(bins=16)
    backend = FakeBackend()
    capture = AudioCapture(analyzer, device=None, backend=backend)
    try:
        capture.start()
        assert _wait_until(lambda: analyzer.view_model()["available"])
        assert analyzer.view_model()["device"] == "System Default Input"

        block = _sine(2000.0, amp=1.0).reshape(-1, 1)   # sounddevice shape: (frames, channels)
        backend.streams[0].callback(block, len(block), None, None)
        assert max(analyzer.view_model()["bins"]) > 0.0
    finally:
        capture.stop()
    assert analyzer.view_model()["available"] is False


def test_audio_capture_start_is_idempotent():
    analyzer = SpectrumAnalyzer()
    backend = FakeBackend()
    capture = AudioCapture(analyzer, backend=backend)
    try:
        capture.start()
        assert _wait_until(lambda: analyzer.view_model()["available"])
        first_thread = capture._thread
        capture.start()   # must not spawn a second thread
        assert capture._thread is first_thread
        assert len(backend.streams) == 1
    finally:
        capture.stop()


def test_audio_capture_resolves_device_by_case_insensitive_substring():
    devices = [
        {"name": "bcm2835 Headphones", "max_input_channels": 0},
        {"name": "USB Audio Device (C-Media)", "max_input_channels": 1},
    ]
    backend = FakeBackend(devices=devices)
    capture = AudioCapture(SpectrumAnalyzer(), device="usb audio", backend=backend)
    assert capture._resolve_device(backend) == 1


def test_audio_capture_unmatched_device_name_falls_back_to_default():
    backend = FakeBackend(devices=[{"name": "Foo", "max_input_channels": 1}])
    capture = AudioCapture(SpectrumAnalyzer(), device="nonexistent", backend=backend)
    assert capture._resolve_device(backend) is None


def test_audio_capture_none_device_name_is_default_without_querying():
    capture = AudioCapture(SpectrumAnalyzer(), device=None, backend=FakeBackend())
    assert capture._resolve_device(FakeBackend()) is None


def test_audio_capture_stream_open_failure_marks_unavailable_not_crash():
    analyzer = SpectrumAnalyzer()
    backend = FakeBackend(fail_open=True)
    capture = AudioCapture(analyzer, backend=backend)
    capture.start()
    capture._thread.join(timeout=2.0)
    assert analyzer.view_model()["available"] is False


def test_audio_capture_missing_backend_marks_unavailable_not_crash(monkeypatch):
    # Simulates `sounddevice` being unimportable (e.g. no libportaudio on
    # this machine) WITHOUT ever touching the real module -- `backend=None`
    # triggers AudioCapture's real lazy `import sounddevice` inside `_run`;
    # this forces that specific import to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("no libportaudio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    analyzer = SpectrumAnalyzer()
    capture = AudioCapture(analyzer)   # backend=None -> real lazy-import path
    capture.start()
    capture._thread.join(timeout=2.0)
    assert analyzer.view_model()["available"] is False


def test_audio_capture_stop_without_start_is_a_noop():
    capture = AudioCapture(SpectrumAnalyzer(), backend=FakeBackend())
    capture.stop()   # must not raise
