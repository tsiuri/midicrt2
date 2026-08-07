from midicrt.clients.chrome import (
    DEFAULT_ALERTS_VM,
    DEFAULT_BEATFLASH_VM,
    DEFAULT_LOOPPROGRESS_VM,
    DEFAULT_STATUS_VM,
    DEFAULT_TIMESIG_VM,
    beatprogress_row_text,
    secondary_status_text,
    status_text,
)
from midicrt.clients.tui import (
    _KEY_ACTIONS,
    RENDERERS,
    _config_body_lines,
    _img2txtviz_grid_lines,
    _pianoroll_grid,
    _render_unknown,
    _roll_glyph,
    _spectrum_bar_rows,
    _spectrum_columns,
    _voices_bar,
    render_beatprogress_row,
    render_config_lines,
    render_harmony_lines,
    render_img2txtviz_lines,
    render_lines,
    render_pianoroll_lines,
    render_screensaver_lines,
    render_secondary_row,
    render_spectrum_lines,
    render_status_row,
    render_tuner_lines,
    render_voices_lines,
    screensaver_row_texts,
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


# -- secondary row: alerts/timesig (phase-3 task 6) --------------------------

def test_render_secondary_row_is_exactly_width_wide():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=15)
    assert len(row) == 15


def test_render_secondary_row_shows_timesig_when_no_alerts():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60)
    assert row.strip() == "Time Signature: (no lock)"


def test_render_secondary_row_prefers_alerts_when_present():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "crit", "held_s": 11.0}]}
    timesig_vm = {"labels": ["4/4"], "confidence": 0.9, "events": 20,
                  "events_window": 20, "events_total": 20, "pending": None}
    row = render_secondary_row(alerts_vm, timesig_vm, width=60)
    assert row.strip() == secondary_status_text(alerts_vm, timesig_vm)
    assert row.strip().startswith("STUCK CRIT:")


def test_render_secondary_row_truncates_to_width():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 2.0}]}
    row = render_secondary_row(alerts_vm, DEFAULT_TIMESIG_VM, width=6)
    assert len(row) == 6
    assert row == secondary_status_text(alerts_vm, DEFAULT_TIMESIG_VM)[:6]


# -- third chrome row: beatflash/loopprogress (phase-3 task 9) ---------------

def test_render_beatprogress_row_is_exactly_width_wide():
    row = render_beatprogress_row(DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, width=40)
    assert len(row) == 40


def test_render_beatprogress_row_delegates_to_shared_chrome_text():
    beatflash_vm = {"intensity": 1.0, "is_bar": False}
    loopprogress_vm = {"fraction": 0.25, "running": True}
    assert render_beatprogress_row(beatflash_vm, loopprogress_vm, width=50) == \
        beatprogress_row_text(beatflash_vm, loopprogress_vm, 50)


# -- screensaver page (phase-3 task 9) ---------------------------------------

def test_renderers_dispatch_table_has_screensaver():
    assert RENDERERS["screensaver"] is render_screensaver_lines


def test_render_screensaver_lines_is_entirely_blank():
    out = render_screensaver_lines({"title": "SCREENSAVER"}, width=20, height=5)
    assert len(out) == 5
    assert all(line == " " * 20 for line in out)


def test_render_screensaver_lines_handles_zero_height():
    assert render_screensaver_lines({"title": "SCREENSAVER"}, width=20, height=0) == []


def test_screensaver_row_texts_is_fully_blank_including_chrome_rows():
    # IMPORTANT fix (task-9 review): a screensaver-page "golden" proving
    # EVERY row -- header, body, AND all three chrome rows -- goes
    # entirely blank, no reverse-video content anywhere (matching v1's
    # true full-screen fb blank). header/body here are exactly what
    # render_screensaver_lines produces for a 10-row page body.
    header = " " * 40
    body = render_screensaver_lines({"title": "SCREENSAVER"}, width=40, height=10)
    rows = screensaver_row_texts(header, body, width=40)
    assert len(rows) == 1 + 10 + 3   # header + body + secondary/status/beatprogress
    assert all(row == " " * 40 for row in rows)


def test_screensaver_row_texts_chrome_rows_are_blank_even_with_active_content():
    # The chrome-row blanking must not depend on what the (unused) status/
    # alerts/beatflash/etc. VMs would otherwise say -- screensaver_row_texts
    # takes no VM args at all, so there is nothing for "active" content to
    # leak through from. Sanity-check the shape directly.
    rows = screensaver_row_texts("H", ["B1", "B2"], width=5)
    assert rows == ["H", "B1", "B2", "     ", "     ", "     "]


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


