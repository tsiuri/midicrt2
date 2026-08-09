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


class ConfigError(ValueError):
    """Raised when a `Config` cannot support a working engine, even though
    it loaded/constructed without error. The one current use (2026-08-07
    fix wave, "Must-fix" finding): `Engine.__init__` raises this when
    `config.pages` resolves to an EMPTY roster (an empty list, or every
    name unknown to `_PAGE_FACTORIES`) -- see engine/core.py's own
    docstring at the raise site for why an empty roster must fail loudly
    at startup rather than silently limping along with no pages to serve.
    A `ValueError` subclass (not a bare new `Exception`) so anything that
    already does broad `except ValueError` config-validation handling
    catches this too, while `except ConfigError` stays available for
    callers that want to be specific."""


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
    # Phase-3 task 12 (gap ports): "help" (pages/help.py), "progchanges"
    # (pages/progchanges.py), "ccmonitor"/"ccdashboard" (pages/ccmonitor.py
    # + pages/ccdashboard.py, analyzers/ccmonitor.py), "chordkey"
    # (pages/chordkey.py), and "sendnotes" (pages/sendnotes.py) all join
    # the default roster too -- every one is self-contained with no
    # unbuilt dependency (the "voices"/"harmony"/.../"config" precedent),
    # unlike "tuner" (excluded: permanently idle until a future
    # audio-capture task). "sendnotes" shows real, useful status (dev/ch/
    # octave/velocity/gate/active) even though the actual note-trigger
    # keys have no client-side keyboard binding yet (same Phase-4 gap as
    # every other page's own unbound interactive keys, e.g. pianoroll's
    # channel-visibility editor) -- reachable via `midicrt action
    # sendnotes.key`, matching the "img2txtviz.charset"/.invert precedent.
    pages: list[str] = field(
        default_factory=lambda: [
            "eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
            "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
            "chordkey", "sendnotes"])
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
    # Phase-3 task 9 (behaviors/screensaver.py) / Phase 8 task 5
    # (behaviors/pagecycle.py, v1-semantics restoration -- see that
    # module's own docstring for the FULL story, including the ID->name
    # mapping evidence): defaults carried over from v1's ACTUAL deployed
    # `~/codex/midicrt/config/settings.json` sections on the Pi (verified,
    # not invented) --
    #   "pagecycle": {"enabled": true, "cycle_pages": [1, 6, 8, 9],
    #                 "interval": 300.0, "user_pause": 3600.0}
    #   "screensaver": {"idle_timeout": 60.0}
    # `pagecycle_interval`/`pagecycle_pages`/`pagecycle_user_pause` are a
    # 1:1 port of v1's `interval`/`cycle_pages`/`user_pause` (`cycle_pages`
    # IDs mapped to v2 page names via behaviors/pagecycle.py's own
    # docstring evidence: 1->harmony, 6->eventlog, 8->pianoroll,
    # 9->spectrum). This REPLACES the phase-3 task 9 `pagecycle_idle_s`
    # field (an idle-triggered re-interpretation of v1's `interval`,
    # explicitly ruled OUT by the 2026-08-08 decisions doc -- "TURN BACK ON
    # with v1 semantics" -- and removed outright, a disclosed breaking
    # config change pre-cutover; no deployed config.toml on the Pi had ever
    # set it). `screensaver_after_s` takes v1's `idle_timeout` directly (a
    # 1:1 port, unaffected by this task). `screensaver_enabled` defaults
    # `True` even though v1 has no enable/disable key for it at all (see
    # behaviors/screensaver.py's module docstring: v1's plugin is simply
    # always loaded, i.e. permanently "enabled") -- v2 adds the knob the
    # brief requires without changing the out-of-the-box behavior.
    pagecycle_enabled: bool = True
    pagecycle_interval: float = 300.0
    pagecycle_pages: list[str] = field(
        default_factory=lambda: ["harmony", "eventlog", "pianoroll", "spectrum"])
    pagecycle_user_pause: float = 3600.0
    screensaver_enabled: bool = True
    screensaver_after_s: float = 60.0
    # Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md):
    # `capture_dir` (default `None`) explicitly overrides `engine/
    # capture.py::resolve_capture_dir`'s own StateDirectory-vs-dev-fallback
    # resolution -- see that function's docstring. `capture_retention`
    # (default 50) is `CaptureSink`'s unpinned-session cap swept at every
    # `capture.start`. `capture_auto_start` defaults `False`, matching v1's
    # OWN deployed behavior verified on the Pi (`~/codex/midicrt/config/
    # settings.json`'s `memory`/`capture` sections have no arm-at-boot
    # flag at all -- v1's `engine/memory/capture.py::MemoryCaptureManager`
    # only ever arms via an explicit `memory_start()` call bound to a
    # pianoroll-page keystroke, `pages/pianoroll_exp.py`, never
    # automatically) -- v2 does not change that out-of-the-box behavior,
    # it just adds the knob (same "add the config the brief requires
    # without changing shipped behavior" precedent as `screensaver_enabled`
    # above).
    capture_dir: str | None = None
    capture_retention: int = 50
    capture_auto_start: bool = False
    # Phase 8 Task 4 (docs/visual-audit.md §20b, "the header page-title
    # scrolling marquee" -- v1's primary anti-burn-in device): v1's own
    # `core.header_scroll_speed` config key (`~/codex/midicrt/config/
    # settings.json`, default 4.0 chars/sec, `midicrt.py`'s
    # `HEADER_SCROLL_SPEED`) -- see `analyzers/marquee.py::MarqueeAnalyzer`
    # for the port. A flat field (not a nested `core.*` section), matching
    # this dataclass's own existing convention for every other v1-ported
    # knob (`screensaver_after_s`, `pagecycle_interval`, ...).
    header_scroll_speed_cps: float = 4.0


def load(path: str | None = None) -> Config:
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
