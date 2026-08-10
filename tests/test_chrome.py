from midicrt.analyzers.marquee import PAGE_IDS, PAGE_TITLES
from midicrt.clients.chrome import (
    DEFAULT_ALERTS_VM,
    DEFAULT_BEATFLASH_VM,
    DEFAULT_LOOPPROGRESS_VM,
    DEFAULT_MARQUEE_VM,
    DEFAULT_STATUS_VM,
    DEFAULT_TIMESIG_VM,
    LOOPPROGRESS_BAR_WIDTH,
    OVERLAY_ALERTS_TOPIC,
    OVERLAY_BEATFLASH_TOPIC,
    OVERLAY_LOOPPROGRESS_TOPIC,
    OVERLAY_MARQUEE_TOPIC,
    OVERLAY_STATUS_TOPIC,
    OVERLAY_TIMESIG_TOPIC,
    alerts_text,
    beatflash_glyph,
    beatprogress_row_text,
    format_bpm,
    header_with_hint,
    loopprogress_bar,
    marquee_window_text,
    overlay_lines,
    page_keymap_hint_text,
    page_keymap_hint_window_text,
    scroll_window,
    secondary_status_text,
    status_text,
    timesig_text,
)
from midicrt.config import Config
from midicrt.engine.keymap import DEFAULT_KEYMAP


def test_overlay_status_topic_matches_engine_convention():
    assert OVERLAY_STATUS_TOPIC == "overlay.status"


def test_default_status_vm_matches_analyzer_initial_view_model():
    from midicrt.analyzers.transport import TransportAnalyzer

    assert DEFAULT_STATUS_VM == TransportAnalyzer().view_model()


def test_format_bpm_none_is_em_dash():
    assert format_bpm(None) == "—"


def test_format_bpm_rounds_to_one_decimal():
    assert format_bpm(120.0) == "120.0"
    assert format_bpm(89.9999) == "90.0"
    assert format_bpm(63.049) == "63.0"


def test_status_text_shows_bar_beat_bpm_state_and_source():
    vm = {"bpm": 120.4, "bar": 3, "beat": 2, "running": True, "source": "USB MIDI"}
    text = status_text(vm)
    assert "BAR 0003" in text
    assert "BEAT 02" in text
    assert "120.4 BPM" in text
    assert "RUN" in text
    assert "USB MIDI" in text


def test_status_text_shows_stop_and_dash_bpm_when_never_started():
    text = status_text(DEFAULT_STATUS_VM)
    assert "BAR 0000" in text
    assert "BEAT 01" in text
    assert "— BPM" in text
    assert "STOP" in text
    assert "no clock" in text


def test_status_text_is_a_single_line():
    assert "\n" not in status_text(DEFAULT_STATUS_VM)


# -- rec flag / chrome indicator (Phase 5 Task 1, docs/phase5-notes.md) ------

def test_default_status_vm_defaults_rec_false():
    assert DEFAULT_STATUS_VM["rec"] is False


def test_status_text_shows_no_rec_marker_when_not_recording():
    text = status_text(DEFAULT_STATUS_VM)
    assert "REC" not in text


def test_status_text_shows_rec_marker_when_recording():
    vm = dict(DEFAULT_STATUS_VM, rec=True)
    text = status_text(vm)
    assert "REC" in text


def test_status_text_rec_marker_does_not_change_line_count():
    vm = dict(DEFAULT_STATUS_VM, rec=True)
    assert "\n" not in status_text(vm)


def test_status_text_omits_rec_marker_when_vm_has_no_rec_key_at_all():
    # Backward-compat: an older vm dict shape (no "rec" key) must render
    # IDENTICALLY to before this task -- `.get("rec")` on a missing key is
    # falsy, so this is the same code path as "rec": False, not a KeyError.
    vm = {"bpm": 120.0, "bar": 1, "beat": 2, "running": True, "source": "USB MIDI"}
    assert "REC" not in status_text(vm)


# -- stuck-notes alerts + time-signature (phase-3 task 6) --------------------

def test_overlay_alerts_and_timesig_topics_match_engine_convention():
    assert OVERLAY_ALERTS_TOPIC == "overlay.alerts"
    assert OVERLAY_TIMESIG_TOPIC == "overlay.timesig"