# -- tuner page (phase-3 task 6) ----------------------------------------------

TUNER_IDLE_VM = {"title": "TUNER", "note": "", "cents": 0.0, "hz": 0.0,
                 "confidence": 0.0, "db": -120.0, "has_signal": False}
TUNER_LOCKED_VM = {"title": "TUNER", "note": "A4", "cents": -3.2, "hz": 439.2,
                   "confidence": 0.82, "db": -18.4, "has_signal": True}

# Frozen against an actual run of render_tuner_lines(TUNER_LOCKED_VM, 60, 5)
# and render_tuner_lines(TUNER_IDLE_VM, 60, 5) -- same "freeze from a real
# run" discipline as GOLDEN_VOICES_FRAME/GOLDEN_HARMONY_FRAME above.
GOLDEN_TUNER_LOCKED_FRAME = [
    "TUNER  [n]ext page [q]uit                                   ",
    "Note:A4    Pitch: 439.20 Hz  Cents:  -3.2  Conf:0.82  Level:",
    "Tuning: -------------------^|-------------------            ",
    "                                                            ",
    "                                                            ",
]
GOLDEN_TUNER_IDLE_FRAME = [
    "TUNER  [n]ext page [q]uit                                   ",
    "Listening...  Conf:0.00  Level:-120.0 dB                    ",
    "                                                            ",
    "                                                            ",
    "                                                            ",
]


def test_tuner_render_matches_frozen_golden_frame_when_locked():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=60, height=5)
    assert out == GOLDEN_TUNER_LOCKED_FRAME
    assert all(len(line) == 60 for line in out)


def test_tuner_render_matches_frozen_golden_frame_when_idle():
    out = render_tuner_lines(TUNER_IDLE_VM, width=60, height=5)
    assert out == GOLDEN_TUNER_IDLE_FRAME
    assert all(len(line) == 60 for line in out)


def test_tuner_render_idle_state_has_no_note_or_meter():
    out = render_tuner_lines(TUNER_IDLE_VM, width=60, height=5)
    assert "Listening" in out[1]
    assert "Note:" not in out[1]
    assert out[2].strip() == ""


def test_tuner_render_locked_state_shows_note_cents_and_meter():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=60, height=5)
    assert "Note:A4" in out[1]
    assert "Cents:  -3.2" in out[1]
    assert "Tuning:" in out[2]
    assert "^" in out[2] and "|" in out[2]


def test_tuner_render_pads_blank_rows_when_height_exceeds_body():
    out = render_tuner_lines(TUNER_IDLE_VM, width=20, height=6)
    assert len(out) == 6
    assert out[-1] == " " * 20


def test_tuner_render_cuts_off_extra_rows_when_height_is_short():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=20, height=2)
    assert len(out) == 2
    assert "Note:" in out[1]


def test_tuner_renderers_dispatch_table_has_tuner():
    assert RENDERERS["tuner"] is render_tuner_lines


