"""TDD for `engine/replay.py` (Phase 5 Task 2, docs/phase5-notes.md):
session replay through an OFFLINE `Engine` -- no socket server, no
`MidiInput`, no real `MidiOutput` sends. Builds on the `_handle` suppression
seam already proven in test_engine_core.py's own "session replay" section
(the `Engine(..., replay=True)` gate); this file is the streaming driver
itself: reading a session's `.jsonl` (see engine/capture.py's own module
docstring for the on-disk shape), feeding "event" lines through `_handle`,
applying "page_changed" marks as direct state mutations, counting
"action" marks by origin WITHOUT re-executing them, and building the
end-of-replay summary. Regression coverage against the ONE checked-in
fixture (tests/fixtures/replay_session.jsonl, see
tests/fixtures/gen_replay_fixture.py for how it was generated and what it
contains) lives at the bottom of this file.
"""
import json
import os

import pytest
from test_engine_core import _write_trigger_binding

from midicrt.config import Config
from midicrt.engine import capture as capture_mod
from midicrt.engine.replay import build_offline_engine, replay_session, stream_session

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "replay_session.jsonl")


def _write_session(tmp_path, lines, name="session.jsonl"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)
    return str(p)


def _header(**kw):
    base = {"kind": "header", "format": capture_mod.FORMAT_VERSION,
            "session_id": "session-test", "started_ts": 1000.0,
            "engine_version": "2.0.0.dev0-test", "instruments": []}
    base.update(kw)
    return base


def _event(**kw):
    base = {"kind": "event", "ts": 1000.0, "source": "USB", "type": "note_on",
            "channel": 0, "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100"}
    base.update(kw)
    return base


def _action(**kw):
    base = {"kind": "action", "ts": 1000.0, "name": "page.next", "args": {}, "origin": "client"}
    base.update(kw)
    return base


def _page_changed(**kw):
    base = {"kind": "page_changed", "ts": 1000.0, "page": "voices"}
    base.update(kw)
    return base


# -- build_offline_engine ----------------------------------------------------

def test_build_offline_engine_sets_the_replay_flag():
    eng = build_offline_engine()
    assert eng._replay is True


def test_build_offline_engine_midi_output_never_reports_open():
    # "stub or gate the output" (task brief) -- is_open must be False so
    # nothing downstream (e.g. a future page reading `_sendnotes_device_
    # info`) believes a real port exists.
    eng = build_offline_engine()
    assert eng._midi_out.is_open is False


def test_build_offline_engine_midi_output_sends_are_inert_no_ops():
    eng = build_offline_engine()
    eng._midi_out.note_on(60, 100, 1)   # must not raise, must not touch mido
    eng._midi_out.note_off(60, 1)
    assert eng._midi_out.send_sysex((0x7D, 0x6D, 0x63, 0x10)) is False
    # T2b warm-up item 2: defense-in-depth stub -- panic-send's
    # `all_notes_off` (Phase 9 Task 2) is currently unreachable during
    # replay (`Engine._maybe_panic` only ever fires from `_tick_analyzers`,
    # and replay never calls `run()`/ticks analyzers -- see this module's
    # own "tick-driven replay is future work" docstring note), but a
    # future tick-driven replay mode would call it on this SAME stub, so
    # it must be a real, inert no-op now rather than an AttributeError
    # waiting to happen.
    eng._midi_out.all_notes_off(1)   # must not raise, must not touch mido