def test_default_alerts_and_timesig_vms_match_analyzer_initial_view_models():
    from midicrt.analyzers.stucknotes import StuckNotesAnalyzer
    from midicrt.analyzers.timesig import TimesigAnalyzer

    assert DEFAULT_ALERTS_VM == StuckNotesAnalyzer().view_model()
    assert DEFAULT_TIMESIG_VM == TimesigAnalyzer().view_model()


def test_alerts_text_is_blank_when_no_alerts():
    assert alerts_text(DEFAULT_ALERTS_VM) == ""


def test_alerts_text_shows_channel_note_and_held_seconds():
    vm = {"alerts": [{"ch": 3, "note": 60, "level": "warn", "held_s": 2.3}]}
    text = alerts_text(vm)
    assert text.startswith("STUCK WARN:")
    assert "CH03" in text
    assert "n060" in text
    assert "2.3s" in text


def test_alerts_text_shows_crit_when_any_alert_is_critical():
    vm = {"alerts": [
        {"ch": 1, "note": 60, "level": "warn", "held_s": 2.1},
        {"ch": 2, "note": 64, "level": "crit", "held_s": 11.0},
    ]}
    assert alerts_text(vm).startswith("STUCK CRIT:")


def test_alerts_text_truncates_to_max_list_with_a_more_suffix():
    vm = {"alerts": [{"ch": i, "note": 60, "level": "warn", "held_s": float(i)} for i in range(1, 6)]}
    text = alerts_text(vm)
    assert text.count("CH0") == 3   # MAX_ALERT_LIST
    assert "+2 more" in text


def test_timesig_text_shows_no_lock_when_no_labels():
    assert timesig_text(DEFAULT_TIMESIG_VM) == "Time Signature: (no lock)"


def test_timesig_text_shows_single_label_conf_and_events():
    vm = {"labels": ["4/4"], "confidence": 0.85, "events": 24,
          "events_window": 24, "events_total": 40, "pending": None}
    text = timesig_text(vm)
    assert text == "Time Signature: 4/4  conf:0.85  events:24/40"


def test_timesig_text_joins_tied_labels_with_slash():
    vm = {"labels": ["3/4", "6/8"], "confidence": 0.5, "events": 12,
          "events_window": 12, "events_total": 12, "pending": None}
    assert "3/4 / 6/8" in timesig_text(vm)


def test_timesig_text_shows_pending_change():
    vm = {"labels": ["4/4"], "confidence": 0.6, "events": 20,
          "events_window": 20, "events_total": 20, "pending": ["3/4"]}
    text = timesig_text(vm)
    assert text.endswith("-> 3/4")


def test_secondary_status_text_prefers_alerts_over_timesig():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}]}
    timesig_vm = {"labels": ["4/4"], "confidence": 0.9, "events": 20,
                  "events_window": 20, "events_total": 20, "pending": None}
    text = secondary_status_text(alerts_vm, timesig_vm)
    assert text.startswith("STUCK WARN:")


def test_secondary_status_text_falls_back_to_timesig_when_no_alerts():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM)
    assert text == "Time Signature: (no lock)"


# -- stuck-linger (Phase 9 Task 2, config.stuck_hold_after) -------------------

def test_alerts_text_shows_stuck_cleared_message_when_lingering():
    vm = {"alerts": [], "cleared": [{"ch": 3, "note": 60, "level": "crit", "held_s": 11.2}]}
    text = alerts_text(vm)
    assert text.startswith("STUCK CLEARED:")
    assert "CH03" in text
    assert "n060" in text
    assert "11.2s" in text


def test_alerts_text_truncates_cleared_list_with_a_more_suffix():
    vm = {"alerts": [],
          "cleared": [{"ch": i, "note": 60, "level": "warn", "held_s": float(i)} for i in range(1, 6)]}
    text = alerts_text(vm)
    assert text.count("CH0") == 3   # MAX_ALERT_LIST
    assert "+2 more" in text


def test_alerts_text_prefers_active_alerts_over_a_lingering_cleared_message():
    vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}],
          "cleared": [{"ch": 2, "note": 64, "level": "crit", "held_s": 9.0}]}
    text = alerts_text(vm)
    assert text.startswith("STUCK WARN:")
    assert "CH02" not in text


