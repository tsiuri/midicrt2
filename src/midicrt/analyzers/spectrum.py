"""SpectrumAnalyzer + AudioCapture: v1's audio spectrum analyzer, ported
from `~/codex/midicrt/pages/audiospectrum.py` (733 lines, READ-ONLY
reference on the Pi) -- see docs/phase3-notes.md's v1 source map and
`.superpowers/sdd/2026-08-06-midicrt2-phase3-parity/task-8-brief.md` for the
task this ports.

Investigation findings (read before assuming this is another deferred-idle
page like analyzers/tuner.py -- it is NOT)
---------------------------------------------------------------------------
- **v1's audio backend is `sounddevice` (PortAudio bindings) + `numpy`**,
  NOT `alsaaudio`/`pyalsaaudio` -- confirmed by reading v1's
  `_try_imports()`/`_audio_loop()` (`import sounddevice as sd`). `aubio`
  (used by v1's tuner, see analyzers/tuner.py's own docstring) is a
  SEPARATE dependency v1's tuner page pulls in on top of this module's
  live PCM stream; this analyzer itself never touches aubio.
- **Real hardware IS present and RUNNING on the Pi right now** (verified
  2026-08-06/07 via `arecord -l` + `pactl list sources short` +
  `systemctl --user status pipewire`): a USB Audio Device (`card 1: Device
  [USB Audio Device]`, C-Media chipset) is plugged in, and PipeWire already
  has `alsa_input.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.mono-
  fallback` in state RUNNING. This is DIFFERENT from the task brief's
  anticipated fallback ("if the Pi has no live audio-in right now,
  implement against a fake PCM source, coded-but-unverified") -- the real
  capture path below is exercised against genuine hardware, not just typed
  against v1's contract on faith. `sounddevice` opens PortAudio's `default`
  input, which PipeWire's ALSA-plugin default routing resolves to this
  device -- confirmed by `~/codex/midicrt-venv` (v1's own venv) already
  having `sounddevice==0.5.2` + `numpy==2.3.4` installed and working on
  this exact box (v1's spectrum/tuner pages are live features on the
  production CRT). `libportaudio2` (system apt package, arm64) is also
  already installed -- `pip install sounddevice` into `~/midicrt2-venv`
  needed no extra system packages beyond what was already on the Pi.
- **v1 has NO peak-hold at all** -- grepped the full 733-line file for
  "peak"/"hold" case-insensitively; the only "peak" hit is v1's unrelated
  `_auto_adapt` headroom-scaling feature (`peak = max(levels) * g`, a
  display-scale auto-adjuster, not a per-bin decaying peak marker). The
  `peak_hold` field this module's `view_model()` reports is accordingly a
  v2 ADDITION, not a port -- see `PEAK_DECAY_PER_S` below and
  `SpectrumAnalyzer.tick()`'s docstring for the disclosed design (a
  standard analog-spectrum-analyzer decaying peak cap, chosen because the
  task brief's VM contract explicitly asks for `"peak_hold": [...]` and
  `pages/voices.py`/`clients/fb/app.py::render_voices_frame` already
  established the "live fill + peak-hold tick" visual convention this
  reuses).
- v1's bin mapping (ported here, log-frequency bands + 'max' per-band
  pooling -- its DEFAULTS): `_freq_scale='log'` (v1 also supports a 'lin'
  alt scale via keypress, not exposed anywhere in v2 config -- NOT
  ported), `_agg_mode='max'` (v1 also supports 'avg' via keypress -- NOT
  ported), `TARGET_BINS=96` default (v2: `DEFAULT_BINS`, exposed as
  `config.spectrum_bins`), `FMIN_HZ=40.0` (log-band low-cut), a single-pole
  DC-blocker/high-pass at `HPF_HZ=30.0` (`HPF_ON=True` in v1 by default,
  not user-toggleable in v2), `FLOOR_DB=-110.0`/`CEIL_DB=10.0` dB-to-[0,1]
  normalization range, `SMOOTHING=0.6` exponential level smoothing.  v1's
  extensive `keypress()` runtime tuning (bins/gain/smoothing/floor/ceil/
  freq-scale/agg-mode/lowcut/HPF-toggle/device-cycle) has NO v2 equivalent
  interactive-keybinding system yet (phase3-notes.md's "Input layer needs a
  key->action TABLE" is phase-4 work) -- none of it is ported; v2 exposes
  only `audio_device`/`spectrum_bins` via `config.toml`, per the task
  brief's explicit ask.

Architecture: the sanctioned impurity, isolated
---------------------------------------------------------------------------
Mirrors `engine/midi_in.py`'s own mixed-purity convention (pure `matches`/
`translate` functions + the impure `MidiInput` thread class, ONE file) --
this module similarly holds pure FFT/bin math (`compute_bins`, `high_pass`)
alongside the one genuinely impure piece, `AudioCapture` (a background
PortAudio input-stream thread). It lives here in `analyzers/` rather than
`engine/` because -- unlike MIDI input, which every page/analyzer's
`handle(ev)` can potentially consume off the shared engine queue -- audio
capture is single-purpose infrastructure only this one page needs; it does
not feed the engine's event queue at all, it calls straight into
`SpectrumAnalyzer.on_audio_block()` (the same "injected-data method, no
engine queue involved" shape `analyzers/tuner.py`'s `on_pitch_sample()`
already established for non-MIDI-driven analyzers).

`AudioCapture` lazily imports `sounddevice` ONLY inside `_run()` (mirroring
v1's own `_try_imports()`/`_audio_loop()` lazy-import pattern) -- merely
importing this module, or constructing `SpectrumAnalyzer`/`AudioCapture`/
`pages.spectrum.SpectrumPage` (as every test in this suite does), NEVER
touches PortAudio/hardware. The real thread only starts when something
calls `.start()` -- production code only (`pages/spectrum.py`'s
`start_capture()`, wired from `daemon.py`'s `run()`) -- so pytest's full
suite (including on a CI image with no `libportaudio2` installed at all)
never runs the audio thread; every test here drives the pure math directly
with synthetic PCM, or drives `AudioCapture` through an injected fake
backend (mirroring `test_midi_in.py`'s `FakeBackend` for `MidiInput`).
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from midicrt.engine.core import MidiEvent

_LOG = logging.getLogger(__name__)

# -- v1 defaults (see module docstring's "Investigation findings") ----------
DEFAULT_SR = 44100
BLOCKSIZE = 1024
DEFAULT_BINS = 96
MIN_BINS = 8
MAX_BINS = 256
SMOOTHING = 0.6
FLOOR_DB = -110.0
CEIL_DB = 10.0
FMIN_HZ = 40.0
HPF_HZ = 30.0
HPF_ON = True

# v2 addition -- NOT in v1, see module docstring. Normalized [0,1] units of
# peak-hold decay per second of injected wall-clock time (a peak takes
# ~1/PEAK_DECAY_PER_S seconds to fall from a full-scale 1.0 down to the
# live level once the live level drops to 0) -- a disclosed, reasonable
# "analog spectrum analyzer" default, not measured against any v1 behavior
# since none exists to measure.
PEAK_DECAY_PER_S = 0.7


# -- pure math: FFT/bin computation ------------------------------------------


@lru_cache(maxsize=8)
def _band_edges(sr: int, blocksize: int, bins: int, fmin_hz: float) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Log-frequency band start/end FFT-bin indices -- pure port of v1's
    `_rebuild_cache`'s 'log' `_freq_scale` branch (v1's 'lin' alt scale is
    not exposed in v2, see module docstring). `lru_cache` replaces v1's
    manual module-global cache-key check (`_cache_key`/`_rebuild_cache`)
    with the same "recompute only when parameters change" effect, without
    any mutable global state -- safe to call from any thread since the
    cache itself is read-only once populated for a given key.
    """
    nsrc = blocksize // 2 + 1
    freqs = np.fft.rfftfreq(blocksize, d=1.0 / float(sr))
    target = max(1, int(bins))
    fmin = max(fmin_hz, float(freqs[1]) if freqs.size > 1 else fmin_hz)
    fmax = float(freqs[-1]) if freqs.size else sr / 2.0
    if fmax <= fmin:
        fmin = max(1.0, fmax * 0.5)
    edges = np.geomspace(fmin, fmax, target + 1)
    starts = np.searchsorted(freqs, edges[:-1]).astype(np.int64)
    ends = np.searchsorted(freqs, edges[1:]).astype(np.int64)
    empty = starts >= ends
    if empty.any():
        cf = np.sqrt(edges[:-1][empty] * edges[1:][empty])
        nearest = np.searchsorted(freqs, cf).clip(0, nsrc - 1).astype(np.int64)
        starts[empty] = nearest
        ends[empty] = nearest + 1
    starts = starts.clip(0, nsrc - 1)
    ends = ends.clip(0, nsrc)
    return tuple(int(s) for s in starts), tuple(int(e) for e in ends)