# -- pianoroll page (phase-3 task 7) ------------------------------------------
#
# A FIXED synthetic note set (three notes, spread across pitch rows/velocity
# tiers/an active-vs-closed span) exercised in BOTH projection modes -- the
# renderer itself is mode-agnostic (it only ever reads the already-projected
# x0/x1/y/vel floats, see clients/tui.py's own module comment), so "golden
# per mode" here proves the renderer's HEADER text tracks `window.mode`
# correctly while the body glyphs stay identical (same input geometry).
PIANOROLL_NOTES = [
    {"ch": 1, "y": 0.0, "x0": 0.1, "x1": 0.9, "vel": 1.0, "active": False},
    {"ch": 2, "y": 0.5, "x0": 0.4, "x1": 0.6, "vel": 0.5, "active": False},
    {"ch": 3, "y": 1.0, "x0": 0.7, "x1": 1.0, "vel": 0.2, "active": True},
]
PIANOROLL_VM_WALLCLOCK = {
    "title": "PIANOROLL",
    "notes": PIANOROLL_NOTES,
    "window": {"mode": "wallclock", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
    "range": {"lo": 60, "hi": 72},
}
PIANOROLL_VM_TEMPO = {
    **PIANOROLL_VM_WALLCLOCK,
    "window": {"mode": "tempo", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
}

# Frozen against an actual run of render_pianoroll_lines(VM, 48, 8) for both
# VMs above -- same "freeze from a real run" discipline as GOLDEN_VOICES_
# FRAME/GOLDEN_HARMONY_FRAME/GOLDEN_TUNER_*_FRAME.
GOLDEN_PIANOROLL_WALLCLOCK_FRAME = [
    'PIANOROLL  (wallclock zoom 1.00, range 60-72)  [',
    '    ████████████████████████████████████████    ',
    '                                                ',
    '                                                ',
    '                   ▓▓▓▓▓▓▓▓▓▓                   ',
    '                                                ',
    '                                                ',
    '                                 ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒',
]
GOLDEN_PIANOROLL_TEMPO_FRAME = [
    'PIANOROLL  (tempo zoom 1.00, range 60-72)  [n]ex',
    '    ████████████████████████████████████████    ',
    '                                                ',
    '                                                ',
    '                   ▓▓▓▓▓▓▓▓▓▓                   ',
    '                                                ',
    '                                                ',
    '                                 ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒',
]


def test_pianoroll_render_matches_frozen_golden_frame_in_wallclock_mode():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=48, height=8)
    assert out == GOLDEN_PIANOROLL_WALLCLOCK_FRAME
    assert all(len(line) == 48 for line in out)


def test_pianoroll_render_matches_frozen_golden_frame_in_tempo_mode():
    out = render_pianoroll_lines(PIANOROLL_VM_TEMPO, width=48, height=8)
    assert out == GOLDEN_PIANOROLL_TEMPO_FRAME
    assert all(len(line) == 48 for line in out)


def test_pianoroll_body_glyphs_are_identical_across_projection_modes():
    # The renderer only ever consumes already-projected coordinates -- see
    # module comment above clients/tui.py's render_pianoroll_lines.
    wallclock = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=48, height=8)[1:]
    tempo = render_pianoroll_lines(PIANOROLL_VM_TEMPO, width=48, height=8)[1:]
    assert wallclock == tempo


def test_pianoroll_render_empty_notes_is_all_blank_body():
    empty = {**PIANOROLL_VM_WALLCLOCK, "notes": []}
    out = render_pianoroll_lines(empty, width=20, height=5)
    assert out[1:] == [" " * 20] * 4


def test_pianoroll_render_pads_to_exactly_height_rows():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=20, height=10)
    assert len(out) == 10
    assert all(len(line) == 20 for line in out)


def test_pianoroll_render_zero_body_height_returns_only_header():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=20, height=1)
    assert len(out) == 1


def test_roll_glyph_thresholds_match_v1s_text_renderer_exactly():
    # Ported byte-for-byte from ui/renderers/text/renderer.py::
    # TextRenderer._velocity_char's raw-velocity thresholds (96/127, 48/127).
    assert _roll_glyph(0.0) == " "
    assert _roll_glyph(1 / 127) == "▒"
    assert _roll_glyph(47 / 127) == "▒"
    assert _roll_glyph(48 / 127) == "▓"
    assert _roll_glyph(95 / 127) == "▓"
    assert _roll_glyph(96 / 127) == "█"
    assert _roll_glyph(1.0) == "█"


def test_pianoroll_grid_picks_highest_velocity_on_overlap():
    notes = [
        {"ch": 1, "y": 0.0, "x0": 0.0, "x1": 1.0, "vel": 0.2, "active": False},
        {"ch": 2, "y": 0.0, "x0": 0.0, "x1": 1.0, "vel": 0.9, "active": False},
    ]
    grid = _pianoroll_grid({"notes": notes}, width=4, body_h=2)
    assert grid[0] == [0.9, 0.9, 0.9, 0.9]
    assert grid[1] == [0.0, 0.0, 0.0, 0.0]


def test_pianoroll_grid_zero_dimensions_do_not_crash():
    assert _pianoroll_grid({"notes": []}, width=0, body_h=5) == [[]] * 5
    assert _pianoroll_grid({"notes": []}, width=5, body_h=0) == []


def test_pianoroll_renderers_dispatch_table_has_pianoroll():
    assert RENDERERS["pianoroll"] is render_pianoroll_lines