def test_alerts_text_is_blank_when_neither_alerts_nor_cleared_are_present():
    assert alerts_text({"alerts": [], "cleared": []}) == ""


def test_secondary_status_text_shows_cleared_message_over_timesig():
    vm = {"alerts": [], "cleared": [{"ch": 3, "note": 60, "level": "warn", "held_s": 2.0}]}
    text = secondary_status_text(vm, DEFAULT_TIMESIG_VM)
    assert text.startswith("STUCK CLEARED:")


def test_secondary_status_dim_is_true_only_while_lingering():
    from midicrt.clients.chrome import secondary_status_dim

    assert secondary_status_dim(DEFAULT_ALERTS_VM) is False
    active_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}], "cleared": []}
    assert secondary_status_dim(active_vm) is False
    cleared_vm = {"alerts": [], "cleared": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}]}
    assert secondary_status_dim(cleared_vm) is True


def test_secondary_status_dim_is_false_when_a_fresh_alert_wins_over_lingering_cleared():
    from midicrt.clients.chrome import secondary_status_dim

    vm = {"alerts": [{"ch": 1, "note": 60, "level": "crit", "held_s": 1.0}],
          "cleared": [{"ch": 2, "note": 64, "level": "warn", "held_s": 9.0}]}
    assert secondary_status_dim(vm) is False


# -- poly-limit chrome flash (Phase 9 Task 2, config.poly_limit_global/
# poly_limit_ch) -- v2-native addition, v1's zvoicemonitor.py has no chrome
# home of its own at all (analyzers/voices.py's own module docstring).
# Shares the SAME urgent second chrome row as stuck alerts/linger, one tier
# below them: stuck alerts/cleared (most urgent) > poly-limit flash > the
# routine time-signature line (least urgent, shown most often) --------------

def test_overlay_polylimit_topic_matches_engine_convention():
    from midicrt.clients.chrome import OVERLAY_POLYLIMIT_TOPIC

    assert OVERLAY_POLYLIMIT_TOPIC == "overlay.polylimit"


def test_default_polylimit_vm_matches_analyzer_initial_flash_view_model():
    from midicrt.analyzers.voices import VoiceMonitorAnalyzer
    from midicrt.clients.chrome import DEFAULT_POLYLIMIT_VM

    assert DEFAULT_POLYLIMIT_VM == VoiceMonitorAnalyzer().flash_view_model()


def test_secondary_status_text_shows_polylimit_flash_when_no_alerts():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, {"flashing": True})
    assert text == "POLY LIMIT EXCEEDED"


def test_secondary_status_text_falls_back_to_timesig_when_not_flashing():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, {"flashing": False})
    assert text == "Time Signature: (no lock)"


def test_secondary_status_text_polylimit_param_is_optional_backward_compatible():
    # Every pre-existing call site (2-arg) must behave identically -- the
    # 3rd param defaults to "not flashing".
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM)
    assert text == "Time Signature: (no lock)"


def test_secondary_status_text_stuck_alerts_win_over_polylimit_flash():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}], "cleared": []}
    text = secondary_status_text(alerts_vm, DEFAULT_TIMESIG_VM, {"flashing": True})
    assert text.startswith("STUCK WARN:")


def test_secondary_status_text_lingering_cleared_message_wins_over_polylimit_flash():
    cleared_vm = {"alerts": [], "cleared": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}]}
    text = secondary_status_text(cleared_vm, DEFAULT_TIMESIG_VM, {"flashing": True})
    assert text.startswith("STUCK CLEARED:")


def test_secondary_status_dim_is_false_while_the_polylimit_flash_is_showing():
    from midicrt.clients.chrome import secondary_status_dim

    # The flash is urgent (full brightness), not the dimmed linger state --
    # secondary_status_dim only ever looks at alerts_vm, unaffected by the
    # (separate-topic) polylimit flash.
    assert secondary_status_dim(DEFAULT_ALERTS_VM) is False


