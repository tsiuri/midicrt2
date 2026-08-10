"""TDD for `engine/sessions.py` -- the capture-store maintenance functions
shared by `midicrt sessions` (clients/cli.py) and the engine's own
`capture.sessions_list`/`capture.sessions_show` actions (engine/core.py).
Fed directly (no CLI subprocess, no running Engine/daemon) -- mirrors
test_capture.py's own "pure logic here, wiring elsewhere" split. CLI
plumbing is covered in test_cli_sessions.py; the two engine actions (and
their liveness wiring against a REAL CaptureSink) are covered in
test_engine_core.py; the web panel's action-based wiring is covered in
test_web_app.py.
"""
import json
import os
import threading
import time

import pytest

from midicrt.engine import capture as capture_mod
from midicrt.engine import sessions
from midicrt.engine.capture import CaptureSink


def make_event(**kw):
    """Same tiny stand-in test_capture.py::make_event uses -- CaptureSink.
    record_event only ever reads plain attributes."""
    base = {"ts": 0.0, "source": "test", "type": "note_on", "channel": 0,
            "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100",
            "clock_batch_start": None, "sysex_data": None}
    base.update(kw)
    return type("FakeEvent", (), base)()


def make_sink(tmp_path, **kw):
    return CaptureSink(capture_dir=str(tmp_path), engine_version="2.0.0.dev0-test",
                       instruments=["Test Instrument"], **kw)


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_index(capture_dir):
    with open(os.path.join(capture_dir, "index.json"), encoding="utf-8") as f:
        return json.load(f)


def record_a_session(tmp_path, *, events=3):
    """Builds one real, STOPPED session (via the real CaptureSink writer,
    not hand-rolled JSON) with `events` note_on/note_off pairs a beat
    apart, returns its session_id."""
    sink = make_sink(tmp_path)
    result = sink.start()
    ts = result["started_ts"]
    for i in range(events):
        ts += 1.0
        sink.record_event(make_event(type="note_on", ts=ts, data1=60 + i))
        ts += 0.5
        sink.record_event(make_event(type="note_off", ts=ts, data1=60 + i))
    sink.stop()
    return result["session_id"]


# -- list_sessions ------------------------------------------------------------

def test_list_sessions_empty_dir_is_empty(tmp_path):
    result = sessions.list_sessions(str(tmp_path))
    assert result == {"capture_dir": str(tmp_path), "sessions": []}


def test_list_sessions_reports_a_finished_session(tmp_path):
    sid = record_a_session(tmp_path, events=2)
    result = sessions.list_sessions(str(tmp_path))
    assert len(result["sessions"]) == 1
    row = result["sessions"][0]
    assert row["id"] == sid
    assert row["status"] == "finished"
    assert row["counts"] == {"note_on": 2, "note_off": 2}
    assert row["pinned"] is False
    assert row["size"] > 0
    assert row["ended_ts"] >= row["started_ts"]


def test_list_sessions_reports_pinned_flag(tmp_path):
    sid = record_a_session(tmp_path)
    sink = make_sink(tmp_path)
    sink.pin(sid)
    result = sessions.list_sessions(str(tmp_path))
    assert result["sessions"][0]["pinned"] is True