# -- spectrum page (phase-3 task 8) -------------------------------------------
#
# A FIXED synthetic bins/peak_hold VM (8 bins, a symmetric "hill" shape) --
# same "freeze from a real run" discipline as the other pages' golden
# frames above. Real audio hardware is never touched by this test (or any
# test in this file) -- see analyzers/spectrum.py's module docstring.
SPECTRUM_AVAILABLE_VM = {
    "title": "SPECTRUM", "available": True, "device": "USB Audio Device",
    "bins": [0.0, 0.25, 0.5, 0.75, 1.0, 0.5, 0.25, 0.0],
    "peak_hold": [0.1, 0.4, 0.6, 0.9, 1.0, 0.7, 0.4, 0.1],
}
SPECTRUM_IDLE_VM = {
    "title": "SPECTRUM", "available": False, "device": None,
    "bins": [0.0] * 8, "peak_hold": [0.0] * 8,
}

# Frozen against an actual run of render_spectrum_lines(SPECTRUM_AVAILABLE_VM,
# 24, 8) -- same "freeze from a real run" discipline as GOLDEN_VOICES_FRAME/
# GOLDEN_HARMONY_FRAME/GOLDEN_TUNER_*_FRAME/GOLDEN_PIANOROLL_*_FRAME.
GOLDEN_SPECTRUM_FRAME = [
    "SPECTRUM  (device: USB A",
    "   -█                   ",
    "    █-                  ",
    "  -██                   ",
    " -████-                 ",
    "  ████                  ",
    "-██████-                ",
    " ██████                 ",
]
GOLDEN_SPECTRUM_IDLE_FRAME = [
    "SPECTRUM  [n]ext page [q",
    "no audio input          ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
]


def test_spectrum_render_matches_frozen_golden_frame_when_available():
    out = render_spectrum_lines(SPECTRUM_AVAILABLE_VM, width=24, height=8)
    assert out == GOLDEN_SPECTRUM_FRAME
    assert all(len(line) == 24 for line in out)


def test_spectrum_render_matches_frozen_golden_frame_when_idle():
    out = render_spectrum_lines(SPECTRUM_IDLE_VM, width=24, height=8)
    assert out == GOLDEN_SPECTRUM_IDLE_FRAME
    assert all(len(line) == 24 for line in out)


def test_spectrum_render_idle_shows_no_audio_input_placeholder():
    out = render_spectrum_lines(SPECTRUM_IDLE_VM, width=40, height=5)
    assert out[1].strip() == "no audio input"
    assert all(line.strip() == "" for line in out[2:])


def test_spectrum_render_header_shows_device_when_available():
    out = render_spectrum_lines(SPECTRUM_AVAILABLE_VM, width=60, height=5)
    assert "USB Audio Device" in out[0]


def test_spectrum_render_header_falls_back_to_default_when_no_device_name():
    vm = {**SPECTRUM_AVAILABLE_VM, "device": None}
    out = render_spectrum_lines(vm, width=60, height=5)
    assert "device: default" in out[0]


def test_spectrum_columns_averages_slices_when_downsampling():
    assert _spectrum_columns([0.0, 1.0, 2.0, 3.0], 2) == [0.5, 2.5]


def test_spectrum_columns_empty_values_returns_empty():
    assert _spectrum_columns([], 4) == []


def test_spectrum_bar_rows_fills_bottom_up_proportionally():
    rows = _spectrum_bar_rows(4, 1, [1.0])
    assert rows == ["█", "█", "█", "█"]
    rows = _spectrum_bar_rows(4, 1, [0.0])
    assert rows == [" ", " ", " ", " "]


def test_spectrum_bar_rows_peak_tick_sits_above_a_lower_live_fill():
    rows = _spectrum_bar_rows(4, 1, [0.25], peaks=[1.0])
    column = "".join(row for row in rows)
    assert column[0] == "-"   # peak at the very top row
    assert column[-1] == "█"  # live fill occupies the bottom row


def test_spectrum_bar_rows_peak_tick_never_overwrites_a_live_bar_cell():
    # peak == level (a bar that just hit its own peak) -- the live "█"
    # glyph wins, no separate "-" tick drawn on top of it.
    rows = _spectrum_bar_rows(4, 1, [1.0], peaks=[1.0])
    assert "".join(rows) == "████"


def test_spectrum_bar_rows_leaves_extra_columns_blank_when_wider_than_bins():
    # v1's own choice: a terminal wider than the bin count does not
    # upsample, it just leaves the extra trailing columns blank.
    rows = _spectrum_bar_rows(2, 5, [1.0, 1.0])
    assert all(row[2:] == "   " for row in rows)


