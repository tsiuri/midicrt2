"""User config: read-only TOML (spec §7 — the engine never writes this file)."""
import os
import tomllib
from dataclasses import dataclass, field, fields

DEFAULT_PATH = os.path.expanduser("~/.config/midicrt/config.toml")


@dataclass
class Config:
    socket_path: str = "/run/midicrt/ctl.sock"
    midi_sources: list[str] = field(default_factory=lambda: ["*"])
    tick_hz: float = 30.0
    eventlog_capacity: int = 200
    pages: list[str] = field(default_factory=lambda: ["eventlog"])


def load(path: str | None = None) -> Config:
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
