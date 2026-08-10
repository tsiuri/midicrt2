"""TDD for `Engine`'s wiring of `engine/sysex_store.py::SysexStore` (Phase 9
Task 5, the SysEx MANAGER): the `overlay.sysex` chrome analyzer, the
`sysex.list/save/play/delete` client actions (registration, provenance,
ENOSPC/traversal error containment), real-event ring recording through
`Engine._handle`, and replay suppression. `SysexStore`'s OWN contract (ring
bounding, atomicity, name sanitization, library CRUD) is covered directly
in test_sysex_store.py instead -- mirrors test_capture.py/test_engine_core.py's
own "pure logic here, registry-aware engine wiring there" split.

Distinct from test_engine_sysex.py/test_engine_sysex_dispatch.py, which
cover the PRE-EXISTING, unrelated Cirklon remote-control COMMAND protocol
(`engine/sysex.py` + `Engine._handle_sysex`) -- see `engine/sysex_store.py`'s
own module docstring for the full "distinct from" writeup.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from midicrt.config import Config
from midicrt.engine.actions import ActionError
from midicrt.engine.core import Engine, MidiEvent
from midicrt.engine.replay import build_offline_engine, stream_session

FIXTURES = Path(__file__).parent / "fixtures" / "sysex_captures"


def load_syx(name: str) -> tuple[int, ...]:
    text = (FIXTURES / name).read_text().strip()
    all_bytes = [int(tok, 16) for tok in text.split()]
    assert all_bytes[0] == 0xF0 and all_bytes[-1] == 0xF7
    return tuple(all_bytes[1:-1])


def sysex_ev(data: tuple[int, ...], ts: float = 1000.0, source: str = "Cirklon") -> MidiEvent:
    return MidiEvent(ts=ts, source=source, type="sysex", channel=None,
                      data1=None, data2=None, summary=f"sysex ({len(data)} bytes)",
                      sysex_data=data)


class _FakeMidiOut:
    port_name = "fake"

    def __init__(self, ok=True):
        self.sent: list[tuple[int, ...]] = []
        self._ok = ok

    def send_sysex(self, data):
        self.sent.append(data)
        return self._ok

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


# -- action registration ------------------------------------------------------

def test_sysex_actions_are_registered_with_expected_schema():
    eng = Engine(Config())
    desc = eng.actions.describe()
    assert desc["sysex.list"]["args"] == {}
    assert desc["sysex.save"]["args"] == {"name": "str", "index": "int"}
    assert desc["sysex.play"]["args"] == {"name": "str"}
    assert desc["sysex.delete"]["args"] == {"name": "str"}


def test_sysex_overlay_topic_is_in_engine_topics():
    eng = Engine(Config())
    assert "overlay.sysex" in eng.topics


# -- real-event ring recording through Engine._handle -------------------------

def test_handle_records_a_midicrt_prefixed_frame_into_the_ring_too():
    """The MANAGER records every incoming sysex frame -- including ones
    that ALSO happen to be a valid midicrt CMD (unrelated subsystem, see
    module docstring) -- not just ones that fail `parse_command`."""
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-switch-page-0.syx")))
    recent = eng._sysex_store.recent()
    assert len(recent) == 1
    assert recent[0]["manufacturer"] == "Non-Commercial"   # 0x7D prefix


def test_handle_records_a_non_midicrt_frame_into_the_ring():
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("non-midicrt-frame.syx")))
    recent = eng._sysex_store.recent()
    assert len(recent) == 1
    assert recent[0]["size"] == 5


def test_handle_records_a_multi_kb_frame_without_error():
    eng, _fake = engine_with_fake_out()
    big = tuple(i % 128 for i in range(8192))   # 8 KiB, well within MAX_FRAME_BYTES
    eng._handle(sysex_ev(big))
    recent = eng._sysex_store.recent()
    assert recent[0]["size"] == 8192
    assert recent[0]["truncated"] is False


def test_handle_records_an_empty_truncated_frame_without_crashing():
    """A malformed/truncated real-world frame: `F0 F7` with nothing in
    between -- `sysex_data` is an empty tuple. Must not raise."""
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(()))
    recent = eng._sysex_store.recent()
    assert recent[0]["size"] == 0
    assert recent[0]["manufacturer"] == "?"


def test_self_output_filter_also_protects_the_sysex_ring():
    """Layer-2 self-subscription defense (engine/midi_out.py's own module
    docstring) sits ABOVE the ring-recording call site in `_handle` -- an
    event whose source names our own MidiOutput port never reaches
    `record_received` at all."""
    eng, _fake = engine_with_fake_out()
    own_port = eng.midi_output_port_name
    eng._handle(sysex_ev((0x7D, 0x01), source=f"RtMidiOut Client:{own_port} 142:0"))
    assert eng._sysex_store.recent() == []


# -- sysex.list / .save / .play / .delete: end-to-end dispatch ----------------

async def test_save_play_delete_round_trip_via_real_dispatch(tmp_path):
    eng, fake = engine_with_fake_out(sysex_dir=str(tmp_path / "lib"))
    eng._capture_start_action()   # so provenance marks land somewhere inspectable
    eng._handle(sysex_ev((0x7D, 0x01, 0x02, 0x03)))

    save_result = await eng.actions.dispatch("sysex.save", {"name": "my patch"},
                                             origin="client")
    assert save_result == {"name": "my patch", "size": 4}

    listing = await eng.actions.dispatch("sysex.list", {}, origin="client")
    assert listing["recent"][0]["size"] == 4
    assert [row["name"] for row in listing["library"]] == ["my patch"]

    play_result = await eng.actions.dispatch("sysex.play", {"name": "my patch"},
                                             origin="client")
    assert play_result == {"sent": True, "size": 4}
    assert fake.sent == [(0x7D, 0x01, 0x02, 0x03)]

    delete_result = await eng.actions.dispatch("sysex.delete", {"name": "my patch"},
                                                origin="client")
    assert delete_result == {"deleted": True}
    assert (await eng.actions.dispatch("sysex.list", {}, origin="client"))["library"] == []
    assert (tmp_path / "lib" / "trash" / "my patch.json").exists()

    marks = [line for line in eng._capture._buffer if line.get("kind") == "action"
             and line.get("name", "").startswith("sysex.")]
    by_name = {m["name"]: m for m in marks}
    assert by_name["sysex.save"]["origin"] == "client"
    assert by_name["sysex.save"]["args"] == {"name": "my patch", "index": 0}
    assert by_name["sysex.play"]["origin"] == "client"
    assert by_name["sysex.delete"]["origin"] == "client"


async def test_save_index_defaults_to_zero_when_omitted(tmp_path):
    eng, _fake = engine_with_fake_out(sysex_dir=str(tmp_path / "lib"))
    eng._handle(sysex_ev((0x01,)))
    eng._handle(sysex_ev((0x02, 0x03)))   # most recent
    result = await eng.actions.dispatch("sysex.save", {"name": "latest"})
    assert result["size"] == 2


# -- ENOSPC / OSError containment: must not kill the engine loop --------------

async def test_save_oserror_becomes_a_clean_action_error(tmp_path):
    eng, _fake = engine_with_fake_out(sysex_dir=str(tmp_path / "lib"))
    eng._handle(sysex_ev((0x01,)))

    def _boom(*a, **kw):
        raise OSError(28, "No space left on device")   # ENOSPC

    eng._sysex_store.save = _boom
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.save", {"name": "x"})


async def test_delete_oserror_becomes_a_clean_action_error(tmp_path):
    eng, _fake = engine_with_fake_out(sysex_dir=str(tmp_path / "lib"))

    def _boom(*a, **kw):
        raise OSError(28, "No space left on device")

    eng._sysex_store.delete = _boom
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.delete", {"name": "x"})


async def test_play_oserror_becomes_a_clean_action_error(tmp_path):
    eng, _fake = engine_with_fake_out(sysex_dir=str(tmp_path / "lib"))

    def _boom(*a, **kw):
        raise OSError(5, "I/O error")

    eng._sysex_store.play = _boom
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.play", {"name": "x"})


# -- traversal: end-to-end through real dispatch, not just the store's own
# unit tests -- proves the ActionError conversion AND that no file lands
# anywhere outside the configured library dir ----------------------------

async def test_save_traversal_name_end_to_end_raises_action_error_writes_nothing(tmp_path):
    lib = tmp_path / "lib"
    eng, _fake = engine_with_fake_out(sysex_dir=str(lib))
    eng._handle(sysex_ev((0x01,)))
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.save", {"name": "../../etc/passwd"})
    assert not (tmp_path / "etc").exists()
    assert not lib.exists() or list(lib.iterdir()) == []


async def test_play_unknown_name_raises_action_error():
    eng, _fake = engine_with_fake_out()
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.play", {"name": "nope"})


async def test_delete_unknown_name_raises_action_error():
    eng, _fake = engine_with_fake_out()
    with pytest.raises(ActionError):
        await eng.actions.dispatch("sysex.delete", {"name": "nope"})


# -- overlay.sysex: chrome data source, tick-driven decay ---------------------

def test_overlay_sysex_view_model_becomes_active_on_receipt_and_marks_dirty():
    # Review fix: overlay.sysex only activates for a REAL CMD-dispatch
    # outcome now -- a generic/malformed frame (e.g. a too-short one that
    # never even matches the midicrt PREFIX) must NOT activate it. Use a
    # real captured command fixture, matching this file's own load_syx
    # convention.
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-switch-page-0.syx"), ts=100.0))
    eng._dirty.clear()
    eng._tick_analyzers(100.0)
    assert "overlay.sysex" in eng._dirty
    vm = eng.analyzers["sysex"].view_model()
    assert vm["active"] is True
    assert vm["text"] == "sx:legacy cmd=0x01 ok page->help"   # page_id 0 -> "help"


def test_overlay_sysex_decays_after_display_window_and_marks_dirty_once():
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("legacy-switch-page-0.syx"), ts=100.0))
    eng._tick_analyzers(100.0)   # transition to active -- dirty
    eng._dirty.clear()
    eng._tick_analyzers(102.0)   # still within SYSEX_DISPLAY_SECS -- no re-dirty
    assert "overlay.sysex" not in eng._dirty
    eng._tick_analyzers(106.0)   # past the 5s window -- transition to inactive
    assert "overlay.sysex" in eng._dirty
    assert eng.analyzers["sysex"].view_model()["active"] is False


def test_overlay_sysex_stays_inactive_for_generic_non_command_traffic():
    # The MANDATORY foreign-frame-does-NOT-set-status proof, at the chrome
    # overlay level (see test_sysex_store.py's own store-level pin for the
    # same fact against SysexStore directly).
    eng, _fake = engine_with_fake_out()
    eng._handle(sysex_ev(load_syx("non-midicrt-frame.syx"), ts=100.0))
    eng._tick_analyzers(100.0)
    assert eng.analyzers["sysex"].view_model() == {"text": "", "active": False}


def test_overlay_sysex_default_view_model_before_any_activity():
    eng = Engine(Config())
    assert eng.analyzers["sysex"].view_model() == {"text": "", "active": False}


# -- replay suppression: sysex.play's real MIDI emission is structurally
# unreachable during replay -- action marks are COUNTED, never re-executed
# (docs/phase5-capture.md §6 / engine/replay.py's own module docstring) --
# the SAME mechanism that already protects capture.start/bind.learn/
# sendnotes.key, with no special-case code needed for sysex.play at all. ----

def test_replay_never_actually_plays_a_sysex_library_entry(tmp_path):
    session = tmp_path / "session.jsonl"
    lines = [
        {"kind": "header", "format": 1, "session_id": "s", "started_ts": 1000.0,
         "engine_version": "test", "instruments": []},
        {"kind": "action", "ts": 1000.0, "name": "sysex.play",
         "args": {"name": "whatever"}, "origin": "client"},
    ]
    with open(session, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)
    eng = build_offline_engine()
    assert eng._midi_out.send_sysex((0x00,)) is False   # the offline stub itself never sends
    summary = stream_session(eng, str(session), instant=True)
    assert summary["actions_by_origin"] == {"client": 1}
    # No library entry named "whatever" exists on this offline engine at
    # all -- if replay HAD genuinely re-dispatched the mark, this would
    # have raised ActionError from inside stream_session (it doesn't
    # catch action-dispatch errors -- proving the dispatch never happened).


def test_replay_still_records_incoming_sysex_frames_into_the_ring():
    """The ONE sysex-related thing replay DOES re-run is `Engine._handle`
    itself for raw "event" lines (docs/phase5-capture.md §6's "SysEx is
    the one exception") -- harmless here too: a pure in-memory ring
    append, no disk write, no real MIDI out (see engine/sysex_store.py's
    own module docstring)."""
    with tempfile.TemporaryDirectory() as d:
        session = os.path.join(d, "session.jsonl")
        lines = [
            {"kind": "header", "format": 1, "session_id": "s", "started_ts": 1000.0,
             "engine_version": "test", "instruments": []},
            {"kind": "event", "ts": 1000.0, "source": "Cirklon", "type": "sysex",
             "channel": None, "data1": None, "data2": None, "summary": "sysex (2 bytes)",
             "sysex_data": [0x7D, 0x01]},
        ]
        with open(session, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)
        eng = build_offline_engine()
        stream_session(eng, session, instant=True)
        assert len(eng._sysex_store.recent()) == 1
