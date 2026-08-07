from midicrt.clients.chrome import DEFAULT_STATUS_VM, status_text
from midicrt.clients.tui import (
    _KEY_ACTIONS,
    RENDERERS,
    _render_unknown,
    render_lines,
    render_status_row,
)

VM = {"title": "EVENT LOG", "count": 12,
      "lines": [{"text": f"line{i}", "style": "normal"} for i in range(10)]}


def test_render_geometry_and_content():
    out = render_lines(VM, width=30, height=5)
    assert len(out) == 5
    assert all(len(line) == 30 for line in out)
    assert out[0].startswith("EVENT LOG  (12 events)")
    assert out[-1].strip() == "line9"          # newest last
    assert out[1].strip() == "line6"           # only the tail fits


def test_render_truncates_long_lines():
    vm = {"title": "EVENT LOG", "count": 1,
          "lines": [{"text": "x" * 100, "style": "normal"}]}
    out = render_lines(vm, width=10, height=2)
    assert all(len(line) == 10 for line in out)


def test_render_empty():
    out = render_lines({"title": "EVENT LOG", "count": 0, "lines": []}, 20, 3)
    assert len(out) == 3 and out[2] == " " * 20


def test_renderers_dispatch_table_has_eventlog():
    assert RENDERERS["eventlog"] is render_lines


def test_key_actions_maps_n_to_page_next_and_c_to_clear():
    assert _KEY_ACTIONS["n"] == "page.next"
    assert _KEY_ACTIONS["c"] == "eventlog.clear"
    assert "q" not in _KEY_ACTIONS  # quit is handled separately, not via action dispatch


def test_render_unknown_fallback_has_no_crash_on_bare_vm():
    out = _render_unknown({}, width=20, height=3)
    assert len(out) == 3
    assert all(len(line) == 20 for line in out)


# -- chrome: status row (phase-3 task 3) -------------------------------------

def test_render_status_row_is_exactly_width_wide():
    row = render_status_row(DEFAULT_STATUS_VM, width=15)
    assert len(row) == 15
    row = render_status_row({"bpm": 128.7, "bar": 12, "beat": 3,
                             "running": True, "source": "USB MIDI"}, width=200)
    assert len(row) == 200


def test_render_status_row_truncates_to_width():
    row = render_status_row({"bpm": 128.7, "bar": 12, "beat": 3,
                             "running": True, "source": "USB MIDI"}, width=6)
    assert len(row) == 6
    assert row == status_text({"bpm": 128.7, "bar": 12, "beat": 3,
                               "running": True, "source": "USB MIDI"})[:6]


def test_render_status_row_matches_shared_chrome_text_when_it_fits():
    vm = {"bpm": None, "bar": 0, "beat": 1, "running": False, "source": None}
    text = status_text(vm)
    row = render_status_row(vm, width=len(text) + 10)
    assert row.strip() == text