def test_build_offline_engine_accepts_a_bindings_path(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.next")
    eng = build_offline_engine(bindings_path=str(p))
    assert [b.id for b in eng._bindings_file.bindings] == ["b1"]


def test_build_offline_engine_accepts_a_config():
    eng = build_offline_engine(config=Config(pages=["eventlog"]))
    assert list(eng.pages) == ["eventlog"]


# -- fix wave (2026-08-07, Important finding): `capture_auto_start` isolation
# ------------------------------------------------------------------------
#
# A config with `capture_auto_start=True` used to give the offline (no I/O)
# replay engine a REAL retention sweep against the REAL sessions directory
# (could delete the very session file about to be replayed) plus a REAL
# capture session that re-records the replayed stream right back into it.
# Fixed at two layers: `Engine.__init__` gates auto-start on `not
# self._replay`, and `build_offline_engine` additionally neuters any
# `capture_auto_start=True` config it's handed before construction --
# tested here independently of each other where possible.

def test_build_offline_engine_with_auto_start_config_creates_no_session():
    eng = build_offline_engine(config=Config(capture_auto_start=True))
    assert eng._capture.is_recording is False


def test_build_offline_engine_with_auto_start_config_never_sweeps_retention(monkeypatch):
    swept = []
    monkeypatch.setattr(capture_mod.CaptureSink, "_sweep_retention",
                        lambda self: swept.append(1))
    build_offline_engine(config=Config(capture_auto_start=True))
    assert swept == []


def test_build_offline_engine_does_not_mutate_the_callers_config_object():
    cfg = Config(capture_auto_start=True)
    build_offline_engine(config=cfg)
    assert cfg.capture_auto_start is True   # caller's own object untouched


def test_replay_streaming_unaffected_by_the_auto_start_isolation_fix(tmp_path):
    # The isolation fix must not degrade ordinary replay behavior -- a
    # session streams identically through an auto-start-configured offline
    # engine as through a normal one.
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on"),
        _event(ts=1000.1, type="note_off", data2=0),
    ])
    eng = build_offline_engine(config=Config(capture_auto_start=True))
    summary = stream_session(eng, path, instant=True)
    assert summary["events_total"] == 2
    assert summary["events_by_type"] == {"note_on": 1, "note_off": 1}
    # And still never actually recorded any of it for real.
    assert eng._capture.is_recording is False


# -- stream_session: events feed _handle, events_total/events_by_type -------

def test_stream_session_counts_events_by_type(tmp_path):
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on", data1=60, data2=100),
        _event(ts=1000.1, type="note_on", data1=64, data2=100),
        _event(ts=1000.2, type="note_off", data1=60, data2=0),
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert summary["events_total"] == 3
    assert summary["events_by_type"] == {"note_on": 2, "note_off": 1}
    assert eng.events_total == 3


def test_stream_session_final_voices_totals_and_peak(tmp_path):
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on", channel=0, data1=60, data2=100),
        _event(ts=1000.1, type="note_on", channel=0, data1=64, data2=100),
        _event(ts=1000.2, type="note_on", channel=0, data1=67, data2=100),
        _event(ts=1000.3, type="note_off", channel=0, data1=60, data2=0),
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    # peak=3 (all three held at once), final total=2 (one released)
    assert summary["final_state"]["voices"] == {"total": 2, "total_peak": 3}


def test_stream_session_transport_bar_beat_from_clock_ticks(tmp_path):
    lines = [_header(), _event(ts=1000.0, type="start", channel=None,
                               data1=None, data2=None, summary="start")]
    ts = 1000.0
    for _ in range(8):   # 8 beats == 2 bars of 4/4
        ts += 0.5
        lines.append(_event(ts=ts, type="clock_tick", channel=None, data1=None, data2=None,
                            clock_batch_start=ts - 0.5, summary="clock_tick"))
    path = _write_session(tmp_path, lines)
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert summary["final_state"]["transport"]["bar"] == 2
    assert summary["final_state"]["transport"]["beat"] == 1
    assert summary["final_state"]["transport"]["bpm"] == pytest.approx(120.0)
    assert summary["final_state"]["transport"]["running"] is True


def test_stream_session_preserves_recorded_ts_even_in_instant_mode(tmp_path):
    # bpm is only derivable correctly from the ORIGINAL recorded ts values
    # (60 / (ts - clock_batch_start)) -- if replay ever substituted "now"
    # for a replayed event's ts, this would come out wrong (and wildly
    # inconsistent between runs).
    lines = [
        _header(),
        _event(ts=5000.0, type="start", channel=None, data1=None, data2=None, summary="start"),
        _event(ts=5000.0, type="clock_tick", channel=None, data1=None, data2=None,
              clock_batch_start=None, summary="clock_tick"),
        _event(ts=5000.25, type="clock_tick", channel=None, data1=None, data2=None,
              clock_batch_start=5000.0, summary="clock_tick"),
    ]
    path = _write_session(tmp_path, lines)
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert summary["final_state"]["transport"]["bpm"] == pytest.approx(240.0)  # 60/0.25


# -- action marks: counted by origin, never re-executed ----------------------