# -- sysex-status text (Phase 9 Task 5, SysEx manager): v1 parity item --
# `plugins/loopprogress.py`'s own left-of-bar sysex status string, ported
# now that `engine/sysex_store.py` finally gives it a v2 data source. Joins
# the SAME shared second chrome row, one tier BELOW alerts/poly-limit
# (both mean "something urgent right now") and ABOVE the routine timesig
# line -- see secondary_status_text's own docstring for the full,
# disclosed row-placement writeup (this is a v2-native row-SHARING
# decision, not a literal replay of v1's own row layout). ------------------

def test_overlay_sysex_topic_matches_engine_convention():
    from midicrt.clients.chrome import OVERLAY_SYSEX_TOPIC

    assert OVERLAY_SYSEX_TOPIC == "overlay.sysex"


def test_default_sysex_vm_matches_analyzer_initial_view_model():
    from midicrt.clients.chrome import DEFAULT_SYSEX_VM
    from midicrt.engine.core import _SysexStatusOverlay
    from midicrt.engine.sysex_store import SysexStore

    assert DEFAULT_SYSEX_VM == _SysexStatusOverlay(SysexStore(library_dir="/nonexistent")).view_model()


def test_secondary_status_text_shows_sysex_status_when_active_and_no_alerts_or_polylimit():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, None,
                                 {"text": "sx: rx 4B Roland", "active": True})
    assert text == "sx: rx 4B Roland"


def test_secondary_status_text_falls_back_to_timesig_when_sysex_inactive():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, None,
                                 {"text": "sx: rx 4B Roland", "active": False})
    assert text == "Time Signature: (no lock)"


def test_secondary_status_text_sysex_param_is_optional_backward_compatible():
    # Every pre-existing 2- and 3-arg call site keeps working unchanged --
    # the 4th param defaults to "not active".
    assert secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM) == "Time Signature: (no lock)"
    assert secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM,
                                 {"flashing": False}) == "Time Signature: (no lock)"


def test_secondary_status_text_stuck_alerts_win_over_sysex_status():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 3.0}], "cleared": []}
    text = secondary_status_text(alerts_vm, DEFAULT_TIMESIG_VM, None,
                                 {"text": "sx: rx 4B", "active": True})
    assert text.startswith("STUCK WARN:")


def test_secondary_status_text_polylimit_flash_wins_over_sysex_status():
    text = secondary_status_text(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, {"flashing": True},
                                 {"text": "sx: rx 4B", "active": True})
    assert text == "POLY LIMIT EXCEEDED"


def test_secondary_status_text_sysex_status_wins_over_timesig():
    timesig_vm = {"labels": ["4/4"], "confidence": 0.9, "events": 10,
                  "events_window": 10, "events_total": 10, "pending": None}
    text = secondary_status_text(DEFAULT_ALERTS_VM, timesig_vm, None,
                                 {"text": "sx: play 'x' (4B)", "active": True})
    assert text == "sx: play 'x' (4B)"


# -- beatflash + loopprogress (phase-3 task 9) --------------------------------

def test_overlay_beatflash_and_loopprogress_topics_match_engine_convention():
    assert OVERLAY_BEATFLASH_TOPIC == "overlay.beatflash"
    assert OVERLAY_LOOPPROGRESS_TOPIC == "overlay.loopprogress"


def test_default_beatflash_and_loopprogress_vms_match_analyzer_initial_view_models():
    from midicrt.analyzers.beatflash import BeatFlashAnalyzer
    from midicrt.analyzers.loopprogress import LoopProgressAnalyzer

    assert DEFAULT_BEATFLASH_VM == BeatFlashAnalyzer().view_model()
    assert DEFAULT_LOOPPROGRESS_VM == LoopProgressAnalyzer().view_model()


def test_beatflash_glyph_is_blank_when_intensity_zero():
    assert beatflash_glyph(DEFAULT_BEATFLASH_VM) == "  "


def test_beatflash_glyph_ramps_through_shades_as_intensity_rises():
    assert beatflash_glyph({"intensity": 0.1, "is_bar": False}) == "░░"
    assert beatflash_glyph({"intensity": 0.5, "is_bar": False}) == "▒▒"
    assert beatflash_glyph({"intensity": 0.9, "is_bar": False}) == "▓▓"


