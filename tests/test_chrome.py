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


def test_overlay_lines_page_jump_out_of_range_position_falls_back_to_bare_name():
    roster = ["eventlog"]
    lines = overlay_lines({"5": {"action": "page.jump", "args": {"position": 5}}}, {},
                          "eventlog", roster)
    assert lines == ["GLOBAL", "  5  page.jump"]


def test_overlay_lines_non_jump_entries_unaffected_by_roster():
    roster = ["eventlog", "voices"]
    lines = overlay_lines({"q": "client.quit"}, {}, "eventlog", roster)
    assert lines == ["GLOBAL", "  q  client.quit"]