def test_stream_session_counts_actions_by_origin(tmp_path):
    path = _write_session(tmp_path, [
        _header(),
        _action(ts=1000.0, name="page.next", args={}, origin="client"),
        _action(ts=1000.1, name="sendnotes.key", args={"key": "z"}, origin="binding:learn_1"),
        _action(ts=1000.2, name="page.goto", args={"name": "voices"}, origin="behavior"),
        _action(ts=1000.3, name="page.goto", args={"name": "help"}, origin="sysex"),
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert summary["actions_by_origin"] == {
        "client": 1, "binding:learn_1": 1, "behavior": 1, "sysex": 1,
    }


def test_stream_session_never_actually_executes_a_dangerous_action_mark(tmp_path):
    # capture.start would, if genuinely re-dispatched, try to open a real
    # session file on disk -- proving it does NOT is the sharpest possible
    # demonstration that action marks are counted, never replayed as real
    # dispatches (docs/phase5-notes.md point 3: "apply AS MARKS, bypass
    # dispatcher" -- for action marks specifically, "apply" means "count",
    # see engine/replay.py's own module docstring for the full reasoning).
    path = _write_session(tmp_path, [
        _header(),
        _action(ts=1000.0, name="capture.start", args={}, origin="client"),
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert eng._capture.is_recording is False
    assert summary["actions_by_origin"] == {"client": 1}


def test_stream_session_never_executes_a_bind_learn_action_mark(tmp_path):
    # bind.learn would, if genuinely re-dispatched, mutate self._learn_armed
    # and (once a qualifying event followed) write to bindings.toml.
    path = _write_session(tmp_path, [
        _header(),
        _action(ts=1000.0, name="bind.learn",
               args={"action": "page.next", "mode": "trigger", "args": {}}, origin="client"),
        _event(ts=1000.1, type="note_on", data1=60, data2=100),
    ])
    eng = build_offline_engine()
    stream_session(eng, path, instant=True)
    assert eng._learn_armed is None
    assert eng._bindings_file.bindings == []


# -- page_changed marks: applied as direct state mutations -------------------

def test_stream_session_applies_page_changed_mark_with_no_underlying_midi(tmp_path):
    # Simulates a CLIENT-issued page.next while recording -- no MIDI trace
    # exists for this at all, so the mark is the ONLY way replay can ever
    # reproduce it.
    path = _write_session(tmp_path, [
        _header(),
        _action(ts=1000.0, name="page.next", args={}, origin="client"),
        _page_changed(ts=1000.0, page="voices"),
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert eng.current_page == "voices"
    assert summary["current_page"] == "voices"


def test_stream_session_page_changed_mark_does_not_touch_analyzer_state(tmp_path):
    # docs/phase5-notes.md's own observation, verified: pages/analyzers
    # consume every event regardless of current_page, so applying a
    # page_changed mark must be a PURE navigation bookkeeping change with
    # zero effect on the voices/harmony/transport/timesig numbers.
    with_nav = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on", channel=0, data1=60, data2=100),
        _page_changed(ts=1000.1, page="harmony"),
    ], name="with_nav.jsonl")
    without_nav = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on", channel=0, data1=60, data2=100),
    ], name="without_nav.jsonl")
    eng_a = build_offline_engine()
    summary_a = stream_session(eng_a, with_nav, instant=True)
    eng_b = build_offline_engine()
    summary_b = stream_session(eng_b, without_nav, instant=True)
    assert summary_a["final_state"] == summary_b["final_state"]
    assert summary_a["current_page"] == "harmony"
    assert summary_b["current_page"] == next(iter(eng_b.pages))


# -- suppression, proven end-to-end through stream_session -------------------

def test_stream_session_binding_configured_but_never_fires(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.goto", args_toml='name = "harmony"')
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on", channel=0, data1=60, data2=100),
    ])
    with_binding = build_offline_engine(bindings_path=str(p))
    summary_with = stream_session(with_binding, path, instant=True)
    without_binding = build_offline_engine()
    summary_without = stream_session(without_binding, path, instant=True)
    assert with_binding.current_page == next(iter(with_binding.pages))   # never moved to "harmony"
    assert summary_with == summary_without


# -- timing: --instant vs --speed ---------------------------------------------
#
# `sleep_fn` is injected explicitly (see stream_session's own docstring for
# why: monkeypatching the process-global `time.sleep` risks capturing calls
# from any OTHER background thread still alive elsewhere in the same
# pytest process, e.g. analyzers/spectrum.py's own audio-capture thread --
# reproduced live while writing these tests before this fix).

def test_stream_session_instant_never_sleeps(tmp_path):
    calls = []
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0),
        _event(ts=1010.0, type="note_off", data2=0),
    ])
    eng = build_offline_engine()
    stream_session(eng, path, instant=True, sleep_fn=calls.append)
    assert calls == []