def test_beatflash_glyph_reaches_full_block_only_above_beat_peak():
    # A regular beat (BEAT_PEAK == 1.0) must never reach the solid block --
    # that top level is reserved for a bar flash's higher peak (task
    # brief: "stronger on bar"). See analyzers/beatflash.py's BEAT_PEAK/
    # BAR_PEAK constants.
    assert beatflash_glyph({"intensity": 1.0, "is_bar": False}) == "▓▓"
    assert beatflash_glyph({"intensity": 1.4, "is_bar": True}) == "██"


def test_loopprogress_bar_is_blank_when_not_running():
    assert loopprogress_bar(DEFAULT_LOOPPROGRESS_VM) == "[" + " " * LOOPPROGRESS_BAR_WIDTH + "]"


def test_loopprogress_bar_places_asterisk_at_fraction_position():
    bar = loopprogress_bar({"fraction": 0.5, "running": True})
    assert bar == "[    *   ]"


def test_loopprogress_bar_clamps_position_within_width_near_full_wrap():
    bar = loopprogress_bar({"fraction": 0.999, "running": True})
    assert bar.count("*") == 1
    assert len(bar) == LOOPPROGRESS_BAR_WIDTH + 2


def test_beatprogress_row_text_is_exactly_width_wide():
    row = beatprogress_row_text(DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, width=40)
    assert len(row) == 40
    row = beatprogress_row_text(DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, width=6)
    assert len(row) == 6


def test_beatprogress_row_text_places_glyph_at_left_and_bar_centered():
    beatflash_vm = {"intensity": 1.4, "is_bar": True}
    loopprogress_vm = {"fraction": 0.5, "running": True}
    row = beatprogress_row_text(beatflash_vm, loopprogress_vm, width=40)
    assert row.startswith("██")
    bar = loopprogress_bar(loopprogress_vm)
    expected_start = (40 - len(bar)) // 2
    assert row[expected_start:expected_start + len(bar)] == bar


# -- header marquee (Phase 8 Task 4, docs/visual-audit.md §20b) --------------

def test_overlay_marquee_topic_matches_engine_convention():
    assert OVERLAY_MARQUEE_TOPIC == "overlay.marquee"


def test_default_marquee_vm_matches_analyzer_initial_view_model():
    from midicrt.analyzers.marquee import MarqueeAnalyzer

    assert DEFAULT_MARQUEE_VM == MarqueeAnalyzer([]).view_model()


def test_scroll_window_shows_text_static_when_it_fits_width():
    assert scroll_window("[0:HELP]", "unused-should-never-be-read", 0, width=40) == "[0:HELP]"


def test_scroll_window_returns_empty_for_non_positive_width():
    assert scroll_window("[0:HELP]", "[0:HELP]    [0:HELP]    ", 0, width=0) == ""
    assert scroll_window("[0:HELP]", "[0:HELP]    [0:HELP]    ", 0, width=-3) == ""


def test_scroll_window_slices_doubled_string_when_wider_than_width():
    text = "[0:HELP]  [1:HARMONY]"
    gap = "    "
    doubled = (text + gap) * 2
    # width smaller than text -> scrolling branch; offset 3 -> slice starts 3 in.
    window = scroll_window(text, doubled, offset=3, width=10)
    assert window == doubled[3:13]
    assert len(window) == 10


def test_scroll_window_never_runs_past_end_of_a_correctly_built_doubled_string():
    # Exercise the boundary case the docstring's proof covers: offset at its
    # maximum possible value (modulo - 1) with width just under len(text).
    text = "[0:HELP]"
    gap = "    "
    modulo = len(text) + len(gap)
    doubled = (text + gap) * 2
    width = len(text) - 1
    window = scroll_window(text, doubled, offset=modulo - 1, width=width)
    assert len(window) == width   # never short -- proves no off-the-end truncation


def test_marquee_window_text_reads_text_doubled_and_offset_from_vm():
    vm = {"text": "[0:HELP]  [1:HARMONY]", "doubled": None, "offset": 0}
    # text fits a wide-enough width -- static branch, doubled/offset unused.
    assert marquee_window_text(vm, width=100) == vm["text"]


