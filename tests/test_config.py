from midicrt.config import Config, load


def test_defaults_when_missing(tmp_path):
    cfg = load(str(tmp_path / "nope.toml"))
    assert cfg == Config()
    assert cfg.socket_path == "/run/midicrt/ctl.sock"
    assert cfg.midi_sources == ["*"]


def test_load_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('socket_path = "/tmp/x.sock"\nmidi_sources = ["NetMIDI*", "USB*"]\n'
                 'tick_hz = 15.0\nunknown_key = 1\n')
    cfg = load(str(p))
    assert cfg.socket_path == "/tmp/x.sock"
    assert cfg.midi_sources == ["NetMIDI*", "USB*"]
    assert cfg.tick_hz == 15.0
    assert cfg.eventlog_capacity == 200


# -- capture (Phase 5 Task 1, docs/phase5-notes.md) --------------------------

def test_capture_defaults_match_v1s_deployed_manual_start_only_behavior():
    cfg = Config()
    assert cfg.capture_dir is None
    assert cfg.capture_retention == 50
    assert cfg.capture_auto_start is False


def test_load_overrides_capture_settings(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('capture_dir = "/tmp/sessions"\ncapture_retention = 10\n'
                 'capture_auto_start = true\n')
    cfg = load(str(p))
    assert cfg.capture_dir == "/tmp/sessions"
    assert cfg.capture_retention == 10
    assert cfg.capture_auto_start is True
