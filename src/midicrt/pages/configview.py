"""Config page (page name "config"): a READ-ONLY viewer of the engine's
effective configuration plus live engine facts -- spec §5's config-page
clarification: "The config page is a *viewer* of effective config plus a
surface for the runtime-adjustable parameters, which it changes via
actions -- durable edits remain hand-edits to `config.toml`, keeping the
never-writes rule intact." This task (10) implements the VIEWING half only,
per the task dispatch's own explicit scope note: "read-only display; NO
mutation actions in this task (runtime-adjustable params already have their
own actions elsewhere)" -- e.g. `pianoroll.zoom`/`.projection`/`.channels`
(task 7), `img2txtviz.charset`/`.invert` (this same task, see
pages/img2txtviz.py).
This page only DISPLAYS the config those actions already mutate live state
for; it registers no actions of its own.

v1 comparison (`~/codex/midicrt/pages/configui.py`, PAGE_ID 14, "Config",
381 lines, READ-ONLY reference): a full recursive dict/list `settings.json`
BROWSER/EDITOR -- arrow-key navigation into nested keys, `+`/`-`/space/`e`
to mutate values in place, `s` to write `settings.json` back to disk via
`configutil.save_settings()` (an unguarded, whole-file rewrite -- exactly
the "runtime settings-rewrite once destroyed the user's instrument names"
clobber class spec §7 exists to eliminate). v2 drops ALL of that: per spec
§7's hard config/state split ("Engine never writes it... `config.reload`
applies hand edits") and the task dispatch's own explicit "NO mutation
actions" scope, this page instead shows a FIXED, flat list of the fields
that actually matter operationally (the ones the task dispatch calls out:
socket path, midi_sources, tick_hz, pages roster, instrument NAME COUNT,
behavior knobs, capture keys "when they exist") rather than a generic
recursive JSON tree. There is no "capture" section in `Config` yet (phase 5
of the migration plan, not built) -- so no capture rows appear here; add
them to `_config_rows()` when that phase lands, per the task dispatch's own
"capture keys when they exist" parenthetical.

Engine-info wiring
------------------
Everything under `Config` is available at construction time -- no engine
reference needed for that half. But `version`/`uptime_s`/`current_page`/the
LIVE pages+analyzers roster are ENGINE-level facts no page can derive from
its own `Config` or MIDI events alone (every other page/analyzer here is
either pure-config or pure-MIDI; this is the first that needs live engine
state). Mirrors `pages/spectrum.py`'s own post-construction-wiring
precedent (`start_capture()` called by `daemon.py`, not the constructor)
applied to a read instead of a thread: `Engine.__init__` calls
`bind_engine_info()` on this page (once, right after building `self.pages`)
with a bound method returning a small dict of engine facts it ALREADY
tracks for its own `status()` command -- not a new I/O source. A page
built directly by a test (no engine involved) simply never gets this
callback wired and falls back to `_IDLE_ENGINE_INFO` below, mirroring
`SpectrumPage`'s own "never touches real anything until production wiring
calls in" contract.
"""
from midicrt.config import Config

_IDLE_ENGINE_INFO = {
    "version": "unknown", "proto_version": "unknown", "uptime_s": 0.0,
    "current_page": "unknown", "pages": [], "analyzers": [],
}


def _fmt_bool(value: bool) -> str:
    return "on" if value else "off"


def _config_rows(cfg: Config) -> list[dict]:
    return [
        {"label": "socket_path", "value": cfg.socket_path},
        {"label": "midi_sources", "value": ", ".join(cfg.midi_sources) or "(none)"},
        {"label": "tick_hz", "value": f"{cfg.tick_hz:g}"},
        {"label": "pages", "value": ", ".join(cfg.pages) or "(none)"},
        {"label": "instruments", "value": f"{len(cfg.instruments)} configured"},
        {"label": "audio_device", "value": cfg.audio_device or "(default)"},
        {"label": "spectrum_bins", "value": str(cfg.spectrum_bins)},
        {"label": "eventlog_capacity", "value": str(cfg.eventlog_capacity)},
        {"label": "pagecycle",
         "value": f"{_fmt_bool(cfg.pagecycle_enabled)} (idle {cfg.pagecycle_idle_s:g}s)"},
        {"label": "screensaver",
         "value": f"{_fmt_bool(cfg.screensaver_enabled)} (after {cfg.screensaver_after_s:g}s)"},
        # No "capture" section exists in `Config` yet -- see module
        # docstring's "capture keys when they exist" note.
    ]


def _engine_rows(info: dict) -> list[dict]:
    return [
        {"label": "engine_version", "value": info["version"]},
        {"label": "proto_version", "value": info["proto_version"]},
        {"label": "uptime_s", "value": f"{info['uptime_s']:.1f}"},
        {"label": "current_page", "value": info["current_page"]},
        {"label": "pages_live", "value": ", ".join(info["pages"]) or "(none)"},
        {"label": "analyzers_live", "value": ", ".join(info["analyzers"]) or "(none)"},
    ]


class ConfigPage:
    name = "config"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._engine_info = None

    def bind_engine_info(self, provider) -> None:
        """Wired once by `Engine.__init__` in production; never called by a
        page constructed directly by a test (see module docstring)."""
        self._engine_info = provider

    def bind_info(self, provider) -> None:
        """Phase 4 Task 0 (engine consolidation, docs/phase4-notes.md):
        formalized name `engine/core.py::PageHooks` discovers generically
        (one hasattr-based pass over the whole page roster, replacing the
        old name-keyed `if "config" in self.pages: ...bind_engine_info`
        special case in `Engine.__init__`) -- a thin delegate to
        `bind_engine_info` above, which stays the page's own real,
        directly-tested method (test_pages_config.py calls it by that more
        descriptive name)."""
        self.bind_engine_info(provider)

    def handle(self, ev) -> bool:
        return False   # a read-only viewer never changes from a MIDI event

    def view_model(self) -> dict:
        info = self._engine_info() if self._engine_info else _IDLE_ENGINE_INFO
        return {
            "title": "CONFIG",
            "config_rows": _config_rows(self._config),
            "engine_rows": _engine_rows(info),
        }
