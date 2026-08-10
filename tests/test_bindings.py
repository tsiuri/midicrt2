"""TDD for engine/bindings.py: the MIDI binding dispatcher (Phase 4 Task 2,
docs/phase4-notes.md). Mirrors test_keymap.py's split -- pure, registry-
unaware pieces tested here (`BindingDispatcher.handle`, `BindingsFile.load`/
`save`, `validate_binding`); the registry-aware engine WIRING (zero-client
firing, `bind.list`/`bind.remove` actions, `config.reload` extension) lives
in test_engine_core.py, same precedent as test_keymap.py vs
test_engine_core.py's own keymap section.
"""
import fnmatch
import logging
import time

import pytest

from midicrt.engine.bindings import (
    CONTINUOUS_FILL_TOKEN,
    LEARN_TIMEOUT_S,
    Binding,
    BindingDispatcher,
    BindingMatch,
    BindingsFile,
    glob_port_pattern,
    is_learnable_event,
    should_replace_on_relearn,
    strip_alsa_port_suffix,
    validate_binding,
)
from midicrt.engine.core import MidiEvent


def ev(**kw):
    base = {"ts": time.time(), "source": "Midi Through:Midi Through Port-0 14:0",
            "type": "note_on", "channel": 0, "data1": 60, "data2": 100,
            "summary": "note_on ch1 n60 v100"}
    base.update(kw)
    return MidiEvent(**base)


def trigger_binding(**kw):
    match_kw = kw.pop("match", {})
    match = BindingMatch(type=match_kw.pop("type", "note_on"),
                         number=match_kw.pop("number", 60), **match_kw)
    defaults = {"id": "b1", "match": match, "action": "page.next", "args": {}}
    defaults.update(kw)
    return Binding(**defaults)


# -- match semantics ----------------------------------------------------------