def test_marquee_window_text_scrolls_when_narrower_than_content():
    text = "[0:HELP]  [1:HARMONY]  [2:SEND NOTES]"
    gap = "    "
    doubled = (text + gap) * 2
    vm = {"text": text, "doubled": doubled, "offset": 5}
    window = marquee_window_text(vm, width=12)
    assert window == doubled[5:17]
    assert len(window) == 12


def test_marquee_window_text_default_vm_is_blank_at_any_width():
    assert marquee_window_text(DEFAULT_MARQUEE_VM, width=40) == ""
    assert marquee_window_text(DEFAULT_MARQUEE_VM, width=0) == ""


# -- on-screen keymap indicator (Phase 8 Task 6) -----------------------------

def test_page_keymap_hint_text_empty_section_is_blank():
    assert page_keymap_hint_text({}) == ""


def test_page_keymap_hint_text_sorts_by_key_and_strips_action_namespace():
    section = {"p": "pianoroll.projection_toggle", "d": {"action": "pianoroll.channel_toggle",
                                                          "args": {"channel": 10}}}
    assert page_keymap_hint_text(section) == "d:channel_toggle  p:projection_toggle"


def test_page_keymap_hint_text_omits_args_from_the_label():
    section = {"1": {"action": "page.jump", "args": {"position": 1}}}
    assert page_keymap_hint_text(section) == "1:jump"


def test_header_with_hint_empty_hint_returns_marquee_slice_unchanged():
    assert header_with_hint("SOME TITLE", "", width=40) == "SOME TITLE"


def test_header_with_hint_zero_width_returns_marquee_slice_unchanged():
    assert header_with_hint("SOME TITLE", "d:ch10", width=0) == "SOME TITLE"


def test_header_with_hint_right_aligns_within_the_full_width():
    result = header_with_hint("TITLE", "d:ch10", width=20)
    assert len(result) == 20
    assert result.endswith("d:ch10")
    assert result.startswith("TITLE")


def test_header_with_hint_truncates_an_oversized_hint_to_fit_width():
    result = header_with_hint("T", "way-too-long-to-fit-here", width=10)
    assert len(result) == 10


def test_page_keymap_hint_window_text_empty_section_is_blank():
    assert page_keymap_hint_window_text({}, offset=0, width=40) == ""


def test_page_keymap_hint_window_text_static_when_it_fits():
    section = {"i": "img2txtviz.invert"}
    assert page_keymap_hint_window_text(section, offset=99, width=40) == "i:invert"


def test_page_keymap_hint_window_text_scrolls_when_narrower_than_content():
    # Same shared `scroll_window` mechanic the marquee uses -- a too-long
    # hint list is a MOVER (burn-in rule), not a second static string.
    section = {"1": "a.one", "2": "b.two", "3": "c.three", "4": "d.four"}
    text = page_keymap_hint_text(section)
    window_0 = page_keymap_hint_window_text(section, offset=0, width=10)
    window_5 = page_keymap_hint_window_text(section, offset=5, width=10)
    assert len(text) > 10   # sanity: scroll IS engaged at this width
    assert len(window_0) == 10 and len(window_5) == 10
    assert window_0 != window_5


# -- help overlay content (Phase 8 Task 6) -----------------------------------

def test_overlay_lines_global_only_when_page_section_is_empty():
    lines = overlay_lines({"q": "client.quit", "n": "page.next"}, {}, "eventlog")
    assert lines == ["GLOBAL", "  n  page.next", "  q  client.quit"]


def test_overlay_lines_includes_page_section_when_present():
    lines = overlay_lines(
        {"q": "client.quit"}, {"p": "pianoroll.projection_toggle"}, "pianoroll")
    assert lines == [
        "GLOBAL", "  q  client.quit", "", "PIANOROLL", "  p  pianoroll.projection_toggle",
    ]


def test_overlay_lines_both_empty_is_an_empty_list():
    assert overlay_lines({}, {}, "eventlog") == []


def test_overlay_lines_shows_full_action_names_not_abbreviated():
    # Unlike the compact indicator (page_keymap_hint_text strips the
    # namespace) -- the overlay is the lookup surface, full names only.
    lines = overlay_lines({}, {"1": {"action": "page.jump", "args": {"position": 1}}}, "eventlog")
    assert lines == ["EVENTLOG", "  1  page.jump"]


