from midicrt.clients.chrome import (
    DEFAULT_STATUS_VM,
    OVERLAY_STATUS_TOPIC,
    format_bpm,
    status_text,
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
