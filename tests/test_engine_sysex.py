"""TDD for engine/sysex.py -- the pure SysEx parse/reply-build half of v1's
`plugins/sysex.py` (Cirklon remote-control receiver). See that module's own
docstring for the full v1 behavioral synthesis and the format-byte
evidence from `sysex.d/` captures on the Pi.
"""
from midicrt.engine import sysex


def test_non_midicrt_prefix_is_a_true_no_op():
    # A real, captured non-midicrt SysEx frame from sysex.d/ (Cirklon's own
    # traffic, logged alongside midicrt's own commands but not addressed
    # to it) -- must return None, not be mistaken for anything.
    assert sysex.parse_command((0x7F, 0x7F, 0x01, 0x01, 0x00)) is None


def test_too_short_to_contain_even_the_prefix_is_a_true_no_op():
    assert sysex.parse_command(()) is None
    assert sysex.parse_command((0x7D, 0x6D)) is None


def test_prefix_with_no_command_byte_at_all_is_a_true_no_op():
    # PREFIX alone, no marker byte -- len(data) < n+1.
    assert sysex.parse_command((0x7D, 0x6D, 0x63)) is None


# -- legacy frames (real captures from sysex.d/ on the Pi) -------------------

def test_legacy_switch_page_0_real_capture():
    # 20260218-201812-000001-rx.syx: F0 7D 6D 63 01 00 F7
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x01, 0x00))
    assert parsed == {"version": None, "cmd": 0x01, "args": (0x00,), "error": None}


def test_legacy_switch_page_8_real_capture():
    # 20260219-214025-000001-rx.syx: F0 7D 6D 63 01 08 F7
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x01, 0x08))
    assert parsed == {"version": None, "cmd": 0x01, "args": (0x08,), "error": None}


def test_legacy_page_cycle_enable_real_capture():
    # 20260218-201812-000002-rx.syx: F0 7D 6D 63 03 01 F7
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x03, 0x01))
    assert parsed == {"version": None, "cmd": 0x03, "args": (0x01,), "error": None}


def test_legacy_page_cycle_disable_real_capture():
    # 20260219-214217-000002-rx.syx: F0 7D 6D 63 03 00 F7
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x03, 0x00))
    assert parsed == {"version": None, "cmd": 0x03, "args": (0x00,), "error": None}


def test_legacy_frame_with_no_args_at_all():
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x10))   # capabilities, legacy
    assert parsed == {"version": None, "cmd": 0x10, "args": (), "error": None}


# -- versioned frames ---------------------------------------------------------

def test_versioned_v1_switch_page_8():
    # F0 7D 6D 63 41 01 08 F7 (README's own documented example)
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x41, 0x01, 0x08))
    assert parsed == {"version": 1, "cmd": 0x01, "args": (0x08,), "error": None}


def test_versioned_negotiate_byte_resolves_to_highest_supported():
    # F0 7D 6D 63 40 10 F7 (README's own capabilities-with-negotiation example)
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x40, 0x10))
    assert parsed == {"version": max(sysex.SUPPORTED_PROTOCOL_VERSIONS),
                       "cmd": 0x10, "args": (), "error": None}


def test_versioned_frame_missing_the_command_byte():
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x41))   # marker but no cmd
    assert parsed == {"version": None, "cmd": None, "args": (), "error": "missing-cmd"}


def test_versioned_frame_with_an_unsupported_version():
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x49, 0x01, 0x08))   # version 9
    assert parsed == {"version": 9, "cmd": 0x01, "args": (0x08,), "error": "unsupported-version"}


def test_negotiate_byte_can_never_produce_unsupported_version():
    # By construction: 0x40 always resolves to max(SUPPORTED_PROTOCOL_VERSIONS).
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x40, 0x01))
    assert parsed["error"] is None


def test_versioned_frame_with_multiple_args():
    parsed = sysex.parse_command((0x7D, 0x6D, 0x63, 0x41, 0x04, 0x08, 0x10))
    assert parsed == {"version": 1, "cmd": 0x04, "args": (0x08, 0x10), "error": None}


# -- build_reply ---------------------------------------------------------------

def test_build_reply_shape_matches_v1s_documented_frame():
    # F0 7D 6D 63 <ver> <cmd> <status> [payload...] F7 -- this returns the
    # F0/F7-free payload (mido.Message("sysex", data=...) adds framing).
    frame = sysex.build_reply(1, 0x01, 0x00, payload=(0x08,))
    assert frame == (0x7D, 0x6D, 0x63, 0x41, 0x01, 0x00, 0x08)


def test_build_reply_no_payload():
    frame = sysex.build_reply(1, 0x10, 0x01)
    assert frame == (0x7D, 0x6D, 0x63, 0x41, 0x10, 0x01)


def test_build_reply_version_byte_is_version_base_plus_version():
    frame = sysex.build_reply(1, 0x02, 0x00, payload=(1,))
    assert frame[3] == sysex.VERSION_BASE + 1


# -- build_status_text (Phase 9 Task 5 review fix -- v1's _format_status,
# plugins/sysex.py:92-95, the loopprogress-status text format) --------------

def test_build_status_text_legacy_frame_says_legacy():
    assert sysex.build_status_text(None, 0x01, "ok", "page->voices") == \
        "sx:legacy cmd=0x01 ok page->voices"


def test_build_status_text_versioned_frame_shows_v_prefixed_version():
    assert sysex.build_status_text(1, 0x10, "ok", "caps-sent") == \
        "sx:v1 cmd=0x10 ok caps-sent"


def test_build_status_text_no_detail_has_no_trailing_space():
    assert sysex.build_status_text(1, 0x03, "err") == "sx:v1 cmd=0x03 err"
    assert sysex.build_status_text(None, 0x02, "err", "") == "sx:legacy cmd=0x02 err"


def test_build_status_text_cmd_byte_is_uppercase_hex_zero_padded():
    assert sysex.build_status_text(1, 0x01, "ok") == "sx:v1 cmd=0x01 ok"
    assert sysex.build_status_text(1, 0x10, "ok") == "sx:v1 cmd=0x10 ok"


def test_build_status_text_replicates_v1s_own_missing_cmd_literal_quirk():
    # v1's handle() (plugins/sysex.py:170) hardcodes version=0 (an int, NOT
    # None) for this ONE case -- a versioned marker byte with no cmd byte
    # to actually report on. Renders "v0", not "legacy" -- a faithfully
    # replicated v1 quirk, not a v2 bug.
    assert sysex.build_status_text(0, 0x00, "err", "missing-cmd") == \
        "sx:v0 cmd=0x00 err missing-cmd"
