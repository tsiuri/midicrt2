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


# -- panic-send / stuck-linger / poly-limit log (Phase 9 Task 2) ------------
#
# v1 evidence: ~/codex/midicrt/plugins/zstucknotes.py:23 `PANIC_ON_CRIT =
# True` (v1's own shipped default -- v2 deliberately posture-changes to
# OFF, a 2026-08-08 decision, not a restoration); line 20 `HOLD_AFTER =
# 15.0`; ~/codex/midicrt/plugins/zvoicemonitor.py:11-12
# `POLY_LIMIT_GLOBAL = 16` / `POLY_LIMIT_CH = 8`.

def test_panic_on_crit_defaults_false_a_deliberate_posture_change_from_v1():
    cfg = Config()
    assert cfg.panic_on_crit is False


def test_stuck_hold_after_defaults_to_v1s_hold_after_constant():
    cfg = Config()
    assert cfg.stuck_hold_after == 15.0


def test_poly_limit_defaults_match_v1s_shipped_constants():
    cfg = Config()
    assert cfg.poly_limit_global == 16
    assert cfg.poly_limit_ch == 8


def test_load_overrides_panic_linger_polylimit_settings(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "panic_on_crit = true\nstuck_hold_after = 5.0\n"
        "poly_limit_global = 4\npoly_limit_ch = 2\n"
    )
    cfg = load(str(p))
    assert cfg.panic_on_crit is True
    assert cfg.stuck_hold_after == 5.0
    assert cfg.poly_limit_global == 4
    assert cfg.poly_limit_ch == 2


# -- SysEx manager (Phase 9 Task 5) -----------------------------------------

def test_sysex_dir_defaults_to_none_letting_resolve_sysex_dir_decide():
    cfg = Config()
    assert cfg.sysex_dir is None


def test_load_overrides_sysex_dir(tmp_path):
    p = tmp_path / "config.toml"
    override = str(tmp_path / "my-sysex-lib")
    p.write_text(f'sysex_dir = "{override}"\n')
    cfg = load(str(p))
    assert cfg.sysex_dir == override
