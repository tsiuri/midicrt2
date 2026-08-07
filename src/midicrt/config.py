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
    # "screensaver" (phase-3 task 9) joins the default roster too, despite
    # being reachable in the ordinary v1-page sense only via
    # `behaviors/screensaver.py`'s own `page.goto screensaver` -- unlike
    # "tuner" (registered but NOT default), the screensaver BEHAVIOR must
    # be able to `page.goto` it out of the box on a stock deploy (v1's own
    # screensaver has no enable-via-config gate at all, see
    # `behaviors/screensaver.py`'s module docstring), which requires the
    # page to already be instantiated in `self.pages` -- `_PAGE_FACTORIES`
    # entries only ever instantiate when their name is in THIS list (see
    # engine/core.py's `Engine.__init__`). The trade-off, disclosed: it
    # also becomes reachable by ordinary `page.next`/`page.prev` cycling,
    # unlike v1 (which has no page concept for it at all) -- harmless (a
    # deliberately blank page a user can also navigate to on purpose), see
    # pages/screensaver.py's own module docstring.
    # "img2txtviz" (phase-3 task 10): a fully self-contained MIDI + wall-
    # clock-reactive animation with no unbuilt dependency (unlike "tuner"),
    # so it joins the default roster by the SAME precedent "voices"/
    # "harmony"/"pianoroll"/"spectrum" already established -- NOT because
    # v1's own idle-triggered pagecycle plugin happened to include page ID
    # 17 in its curated `cycle_pages=[1,6,8,9]` subset (it didn't; see
    # pages/img2txtviz.py's own module docstring for why that v1 fact isn't
    # actually the deciding signal for v2's roster, given task 9's own
    # finding that `behaviors/pagecycle.py` doesn't model that curated-
    # subset concept at all). "config" (also task 10) is a read-only
    # viewer with zero dependency of any kind -- the task dispatch's own
    # explicit ask ("roster: add 'config'") -- appended last as the newest
    # addition, same convention every prior task's page followed.
    pages: list[str] = field(
        default_factory=lambda: [
            "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
            "img2txtviz", "config"])
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
    # Phase-3 task 9 (behaviors/pagecycle.py, behaviors/screensaver.py):
    # defaults carried over from v1's ACTUAL deployed
    # `~/codex/midicrt/config/settings.json` sections on the Pi (verified,
    # not invented) --
    #   "pagecycle": {"enabled": true, "cycle_pages": [1, 6, 8, 9],
    #                 "interval": 300.0, "user_pause": 3600.0}
    #   "screensaver": {"idle_timeout": 60.0}
    # `pagecycle_idle_s` takes v1's `interval` (the cadence of automatic
    # advances, see behaviors/pagecycle.py's module docstring for why this
    # is the closest honest analog under the brief's idle-triggered
    # re-interpretation -- v1's own `cycle_pages`/`user_pause` have no
    # clean v2 mapping and are not ported). `screensaver_after_s` takes
    # v1's `idle_timeout` directly (a 1:1 port, no re-interpretation
    # needed there). `screensaver_enabled` defaults `True` even though v1
    # has no enable/disable key for it at all (see behaviors/
    # screensaver.py's module docstring: v1's plugin is simply always
    # loaded, i.e. permanently "enabled") -- v2 adds the knob the brief
    # requires without changing the out-of-the-box behavior.
    pagecycle_enabled: bool = True
    pagecycle_idle_s: float = 300.0
    screensaver_enabled: bool = True
    screensaver_after_s: float = 60.0


def load(path: str | None = None) -> Config:
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
