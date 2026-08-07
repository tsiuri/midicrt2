from midicrt.clients.chrome import (
    DEFAULT_ALERTS_VM,
    DEFAULT_STATUS_VM,
    DEFAULT_TIMESIG_VM,
    OVERLAY_ALERTS_TOPIC,
    OVERLAY_STATUS_TOPIC,
    OVERLAY_TIMESIG_TOPIC,
    alerts_text,
    format_bpm,
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