def test_overlay_lines_resolves_page_jump_to_the_target_page_name_when_roster_given():
    # Live-verification finding (task-6-report.md's self-review): without
    # a roster, all 20 jump keys show the uninformative literal
    # "page.jump" -- with one, each resolves to the actual page it jumps
    # TO, which is what a human actually wants to know.
    roster = ["eventlog", "voices", "harmony"]
    lines = overlay_lines({"2": {"action": "page.jump", "args": {"position": 2}}}, {},
                          "eventlog", roster)
    assert lines == ["GLOBAL", "  2  -> voices"]


def test_overlay_lines_page_jump_out_of_range_position_shows_unassigned_label():
    roster = ["eventlog"]
    lines = overlay_lines({"5": {"action": "page.jump", "args": {"position": 5}}}, {},
                          "eventlog", roster)
    assert lines == ["GLOBAL", "  5  5 (unassigned)"]


def test_overlay_lines_non_jump_entries_unaffected_by_roster():
    roster = ["eventlog", "voices"]
    lines = overlay_lines({"q": "client.quit"}, {}, "eventlog", roster)
    assert lines == ["GLOBAL", "  q  client.quit"]


# -- page.goto v1-ID labels (Phase 9 Task 0, docs/gui-phase-decisions-      --
# -- 2026-08-08.md digit-nav reconciliation) ---------------------------------

def test_overlay_lines_resolves_page_goto_to_its_v1_id_and_title():
    # "8 -> 8:PIANOROLL" -- matches the marquee's OWN [8:PIANOROLL] text
    # for the same page (analyzers/marquee.py::PAGE_TITLES), the whole
    # point of mapping digits to v1 IDs.
    lines = overlay_lines({"8": {"action": "page.goto", "args": {"name": "pianoroll"}}}, {},
                          "eventlog")
    assert lines == ["GLOBAL", "  8  -> 8:PIANOROLL"]


def test_overlay_lines_page_goto_v1_id_label_does_not_need_a_roster():
    # Unlike page.jump, the v1 ID comes from the STATIC marquee.PAGE_IDS
    # table, not the live cycle order -- resolves identically with or
    # without a roster argument.
    entry = {"1": {"action": "page.goto", "args": {"name": "harmony"}}}
    assert overlay_lines(entry, {}, "eventlog") == overlay_lines(entry, {}, "eventlog", [])
    assert overlay_lines(entry, {}, "eventlog") == ["GLOBAL", "  1  -> 1:HARMONY"]


def test_overlay_lines_shows_v1_id_label_even_for_a_page_absent_from_the_roster():
    # "tuner" (v1 ID 10) isn't in config.py's default roster, but its
    # DEFAULT_KEYMAP binding is still PRESENT (engine/keymap.py) -- the
    # overlay shows what it WOULD do, informatively, even though
    # dispatching it is a graceful no-op on this build
    # (engine/core.py::Engine._page_goto).
    lines = overlay_lines({")": {"action": "page.goto", "args": {"name": "tuner"}}}, {},
                          "eventlog")
    assert lines == ["GLOBAL", "  )  -> 10:TUNER"]


def test_overlay_lines_page_goto_without_a_v1_id_falls_back_to_bare_name():
    lines = overlay_lines({"v": {"action": "page.goto", "args": {"name": "screensaver"}}}, {},
                          "eventlog")
    assert lines == ["GLOBAL", "  v  -> screensaver"]


def test_overlay_lines_v1_id_digit_labels_match_marquee_titles_for_every_default_roster_page():
    # Consistency test (task brief): for every marquee.PAGE_IDS entry
    # whose page is in the default roster, DEFAULT_KEYMAP's own digit
    # binding overlay label reads the SAME "id:TITLE" text the marquee
    # itself shows for that page.
    roster = Config().pages
    lines = overlay_lines(DEFAULT_KEYMAP, {}, "eventlog")
    rendered = "\n".join(lines)
    for name, page_id in PAGE_IDS.items():
        if name in roster:
            assert f"-> {page_id}:{PAGE_TITLES[name]}" in rendered