def test_spectrum_bar_rows_empty_bins_is_all_blank():
    rows = _spectrum_bar_rows(3, 5, [])
    assert rows == [" " * 5] * 3


def test_spectrum_renderers_dispatch_table_has_spectrum():
    assert RENDERERS["spectrum"] is render_spectrum_lines


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# A tiny hand-picked 2x2 grid (decoupled from the real analyzer's wave math
# -- same "renderer unit-tested against a directly-constructed VM" style as
# the spectrum tests above) exercises the nearest-neighbor upsample from a
# small fixed grid onto a wider/taller terminal body -- see
# analyzers/img2txtviz.py's module docstring for why the grid is fixed-size
# independent of any client's raster.
IMG2TXTVIZ_VM = {
    "title": "IMG2TXT", "active_notes": 3, "energy": 1.23, "vel_splash": 0.45,
    "invert": False, "charset": " .:#",
    "grid": [[0.0, 1.0], [0.25, 0.75]],
}


def test_img2txtviz_grid_lines_nearest_neighbor_upsamples_correctly():
    # width=4 doubles each of the 2 grid columns; body_h=2 maps 1:1 to the
    # 2 grid rows. charset " .:#" -> index 0=' ', 1='.', 2=':', 3='#'.
    # Row 0 (0.0, 1.0) -> idx 0, 0, 3, 3 -> "  ##"
    # Row 1 (0.25, 0.75) -> idx 1, 1, 3, 3 -> "..##" (0.75*4 == 3.0 exactly)
    lines = _img2txtviz_grid_lines(IMG2TXTVIZ_VM, width=4, body_h=2)
    assert lines == ["  ##", "..##"]


def test_img2txtviz_grid_lines_empty_grid_is_all_blank():
    vm = {**IMG2TXTVIZ_VM, "grid": []}
    lines = _img2txtviz_grid_lines(vm, width=5, body_h=3)
    assert lines == ["     "] * 3


def test_render_img2txtviz_lines_header_shows_notes_and_energy():
    out = render_img2txtviz_lines(IMG2TXTVIZ_VM, width=60, height=5)
    assert "notes:03" in out[0]
    assert "energy:1.23" in out[0]
    assert "splash:0.45" in out[0]


def test_render_img2txtviz_lines_header_shows_invert_flag_only_when_set():
    assert "INV" not in render_img2txtviz_lines(IMG2TXTVIZ_VM, width=60, height=5)[0]
    inverted = {**IMG2TXTVIZ_VM, "invert": True}
    assert "INV" in render_img2txtviz_lines(inverted, width=60, height=5)[0]


def test_render_img2txtviz_lines_pads_to_exact_dimensions():
    out = render_img2txtviz_lines(IMG2TXTVIZ_VM, width=10, height=6)
    assert len(out) == 6
    assert all(len(ln) == 10 for ln in out)


def test_img2txtviz_renderers_dispatch_table_has_img2txtviz():
    assert RENDERERS["img2txtviz"] is render_img2txtviz_lines


# -- config page (phase-3 task 10) -------------------------------------------
#
# Plain "label: value" row dump -- see pages/configview.py's module
# docstring for why this is a fixed flat list rather than v1's recursive
# JSON tree/editor.
CONFIG_VM = {
    "title": "CONFIG",
    "config_rows": [
        {"label": "tick_hz", "value": "30"},
        {"label": "pages", "value": "eventlog, voices"},
    ],
    "engine_rows": [
        {"label": "engine_version", "value": "2.0.0.dev0"},
        {"label": "current_page", "value": "eventlog"},
    ],
}


def test_config_body_lines_lists_config_then_engine_sections():
    lines = _config_body_lines(CONFIG_VM)
    assert lines[0] == "-- Config --"
    assert "tick_hz: 30" in lines
    assert "pages: eventlog, voices" in lines
    assert "-- Engine --" in lines
    assert "engine_version: 2.0.0.dev0" in lines
    assert "current_page: eventlog" in lines


def test_render_config_lines_pads_to_exact_dimensions():
    out = render_config_lines(CONFIG_VM, width=40, height=12)
    assert len(out) == 12
    assert all(len(ln) == 40 for ln in out)


def test_render_config_lines_cuts_off_extra_rows_when_height_is_short():
    out = render_config_lines(CONFIG_VM, width=40, height=2)
    assert len(out) == 2   # header + exactly 1 body row


def test_config_renderers_dispatch_table_has_config():
    assert RENDERERS["config"] is render_config_lines