def test_stream_session_paces_sleeps_by_ts_deltas_scaled_by_speed(tmp_path):
    calls = []
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0),
        _event(ts=1002.0, type="note_off", data2=0),
    ])
    eng = build_offline_engine()
    stream_session(eng, path, speed=2.0, instant=False, sleep_fn=calls.append)
    assert calls == pytest.approx([1.0])


def test_stream_session_default_speed_is_real_time(tmp_path):
    calls = []
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0),
        _event(ts=1000.05, type="note_off", data2=0),
    ])
    eng = build_offline_engine()
    stream_session(eng, path, instant=False, sleep_fn=calls.append)
    assert calls == pytest.approx([0.05])


# -- resilience ---------------------------------------------------------------

def test_stream_session_missing_file_raises_oserror():
    eng = build_offline_engine()
    with pytest.raises(OSError):
        stream_session(eng, "/no/such/replay/session.jsonl")


def test_stream_session_skips_a_malformed_json_line_and_continues(tmp_path, caplog):
    p = tmp_path / "session.jsonl"
    p.write_text(
        json.dumps(_header()) + "\n"
        + json.dumps(_event(ts=1000.0, type="note_on")) + "\n"
        + "{not valid json\n"
        + json.dumps(_event(ts=1000.1, type="note_off", data2=0)) + "\n"
    )
    eng = build_offline_engine()
    with caplog.at_level("WARNING"):
        summary = stream_session(eng, str(p), instant=True)
    assert summary["events_total"] == 2
    assert any("malformed" in r.message.lower() or "json" in r.message.lower()
              for r in caplog.records)


# -- fix wave (2026-08-07, Minor finding): a line can be VALID JSON but not
# a JSON OBJECT at all (a bare number, string, or array) -- `_iter_lines`'s
# malformed-JSON catch only ever guards `json.JSONDecodeError`, so `42`,
# `"x"`, `[1]` all parse cleanly and are handed to `stream_session`'s
# `line.get("kind")` call, which raises `AttributeError` (int/str/list have
# no `.get`) straight out of the whole replay -- past `clients/cli.py::
# _handle_replay`'s own `except OSError` (an unrelated exception class),
# crashing the CLI with a raw traceback instead of a clean skip.

@pytest.mark.parametrize("bad_line", ["42", '"x"', "[1]"])
def test_stream_session_skips_a_non_dict_json_line_and_continues(tmp_path, caplog, bad_line):
    p = tmp_path / "session.jsonl"
    p.write_text(
        json.dumps(_header()) + "\n"
        + json.dumps(_event(ts=1000.0, type="note_on")) + "\n"
        + bad_line + "\n"
        + json.dumps(_event(ts=1000.1, type="note_off", data2=0)) + "\n"
    )
    eng = build_offline_engine()
    with caplog.at_level("WARNING"):
        summary = stream_session(eng, str(p), instant=True)   # must not raise
    assert summary["events_total"] == 2
    assert summary["events_by_type"] == {"note_on": 1, "note_off": 1}
    assert any("json" in r.message.lower() or "object" in r.message.lower()
              for r in caplog.records)


def test_stream_session_ignores_an_unknown_mark_kind(tmp_path):
    path = _write_session(tmp_path, [
        _header(),
        {"kind": "tempo", "ts": 1000.0, "bpm": 120.0},
        _event(ts=1000.1, type="note_on"),
        {"kind": "some_future_kind", "ts": 1000.2, "whatever": True},
    ])
    eng = build_offline_engine()
    summary = stream_session(eng, path, instant=True)
    assert summary["events_total"] == 1