def compute_bins(block: np.ndarray, sr: int, bins: int,
                  fmin_hz: float = FMIN_HZ, floor_db: float = FLOOR_DB,
                  ceil_db: float = CEIL_DB) -> list[float]:
    """Pure port of v1's `_compute_spectrum`: Hanning window -> `rfft` ->
    power spectrum -> per-band 'max' pooling (v1's default `_agg_mode`) ->
    dB -> normalized-and-clipped to [0, 1]. Unlike v1's version, this takes
    NO high-pass state -- that's `high_pass()` below, applied by the
    CALLER (`SpectrumAnalyzer.on_audio_block`) before this function ever
    sees the block, so this stays a pure, stateless FFT pipeline directly
    testable with synthetic PCM (silence -> all zero, a sine -> one
    concentrated band, white noise -> spread across many bands).
    """
    block = np.asarray(block, dtype=np.float32)
    n = block.size
    target = max(1, int(bins))
    if n == 0:
        return [0.0] * target
    win = np.hanning(n).astype(np.float32)
    winp = float(np.sum(win ** 2)) + 1e-12
    xw = block * win
    xf = np.fft.rfft(xw)
    ps = (np.abs(xf) ** 2) / winp
    if ps.size:
        ps[0] = 0.0
    nsrc = ps.size
    if nsrc <= 1:
        return [0.0] * target
    starts, ends = _band_edges(sr, n, target, fmin_hz)
    band_val = np.zeros(target, dtype=np.float32)
    for i in range(target):
        seg = ps[starts[i]:ends[i]]
        band_val[i] = float(seg.max()) if seg.size else 0.0
    eps = 1e-15
    db = 10.0 * np.log10(np.maximum(band_val, eps))
    norm = (db - floor_db) / max(1e-6, (ceil_db - floor_db))
    norm = np.clip(norm, 0.0, 1.0)
    return norm.tolist()


