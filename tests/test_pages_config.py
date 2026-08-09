"""TDD for ConfigPage (page name "config") -- a READ-ONLY viewer of the
effective Config plus live engine facts, per spec §5's config-page
clarification. See pages/configview.py's own module docstring for the full
v1 comparison and the "Engine-info wiring" design (no engine reference
needed for the Config half; the engine-facts half is bound in separately by
`Engine.__init__`, exercised in test_engine_core.py).
"""
from midicrt.config import Config
from midicrt.engine.core import MidiEvent
from midicrt.pages.configview import _IDLE_ENGINE_INFO, ConfigPage


def _row_value(rows: list[dict], label: str):
    for r in rows:
        if r["label"] == label:
            return r["value"]
    raise KeyError(label)


def test_view_model_shape_and_title():
    page = ConfigPage(Config())
    vm = page.view_model()
    assert vm["title"] == "CONFIG"
    assert "config_rows" in vm
    assert "engine_rows" in vm


def test_config_rows_reflect_the_effective_config():
    cfg = Config(socket_path="/tmp/x.sock", tick_hz=42.0,
                 midi_sources=["USB*", "Net*"], pages=["eventlog", "voices"])
    page = ConfigPage(cfg)
    rows = page.view_model()["config_rows"]
    assert _row_value(rows, "socket_path") == "/tmp/x.sock"
    assert _row_value(rows, "tick_hz") == "42"
    assert _row_value(rows, "midi_sources") == "USB*, Net*"
    assert _row_value(rows, "pages") == "eventlog, voices"


def test_instrument_count_row_reports_a_count_not_the_full_list():
    cfg = Config(instruments=["A", "B", "C"])
    page = ConfigPage(cfg)
    rows = page.view_model()["config_rows"]
    assert _row_value(rows, "instruments") == "3 configured"


def test_behavior_knob_rows_reflect_pagecycle_and_screensaver_config():
    # Phase 8 Task 5: `pagecycle_idle_s` (idle-triggered re-interpretation)
    # is gone -- replaced by `pagecycle_interval`/`pagecycle_pages`, the
    # restored v1 knobs (see config.py's own comment and
    # behaviors/pagecycle.py's module docstring).
    cfg = Config(pagecycle_enabled=False, pagecycle_interval=99.0,
                 pagecycle_pages=["harmony", "eventlog", "pianoroll"],
                 screensaver_enabled=True, screensaver_after_s=12.5)
    page = ConfigPage(cfg)
    rows = page.view_model()["config_rows"]
    assert _row_value(rows, "pagecycle") == "off (every 99s, 3 pages)"
    assert _row_value(rows, "screensaver") == "on (after 12.5s)"


def test_engine_rows_fall_back_to_idle_defaults_without_a_bound_engine():
    page = ConfigPage(Config())
    rows = page.view_model()["engine_rows"]
    assert _row_value(rows, "engine_version") == _IDLE_ENGINE_INFO["version"]
    assert _row_value(rows, "current_page") == "unknown"


def test_bind_engine_info_wires_the_engine_rows():
    page = ConfigPage(Config())
    page.bind_engine_info(lambda: {
        "version": "2.0.0.dev0", "proto_version": "1.0", "uptime_s": 12.3,
        "current_page": "voices", "pages": ["eventlog", "voices"], "analyzers": ["status"],
    })
    rows = page.view_model()["engine_rows"]
    assert _row_value(rows, "engine_version") == "2.0.0.dev0"
    assert _row_value(rows, "uptime_s") == "12.3"
    assert _row_value(rows, "current_page") == "voices"
    assert _row_value(rows, "pages_live") == "eventlog, voices"
    assert _row_value(rows, "analyzers_live") == "status"


def test_handle_is_a_true_noop_for_midi_events():
    page = ConfigPage(Config())
    ev = MidiEvent(ts=0.0, source="kbd", type="note_on", channel=0,
                   data1=60, data2=100, summary="note_on ch1 n60 v100")
    assert page.handle(ev) is False


def test_each_page_instance_is_independent():
    page_a = ConfigPage(Config())
    page_b = ConfigPage(Config())
    page_a.bind_engine_info(lambda: {
        "version": "x", "proto_version": "y", "uptime_s": 1.0,
        "current_page": "z", "pages": [], "analyzers": [],
    })
    assert page_a.view_model()["engine_rows"] != page_b.view_model()["engine_rows"]
