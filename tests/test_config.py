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
