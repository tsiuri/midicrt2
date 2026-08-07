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
    # somewhere real to go on a stock deploy.
    pages: list[str] = field(default_factory=lambda: ["eventlog", "voices"])
    instruments: list[str] = field(default_factory=lambda: list(DEFAULT_INSTRUMENTS))


def load(path: str | None = None) -> Config:
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