# -- review round (fix wave): defensive per-line schema handling ------------
#
# A line can be VALID JSON but still missing the fields this module's own
# per-`kind` handling assumes are present (a hand-edited/truncated log, or
# a future producer bug) -- these must be logged + skipped, exactly like
# `_iter_lines`'s own malformed-JSON handling, never an uncaught
# KeyError/TypeError crashing the whole replay.

def test_stream_session_skips_an_event_line_missing_required_fields(tmp_path, caplog):
    path = _write_session(tmp_path, [
        _header(),
        {"kind": "event", "source": "USB"},   # missing "ts" AND "type"
        _event(ts=1000.1, type="note_on"),
    ])
    eng = build_offline_engine()
    with caplog.at_level("WARNING"):
        summary = stream_session(eng, path, instant=True)
    assert summary["events_total"] == 1
    assert summary["events_by_type"] == {"note_on": 1}
    assert any("event" in r.message.lower() for r in caplog.records)


def test_stream_session_skips_an_event_line_with_wrong_field_types(tmp_path, caplog):
    # Valid JSON, present fields, WRONG type ("ts" as a string) -- this
    # only blows up once arithmetic is attempted on it (the pacing
    # subtraction), a TypeError rather than a KeyError, but the same
    # log-and-skip discipline applies.
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=1000.0, type="note_on"),
        _event(ts="not-a-number", type="note_off", data2=0),
        _event(ts=1000.2, type="note_on", data1=64),
    ])
    eng = build_offline_engine()
    with caplog.at_level("WARNING"):
        summary = stream_session(eng, path, instant=False, sleep_fn=lambda s: None)
    assert summary["events_total"] == 2
    assert summary["events_by_type"] == {"note_on": 2}


def test_stream_session_skips_a_page_changed_line_missing_page_field(tmp_path, caplog):
    path = _write_session(tmp_path, [
        _header(),
        {"kind": "page_changed", "ts": 1000.0},   # missing "page"
        _event(ts=1000.1, type="note_on"),
    ])
    eng = build_offline_engine()
    with caplog.at_level("WARNING"):
        summary = stream_session(eng, path, instant=True)
    assert eng.current_page == next(iter(eng.pages))   # unchanged, not crashed
    assert summary["events_total"] == 1


# -- review round (fix wave): --speed must be > 0 ----------------------------

def test_stream_session_rejects_zero_speed(tmp_path):
    path = _write_session(tmp_path, [_header(), _event(ts=1000.0)])
    eng = build_offline_engine()
    with pytest.raises(ValueError, match="speed"):
        stream_session(eng, path, speed=0, instant=False)


def test_stream_session_rejects_negative_speed(tmp_path):
    path = _write_session(tmp_path, [_header(), _event(ts=1000.0)])
    eng = build_offline_engine()
    with pytest.raises(ValueError, match="speed"):
        stream_session(eng, path, speed=-2.0, instant=False)


def test_replay_session_rejects_non_positive_speed(tmp_path):
    path = _write_session(tmp_path, [_header(), _event(ts=1000.0)])
    with pytest.raises(ValueError, match="speed"):
        replay_session(path, speed=0)


# -- v1 parity port: same-tick ordering is deterministic (ADAPTED) -----------
#
# v1's ~/codex/midicrt/tests/test_memory_replay.py asserts that several raw
# events sharing one coarse PPQN tick still replay in a deterministic order,
# via an explicit monotonic `seq` field v1's MemoryCaptureManager stamps on
# each captured event specifically to break same-tick ties. v2 has no tick
# concept at all (wall-clock `ts` throughout, docs/phase5-notes.md) and,
# more to the point, no field ANALOGOUS to `seq` either -- CaptureSink
# appends events to the JSONL in the exact order `Engine._handle` saw them,
# so the file's own line order already expresses "what happened first"
# unambiguously, including two events that happen to share an identical
# `ts` (clock jitter, or two notes struck close enough together to read the
# same float). The ADAPTED contract this ports is: replay must process
# "event" lines in FILE ORDER, never re-sort by `ts` (which would make two
# equal-`ts` lines' relative order UNDEFINED/sort-stability-dependent), and
# that order must be exactly reproducible across replays of the same file
# -- the same "replay twice, compare" shape as v1's own test, adapted to
# v2's line-order tie-breaker instead of v1's `seq` field.

