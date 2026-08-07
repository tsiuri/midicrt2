"""TDD for SendNotesPage -- see pages/sendnotes.py's own module docstring
for the full v1 (`pages/sendnotes.py`) behavioral synthesis: control keys
before note keys (with a real, faithfully-preserved KEYMAP-shadowing
quirk for ',', '.', 'g', 'h'), and a front-only FIFO expiry check.
"""
from midicrt.pages.sendnotes import SendNotesPage


def test_view_model_shape_and_defaults():
    page = SendNotesPage()
    vm = page.view_model()
    assert vm["title"] == "SEND NOTES"
    assert vm["device"] is None   # no bound provider -- idle default
    assert vm["channel"] == 1
    assert vm["octave"] == 4
    assert vm["velocity"] == 96
    assert vm["gate_ms"] == 120
    assert vm["active"] == 0


def test_device_info_reflects_a_bound_provider():
    page = SendNotesPage()
    page.bind_device_info(lambda: {"is_open": True, "port_name": "midicrt2 Output"})
    assert page.view_model()["device"] == "midicrt2 Output"


def test_device_info_reports_none_when_provider_says_closed():
    page = SendNotesPage()
    page.bind_device_info(lambda: {"is_open": False, "port_name": "midicrt2 Output"})
    assert page.view_model()["device"] is None


def test_empty_key_is_a_true_no_op():
    page = SendNotesPage()
    assert page.apply_key("", now=0.0) is None


def test_unrecognized_key_is_a_true_no_op():
    page = SendNotesPage()
    assert page.apply_key("q", now=0.0) is None


# -- note keys ----------------------------------------------------------------

def test_note_key_triggers_a_note_on_at_octave_4_c4_equals_60():
    page = SendNotesPage()
    result = page.apply_key("z", now=0.0)   # offset 0
    assert result == {"note_on": True, "note": 60, "vel": 96, "ch": 1}
    assert page.view_model()["active"] == 1


def test_note_key_uses_the_current_channel_octave_and_velocity():
    page = SendNotesPage()
    page.apply_key(",", now=0.0)   # channel -> ... clamped at 1, no-op here
    page.apply_key("]", now=0.0)   # octave 4 -> 5
    page.apply_key("=", now=0.0)   # velocity 96 -> 104
    result = page.apply_key("s", now=0.0)   # offset 1
    assert result == {"note_on": True, "note": 12 * 6 + 1, "vel": 104, "ch": 1}


def test_note_key_is_case_insensitive():
    page = SendNotesPage()
    result = page.apply_key("Z", now=0.0)
    assert result["note_on"] is True
    assert result["note"] == 60


# -- control keys ---------------------------------------------------------------

def test_comma_and_period_adjust_channel_clamped_1_to_16():
    page = SendNotesPage()
    assert page.apply_key(",", now=0.0) == {"note_on": False}
    assert page.view_model()["channel"] == 1   # clamped, was already 1
    for _ in range(20):
        page.apply_key(".", now=0.0)
    assert page.view_model()["channel"] == 16   # clamped at 16


def test_brackets_adjust_octave_clamped_neg1_to_9():
    page = SendNotesPage()
    for _ in range(20):
        page.apply_key("[", now=0.0)
    assert page.view_model()["octave"] == -1
    for _ in range(20):
        page.apply_key("]", now=0.0)
    assert page.view_model()["octave"] == 9


def test_dash_and_equals_adjust_velocity_clamped_1_to_127_by_8():
    page = SendNotesPage()
    page.apply_key("-", now=0.0)
    assert page.view_model()["velocity"] == 88
    for _ in range(20):
        page.apply_key("-", now=0.0)
    assert page.view_model()["velocity"] == 1
    for _ in range(20):
        page.apply_key("=", now=0.0)
    assert page.view_model()["velocity"] == 127


def test_g_and_h_adjust_gate_clamped_20_to_2000_by_20ms():
    page = SendNotesPage()
    page.apply_key("g", now=0.0)
    assert page.view_model()["gate_ms"] == 100
    for _ in range(20):
        page.apply_key("g", now=0.0)
    assert page.view_model()["gate_ms"] == 20
    for _ in range(200):
        page.apply_key("h", now=0.0)
    assert page.view_model()["gate_ms"] == 2000


def test_gate_keys_are_case_insensitive():
    page = SendNotesPage()
    result = page.apply_key("G", now=0.0)
    assert result == {"note_on": False}
    assert page.view_model()["gate_ms"] == 100


# -- real, faithfully-preserved v1 KEYMAP-shadowing quirk --------------------

def test_comma_period_g_h_never_trigger_a_note_despite_being_in_keymap():
    # KEYMAP maps ',' -> 12, '.' -> 14, 'g' -> 6, 'h' -> 8 -- but v1's own
    # keypress() checks the control-key branches FIRST and always returns
    # before KEYMAP is ever consulted for these four characters. Verified
    # against v1's real source, not assumed -- see pages/sendnotes.py's
    # own module docstring.
    page = SendNotesPage()
    for key in (",", ".", "g", "h"):
        result = page.apply_key(key, now=0.0)
        assert result == {"note_on": False}, f"{key!r} unexpectedly triggered a note"
    assert page.view_model()["active"] == 0


# -- expiry (drain_expired) ---------------------------------------------------

def test_drain_expired_returns_nothing_before_the_gate_elapses():
    page = SendNotesPage()
    page.apply_key("z", now=0.0)   # gate_ms=120 -> expires at 0.12
    assert page.drain_expired(now=0.1) == []
    assert page.view_model()["active"] == 1


def test_drain_expired_releases_a_note_once_its_gate_elapses():
    page = SendNotesPage()
    page.apply_key("z", now=0.0)   # note 60, ch 1, expires at 0.12
    released = page.drain_expired(now=0.2)
    assert released == [(60, 1)]
    assert page.view_model()["active"] == 0


def test_drain_expired_releases_multiple_in_fifo_order():
    page = SendNotesPage()
    page.apply_key("z", now=0.0)    # note 60
    page.apply_key("s", now=0.01)   # note 61
    released = page.drain_expired(now=1.0)
    assert released == [(60, 1), (61, 1)]


def test_drain_expired_front_only_quirk_a_shortened_gate_can_stick_behind_an_earlier_note():
    # Real v1 quirk, faithfully ported (see drain_expired's own
    # docstring): press a note with a LONG gate, shorten the gate, press
    # a SECOND note -- the second note's absolute expiry is earlier, but
    # it is stuck in the FIFO behind the first and won't release until
    # the first also expires.
    page = SendNotesPage()
    page.apply_key("h", now=0.0)     # gate 120 -> 140ms
    page.apply_key("h", now=0.0)     # 140 -> 160ms
    page.apply_key("z", now=0.0)     # note 60, expires at 0.16
    for _ in range(6):
        page.apply_key("g", now=0.0)   # 160 -> 40ms
    page.apply_key("s", now=0.05)    # note 61, expires at 0.09 -- EARLIER than note 60's 0.16

    # At t=0.10, note 61's gate has elapsed but note 60's has not -- yet
    # nothing is released, because note 61 is stuck behind note 60 in the
    # FIFO (the front-only check never looks past the un-expired head).
    assert page.drain_expired(now=0.10) == []
    assert page.view_model()["active"] == 2

    # Once note 60 (the front) finally expires, BOTH release together.
    released = page.drain_expired(now=0.20)
    assert released == [(60, 1), (61, 1)]
