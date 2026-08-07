"""TDD for Engine._handle_sysex and its dispatch helpers -- the DISPATCH
half of v1's `plugins/sysex.py` port (see engine/sysex.py for the pure
PARSE half these tests build MidiEvents around). Uses real captured .syx
fixtures from `~/codex/midicrt/sysex.d/` on the Pi (copied out under
tests/fixtures/sysex_captures/, sanctioned by the task brief -- small,
genuine production captures, not synthetic).
"""
from pathlib import Path

from midicrt.config import Config
from midicrt.engine.core import Engine, MidiEvent

FIXTURES = Path(__file__).parent / "fixtures" / "sysex_captures"


def load_syx(name: str) -> tuple[int, ...]:
    """Parse one of the copied-out `.syx` text captures (`F0 .. .. F7`
    hex-byte lines) into the F0/F7-free byte tuple `translate()` would
    hand `MidiEvent.sysex_data` -- mirrors mido's own `msg.data` shape."""
    text = (FIXTURES / name).read_text().strip()
    all_bytes = [int(tok, 16) for tok in text.split()]
    assert all_bytes[0] == 0xF0 and all_bytes[-1] == 0xF7
    return tuple(all_bytes[1:-1])


def sysex_ev(data: tuple[int, ...], ts: float = 1000.0) -> MidiEvent:
    return MidiEvent(ts=ts, source="Cirklon", type="sysex", channel=None,
                      data1=None, data2=None, summary=f"sysex ({len(data)} bytes)",
                      sysex_data=data)


class _FakeMidiOut:
    def __init__(self):
        self.sent: list[tuple[int, ...]] = []
        self.is_open = True
        self.port_name = "fake"

    def send_sysex(self, data):
        self.sent.append(data)
        return True

    def note_on(self, *a, **kw):
        pass

    def note_off(self, *a, **kw):
        pass

    def close(self):
        pass


def engine_with_fake_out(**cfg) -> tuple[Engine, _FakeMidiOut]:
    eng = Engine(Config(**cfg))
    fake = _FakeMidiOut()
    eng._midi_out = fake
    return eng, fake


# -- real captured fixtures ----------------------------------------------------

def test_real_capture_non_midicrt_frame_is_ignored_entirely():
    eng, fake = engine_with_fake_out()
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    before_page = eng.current_page
    eng._handle(sysex_ev(load_syx("non-midicrt-frame.syx")))
    assert eng.current_page == before_page
    assert fake.sent == []
    assert events == []   # no sysex_command event for non-matching traffic


def test_real_capture_legacy_switch_page_0_switches_to_help():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-switch-page-0.syx")))
    assert eng.current_page == "help"
    assert fake.sent == []   # legacy frame -- no reply channel


def test_real_capture_legacy_switch_page_8_switches_to_pianoroll():
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-switch-page-8.syx")))
    assert eng.current_page == "pianoroll"


def test_real_capture_legacy_pagecycle_enable_and_disable():
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-pagecycle-disable.syx")))
    assert eng._pagecycle_behavior.enabled is False
    eng._handle(sysex_ev(load_syx("legacy-pagecycle-enable.syx")))
    assert eng._pagecycle_behavior.enabled is True


# -- activity / event emission --------------------------------------------------

def test_any_matched_prefix_command_bumps_activity():
    eng, _fake = engine_with_fake_out()
    eng._last_activity_ts = 0.0
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x01, 0x00), ts=5000.0))
    assert eng._last_activity_ts == 5000.0


def test_non_prefix_sysex_does_not_bump_activity():
    eng, _fake = engine_with_fake_out()
    eng._last_activity_ts = 0.0
    eng._handle(sysex_ev((0x01, 0x02, 0x03), ts=5000.0))
    assert eng._last_activity_ts == 0.0


def test_matched_command_emits_a_sysex_command_event():
    eng, _fake = engine_with_fake_out()
    events = []
    eng.add_listener(lambda m: events.append(m) if m.get("kind") == "event" else None)
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x01, 0x08)))
    matches = [e for e in events if e["name"] == "sysex_command"]
    assert len(matches) == 1
    assert matches[0]["data"] == {"version": None, "cmd": 0x01, "args": [0x08], "error": None}


# -- CMD_SWITCH_PAGE (versioned) -----------------------------------------------

def test_versioned_switch_page_replies_ok_with_the_page_id_echoed():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x01, 0x08)))   # v1, switch to page 8
    assert eng.current_page == "pianoroll"
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x01, 0x00, 0x08)]


def test_switch_page_to_an_unmapped_id_replies_error_and_does_not_move():
    eng, fake = engine_with_fake_out()
    before = eng.current_page
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x01, 0x03)))   # page 3 (transport) -- no v2 home
    assert eng.current_page == before
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x01, 0x01, 0x03)]


def test_switch_page_to_a_mapped_but_not_in_roster_id_replies_error():
    eng, fake = engine_with_fake_out(pages=["eventlog"])   # "tuner" (id 10) not in a minimal roster
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x01, 0x0A)))
    assert eng.current_page == "eventlog"
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x01, 0x01, 0x0A)]


def test_switch_page_with_no_args_replies_error():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x01)))
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x01, 0x01)]


# -- CMD_SCREENSAVER ------------------------------------------------------------