def test_note_on_matches_by_type_and_number():
    b = trigger_binding(match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle(ev(data1=60)) == [("page.next", {})]
    assert d.handle(ev(data1=61)) == []


def test_note_on_does_not_match_control_change_event():
    b = trigger_binding(match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle(ev(type="control_change", data1=60, data2=100)) == []


def test_channel_none_matches_any_channel():
    b = trigger_binding(match={"type": "note_on", "number": 60, "channel": None})
    d = BindingDispatcher([b])
    assert d.handle(ev(channel=0)) == [("page.next", {})]
    assert d.handle(ev(channel=5)) == [("page.next", {})]


def test_channel_set_only_matches_that_channel():
    b = trigger_binding(match={"type": "note_on", "number": 60, "channel": 3})
    d = BindingDispatcher([b])
    assert d.handle(ev(channel=3)) == [("page.next", {})]
    assert d.handle(ev(channel=0)) == []


def test_port_pattern_none_matches_any_source():
    b = trigger_binding(match={"type": "note_on", "number": 60, "port_pattern": None})
    d = BindingDispatcher([b])
    assert d.handle(ev(source="anything at all")) == [("page.next", {})]


def test_port_pattern_fnmatch_on_source():
    b = trigger_binding(match={"type": "note_on", "number": 60, "port_pattern": "Midi Through*"})
    d = BindingDispatcher([b])
    assert d.handle(ev(source="Midi Through:Midi Through Port-0 14:0")) == [("page.next", {})]
    assert d.handle(ev(source="USB MIDI Interface 20:0")) == []


# -- device identity (Phase 9 Task 1): primary when present, port_pattern -----
# untouched otherwise -- see BindingMatch's own docstring for the chosen
# precedence.

def test_device_present_matches_by_device_id_regardless_of_port_pattern():
    """A deliberately WRONG port_pattern (would never fnmatch this
    source) must not matter at all once `device` is set -- proves device
    is not ANDed with port_pattern."""
    b = trigger_binding(match={"type": "note_on", "number": 60,
                               "port_pattern": "this pattern matches nothing*",
                               "device": "usb:1234:5678:SN1"})
    d = BindingDispatcher([b])
    assert d.handle(ev(device_id="usb:1234:5678:SN1", source="anything at all")) == [
        ("page.next", {})]


def test_device_present_does_not_fall_back_to_port_pattern_on_mismatch():
    """Proves device is not ORed with port_pattern either: a port_pattern
    that WOULD match is still ignored once `device` disagrees."""
    b = trigger_binding(match={"type": "note_on", "number": 60,
                               "port_pattern": "Midi Through*",
                               "device": "usb:1234:5678:SN1"})
    d = BindingDispatcher([b])
    assert d.handle(ev(device_id="usb:1234:5678:SN2",
                       source="Midi Through:Midi Through Port-0 14:0")) == []


def test_device_bound_binding_fires_via_port_pattern_rescue_when_event_has_no_device_id():
    """Important review fix: a transient identity-resolution outage on
    the EVENT's own port (ev.device_id is None) must not strand a
    device-bound binding dead -- it falls back to the pre-existing
    port_pattern check for that one event."""
    b = trigger_binding(match={"type": "note_on", "number": 60,
                               "port_pattern": "Midi Through*",
                               "device": "usb:1234:5678:SN1"})
    d = BindingDispatcher([b])
    assert d.handle(ev(device_id=None, source="Midi Through:Midi Through Port-0 14:0")) == [
        ("page.next", {})]


def test_device_bound_binding_rescue_still_respects_a_non_matching_pattern():
    b = trigger_binding(match={"type": "note_on", "number": 60,
                               "port_pattern": "Midi Through*",
                               "device": "usb:1234:5678:SN1"})
    d = BindingDispatcher([b])
    assert d.handle(ev(device_id=None, source="USB MIDI Interface 20:0")) == []


def test_device_none_falls_back_to_port_pattern_exactly_as_before():
    """Migration: a binding with no `device` at all (every binding
    persisted before this task) must keep matching purely on port_pattern
    -- even against an event that itself DOES carry a resolved
    `device_id` (a real, fully-upgraded engine populates it on every
    event; the OLD binding simply never asked for it)."""
    b = trigger_binding(match={"type": "note_on", "number": 60,
                               "port_pattern": "Midi Through*", "device": None})
    d = BindingDispatcher([b])
    assert d.handle(ev(device_id="usb:1234:5678:SN1",
                       source="Midi Through:Midi Through Port-0 14:0")) == [
        ("page.next", {})]
    assert d.handle(ev(device_id="usb:1234:5678:SN1", source="USB MIDI Interface 20:0")) == []


# -- trigger: note_on (velocity > 0 only) --------------------------------------

def test_trigger_note_on_fires_with_positive_velocity():
    b = trigger_binding(match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle(ev(data1=60, data2=100)) == [("page.next", {})]


def test_trigger_note_on_does_not_fire_with_zero_velocity():
    # A note_on with velocity 0 is a running-status note-off, not a real
    # trigger -- see module docstring.
    b = trigger_binding(match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle(ev(data1=60, data2=0)) == []


def test_trigger_note_on_passes_through_its_static_args():
    b = trigger_binding(match={"type": "note_on", "number": 60},
                        action="page.goto", args={"name": "screensaver"})
    d = BindingDispatcher([b])
    assert d.handle(ev(data1=60, data2=100)) == [("page.goto", {"name": "screensaver"})]


# -- trigger: CC crossing threshold upward only --------------------------------

def test_cc_trigger_fires_on_upward_crossing():
    b = trigger_binding(match={"type": "control_change", "number": 20}, threshold=64)
    d = BindingDispatcher([b])
    cc = lambda v: ev(type="control_change", data1=20, data2=v)
    assert d.handle(cc(10)) == []    # baseline only -- no edge yet, see module docstring
    assert d.handle(cc(30)) == []    # still below threshold
    assert d.handle(cc(70)) == [("page.next", {})]   # crossed upward: fires
    assert d.handle(cc(90)) == []    # still above -- no re-fire while staying up


def test_cc_trigger_resets_below_threshold_and_can_refire():
    b = trigger_binding(match={"type": "control_change", "number": 20}, threshold=64)
    d = BindingDispatcher([b])
    cc = lambda v: ev(type="control_change", data1=20, data2=v)
    d.handle(cc(70))                          # baseline above threshold -- no edge
    assert d.handle(cc(90)) == []             # still above, no baseline-below yet
    assert d.handle(cc(30)) == []             # drops below -- resets
    assert d.handle(cc(80)) == [("page.next", {})]   # crosses upward again: fires


def test_cc_trigger_first_ever_message_never_fires_regardless_of_value():
    # No prior sample means no "lo" side to have crossed FROM -- an "edge"
    # is only detectable once a second sample arrives (see module
    # docstring's "no prior value" note). A knob already sitting high when
    # the daemon boots must not spuriously fire on its very first message.
    b = trigger_binding(match={"type": "control_change", "number": 20}, threshold=64)
    d = BindingDispatcher([b])
    assert d.handle(ev(type="control_change", data1=20, data2=127)) == []


def test_cc_trigger_exact_threshold_value_counts_as_crossed():
    b = trigger_binding(match={"type": "control_change", "number": 20}, threshold=64)
    d = BindingDispatcher([b])
    cc = lambda v: ev(type="control_change", data1=20, data2=v)
    d.handle(cc(10))
    assert d.handle(cc(64)) == [("page.next", {})]


def test_cc_trigger_tracks_state_independently_per_source_and_channel():
    b = trigger_binding(match={"type": "control_change", "number": 20, "channel": None},
                        threshold=64)
    d = BindingDispatcher([b])
    d.handle(ev(type="control_change", data1=20, data2=10, channel=0, source="A"))
    d.handle(ev(type="control_change", data1=20, data2=10, channel=1, source="A"))
    # Channel 0 crosses; channel 1's independent baseline is untouched.
    assert d.handle(ev(type="control_change", data1=20, data2=70, channel=0, source="A")) == \
        [("page.next", {})]
    assert d.handle(ev(type="control_change", data1=20, data2=70, channel=1, source="B")) == []


def test_cc_trigger_state_is_independent_per_binding():
    # Two different bindings both watching the same physical CC/channel with
    # different thresholds must not share an edge-detection baseline.
    lo = trigger_binding(id="lo", match={"type": "control_change", "number": 20},
                         threshold=32, action="page.next")
    hi = trigger_binding(id="hi", match={"type": "control_change", "number": 20},
                         threshold=100, action="page.prev")
    d = BindingDispatcher([lo, hi])
    cc = lambda v: ev(type="control_change", data1=20, data2=v)
    d.handle(cc(0))                                    # baseline for both
    assert d.handle(cc(50)) == [("page.next", {})]     # crosses "lo" only
    assert d.handle(cc(110)) == [("page.prev", {})]    # crosses "hi" only (lo stays above)


def test_set_bindings_clears_edge_state():
    b = trigger_binding(match={"type": "control_change", "number": 20}, threshold=64)
    d = BindingDispatcher([b])
    d.handle(ev(type="control_change", data1=20, data2=70))   # establishes a baseline above
    d.set_bindings([b])
    # After set_bindings, the baseline is gone -- next message is a fresh
    # "first ever", never fires regardless of value (see module docstring:
    # a reload's edge state is deliberately reset, not migrated).
    assert d.handle(ev(type="control_change", data1=20, data2=90)) == []


# -- continuous: CC value lerped into [lo, hi], filling the args template -----

def continuous_binding(**kw):
    match_kw = kw.pop("match", {})
    match = BindingMatch(type=match_kw.pop("type", "control_change"),
                         number=match_kw.pop("number", 21), **match_kw)
    defaults = {"id": "c1", "match": match, "action": "chrome.brightness",
                "args": {"level": None}, "mode": "continuous", "range": (0.0, 1.0)}
    defaults.update(kw)
    return Binding(**defaults)


def test_continuous_lerps_full_range():
    b = continuous_binding(range=(0.0, 1.0))
    d = BindingDispatcher([b])
    name, args = d.handle(ev(type="control_change", data1=21, data2=0))[0]
    assert name == "chrome.brightness" and args["level"] == pytest.approx(0.0)
    name, args = d.handle(ev(type="control_change", data1=21, data2=127))[0]
    assert args["level"] == pytest.approx(1.0)
    name, args = d.handle(ev(type="control_change", data1=21, data2=64))[0]
    assert args["level"] == pytest.approx(64 / 127)


def test_continuous_lerps_an_arbitrary_range():
    b = continuous_binding(range=(-40.0, 20.0))
    d = BindingDispatcher([b])
    _, args = d.handle(ev(type="control_change", data1=21, data2=0))[0]
    assert args["level"] == pytest.approx(-40.0)
    _, args = d.handle(ev(type="control_change", data1=21, data2=127))[0]
    assert args["level"] == pytest.approx(20.0)


def test_continuous_lerps_an_inverted_range():
    # range=[hi, lo] -- CC=0 maps to the FIRST endpoint regardless of which
    # is numerically larger; the lerp formula needs no special-casing for
    # this (see module docstring).
    b = continuous_binding(range=(1.0, 0.0))
    d = BindingDispatcher([b])
    _, args = d.handle(ev(type="control_change", data1=21, data2=0))[0]
    assert args["level"] == pytest.approx(1.0)
    _, args = d.handle(ev(type="control_change", data1=21, data2=127))[0]
    assert args["level"] == pytest.approx(0.0)


def test_continuous_fills_the_named_template_arg_and_keeps_other_static_args():
    b = continuous_binding(action="chrome.tint", args={"channel": "warm", "level": None},
                           range=(0.0, 1.0))
    d = BindingDispatcher([b])
    name, args = d.handle(ev(type="control_change", data1=21, data2=127))[0]
    assert name == "chrome.tint"
    assert args["channel"] == "warm"
    assert args["level"] == pytest.approx(1.0)


def test_continuous_every_call_produces_an_intent_no_edge_detection():
    # Unlike trigger-mode CC, continuous mode has no threshold/edge concept
    # -- every matching event produces an intent, including the very first.
    b = continuous_binding()
    d = BindingDispatcher([b])
    assert len(d.handle(ev(type="control_change", data1=21, data2=10))) == 1
    assert len(d.handle(ev(type="control_change", data1=21, data2=11))) == 1


# -- BindingMatch / Binding defaults -------------------------------------------

def test_binding_match_defaults_channel_and_port_pattern_to_none():
    m = BindingMatch(type="note_on", number=60)
    assert m.channel is None
    assert m.port_pattern is None
    assert m.device is None


def test_binding_defaults_mode_trigger_threshold_64():
    b = Binding(id="x", match=BindingMatch(type="note_on", number=60), action="page.next")
    assert b.mode == "trigger"
    assert b.threshold == 64
    assert b.args == {}


# -- should_replace_on_relearn (Phase 9 Task 1 follow-up, review Critical, --
# live-reproduced): the fix for `BindingMatch.device` breaking replace-on-
# relearn's original plain `==` check the moment a fresh capture starts
# carrying a real device_id against an old device=None binding.

def test_should_replace_full_match_equal_including_device():
    m1 = BindingMatch(type="note_on", number=60, channel=0,
                      port_pattern="A*", device="usb:1:2:S")
    m2 = BindingMatch(type="note_on", number=60, channel=0,
                      port_pattern="A*", device="usb:1:2:S")
    assert should_replace_on_relearn(m1, m2)


def test_should_replace_pre_device_binding_against_a_freshly_device_stamped_capture():
    """THE fix: an existing device=None binding, identical type/number/
    channel/port_pattern to a fresh capture that NOW also carries a
    resolved device -- must still be recognized as the same physical
    control worth replacing, not silently kept alongside the new one."""
    existing = BindingMatch(type="note_on", number=60, channel=0,
                            port_pattern="Midi Through:Midi Through Port-0*", device=None)
    fresh = BindingMatch(type="note_on", number=60, channel=0,
                         port_pattern="Midi Through:Midi Through Port-0*",
                         device="virt:Midi Through:Midi Through Port-0")
    assert should_replace_on_relearn(existing, fresh)


def test_should_not_replace_different_control_even_with_identical_port_pattern():
    existing = BindingMatch(type="note_on", number=60, channel=0,
                            port_pattern="A*", device=None)
    fresh = BindingMatch(type="note_on", number=61, channel=0, port_pattern="A*", device="usb:1:2")
    assert not should_replace_on_relearn(existing, fresh)


def test_should_not_replace_when_existing_device_stamped_differently_even_if_pattern_matches():
    """Full-equality rule (a) only -- a DIFFERENT device on an already
    device-stamped existing binding is not the migration case (b) at
    all (existing.device is not None), so it is left alone."""
    existing = BindingMatch(type="note_on", number=60, channel=0,
                            port_pattern="A*", device="usb:1:2:SN1")
    fresh = BindingMatch(type="note_on", number=60, channel=0,
                         port_pattern="A*", device="usb:1:2:SN2")
    assert not should_replace_on_relearn(existing, fresh)


def test_should_not_replace_wildcard_overlapping_but_not_identical_port_pattern():
    """Unchanged Phase-4 disclosed contract: STRING equality, not
    fnmatch overlap."""
    existing = BindingMatch(type="note_on", number=60, channel=0,
                            port_pattern="Midi Through*", device=None)
    fresh = BindingMatch(type="note_on", number=60, channel=0,
                         port_pattern="Midi Through:Midi Through Port-0*",
                         device="virt:Midi Through:Midi Through Port-0")
    assert not should_replace_on_relearn(existing, fresh)


def test_should_not_replace_when_fresh_device_is_none_but_existing_is_device_stamped():
    """Deliberately NOT symmetric (disclosed): a fresh capture that
    itself failed to resolve identity (device=None) does not replace an
    existing, already-disambiguated device-stamped binding just because
    the port_pattern matches."""
    existing = BindingMatch(type="note_on", number=60, channel=0,
                            port_pattern="A*", device="usb:1:2:SN1")
    fresh = BindingMatch(type="note_on", number=60, channel=0, port_pattern="A*", device=None)
    assert not should_replace_on_relearn(existing, fresh)


# -- BindingsFile: load ---------------------------------------------------------

def test_bindingsfile_load_missing_file_returns_empty():
    bf = BindingsFile.load("/nonexistent/path/bindings.toml")
    assert bf.bindings == []


def test_bindingsfile_load_a_trigger_note_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.next"\n'
        'mode = "trigger"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
        'channel = 2\n'
        'port_pattern = "Midi Through*"\n'
    )
    bf = BindingsFile.load(str(p))
    assert len(bf.bindings) == 1
    b = bf.bindings[0]
    assert b.id == "b1"
    assert b.action == "page.next"
    assert b.mode == "trigger"
    assert b.match == BindingMatch(type="note_on", number=60, channel=2,
                                   port_pattern="Midi Through*")


def test_bindingsfile_load_omits_optional_match_fields_as_none(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.next"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
    )
    bf = BindingsFile.load(str(p))
    b = bf.bindings[0]
    assert b.match.channel is None
    assert b.match.port_pattern is None


def test_bindingsfile_load_a_continuous_binding_with_fill_token(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.c1]\n'
        'action = "chrome.brightness"\n'
        'mode = "continuous"\n'
        'range = [0.0, 1.0]\n'
        '\n'
        '[bindings.c1.args]\n'
        f'level = "{CONTINUOUS_FILL_TOKEN}"\n'
        '\n'
        '[bindings.c1.match]\n'
        'type = "control_change"\n'
        'number = 21\n'
    )
    bf = BindingsFile.load(str(p))
    b = bf.bindings[0]
    assert b.mode == "continuous"
    assert b.range == (0.0, 1.0)
    assert b.args == {"level": None}   # sentinel translated to real None in memory


# -- sentinel substitution is scoped to mode == "continuous" only -----------
# (review finding, Important, live-reproduced: the token->None translation
# ran unconditionally for every binding regardless of mode -- a TRIGGER
# binding whose args happened to carry the literal sentinel STRING as a
# genuine static value was silently corrupted to None, even though trigger
# mode has no fill-target concept at all to make sense of that.)

def test_bindingsfile_load_trigger_binding_with_literal_sentinel_string_keeps_it_verbatim(
        tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.goto"\n'
        'mode = "trigger"\n'
        '\n'
        '[bindings.b1.args]\n'
        f'name = "{CONTINUOUS_FILL_TOKEN}"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
    )
    bf = BindingsFile.load(str(p))
    b = bf.bindings[0]
    assert b.mode == "trigger"
    # Must stay the literal string -- NOT translated to None, unlike the
    # continuous case right above.
    assert b.args == {"name": CONTINUOUS_FILL_TOKEN}


def test_bindingsfile_roundtrip_trigger_binding_with_literal_sentinel_string(tmp_path):
    p = tmp_path / "bindings.toml"
    original = trigger_binding(action="page.goto", args={"name": CONTINUOUS_FILL_TOKEN})
    bf = BindingsFile([original], path=str(p))
    bf.save()
    reloaded = BindingsFile.load(str(p))
    assert reloaded.bindings == [original]   # verbatim round-trip, not corrupted to None


def test_bindingsfile_load_tolerates_unknown_keys(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.next"\n'
        'some_future_field = "whatever"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
        'also_unknown = 5\n'
    )
    bf = BindingsFile.load(str(p))
    assert len(bf.bindings) == 1
    assert bf.bindings[0].action == "page.next"


def test_bindingsfile_load_raises_valueerror_when_bindings_is_not_a_table(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text('bindings = "oops"\n')
    with pytest.raises(ValueError, match="bindings"):
        BindingsFile.load(str(p))


def test_bindingsfile_load_no_bindings_table_at_all_is_empty(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text('# nothing here\n')
    bf = BindingsFile.load(str(p))
    assert bf.bindings == []


def test_bindingsfile_load_skips_entry_missing_action_and_keeps_the_rest(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        '[bindings.bad.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
        '\n'
        '[bindings.good]\n'
        'action = "page.next"\n'
        '[bindings.good.match]\n'
        'type = "note_on"\n'
        'number = 61\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert [b.id for b in bf.bindings] == ["good"]
    assert "bad" in caplog.text


def test_bindingsfile_load_skips_entry_with_bad_match_shape(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "page.next"\n'
        'match = "oops"\n'
        '\n'
        '[bindings.good]\n'
        'action = "page.next"\n'
        '[bindings.good.match]\n'
        'type = "note_on"\n'
        'number = 61\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert [b.id for b in bf.bindings] == ["good"]
    assert "bad" in caplog.text


def test_bindingsfile_load_skips_entry_with_unknown_match_type(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "page.next"\n'
        '[bindings.bad.match]\n'
        'type = "sysex"\n'
        'number = 1\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert bf.bindings == []
    assert "sysex" in caplog.text


def test_bindingsfile_load_skips_continuous_entry_missing_range(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "chrome.brightness"\n'
        'mode = "continuous"\n'
        '\n'
        '[bindings.bad.args]\n'
        f'level = "{CONTINUOUS_FILL_TOKEN}"\n'
        '\n'
        '[bindings.bad.match]\n'
        'type = "control_change"\n'
        'number = 21\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert bf.bindings == []
    assert "range" in caplog.text.lower()


def test_bindingsfile_load_skips_continuous_entry_with_no_fill_marker(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "chrome.brightness"\n'
        'mode = "continuous"\n'
        'range = [0.0, 1.0]\n'
        '\n'
        '[bindings.bad.args]\n'
        'level = "0.5"\n'
        '\n'
        '[bindings.bad.match]\n'
        'type = "control_change"\n'
        'number = 21\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert bf.bindings == []
    assert "fill" in caplog.text.lower()


# -- wrong-SHAPED (but syntactically valid) values never crash the loader ----
# (self-review, before this bug got live-reproduced the way keymap.py's own
# re-review round found the analogous `keys = "oops"` class: an unhashable
# raw TOML value -- e.g. a list -- in a set-membership check raises an
# uncaught TypeError instead of a clean validation failure. Guarded in
# `_parse_binding_entry`/`_parse_match` with an isinstance check BEFORE the
# `in` test, not by widening a catch tuple.)

def test_bindingsfile_load_skips_entry_with_a_list_valued_mode(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "page.next"\n'
        'mode = ["oops"]\n'
        '[bindings.bad.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))   # must not raise
    assert bf.bindings == []
    assert "mode" in caplog.text.lower()


def test_bindingsfile_load_skips_entry_with_a_list_valued_match_type(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "page.next"\n'
        '[bindings.bad.match]\n'
        'type = ["oops"]\n'
        'number = 60\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))   # must not raise
    assert bf.bindings == []
    assert "type" in caplog.text.lower()


def test_bindingsfile_load_or_warn_never_raises_on_list_valued_mode(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.bad]\n'
        'action = "page.next"\n'
        'mode = ["oops"]\n'
        '[bindings.bad.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
    )
    bf, warning = BindingsFile.load_or_warn(str(p))   # must not raise
    assert warning is None   # a per-entry skip, not a file-level failure
    assert bf.bindings == []


def test_bindingsfile_load_or_warn_success(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text('[bindings.b1]\naction = "page.next"\n[bindings.b1.match]\n'
                 'type = "note_on"\nnumber = 60\n')
    bf, warning = BindingsFile.load_or_warn(str(p))
    assert warning is None
    assert bf.bindings[0].id == "b1"


def test_bindingsfile_load_or_warn_malformed_toml_returns_none_and_warning(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text("this is not valid toml {{{ [[[ ===\n")
    bf, warning = BindingsFile.load_or_warn(str(p))
    assert bf is None
    assert warning is not None
    assert "bindings.toml" in warning.lower()


def test_bindingsfile_load_or_warn_never_raises_when_bindings_is_wrong_shaped(tmp_path):
    p = tmp_path / "bindings.toml"
    p.write_text('bindings = 5\n')
    bf, warning = BindingsFile.load_or_warn(str(p))   # must not raise
    assert bf is None
    assert warning is not None


# -- BindingsFile: atomic save + roundtrip -------------------------------------

def test_bindingsfile_save_writes_a_machine_managed_header(tmp_path):
    p = tmp_path / "bindings.toml"
    bf = BindingsFile([], path=str(p))
    bf.save()
    text = p.read_text()
    assert text.startswith("#")
    assert "machine-managed" in text.lower()


def test_bindingsfile_save_is_atomic_no_leftover_tmp_file(tmp_path):
    p = tmp_path / "bindings.toml"
    bf = BindingsFile([trigger_binding()], path=str(p))
    bf.save()
    leftovers = [f for f in tmp_path.iterdir() if f.name != "bindings.toml"]
    assert leftovers == []


def test_bindingsfile_save_creates_parent_directory(tmp_path):
    p = tmp_path / "nested" / "dir" / "bindings.toml"
    bf = BindingsFile([trigger_binding()], path=str(p))
    bf.save()
    assert p.exists()


def test_bindingsfile_roundtrip_trigger_binding(tmp_path):
    p = tmp_path / "bindings.toml"
    original = trigger_binding(match={"type": "note_on", "number": 60, "channel": 2,
                                      "port_pattern": "Midi Through*"},
                               action="page.goto", args={"name": "harmony"}, threshold=99)
    bf = BindingsFile([original], path=str(p))
    bf.save()
    reloaded = BindingsFile.load(str(p))
    assert reloaded.bindings == [original]


def test_bindingsfile_roundtrip_continuous_binding_with_fill_marker(tmp_path):
    p = tmp_path / "bindings.toml"
    original = continuous_binding(args={"level": None}, range=(-1.0, 1.0))
    bf = BindingsFile([original], path=str(p))
    bf.save()
    reloaded = BindingsFile.load(str(p))
    assert reloaded.bindings == [original]


def test_bindingsfile_roundtrip_binding_with_device_identity(tmp_path):
    p = tmp_path / "bindings.toml"
    original = trigger_binding(
        match={"type": "note_on", "number": 60, "port_pattern": "Midi Through*",
              "device": "usb:1234:5678:SN1"})
    bf = BindingsFile([original], path=str(p))
    bf.save()
    assert 'device = "usb:1234:5678:SN1"' in p.read_text()
    reloaded = BindingsFile.load(str(p))
    assert reloaded.bindings == [original]
    assert reloaded.bindings[0].match.device == "usb:1234:5678:SN1"


def test_bindingsfile_load_tolerates_a_file_with_no_device_key_at_all(tmp_path):
    """Migration in the OTHER direction: every bindings.toml written
    before this task simply has no `device` key -- must load exactly as
    it always did, `match.device` defaulting to None."""
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.next"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
        'port_pattern = "Midi Through*"\n'
    )
    bf = BindingsFile.load(str(p))
    assert bf.bindings[0].match.device is None
    assert bf.bindings[0].match.port_pattern == "Midi Through*"


def test_bindingsfile_load_skips_entry_with_a_non_string_device(tmp_path, caplog):
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[bindings.b1]\n'
        'action = "page.next"\n'
        '\n'
        '[bindings.b1.match]\n'
        'type = "note_on"\n'
        'number = 60\n'
        'device = 5\n'
    )
    with caplog.at_level(logging.WARNING):
        bf = BindingsFile.load(str(p))
    assert bf.bindings == []
    assert "match.device" in caplog.text


def test_bindingsfile_roundtrip_multiple_bindings_preserves_all(tmp_path):
    p = tmp_path / "bindings.toml"
    b1 = trigger_binding(id="b1")
    b2 = continuous_binding(id="c1")
    bf = BindingsFile([b1, b2], path=str(p))
    bf.save()
    reloaded = BindingsFile.load(str(p))
    assert {b.id for b in reloaded.bindings} == {"b1", "c1"}


# -- BindingsFile: add/get/remove -----------------------------------------------

def test_bindingsfile_get_by_id():
    b = trigger_binding(id="b1")
    bf = BindingsFile([b])
    assert bf.get("b1") is b
    assert bf.get("nope") is None


def test_bindingsfile_remove_returns_true_and_drops_it():
    b = trigger_binding(id="b1")
    bf = BindingsFile([b])
    assert bf.remove("b1") is True
    assert bf.bindings == []


def test_bindingsfile_remove_unknown_id_returns_false_and_is_a_noop():
    b = trigger_binding(id="b1")
    bf = BindingsFile([b])
    assert bf.remove("nope") is False
    assert bf.bindings == [b]


def test_bindingsfile_add_appends():
    bf = BindingsFile([])
    b = trigger_binding(id="b1")
    bf.add(b)
    assert bf.bindings == [b]


# -- validate_binding -----------------------------------------------------------

def test_validate_binding_unknown_action():
    b = trigger_binding(action="bogus.action")
    err = validate_binding(b, {"page.next": {"description": "", "args": {}}})
    assert err is not None
    assert "bogus.action" in err


def test_validate_binding_trigger_action_with_no_args_is_valid():
    b = trigger_binding(action="page.next", args={})
    err = validate_binding(b, {"page.next": {"description": "", "args": {}}})
    assert err is None


def test_validate_binding_trigger_missing_required_arg():
    b = trigger_binding(action="page.goto", args={})
    actions = {"page.goto": {"description": "", "args": {"name": "str"}}}
    err = validate_binding(b, actions)
    assert err is not None
    assert "name" in err


def test_validate_binding_trigger_with_satisfied_args_is_valid():
    b = trigger_binding(action="page.goto", args={"name": "screensaver"})
    actions = {"page.goto": {"description": "", "args": {"name": "str"}}}
    assert validate_binding(b, actions) is None


def test_validate_binding_trigger_unknown_extra_arg():
    b = trigger_binding(action="page.next", args={"bogus": "x"})
    actions = {"page.next": {"description": "", "args": {}}}
    err = validate_binding(b, actions)
    assert err is not None
    assert "bogus" in err


def test_validate_binding_continuous_valid_fill_arg():
    b = continuous_binding(action="chrome.brightness", args={"level": None})
    actions = {"chrome.brightness": {"description": "", "args": {"level": "float"}}}
    assert validate_binding(b, actions) is None


def test_validate_binding_continuous_fill_arg_not_declared_float():
    b = continuous_binding(action="page.goto", args={"name": None})
    actions = {"page.goto": {"description": "", "args": {"name": "str"}}}
    err = validate_binding(b, actions)
    assert err is not None
    assert "float" in err.lower()


def test_validate_binding_continuous_no_fill_marker_is_invalid():
    b = continuous_binding(action="chrome.brightness", args={"level": 0.5})
    actions = {"chrome.brightness": {"description": "", "args": {"level": "float"}}}
    err = validate_binding(b, actions)
    assert err is not None


def test_validate_binding_continuous_with_extra_static_arg_satisfied():
    b = continuous_binding(action="chrome.tint", args={"channel": "warm", "level": None})
    actions = {"chrome.tint": {"description": "", "args": {"channel": "str", "level": "float"}}}
    assert validate_binding(b, actions) is None


def test_validate_binding_trigger_rejects_a_none_valued_static_arg():
    # Review fix (Minor): trigger mode has no fill-target concept at all
    # (only continuous does) -- a None-valued arg here would silently
    # coerce to the STRING "None" at dispatch time (`ActionRegistry.
    # dispatch`'s own `str(None)`), a footgun `bind.learn`'s arm-time
    # validation should catch with a clear error instead of ever reaching
    # a real MIDI event.
    b = trigger_binding(action="page.goto", args={"name": None})
    actions = {"page.goto": {"description": "", "args": {"name": "str"}}}
    err = validate_binding(b, actions)
    assert err is not None
    assert "none" in err.lower()


# -- is_learnable_event (Phase 4 Task 3, docs/phase4-notes.md) ---------------
#
# Pure MIDI-semantics predicate, deliberately placed here (not engine/core.py)
# -- mirrors this module's own "no registry, no engine, just MIDI shapes"
# charter (see module docstring): the question "is this a MIDI shape a
# binding could ever be learned from" needs no Engine/ActionRegistry access
# at all, same as `_MATCH_TYPES`/`BindingDispatcher._matches` right above.
# `Engine._handle` (engine/core.py) is the only call site -- see that
# method's own comment for why capture must be checked BEFORE the binding
# dispatcher sees the event.

def test_is_learnable_event_true_for_note_on_with_positive_velocity():
    assert is_learnable_event(ev(type="note_on", data2=100)) is True


def test_is_learnable_event_false_for_note_on_with_zero_velocity():
    # Running-status note-off, not a real trigger -- same rule
    # BindingDispatcher._trigger already applies for a stored binding.
    assert is_learnable_event(ev(type="note_on", data2=0)) is False


def test_is_learnable_event_true_for_control_change_any_value():
    assert is_learnable_event(ev(type="control_change", data1=20, data2=0)) is True
    assert is_learnable_event(ev(type="control_change", data1=20, data2=127)) is True


def test_is_learnable_event_false_for_note_off():
    assert is_learnable_event(ev(type="note_off", data2=100)) is False


# -- glob_port_pattern (Phase 5 Task 3, docs/phase5-notes.md carry-over from
# the phase-4 final review's own Important #1: "learned port_pattern
# durability") ----------------------------------------------------------------
#
# Pure string transform, same "no registry, no engine" charter as
# `is_learnable_event` right above -- `Engine._capture_learn` (engine/core.py)
# is the only production call site.

def test_glob_port_pattern_strips_the_trailing_alsa_client_port_suffix():
    pattern = glob_port_pattern("Midi Through:Midi Through Port-0 14:0")
    assert pattern == "Midi Through:Midi Through Port-0*"


def test_glob_port_pattern_still_matches_the_exact_source_it_was_learned_from():
    source = "Midi Through:Midi Through Port-0 14:0"
    assert fnmatch.fnmatch(source, glob_port_pattern(source))


def test_glob_port_pattern_matches_the_same_port_after_alsa_renumbers_it():
    """The actual durability proof: a pattern learned against one
    client:port suffix must still match the SAME logical port reported
    under a DIFFERENT suffix later (a reboot/replug/rtpmidid session
    restart) -- see this module's own module-level comment right above
    `glob_port_pattern` for why the OLD verbatim behavior broke this."""
    pattern = glob_port_pattern("Midi Through:Midi Through Port-0 14:0")
    assert fnmatch.fnmatch("Midi Through:Midi Through Port-0 23:1", pattern)
    # A genuinely DIFFERENT port (different base name) must NOT match.
    assert not fnmatch.fnmatch("USB MIDI Interface 20:0", pattern)


def test_glob_port_pattern_with_no_alsa_suffix_falls_back_to_an_exact_pattern():
    # No trailing "NN:M" -- nothing volatile to strip, so no "*" appended
    # either (never observed against real hardware, but a pure string
    # function can't assume every future source string has the suffix).
    assert glob_port_pattern("Midi Through:0") == "Midi Through:0"
    assert glob_port_pattern("A") == "A"


def test_strip_alsa_port_suffix_removes_the_trailing_client_port_numbering():
    # Phase 9 Task 1: extracted from glob_port_pattern's own suffix-
    # stripping so engine/midi_identity.py's virt: fallback can reuse the
    # exact same rule -- verified directly here, not just indirectly via
    # glob_port_pattern's own tests.
    assert (strip_alsa_port_suffix("Midi Through:Midi Through Port-0 14:0")
            == "Midi Through:Midi Through Port-0")


def test_strip_alsa_port_suffix_with_no_suffix_returns_unchanged():
    assert strip_alsa_port_suffix("Midi Through:0") == "Midi Through:0"
    assert strip_alsa_port_suffix("A") == "A"


def test_glob_port_pattern_escapes_fnmatch_specials_in_the_port_name():
    """A port name containing a literal fnmatch special character
    (`*`/`?`/`[`) must still self-match after going through
    `glob_port_pattern` -- unescaped, `[Ch1]` would be reinterpreted as an
    `fnmatch` bracket expression instead of matched literally. No real
    port name observed on this Pi's hardware contains one of these (see
    `glob_port_pattern`'s own docstring), but this proves the escaping is
    actually correct if one ever does."""
    source = "Synth [Ch1]?:Port*A 12:0"
    pattern = glob_port_pattern(source)
    assert fnmatch.fnmatch(source, pattern)
    # Proves the literal "*" in the port name is actually ESCAPED, not
    # left as a live wildcard -- if escaping had failed, this string (the
    # literal "*" swapped for an arbitrary character) would wrongly match
    # too, since an unescaped "*" matches anything.
    assert not fnmatch.fnmatch("Synth [Ch1]?:PortXA 12:0", pattern)
    # Same proof for "?" -- unescaped, fnmatch's "?" means "any single
    # character", so swapping it for a DIFFERENT single character would
    # still wrongly match if escaping had failed.
    assert not fnmatch.fnmatch("Synth [Ch1]X:Port*A 12:0", pattern)


def test_is_learnable_event_false_for_clock_tick():
    assert is_learnable_event(ev(type="clock_tick", channel=None, data1=24, data2=None)) is False


def test_is_learnable_event_false_for_program_change():
    assert is_learnable_event(ev(type="program_change", data1=5, data2=None)) is False


def test_is_learnable_event_false_for_sysex():
    assert is_learnable_event(
        ev(type="sysex", channel=None, data1=None, data2=None, sysex_data=(1, 2, 3))) is False


def test_is_learnable_event_false_for_transport_start_stop_continue_songpos():
    for transport_type in ("start", "stop", "continue", "songpos"):
        assert is_learnable_event(
            ev(type=transport_type, channel=None, data1=None, data2=None)) is False


def test_learn_timeout_s_is_thirty_seconds():
    # Task brief: "30s timeout via the engine tick path" -- a plain module
    # constant so `engine/core.py`'s `_tick_learn` and `clients/cli.py`'s
    # own wait-timeout default (engine timeout + slack) both read the SAME
    # number instead of two independently-maintained literals.
    assert LEARN_TIMEOUT_S == 30.0


# -- handle_with_origin (Phase 5 Task 1, docs/phase5-notes.md) ---------------
#
# Additive -- `handle()` itself (tested exhaustively above) is completely
# unchanged; this is the SAME matching/evaluation, just with the
# originating binding's id kept alongside each intent, for
# `Engine._dispatch_bindings` to stamp a capture action mark's
# `origin=f"binding:{binding_id}"`.

def test_handle_with_origin_returns_the_matching_bindings_id():
    b = trigger_binding(id="my-binding", match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle_with_origin(ev(data1=60)) == [("my-binding", "page.next", {})]


def test_handle_with_origin_returns_empty_list_for_no_match():
    b = trigger_binding(match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    assert d.handle_with_origin(ev(data1=61)) == []


def test_handle_with_origin_tags_each_intent_with_its_own_binding_id_when_several_match():
    b1 = trigger_binding(id="b1", action="page.next", match={"type": "note_on", "number": 60})
    b2 = trigger_binding(id="b2", action="page.prev", match={"type": "note_on", "number": 60,
                                                             "channel": 0})
    d = BindingDispatcher([b1, b2])
    intents = d.handle_with_origin(ev(data1=60, channel=0))
    assert sorted(intents, key=lambda t: t[0]) == [
        ("b1", "page.next", {}), ("b2", "page.prev", {}),
    ]


def test_handle_and_handle_with_origin_agree_modulo_the_binding_id():
    b = trigger_binding(id="b1", match={"type": "note_on", "number": 60})
    d = BindingDispatcher([b])
    e = ev(data1=60)
    plain = d.handle(e)
    with_origin = d.handle_with_origin(e)
    assert [(name, args) for _, name, args in with_origin] == plain
