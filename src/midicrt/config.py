"""User config: read-only TOML (spec §7 — the engine never writes this file)."""
import os
import tomllib
from dataclasses import dataclass, field, fields

DEFAULT_PATH = os.path.expanduser("~/.config/midicrt/config.toml")

# v1's 16 instrument names, one per MIDI channel (1-indexed: index 0 == ch 1)
# -- verified against `~/codex/midicrt/config/settings.json`'s
# `instruments.names` on the Pi (phase-3 task 4), not invented. Baked in as
# the dataclass default so a fresh Pi with no config.toml still shows real
# names on the voices page; a `config.toml` `instruments` key still overrides
# (see `load()` below -- unknown/missing keys just keep the dataclass default).
DEFAULT_INSTRUMENTS = [
    "Kawai XD5", "Matrix-1k", "BassStaRack", "Mnilogue", "Arp 2600",
    "Yamaha 1", "Yamaha 2", "Yamaha 3", "Akai S 1", "Akai S 2",
    "Akai S 3", "Akai S 4", "Akai CD1", "Akai CD2", "Akai CD3", "Akai CD4",
]


@dataclass
class Config:
    socket_path: str = "/run/midicrt/ctl.sock"
    midi_sources: list[str] = field(default_factory=lambda: ["*"])
    tick_hz: float = 30.0
    eventlog_capacity: int = 200
    # "voices" is phase-3 task 4's page -- the first second page, live by
    # default (no config.toml required) so `page.next`/`page.goto` have
    # somewhere real to go on a stock deploy. "harmony" is phase-3 task 5's
    # page, appended the same way. "pianoroll" (phase-3 task 7) shows real
    # data with just a running daemon + MIDI input too, same as those two --
    # unlike "tuner" (task 6), which stays out of this default list since it
    # can only ever show its idle state until a future audio-capture task
    # lands (see pages/tuner.py's own docstring). "spectrum" (phase-3 task 8)
    # DOES join this list despite also depending on audio hardware --
    # unlike tuner, it was built with graceful degradation as a first-class
    # state (`available: false` -> "no audio input" placeholder, see
    # analyzers/spectrum.py's module docstring), so a stock deploy with no
    # USB audio device plugged in still shows a real, correct page instead
    # of a permanently-broken one; tuner has no such fallback to show yet.
    pages: list[str] = field(
        default_factory=lambda: ["eventlog", "voices", "harmony", "pianoroll", "spectrum"])
    instruments: list[str] = field(default_factory=lambda: list(DEFAULT_INSTRUMENTS))
    # Phase-3 task 8 (analyzers/spectrum.py): `audio_device` substring-
    # matches a PortAudio input device name (case-insensitive, v1's
    # `_refresh_devices()` approach) -- None (the default) means "use
    # PortAudio's own system-default input device", matching v1's
    # `_device_index = None` convention. `spectrum_bins` is v1's
    # `TARGET_BINS` (default 96), clamped to v1's own [8, 256] keypress
    # range by `SpectrumAnalyzer.__init__` regardless of what's configured
    # here.
    audio_device: str | None = None
    spectrum_bins: int = 96


def load(path: str | None = None) -> Config:
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