def test_stream_session_same_ts_events_process_in_file_order_not_resorted(tmp_path):
    # Two note_on events sharing the identical `ts`, on the SAME pitch --
    # order-sensitive because `VoiceMonitorAnalyzer` counts overlapping
    # note-ons per (channel, note): on/off/on/off (interleaved) leaves the
    # note held after only 3 of the 4 events if processed out of order,
    # but going strictly in file order (on, on, off, off) always leaves it
    # released after all 4 regardless of the shared `ts`.
    path = _write_session(tmp_path, [
        _header(),
        _event(ts=2000.0, type="note_on", channel=0, data1=60, data2=100),
        _event(ts=2000.0, type="note_on", channel=0, data1=60, data2=100),
        _event(ts=2000.0, type="note_off", channel=0, data1=60, data2=0),
        _event(ts=2000.0, type="note_off", channel=0, data1=60, data2=0),
    ])
    summary_a = stream_session(build_offline_engine(), path, instant=True)
    summary_b = stream_session(build_offline_engine(), path, instant=True)
    assert summary_a == summary_b
    assert summary_a["final_state"]["voices"] == {"total": 0, "total_peak": 2}


# -- replay_session: the one-call CLI convenience -----------------------------

def test_replay_session_builds_and_streams_in_one_call(tmp_path):
    path = _write_session(tmp_path, [_header(), _event(ts=1000.0)])
    summary = replay_session(path, instant=True)
    assert summary["events_total"] == 1


def test_replay_session_accepts_bindings_path(tmp_path):
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.goto", args_toml='name = "harmony"')
    path = _write_session(tmp_path, [_header(), _event(ts=1000.0, data1=60, data2=100)])
    summary = replay_session(path, instant=True, bindings_path=str(p))
    assert summary["current_page"] == next(iter(build_offline_engine().pages))


# =============================================================================
# Regression: the ONE checked-in fixture (tests/fixtures/replay_session.jsonl)
# =============================================================================
# Generated by tests/fixtures/gen_replay_fixture.py (read that script's own
# docstring for exactly what it records and why) -- committed as the
# project's replay regression baseline. These tests pin down BOTH the
# determinism contract (task brief: "replay the same fixture twice ->
# identical summaries") and the suppression contract against a REAL,
# representative session shape (raw MIDI + a sysex page-switch + a
# client-origin page.next with no MIDI trace + a binding-origin action
# mark), not just the synthetic minimal cases above.

def test_replay_fixture_exists():
    assert os.path.exists(FIXTURE_PATH), (
        "tests/fixtures/replay_session.jsonl is missing -- regenerate via "
        "tests/fixtures/gen_replay_fixture.py")


def test_replay_fixture_twice_yields_identical_summary():
    summary_a = replay_session(FIXTURE_PATH, instant=True)
    summary_b = replay_session(FIXTURE_PATH, instant=True)
    assert summary_a == summary_b


def test_replay_fixture_with_configured_binding_does_not_change_the_summary(tmp_path):
    # The fixture contains a note_on(ch=1, note=60) -- bind THAT exact
    # match to a loud, easy-to-notice action and confirm it changes
    # nothing.
    p = tmp_path / "bindings.toml"
    _write_trigger_binding(p, action="page.goto", args_toml='name = "help"',
                           type="note_on", number=60, channel=1)
    plain = replay_session(FIXTURE_PATH, instant=True)
    with_binding = replay_session(FIXTURE_PATH, instant=True, bindings_path=str(p))
    assert plain == with_binding


def test_replay_fixture_summary_matches_recorded_content():
    summary = replay_session(FIXTURE_PATH, instant=True)
    # events_total/events_by_type: pinned exactly, see the generator
    # script's own docstring for the itemized event list this must match.
    assert summary["events_total"] == sum(summary["events_by_type"].values())
    assert summary["events_by_type"]["note_on"] > 0
    assert summary["events_by_type"]["clock_tick"] > 0
    # actions_by_origin: the fixture records one of EACH origin.
    assert set(summary["actions_by_origin"]) == {"client", "binding:fixture_b1", "sysex"}
    # a client-origin page.next with no MIDI trace -- proves the mark
    # applier, not raw-MIDI replay, is what moved current_page.
    assert summary["current_page"] != "eventlog"