def high_pass(x: np.ndarray, sr: int, cutoff_hz: float,
              x_prev: float, y_prev: float) -> tuple[np.ndarray, float, float]:
    """Pure port of v1's single-pole DC-blocker/high-pass filter (inlined
    in `_compute_spectrum`): `y[n] = (x[n]-x[n-1]) + R*y[n-1]`, vectorised
    via the same geometric-prefix-sum v1 uses. v1 threads `x_prev`/`y_prev`
    through MODULE GLOBALS (`_hpf_x_prev`/`_hpf_y_prev`) mutated inside the
    audio callback; this port makes that state an explicit parameter/
    return pair instead, so it's directly unit-testable with synthetic PCM
    (no global state, no audio thread needed) -- `SpectrumAnalyzer.
    on_audio_block` is the only caller that threads the state across real
    blocks, one call per block, exactly like v1's callback does per-call
    with its globals.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return x, x_prev, y_prev
    r = float(np.exp(-2.0 * np.pi * cutoff_hz / float(sr)))
    n = x.size
    d = np.empty(n, dtype=np.float64)
    d[0] = x[0] - x_prev
    d[1:] = np.diff(x)
    r_pow = r ** np.arange(n, dtype=np.float64)
    d_scaled = d / np.where(r_pow == 0.0, 1.0, r_pow)
    y = r_pow * (np.cumsum(d_scaled) + y_prev * r)
    new_x_prev = float(x[-1])
    new_y_prev = float(y[-1])
    return y.astype(np.float32), new_x_prev, new_y_prev


# -- SpectrumAnalyzer: pure state machine, non-MIDI-driven -------------------


class SpectrumAnalyzer:
    """Pure state machine (aside from the `threading.Lock` guarding state
    shared with `AudioCapture`'s callback thread) -- NOT MIDI-driven, same
    "genuine exception" shape as `analyzers/tuner.py`'s `TunerAnalyzer`:
    `handle(MidiEvent) -> bool` is always a no-op (v1's audiospectrum.py
    has no MIDI handler either); the real input is the injected-data
    method `on_audio_block(block, sr) -> bool`, called by `AudioCapture`'s
    PortAudio callback thread (impure caller; this method's BODY is pure
    per-block math delegating to `high_pass()`/`compute_bins()` above).
    """

    def __init__(self, bins: int = DEFAULT_BINS) -> None:
        bins = max(MIN_BINS, min(MAX_BINS, int(bins)))
        self._bins_n = bins
        self._levels = [0.0] * bins
        self._peak = [0.0] * bins
        self._hpf_x_prev = 0.0
        self._hpf_y_prev = 0.0
        self._available = False
        self._device: str | None = None
        self._last_tick: float | None = None
        self._lock = threading.Lock()

    def handle(self, ev: MidiEvent) -> bool:
        return False   # v1's audiospectrum.py has no MIDI handler -- see module docstring

    def on_audio_block(self, block: np.ndarray, sr: int) -> bool:
        """Injected-data method: one real PCM block in, dirty-bool out.
        Threads the HPF state across calls (mirroring v1's per-callback
        global mutation, see `high_pass()`'s docstring), then EMA-smooths
        each band (v1's `SMOOTHING`) and raises that bin's peak-hold
        immediately if the new smoothed level exceeds it (peak-hold only
        ever rises here -- it FALLS only in `tick()`, via injected
        wall-clock decay, the standard "instant attack, slow release"
        peak-meter shape).
        """
        if HPF_ON:
            x, self._hpf_x_prev, self._hpf_y_prev = high_pass(
                block, sr, HPF_HZ, self._hpf_x_prev, self._hpf_y_prev)
        else:
            x = np.asarray(block, dtype=np.float32)
        raw = compute_bins(x, sr, self._bins_n)
        with self._lock:
            for i, v in enumerate(raw):
                self._levels[i] = SMOOTHING * self._levels[i] + (1.0 - SMOOTHING) * v
                self._peak[i] = max(self._peak[i], self._levels[i])
        return True

    def tick(self, now: float) -> bool:
        """Decay peak-hold toward the live level over injected wall-clock
        time (v2 addition -- see `PEAK_DECAY_PER_S`/module docstring).
        Never reads a real clock -- `now` is always injected by the
        engine's `_tick_pages` (via `pages.spectrum.SpectrumPage.tick`),
        the same convention `pages/pianoroll.py`'s own `tick(now)` uses.
        Returns False (not dirty) on the very first call and whenever
        nothing has ever played (both levels and peaks already at rest at
        0.0, decaying 0.0 changes nothing) -- mirrors `PianorollState.
        tick()`'s "nothing has ever played" no-op contract.
        """
        with self._lock:
            if self._last_tick is None:
                self._last_tick = now
                dt = 0.0
            else:
                dt = max(0.0, now - self._last_tick)
                self._last_tick = now
            if dt == 0.0:
                return False
            changed = False
            decay = PEAK_DECAY_PER_S * dt
            for i in range(self._bins_n):
                new_peak = max(self._levels[i], self._peak[i] - decay)
                if new_peak != self._peak[i]:
                    changed = True
                self._peak[i] = new_peak
            return changed

    def mark_available(self, device_desc: str) -> None:
        """Called by `AudioCapture` once its input stream is genuinely
        open -- no I/O here, just a thread-safe setter (logging, if any,
        is `AudioCapture`'s job, matching this class's "no I/O" analyzer
        convention)."""
        with self._lock:
            self._available = True
            self._device = device_desc

    def mark_unavailable(self) -> None:
        """Called by `AudioCapture` before it was ever able to open a
        stream, or after one closes/fails -- drives the page's "no audio
        input" placeholder (task brief's explicit degrade-gracefully
        contract)."""
        with self._lock:
            self._available = False
            self._device = None

    def view_model(self) -> dict:
        with self._lock:
            return {
                "bins": list(self._levels),
                "peak_hold": list(self._peak),
                "device": self._device,
                "available": self._available,
            }


# -- AudioCapture: the sanctioned impurity -----------------------------------


class AudioCapture:
    """Background PortAudio input-stream thread -- mirrors `engine/
    midi_in.py`'s `MidiInput` shape (a `backend` injection point for tests,
    a daemon `threading.Thread`, a `threading.Event` to stop it) applied to
    audio capture instead of MIDI ports. See module docstring for why this
    lives here rather than `engine/` and why merely constructing this class
    never touches real hardware.

    `device` is a case-insensitive SUBSTRING match against PortAudio input
    device names (v1's `_refresh_devices()`/`_inputs` list approach) --
    `None` (the default, matching `config.audio_device`'s default) means
    "use PortAudio's own system-default input device", v1's
    `_device_index = None` convention. An unmatched name falls back to the
    same default rather than raising, since a stale/typo'd config value
    should degrade to "some audio" rather than hard-fail the page.
    """

    def __init__(self, analyzer: SpectrumAnalyzer, device: str | None = None,
                 sr: int = DEFAULT_SR, blocksize: int = BLOCKSIZE, backend=None) -> None:
        self._analyzer = analyzer
        self._device_name = device
        self._sr = sr
        self._blocksize = blocksize
        self._backend = backend
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-spectrum", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _resolve_device(self, backend) -> int | None:
        if not self._device_name:
            return None
        try:
            devices = backend.query_devices()
        except Exception:  # noqa: BLE001 -- a broken query must fall back, not crash
            return None
        needle = self._device_name.lower()
        for i, dev in enumerate(devices):
            try:
                if dev.get("max_input_channels", 0) > 0 and needle in dev.get("name", "").lower():
                    return i
            except AttributeError:
                continue
        return None

    def _describe_device(self, backend, device_idx: int | None) -> str:
        try:
            if device_idx is None:
                return backend.query_devices(kind="input").get("name", "default")
            return backend.query_devices(device_idx).get("name", f"device {device_idx}")
        except Exception:  # noqa: BLE001 -- a description failure must not block capture
            return "default" if device_idx is None else f"device {device_idx}"

    def _run(self) -> None:
        backend = self._backend
        try:
            if backend is None:
                import sounddevice as backend
        except Exception as exc:  # noqa: BLE001 -- missing lib must degrade, not crash the daemon
            _LOG.info("audio capture unavailable (%s); spectrum page will show "
                      "the no-audio-input placeholder", exc)
            self._analyzer.mark_unavailable()
            return

        try:
            device_idx = self._resolve_device(backend)
            device_desc = self._describe_device(backend, device_idx)

            def callback(indata, frames, time_info, status) -> None:
                if self._stop.is_set():
                    raise backend.CallbackStop
                block = indata.mean(axis=1) if getattr(indata, "ndim", 1) == 2 else indata
                self._analyzer.on_audio_block(np.asarray(block, dtype=np.float32), self._sr)

            with backend.InputStream(samplerate=self._sr, channels=1, dtype="float32",
                                     blocksize=self._blocksize, callback=callback,
                                     device=device_idx):
                self._analyzer.mark_available(device_desc)
                while not self._stop.is_set():
                    self._stop.wait(0.2)
        except Exception as exc:  # noqa: BLE001 -- device open/runtime errors must not crash the daemon
            _LOG.warning("audio capture failed (%s); spectrum page will show "
                        "the no-audio-input placeholder", exc)
        finally:
            self._analyzer.mark_unavailable()
