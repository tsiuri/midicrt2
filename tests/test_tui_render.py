from midicrt.clients.chrome import DEFAULT_STATUS_VM, status_text
from midicrt.clients.tui import (
    _KEY_ACTIONS,
    RENDERERS,
    _render_unknown,
    _voices_bar,
    render_harmony_lines,
    render_lines,
    render_status_row,
    render_voices_lines,
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


# -- voices page (phase-3 task 4) --------------------------------------------

VOICES_ROWS = [
    {"ch": i, "name": f"Instr{i}", "active": 0, "peak": 0, "notes": []}
    for i in range(1, 17)
]
VOICES_ROWS[0] = {"ch": 1, "name": "Kawai XD5", "active": 3, "peak": 8, "notes": [60, 64, 67]}
VOICES_ROWS[2] = {"ch": 3, "name": "BassStaRack", "active": 12, "peak": 12,
                  "notes": list(range(30, 42))}
VOICES_VM = {"title": "VOICES", "total": 15, "total_peak": 20, "rows": VOICES_ROWS}

# Frozen against an actual run of render_voices_lines(VOICES_VM, 40, 17) --
# the golden text-frame test for this renderer (TUI's equivalent of a golden
# PNG: exact output, not just shape assertions).
GOLDEN_VOICES_FRAME = [
    "VOICES  (poly 15/20)  [n]ext page [q]uit",
    "01 Kawai XD5    ▓▓▓░░░░░  3/8           ",
    "02 Instr2       ░░░░░░░░  0/0           ",
    "03 BassStaRack  ▓▓▓▓▓▓▓▓ 12/12          ",
    "04 Instr4       ░░░░░░░░  0/0           ",
    "05 Instr5       ░░░░░░░░  0/0           ",
    "06 Instr6       ░░░░░░░░  0/0           ",
    "07 Instr7       ░░░░░░░░  0/0           ",
    "08 Instr8       ░░░░░░░░  0/0           ",
    "09 Instr9       ░░░░░░░░  0/0           ",
    "10 Instr10      ░░░░░░░░  0/0           ",
    "11 Instr11      ░░░░░░░░  0/0           ",
    "12 Instr12      ░░░░░░░░  0/0           ",
    "13 Instr13      ░░░░░░░░  0/0           ",
    "14 Instr14      ░░░░░░░░  0/0           ",
    "15 Instr15      ░░░░░░░░  0/0           ",
    "16 Instr16      ░░░░░░░░  0/0           ",
]


def test_voices_render_matches_frozen_golden_frame():
    out = render_voices_lines(VOICES_VM, width=40, height=17)
    assert out == GOLDEN_VOICES_FRAME
    assert all(len(line) == 40 for line in out)


def test_voices_bar_fills_proportionally_and_caps_at_v1_poly_limit_scale():
    # 8 segments == v1's zvoicemonitor.py POLY_LIMIT_CH default -- a fixed
    # visual scale, not an enforced limit (see analyzers/voices.py).
    assert _voices_bar(0) == "░" * 8
    assert _voices_bar(3) == "▓▓▓" + "░" * 5
    assert _voices_bar(8) == "▓" * 8
    assert _voices_bar(20) == "▓" * 8   # capped visually; numeric label stays exact


def test_voices_row_text_shows_true_counts_even_when_bar_is_capped():
    row = {"ch": 3, "name": "BassStaRack", "active": 12, "peak": 12, "notes": []}
    text = render_voices_lines({"title": "VOICES", "total": 12, "total_peak": 12,
                                "rows": [row]}, width=40, height=2)[1]
    assert "12/12" in text
    assert "▓▓▓▓▓▓▓▓" in text   # bar itself is capped at 8 segments


def test_voices_render_truncates_name_to_fixed_width():
    row = {"ch": 1, "name": "A Very Long Instrument Name", "active": 0, "peak": 0, "notes": []}
    out = render_voices_lines({"title": "VOICES", "total": 0, "total_peak": 0, "rows": [row]},
                               width=60, height=2)
    assert "A Very Long " in out[1]
    assert "Instrument" not in out[1]   # truncated to 12 chars, matching name field width


def test_voices_render_pads_blank_rows_at_bottom_when_height_exceeds_row_count():
    row = {"ch": 1, "name": "X", "active": 0, "peak": 0, "notes": []}
    out = render_voices_lines({"title": "VOICES", "total": 0, "total_peak": 0, "rows": [row]},
                               width=20, height=5)
    assert len(out) == 5
    assert out[2] == " " * 20 and out[3] == " " * 20 and out[4] == " " * 20


def test_voices_render_cuts_off_extra_rows_when_height_is_short():
    # Unlike eventlog's newest-at-bottom tail, channels always render in
    # order top-down; a short terminal just loses the highest-numbered rows.
    out = render_voices_lines(VOICES_VM, width=20, height=5)
    assert len(out) == 5
    assert out[1].strip().startswith("01")
    assert out[4].strip().startswith("04")


def test_voices_renderers_dispatch_table_has_voices():
    assert RENDERERS["voices"] is render_voices_lines


# -- harmony page (phase-3 task 5) --------------------------------------------

HARMONY_VM = {
    "title": "HARMONY",
    "chords": [
        {"name": "C maj", "conf": 1.0, "missing": []},
        {"name": "A m", "conf": None, "missing": []},
    ],
    "scales": [
        {"name": "C Ionian", "conf": 0.86, "missing": ["D"]},
        {"name": "A Aeolian 7", "conf": None, "missing": []},
    ],
    "inside": ["C", "E", "G"],
    "outside": ["C#"],
    "key": "C maj",
    "key_conf": 0.83,
    "key_alternatives": ["A min"],
    "tension": 0.35,
    "tension_label": "mild",
    "tension_worst_interval": "M3/m6",
    "harmonic_rhythm": {"changes_per_bar": 1.2, "label": "moderate"},
    "motif": {"found": True, "pattern": "+2 -1 +4", "count": 2},
    "silent": False,
}

# Frozen against an actual run of render_harmony_lines(HARMONY_VM, 60, 13) --
# same "freeze from a real run" discipline as GOLDEN_VOICES_FRAME above.
GOLDEN_HARMONY_FRAME = [
    "HARMONY  (key: C maj)  [n]ext page [q]uit                   ",
    "Chord: Last         2nd          3rd          4th           ",
    "Chord: C maj        A m          --           --            ",
    "Scale: Last         2nd          3rd          4th           ",
    "Scale: C Ionian     A Aeolian 7  --           --            ",
    "Inside: C E G                                               ",
    "Outside: C#                                                 ",
    "Chord conf: 1.00  missing: -                                ",
    "Scale conf: 0.86  missing: D                                ",
    "Key: C maj  (alts: A min)                                   ",
    "Tension: ███████░░░░░░░░░░░░░  0.35  mild  [M3/m6]          ",
    "Harm.rhy: 1.2 ch/bar  moderate                              ",
    "Motif: +2 -1 +4  [x2]                                       ",
]


def test_harmony_render_matches_frozen_golden_frame():
    out = render_harmony_lines(HARMONY_VM, width=60, height=13)
    assert out == GOLDEN_HARMONY_FRAME
    assert all(len(line) == 60 for line in out)


def test_harmony_render_empty_state_shows_placeholders():
    empty = {
        "title": "HARMONY", "chords": [], "scales": [], "inside": [], "outside": [],
        "key": None, "key_conf": 0.0, "key_alternatives": [],
        "tension": 0.0, "tension_label": "silent", "tension_worst_interval": "",
        "harmonic_rhythm": {"changes_per_bar": None, "label": ""},
        "motif": {"found": False, "pattern": None, "count": 0},
        "silent": True,
    }
    out = render_harmony_lines(empty, width=48, height=13)
    assert out[0].startswith("HARMONY  (key: ?)")
    assert "-- " in out[2]           # Chord values row: all placeholders
    assert out[5].strip() == "Inside: -"
    assert out[6].strip() == "Outside: -"
    assert "Chord conf: --  missing: -" in out[7]
    assert "Scale conf: --  missing: -" in out[8]
    assert out[9].strip() == "Key: ?"
    assert "░" * 20 in out[10]        # fully-empty tension bar
    assert out[11].strip() == "Harm.rhy: --"
    assert out[12].strip() == "Motif: --"


def test_harmony_tension_bar_fills_proportionally():
    from midicrt.clients.tui import _harmony_tension_line

    line = _harmony_tension_line({"tension": 0.5, "tension_label": "mild",
                                   "tension_worst_interval": ""})
    assert "█" * 10 in line
    assert "░" * 10 in line


def test_harmony_render_pads_blank_rows_when_height_exceeds_body():
    out = render_harmony_lines(HARMONY_VM, width=20, height=20)
    assert len(out) == 20
    assert out[-1] == " " * 20


def test_harmony_render_cuts_off_extra_rows_when_height_is_short():
    out = render_harmony_lines(HARMONY_VM, width=20, height=3)
    assert len(out) == 3
    assert out[1].startswith("Chord: Last")


def test_harmony_renderers_dispatch_table_has_harmony():
    assert RENDERERS["harmony"] is render_harmony_lines