def test_list_sessions_missing_file_drift_is_reported_not_crashed(tmp_path):
    sid = record_a_session(tmp_path)
    os.remove(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    result = sessions.list_sessions(str(tmp_path))
    row = result["sessions"][0]
    assert row["id"] == sid
    assert row["status"] == "missing_file"
    assert row["size"] is None


def test_list_sessions_orphan_file_with_no_index_row_is_reported(tmp_path):
    sid = record_a_session(tmp_path)
    # Corrupt the index down to an empty list -- simulates an unclean
    # shutdown that lost the index row (see engine/capture.py's own
    # "loss window" docs) while the .jsonl file survived.
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump([], f)
    result = sessions.list_sessions(str(tmp_path))
    assert len(result["sessions"]) == 1
    row = result["sessions"][0]
    assert row["id"] == sid
    assert row["status"] == "orphan"
    assert row["started_ts"] is not None   # recovered from the file's own header
    assert row["ended_ts"] is None
    assert row["pinned"] is False


def test_list_sessions_labels_the_live_session_as_recording_not_orphan(tmp_path):
    sink = make_sink(tmp_path)
    result = sink.start()   # still recording -- no index row exists yet
    live_id = result["session_id"]
    listing = sessions.list_sessions(str(tmp_path), live_session_id=live_id)
    row = listing["sessions"][0]
    assert row["id"] == live_id
    assert row["status"] == "recording"
    sink.stop()


def test_list_sessions_sorted_newest_first(tmp_path):
    first = record_a_session(tmp_path)
    time.sleep(0.01)
    second = record_a_session(tmp_path)
    result = sessions.list_sessions(str(tmp_path))
    assert [r["id"] for r in result["sessions"]] == [second, first]


def test_list_sessions_never_writes_anything(tmp_path):
    record_a_session(tmp_path)
    index_path = os.path.join(str(tmp_path), "index.json")
    before = os.path.getmtime(index_path)
    time.sleep(0.01)
    sessions.list_sessions(str(tmp_path))
    assert os.path.getmtime(index_path) == before


# -- show_session ---------------------------------------------------------

def _fake_replay_fn(events_total=4, events_by_type=None, actions_by_origin=None):
    def replay_fn(path):
        return {
            "events_total": events_total,
            "events_by_type": events_by_type or {"note_on": 2, "note_off": 2},
            "actions_by_origin": actions_by_origin or {},
            "marks_by_kind": {"event": events_total},
        }
    return replay_fn


def test_show_session_unknown_id_raises(tmp_path):
    with pytest.raises(sessions.UnknownSessionError):
        sessions.show_session(str(tmp_path), "no-such-session", replay_fn=_fake_replay_fn())


def test_show_session_reports_session_level_facts(tmp_path):
    sid = record_a_session(tmp_path, events=2)
    result = sessions.show_session(str(tmp_path), sid, replay_fn=_fake_replay_fn())
    assert result["id"] == sid
    assert result["status"] == "finished"
    assert result["pinned"] is False
    assert result["duration_s"] == pytest.approx(result["ended_ts"] - result["started_ts"])
    assert result["size"] > 0


def test_show_session_reuses_the_replay_summarizer_verbatim(tmp_path):
    sid = record_a_session(tmp_path, events=2)
    fake = _fake_replay_fn(events_total=99, events_by_type={"note_on": 50, "note_off": 49})
    result = sessions.show_session(str(tmp_path), sid, replay_fn=fake)
    assert result["replay"]["events_total"] == 99
    assert result["replay"]["events_by_type"] == {"note_on": 50, "note_off": 49}


def test_show_session_default_replay_fn_actually_replays(tmp_path):
    # No injected replay_fn -- exercises the real lazy import + a real
    # offline Engine build against a real recorded session.
    sid = record_a_session(tmp_path, events=2)
    result = sessions.show_session(str(tmp_path), sid)
    assert result["replay"]["events_by_type"] == {"note_on": 2, "note_off": 2}


def test_show_session_labels_the_live_session_as_recording(tmp_path):
    sink = make_sink(tmp_path)
    started = sink.start()
    sink.record_event(make_event(ts=started["started_ts"] + 1.0))
    sink.flush()
    result = sessions.show_session(str(tmp_path), started["session_id"],
                                   live_session_id=started["session_id"],
                                   replay_fn=_fake_replay_fn())
    assert result["status"] == "recording"
    assert result["ended_ts"] is None
    sink.stop()


def test_show_session_missing_file_reports_status_without_replaying(tmp_path):
    sid = record_a_session(tmp_path)
    os.remove(os.path.join(str(tmp_path), f"{sid}.jsonl"))

    def boom(path):
        raise AssertionError("must not attempt to replay a missing file")

    result = sessions.show_session(str(tmp_path), sid, replay_fn=boom)
    assert result["status"] == "missing_file"
    assert result["replay"] is None


def test_show_session_surfaces_derived_from_when_present(tmp_path):
    sid = record_a_session(tmp_path, events=3)
    trim_result = sessions.trim_session(str(tmp_path), sid, 0.0, 10.0,
                                        now_fn=lambda: 1786169999.0,
                                        id_fn=lambda now: "session-trimmed-fixture")
    result = sessions.show_session(str(tmp_path), trim_result["id"], replay_fn=_fake_replay_fn())
    assert result["derived_from"]["session_id"] == sid
    assert result["derived_from"]["trim_from_s"] == 0.0
    assert result["derived_from"]["trim_to_s"] == 10.0


# -- trim_session ---------------------------------------------------------

def test_trim_session_creates_a_new_session_with_a_fresh_id(tmp_path):
    sid = record_a_session(tmp_path, events=3)
    result = sessions.trim_session(str(tmp_path), sid, 0.5, 2.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-1")
    assert result["id"] == "session-trim-1"
    assert result["id"] != sid
    assert os.path.exists(os.path.join(str(tmp_path), "session-trim-1.jsonl"))


def test_trim_session_leaves_the_original_file_byte_identical(tmp_path):
    sid = record_a_session(tmp_path, events=4)
    src_path = os.path.join(str(tmp_path), f"{sid}.jsonl")
    with open(src_path, "rb") as f:
        before = f.read()
    sessions.trim_session(str(tmp_path), sid, 0.5, 2.0,
                          now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-2")
    with open(src_path, "rb") as f:
        after = f.read()
    assert before == after


def test_trim_session_only_keeps_lines_within_the_relative_window(tmp_path):
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    sink.record_event(make_event(type="note_on", ts=base + 0.5, data1=1))    # before window
    sink.record_event(make_event(type="note_off", ts=base + 0.6, data1=1))  # ended before the
                                                                              # window too (NOT
                                                                              # sustained -- see
                                                                              # the dedicated
                                                                              # boundary-synthesis
                                                                              # tests for that case)
    sink.record_event(make_event(type="note_on", ts=base + 1.5, data1=2))   # inside
    sink.record_event(make_event(type="note_on", ts=base + 2.5, data1=3))   # inside
    sink.record_event(make_event(type="note_on", ts=base + 5.0, data1=4))   # after window
    sink.stop()
    sid = start["session_id"]

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 3.0,
                                   now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-3")
    assert result["counts"] == {"note_on": 2}
    lines = read_jsonl(os.path.join(str(tmp_path), "session-trim-3.jsonl"))
    kept_data1 = [line["data1"] for line in lines if line["kind"] == "event"]
    assert kept_data1 == [2, 3]


def test_trim_session_writes_provenance_in_the_new_headers_derived_from(tmp_path):
    sid = record_a_session(tmp_path, events=3)
    sessions.trim_session(str(tmp_path), sid, 0.5, 2.0,
                          now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-4")
    lines = read_jsonl(os.path.join(str(tmp_path), "session-trim-4.jsonl"))
    header = lines[0]
    assert header["kind"] == "header"
    assert header["session_id"] == "session-trim-4"
    assert header["derived_from"] == {
        "session_id": sid, "trim_from_s": 0.5, "trim_to_s": 2.0, "trimmed_at": 1786170000.0}


def test_trim_session_registers_an_index_row_for_the_new_session(tmp_path):
    sid = record_a_session(tmp_path, events=3)
    result = sessions.trim_session(str(tmp_path), sid, 0.5, 2.0,
                                   now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-5")
    rows = {r["id"]: r for r in read_index(str(tmp_path))}
    assert "session-trim-5" in rows
    assert rows["session-trim-5"]["pinned"] is False
    assert rows["session-trim-5"]["counts"] == result["counts"]


def test_trim_session_output_is_replayable(tmp_path):
    from midicrt.engine.replay import replay_session
    sid = record_a_session(tmp_path, events=4)
    result = sessions.trim_session(str(tmp_path), sid, 0.0, 100.0,
                                   now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-6")
    summary = replay_session(result["path"], instant=True)
    assert summary["events_total"] == 8   # 4 note_on + 4 note_off


# -- boundary-state synthesis: notes sustained across --from (review round 2,
# Important finding, live-reproduced by the reviewer) -------------------------

def test_trim_session_synthesizes_a_note_sustained_across_from(tmp_path):
    """The reviewer's exact reproduction: a note turned on BEFORE the
    trim window and still held (no note_off) at the window's own start
    must NOT vanish from the trimmed replay -- its sustained state must
    be synthesized at the window boundary so a replay sees it as already
    sounding, exactly like the live session did."""
    from midicrt.engine.replay import replay_session
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    # Note A (ch0 n60): on at 0.5 (BEFORE --from=1.0), off at 3.0 (inside
    # the window) -- sustained across the boundary.
    sink.record_event(make_event(type="note_on", ts=base + 0.5, channel=0,
                                 data1=60, data2=100, summary="note_on ch1 n60 v100"))
    # Note B (ch0 n64): entirely inside the window -- ordinary.
    sink.record_event(make_event(type="note_on", ts=base + 1.5, channel=0,
                                 data1=64, data2=90, summary="note_on ch1 n64 v90"))
    sink.record_event(make_event(type="note_off", ts=base + 2.5, channel=0,
                                 data1=64, data2=0, summary="note_off ch1 n64"))
    sink.record_event(make_event(type="note_off", ts=base + 3.0, channel=0,
                                 data1=60, data2=0, summary="note_off ch1 n60"))
    sink.stop()
    sid = start["session_id"]

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-sustain")
    lines = read_jsonl(result["path"])
    events = [line for line in lines if line["kind"] == "event"]

    # A synthesized note_on for note A leads the kept events, at ts ==
    # abs_start, honestly marked.
    synthetic = [e for e in events if e.get("synthetic")]
    assert len(synthetic) == 1
    assert synthetic[0]["type"] == "note_on"
    assert synthetic[0]["channel"] == 0
    assert synthetic[0]["data1"] == 60
    assert synthetic[0]["data2"] == 100   # real velocity preserved
    assert synthetic[0]["ts"] == pytest.approx(base + 1.0)   # == abs_start

    # No real captured event is ever marked synthetic.
    assert all(not e.get("synthetic") for e in events if e is not synthetic[0])

    # The replayed voice state is now CORRECT: both notes were held
    # simultaneously between 1.5 and 2.5 -- peak concurrent voices == 2,
    # not 1 (the reviewer's own reproduction of the bug: total_peak read
    # 1, note A never registering at all).
    summary = replay_session(result["path"], instant=True)
    assert summary["final_state"]["voices"]["total_peak"] == 2


def test_trim_session_does_not_synthesize_for_a_note_that_ended_before_the_window(tmp_path):
    # A note fully on-and-off BEFORE --from must NOT be synthesized --
    # only a note still ACTIVE (no note_off yet) at abs_start qualifies.
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    sink.record_event(make_event(type="note_on", ts=base + 0.1, channel=0, data1=60, data2=100))
    sink.record_event(make_event(type="note_off", ts=base + 0.3, channel=0, data1=60, data2=0))
    sink.record_event(make_event(type="note_on", ts=base + 2.0, channel=0, data1=64, data2=90))
    sink.stop()
    sid = start["session_id"]

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-no-sustain")
    lines = read_jsonl(result["path"])
    events = [line for line in lines if line["kind"] == "event"]
    assert not any(e.get("synthetic") for e in events)
    # Only note B (real, inside the window) is present.
    assert [e["data1"] for e in events] == [64]


def test_trim_session_boundary_exact_from_lands_on_a_note_on_no_duplicate_synthesis(tmp_path):
    """`--from` landing EXACTLY on a real note_on's own ts must keep that
    note_on as a REAL kept event and must NOT also synthesize a duplicate
    for the same note -- the reviewer's own explicitly-requested edge
    case."""
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    # note_on lands EXACTLY at the from=2.0 boundary.
    sink.record_event(make_event(type="note_on", ts=base + 2.0, channel=0, data1=60, data2=100))
    sink.record_event(make_event(type="note_off", ts=base + 3.0, channel=0, data1=60, data2=0))
    sink.stop()
    sid = start["session_id"]

    result = sessions.trim_session(str(tmp_path), sid, 2.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-boundary-exact")
    lines = read_jsonl(result["path"])
    events = [line for line in lines if line["kind"] == "event"]
    note_ons = [e for e in events if e["type"] == "note_on" and e["data1"] == 60]
    assert len(note_ons) == 1   # exactly one -- the real one, not duplicated
    assert note_ons[0].get("synthetic") is not True
    assert note_ons[0]["ts"] == pytest.approx(base + 2.0)


def test_trim_session_synthesizes_independently_per_channel(tmp_path):
    # Same note NUMBER on two different channels must be tracked
    # independently -- (channel, data1) is the sustain key, not data1 alone.
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    sink.record_event(make_event(type="note_on", ts=base + 0.1, channel=0, data1=60, data2=100))
    sink.record_event(make_event(type="note_on", ts=base + 0.2, channel=1, data1=60, data2=80))
    sink.record_event(make_event(type="note_off", ts=base + 0.5, channel=1, data1=60, data2=0))
    # ch1 n60 ends before the window; ch0 n60 stays held across it.
    sink.record_event(make_event(type="note_off", ts=base + 3.0, channel=0, data1=60, data2=0))
    sink.stop()
    sid = start["session_id"]

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-per-channel")
    lines = read_jsonl(result["path"])
    synthetic = [line for line in lines if line.get("synthetic")]
    assert len(synthetic) == 1
    assert synthetic[0]["channel"] == 0
    assert synthetic[0]["data1"] == 60


# -- panic-release consumption in the pre-window sustain scan (Phase 9
# close-out fix wave, reviewer-verified regression) --------------------------
#
# `engine/core.py::_release_for_panic` records a `panic.release` mark
# (`{"ch", "notes"}`, 1-INDEXED `ch` -- see that method's own docstring) for
# a note it silences internally via `_dispatch_to_state`, WITHOUT a matching
# `kind="event"` note_off line. Before this fix, trim's pre-window scan only
# ever looked at `kind="event"` lines, so a note panic already released
# BEFORE the trim window still looked "active" at the boundary (its real
# `note_on` seen, no `note_off` EVENT ever seen) -- trim would synthesize a
# bogus boundary note_on for a note that was honestly silent. `CaptureSink.
# record_action` has no `ts`-injection point (always `time.time()`), so
# these tests append the `panic.release` line directly to the raw `.jsonl`
# file (bypassing the sink) to get a precise, session-relative `ts` --
# exactly like a real capture would order it, just with one line's `ts`
# rigged for the test.

def _append_raw_line(tmp_path, session_id, line):
    path = os.path.join(str(tmp_path), f"{session_id}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def test_trim_session_does_not_synthesize_a_note_panic_already_released(tmp_path):
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    sid = start["session_id"]
    # ch0 (0-indexed) n60: turned on before the window, no real note_off --
    # but IS released by a panic.release mark (1-indexed ch=1) at 0.8s,
    # still before --from=1.0.
    sink.record_event(make_event(type="note_on", ts=base + 0.5, channel=0, data1=60, data2=100))
    sink.flush()
    _append_raw_line(tmp_path, sid, {"kind": "action", "ts": base + 0.8, "name": "panic.release",
                                     "args": {"ch": 1, "notes": [60]}, "origin": "alert"})
    sink.stop()

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-panic-released")
    lines = read_jsonl(result["path"])
    events = [line for line in lines if line["kind"] == "event"]
    assert not any(e.get("synthetic") for e in events)
    assert not any(e["data1"] == 60 for e in events)   # not present at all -- honestly silent


def test_trim_session_panic_release_channel_index_conversion_is_honored(tmp_path):
    """The reviewer's own explicitly-flagged trap: the mark's `ch` is
    1-indexed, the event/synthesis `channel` field is 0-indexed. A
    `panic.release` mark for `ch=1` (-> 0-indexed channel 0) must NOT
    release a held note on a DIFFERENT channel -- proven here by holding
    the SAME note number on channel 1 (0-indexed) too, which the mark must
    leave untouched."""
    sink = make_sink(tmp_path)
    start = sink.start()
    base = start["started_ts"]
    sid = start["session_id"]
    sink.record_event(make_event(type="note_on", ts=base + 0.5, channel=0, data1=60, data2=100))
    sink.record_event(make_event(type="note_on", ts=base + 0.6, channel=1, data1=60, data2=90))
    sink.flush()
    # ch=1 (1-indexed) -> 0-indexed channel 0 -- releases ONLY the ch0 note.
    _append_raw_line(tmp_path, sid, {"kind": "action", "ts": base + 0.8, "name": "panic.release",
                                     "args": {"ch": 1, "notes": [60]}, "origin": "alert"})
    sink.stop()

    result = sessions.trim_session(str(tmp_path), sid, 1.0, 5.0,
                                   now_fn=lambda: 1786170000.0,
                                   id_fn=lambda now: "session-trim-panic-channel")
    lines = read_jsonl(result["path"])
    synthetic = [line for line in lines if line.get("synthetic")]
    assert len(synthetic) == 1
    assert synthetic[0]["channel"] == 1   # the UNRELEASED note, on 0-indexed channel 1
    assert synthetic[0]["data1"] == 60


def test_trim_session_refuses_the_live_session(tmp_path):
    sink = make_sink(tmp_path)
    result = sink.start()
    with pytest.raises(sessions.LiveSessionError):
        sessions.trim_session(str(tmp_path), result["session_id"], 0.0, 1.0,
                              live_session_id=result["session_id"])
    sink.stop()


def test_trim_session_refusing_the_live_session_creates_no_new_file(tmp_path):
    sink = make_sink(tmp_path)
    result = sink.start()
    before = set(os.listdir(str(tmp_path)))
    with pytest.raises(sessions.LiveSessionError):
        sessions.trim_session(str(tmp_path), result["session_id"], 0.0, 1.0,
                              live_session_id=result["session_id"])
    assert set(os.listdir(str(tmp_path))) == before
    sink.stop()


def test_trim_session_rejects_inverted_range(tmp_path):
    sid = record_a_session(tmp_path)
    with pytest.raises(ValueError, match="must be greater than"):
        sessions.trim_session(str(tmp_path), sid, 5.0, 1.0)


def test_trim_session_rejects_zero_width_range(tmp_path):
    sid = record_a_session(tmp_path)
    with pytest.raises(ValueError, match="must be greater than"):
        sessions.trim_session(str(tmp_path), sid, 2.0, 2.0)


def test_trim_session_rejects_negative_from(tmp_path):
    sid = record_a_session(tmp_path)
    with pytest.raises(ValueError, match="must be >= 0"):
        sessions.trim_session(str(tmp_path), sid, -1.0, 5.0)


def test_trim_session_rejects_nan(tmp_path):
    sid = record_a_session(tmp_path)
    with pytest.raises(ValueError, match="NaN"):
        sessions.trim_session(str(tmp_path), sid, float("nan"), 5.0)


def test_trim_session_unknown_source_raises(tmp_path):
    with pytest.raises(sessions.UnknownSessionError):
        sessions.trim_session(str(tmp_path), "no-such-session", 0.0, 1.0)


def test_trim_session_to_beyond_the_recorded_span_is_graceful_not_an_error(tmp_path):
    sid = record_a_session(tmp_path, events=1)
    result = sessions.trim_session(str(tmp_path), sid, 0.0, 999999.0,
                                   now_fn=lambda: 1786170000.0, id_fn=lambda now: "session-trim-7")
    assert result["events_kept"] == 2   # note_on + note_off, the whole session


def test_trim_session_default_id_fn_produces_a_capturesink_shaped_id(tmp_path):
    sid = record_a_session(tmp_path, events=1)
    result = sessions.trim_session(str(tmp_path), sid, 0.0, 999.0)
    assert result["id"].startswith("session-")
    assert result["id"] != sid


# -- repair_index -----------------------------------------------------------

def test_repair_index_keeps_a_healthy_row_verbatim(tmp_path):
    sid = record_a_session(tmp_path, events=2)
    before = read_index(str(tmp_path))[0]
    report = sessions.repair_index(str(tmp_path))
    assert report == {"kept": [sid], "adopted": [], "dropped": [], "skipped_live": []}
    after = read_index(str(tmp_path))[0]
    assert after == before


def test_repair_index_adopts_an_orphan_file_recovering_real_counts(tmp_path):
    sid = record_a_session(tmp_path, events=3)
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump([], f)   # simulate the lost-row crash scenario
    report = sessions.repair_index(str(tmp_path))
    assert report["adopted"] == [sid]
    rows = {r["id"]: r for r in read_index(str(tmp_path))}
    assert rows[sid]["counts"] == {"note_on": 3, "note_off": 3}
    assert rows[sid]["ended_ts"] is not None
    assert rows[sid]["pinned"] is False


def test_repair_index_drops_a_row_whose_file_is_gone(tmp_path):
    sid = record_a_session(tmp_path)
    os.remove(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    report = sessions.repair_index(str(tmp_path))
    assert report["dropped"] == [sid]
    assert read_index(str(tmp_path)) == []


def test_repair_index_skips_the_live_session(tmp_path):
    sink = make_sink(tmp_path)
    result = sink.start()
    report = sessions.repair_index(str(tmp_path), live_session_id=result["session_id"])
    assert report["skipped_live"] == [result["session_id"]]
    assert report["adopted"] == []
    assert read_index(str(tmp_path)) == []   # not adopted into the index either
    sink.stop()


def test_repair_index_skipping_the_live_session_preserves_its_existing_row(tmp_path):
    # Review-round fix (Minor finding): "skipped" must mean "left exactly
    # as found," not "dropped" -- an index row for the live session's own
    # id (an edge case: ids are unique per capture in practice, but a
    # hand-restored/edited index.json could contain one) must survive a
    # repair_index run untouched, not be silently erased just because
    # this function chooses not to inspect/update it.
    sink = make_sink(tmp_path)
    result = sink.start()
    live_id = result["session_id"]
    # Plant a pre-existing row for the live id directly (contrived --
    # `repair_index` itself would never normally produce this state).
    planted_row = {"id": live_id, "started_ts": result["started_ts"],
                   "ended_ts": None, "counts": {"note_on": 1}, "pinned": True}
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump([planted_row], f)

    report = sessions.repair_index(str(tmp_path), live_session_id=live_id)
    assert report["skipped_live"] == [live_id]
    assert report["adopted"] == []
    rows = read_index(str(tmp_path))
    assert rows == [planted_row]   # preserved verbatim, not dropped
    sink.stop()


def test_repair_index_is_idempotent(tmp_path):
    record_a_session(tmp_path, events=2)
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump([], f)
    first = sessions.repair_index(str(tmp_path))
    assert first["adopted"]
    second = sessions.repair_index(str(tmp_path))
    assert second["adopted"] == []
    assert second["dropped"] == []
    assert second["kept"] == first["adopted"]


def test_repair_index_report_covers_a_mixed_store(tmp_path):
    kept_id = record_a_session(tmp_path, events=1)
    orphan_id = record_a_session(tmp_path, events=1)
    dead_id = record_a_session(tmp_path, events=1)
    os.remove(os.path.join(str(tmp_path), f"{dead_id}.jsonl"))
    rows = [r for r in read_index(str(tmp_path)) if r["id"] != orphan_id]
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f)

    report = sessions.repair_index(str(tmp_path))
    assert report["kept"] == [kept_id]
    assert report["adopted"] == [orphan_id]
    assert report["dropped"] == [dead_id]


# -- delete_session -----------------------------------------------------------

def test_delete_session_stages_the_file_to_trash(tmp_path):
    sid = record_a_session(tmp_path)
    result = sessions.delete_session(str(tmp_path), sid)
    assert result == {"deleted": True, "id": sid, "trashed": True}
    assert not os.path.exists(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    assert os.path.exists(os.path.join(str(tmp_path), "trash", f"{sid}.jsonl"))


def test_delete_session_removes_the_index_row(tmp_path):
    sid = record_a_session(tmp_path)
    sessions.delete_session(str(tmp_path), sid)
    assert read_index(str(tmp_path)) == []


def test_delete_session_refuses_a_pinned_session(tmp_path):
    sid = record_a_session(tmp_path)
    sink = make_sink(tmp_path)
    sink.pin(sid)
    with pytest.raises(sessions.PinnedSessionError):
        sessions.delete_session(str(tmp_path), sid)
    # Refusal must not touch anything.
    assert os.path.exists(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    assert read_index(str(tmp_path))[0]["pinned"] is True


def test_delete_session_refuses_the_live_session(tmp_path):
    sink = make_sink(tmp_path)
    result = sink.start()
    with pytest.raises(sessions.LiveSessionError):
        sessions.delete_session(str(tmp_path), result["session_id"],
                                live_session_id=result["session_id"])
    assert os.path.exists(os.path.join(str(tmp_path), f"{result['session_id']}.jsonl"))
    sink.stop()


def test_delete_session_unknown_id_raises(tmp_path):
    with pytest.raises(sessions.UnknownSessionError):
        sessions.delete_session(str(tmp_path), "no-such-session")


def test_delete_session_can_delete_an_orphan(tmp_path):
    sid = record_a_session(tmp_path)
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump([], f)
    result = sessions.delete_session(str(tmp_path), sid)
    assert result["trashed"] is True
    assert os.path.exists(os.path.join(str(tmp_path), "trash", f"{sid}.jsonl"))


def test_delete_session_twice_overwrites_the_trash_entry_not_raising(tmp_path):
    sid = record_a_session(tmp_path, events=1)
    sessions.delete_session(str(tmp_path), sid)
    # A second, unrelated session recorded under the SAME id is contrived
    # (ids are unique in practice) -- exercised here directly against the
    # index+trash machinery to prove the documented overwrite behavior
    # rather than relying on a real uuid collision.
    sink = make_sink(tmp_path)
    started = sink.start()
    sink.record_event(make_event(ts=started["started_ts"] + 1.0, data1=99))
    sink.stop()
    # Force the id to collide with the already-trashed one.
    collided_path = os.path.join(str(tmp_path), f"{started['session_id']}.jsonl")
    os.rename(collided_path, os.path.join(str(tmp_path), f"{sid}.jsonl"))
    rows = [r for r in read_index(str(tmp_path)) if r["id"] != started["session_id"]]
    rows.append({"id": sid, "started_ts": started["started_ts"], "ended_ts": time.time(),
                "counts": {"note_on": 1}, "pinned": False})
    with open(os.path.join(str(tmp_path), "index.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f)

    result = sessions.delete_session(str(tmp_path), sid)   # must not raise
    assert result["trashed"] is True
    trashed = read_jsonl(os.path.join(str(tmp_path), "trash", f"{sid}.jsonl"))
    assert any(line.get("data1") == 99 for line in trashed if line.get("kind") == "event")


# -- index.json locking: review-round regression tests (Important finding)
# -------------------------------------------------------------------------
# `engine/capture.py::_index_write_lock` and `engine/sessions.py::
# _index_write_lock` are two independent copies of the SAME `fcntl.flock`
# context manager (see either one's own docstring for why duplicating is
# deliberate) -- what matters for correctness is only that they target the
# identical `<capture_dir>/.index.lock` path, so a lock held by one module
# genuinely excludes the other. Both tests below were RED before the
# review-round fix landed (verified by temporarily reverting to the
# pre-fix, lock-free `trim_session`/`delete_session`/`repair_index`/
# `CaptureSink.pin`/`_update_index_on_stop`/`_sweep_retention`/`fail` and
# re-running -- both failed: the timing test observed near-zero elapsed
# time (no contention at all), and the no-lost-update test occasionally
# lost one side's write depending on scheduling, confirmed flaky-failing
# over repeated runs).

def test_index_write_lock_is_mutually_exclusive_across_both_modules(tmp_path):
    """Direct proof `sessions.py`'s plain blocking `_index_write_lock` and
    `capture.py`'s bounded, non-blocking-with-retry `_try_index_write_lock`
    (Task 6 SECOND review round -- capture.py's own lock had to become
    LOCK_NB + bounded-retry so an engine action can never block the
    asyncio loop, see that module's own docstring) still contend on the
    SAME underlying file: a sessions.py-side lock holder sleeps while
    holding it; a capture.py-side acquisition attempt started concurrently
    must observably wait for the FULL sleep before proceeding -- if the
    two locks were NOT targeting the same file (or `fcntl.flock` weren't
    actually being exercised), the second acquisition would return
    near-instantly instead. Uses a GENEROUS timeout (well past
    `hold_seconds`) so this test proves CONTENTION, not a busy-timeout."""
    capture_dir = str(tmp_path)
    hold_seconds = 0.4
    acquired_second_at = []

    def hold_sessions_lock():
        with sessions._index_write_lock(capture_dir):
            time.sleep(hold_seconds)

    holder = threading.Thread(target=hold_sessions_lock)
    holder.start()
    time.sleep(0.05)   # let the holder thread acquire the lock first

    start = time.monotonic()
    with capture_mod._try_index_write_lock(capture_dir, timeout_s=5.0):
        acquired_second_at.append(time.monotonic() - start)
    holder.join()

    # The second (capture.py-side) acquisition must have waited for AT
    # LEAST most of the holder's sleep -- generous slack for scheduling
    # jitter on a Pi, but nowhere near "returned immediately."
    assert acquired_second_at[0] >= hold_seconds * 0.6


def test_concurrent_capture_pin_and_sessions_delete_both_survive(tmp_path, monkeypatch):
    """No-lost-update proof against the REAL call chains (not the bare
    lock primitive): `CaptureSink.pin` (capture.py) and `sessions.
    delete_session` (sessions.py) racing on the SAME index.json, each
    targeting a DIFFERENT, unrelated session, must BOTH land -- neither
    a pin flip nor a delete's row-removal may be silently clobbered by
    the other's read-modify-write."""
    capture_dir = str(tmp_path)
    sink = make_sink(tmp_path)
    to_pin = record_a_session(tmp_path, events=1)
    to_delete = record_a_session(tmp_path, events=1)

    # Force sessions.delete_session's own locked write to hold the lock
    # for a real, observable duration, so CaptureSink.pin's concurrent
    # attempt has to genuinely wait rather than "happen to" interleave
    # safely by luck of thread scheduling.
    real_write = sessions._write_index_rows

    def slow_write(cdir, rows):
        time.sleep(0.3)
        real_write(cdir, rows)

    monkeypatch.setattr(sessions, "_write_index_rows", slow_write)

    results = {}

    def do_delete():
        results["delete"] = sessions.delete_session(capture_dir, to_delete)

    def do_pin():
        time.sleep(0.05)   # ensure the delete thread grabs the lock first
        results["pin"] = sink.pin(to_pin)

    t_delete = threading.Thread(target=do_delete)
    t_pin = threading.Thread(target=do_pin)
    t_delete.start()
    t_pin.start()
    t_delete.join(timeout=5)
    t_pin.join(timeout=5)

    assert results["delete"]["deleted"] is True
    assert results["pin"] == {"pinned": True, "id": to_pin}

    rows = {r["id"]: r for r in read_index(str(tmp_path))}
    assert to_delete not in rows          # the delete survived
    assert rows[to_pin]["pinned"] is True  # the pin survived, not clobbered