def test_screensaver_force_on_switches_to_the_screensaver_page():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x02, 0x01)))
    assert eng.current_page == "screensaver"
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x02, 0x00, 0x01)]


def test_screensaver_wake_does_not_itself_change_the_page_but_bumps_activity():
    eng, fake = engine_with_fake_out()
    eng.current_page = "screensaver"
    eng._last_activity_ts = 0.0
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x02, 0x00), ts=42.0))
    # No direct page change from this command alone -- the activity bump
    # (see _handle_sysex's own docstring) is what lets ScreensaverBehavior
    # restore it on its own next tick, exactly like a real note would.
    assert eng._last_activity_ts == 42.0
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x02, 0x00, 0x00)]


# -- CMD_PAGE_CYCLE (versioned) -------------------------------------------------

async def test_versioned_page_cycle_disable_replies_ok_and_matches_the_action():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x03, 0x00)))
    assert eng._pagecycle_behavior.enabled is False
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x03, 0x00, 0x00)]
    # Same underlying capability as the pagecycle.enable action.
    r = await eng.actions.dispatch("pagecycle.enable", {"enabled": "true"})
    assert r == {"enabled": True}
    assert eng._pagecycle_behavior.enabled is True


# -- CMD_CAPTURE_RECENT (not ported -- Phase 5) ---------------------------------

def test_capture_recent_versioned_replies_error_not_silently_ok():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x04, 0x02)))
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x04, 0x01)]


def test_capture_recent_legacy_frame_has_no_reply_channel():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x04, 0x02)))
    assert fake.sent == []


# -- CMD_CAPABILITIES -----------------------------------------------------------

def _parse_capabilities_payload(payload: tuple[int, ...]) -> dict:
    """Mirrors `Engine._sysex_capabilities_payload`'s own layout exactly
    (version, profile, backend, n_versions+versions, n_flags+flags,
    n_pages+page_ids) -- a single shared parser so every test below agrees
    on the field offsets instead of each hand-rolling its own slicing."""
    version, _profile, _backend = payload[0], payload[1], payload[2]
    n_versions = payload[3]
    versions_end = 4 + n_versions
    versions = payload[4:versions_end]
    n_flags = payload[versions_end]
    flags_end = versions_end + 1 + n_flags
    flags = payload[versions_end + 1:flags_end]
    n_pages = payload[flags_end]
    page_ids = payload[flags_end + 1:flags_end + 1 + n_pages]
    return {"version": version, "versions": versions, "flags": flags, "page_ids": page_ids}


def test_capabilities_versioned_reply_shape():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x40, 0x10)))   # negotiate + capabilities
    assert len(fake.sent) == 1
    reply = fake.sent[0]
    assert reply[:3] == (0x7D, 0x6D, 0x63)
    assert reply[3] == 0x41   # version 1
    assert reply[4] == 0x10   # cmd echoed
    assert reply[5] == 0x00   # ok
    parsed = _parse_capabilities_payload(reply[6:])
    assert parsed["version"] == 1
    assert parsed["versions"] == (1,)
    assert parsed["flags"] == (0, 1, 1)   # capture=0, screensaver=1, pagecycle=1
    assert parsed["page_ids"] == tuple(sorted(parsed["page_ids"]))
    assert 0 in parsed["page_ids"]   # "help" is in the default roster


def test_capabilities_legacy_frame_gets_no_reply_at_all():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x10)))
    assert fake.sent == []


def test_capabilities_pages_list_shrinks_with_a_minimal_roster():
    eng, fake = engine_with_fake_out(pages=["eventlog"])
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x40, 0x10)))
    parsed = _parse_capabilities_payload(fake.sent[0][6:])
    assert parsed["page_ids"] == (6,)   # only "eventlog" (id 6) reachable


# -- unsupported version / missing cmd ------------------------------------------

def test_unsupported_version_replies_with_max_supported_version_byte_not_the_requested_one():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x49, 0x01, 0x08)))   # requests version 9
    assert len(fake.sent) == 1
    reply = fake.sent[0]
    assert reply[3] == 0x41   # v1's own reply quirk: max SUPPORTED version, not the requested one
    assert reply[5] == 0x01   # error status
    assert eng.current_page != "pianoroll"   # never dispatched


def test_missing_cmd_byte_gets_no_reply_and_still_bumps_activity():
    eng, fake = engine_with_fake_out()
    eng._last_activity_ts = 0.0
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41), ts=99.0))
    assert fake.sent == []
    assert eng._last_activity_ts == 99.0


# -- unknown command -------------------------------------------------------------

def test_unknown_command_versioned_replies_error():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x41, 0x7F)))
    assert fake.sent == [(0x7D, 0x6D, 0x63, 0x41, 0x7F, 0x01)]


def test_unknown_command_legacy_has_no_reply_channel():
    eng, fake = engine_with_fake_out()
    eng._handle(sysex_ev((0x7D, 0x6D, 0x63, 0x7F)))
    assert fake.sent == []


# -- guard: no sysex_data at all (defensive) ------------------------------------

def test_sysex_event_with_no_sysex_data_is_a_safe_no_op():
    eng, fake = engine_with_fake_out()
    ev = MidiEvent(ts=0.0, source="x", type="sysex", channel=None,
                    data1=None, data2=None, summary="sysex (0 bytes)", sysex_data=None)
    eng._handle(ev)   # must not raise
    assert fake.sent == []
