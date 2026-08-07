"""TDD for `engine/capture.py::CaptureSink` in isolation (no `Engine`
needed) -- lifecycle, the queued writer/flush cadence, retention, header
versioning, malformed-index recovery, and `resolve_capture_dir`'s
production-vs-dev-fallback resolution. Engine-level wiring (provenance
origins at the four dispatch sites, the `rec` chrome flag, auto-start
config, wire-protocol lifecycle) is covered in test_engine_core.py/
test_server.py instead -- this file is CaptureSink's own contract, fed
directly, mirroring test_bindings.py's own "pure logic here, registry-
aware engine wiring there" split (see that file's own module comment).
"""
import json
import os
import time

import pytest

from midicrt.engine import capture as capture_mod
from midicrt.engine.capture import CaptureSink, resolve_capture_dir


def make_event(**kw):
    """A tiny stand-in for `engine.core.MidiEvent` -- `CaptureSink.
    record_event` only ever reads plain attributes (duck-typed, like every
    other analyzer/page in this codebase), so a bare namespace object
    avoids importing `engine.core` into this file at all."""
    base = {"ts": 0.0, "source": "test", "type": "note_on", "channel": 0,
            "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100",
            "clock_batch_start": None, "sysex_data": None}
    base.update(kw)
    return type("FakeEvent", (), base)()


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_index(sessions_dir):
    with open(os.path.join(sessions_dir, "index.json"), encoding="utf-8") as f:
        return json.load(f)


# -- resolve_capture_dir --------------------------------------------------

def test_resolve_capture_dir_explicit_override_wins(tmp_path):
    override = str(tmp_path / "explicit")
    assert resolve_capture_dir(override) == override


def test_resolve_capture_dir_falls_back_when_default_parent_unwritable(monkeypatch, tmp_path):
    unwritable_parent = tmp_path / "no-such-parent" / "sessions"
    fallback = tmp_path / "fallback" / "sessions"
    monkeypatch.setattr(capture_mod, "DEFAULT_STATE_DIR", str(unwritable_parent))
    monkeypatch.setattr(capture_mod, "DEV_FALLBACK_STATE_DIR", str(fallback))
    assert resolve_capture_dir(None) == str(fallback)


def test_resolve_capture_dir_uses_default_when_parent_exists_and_writable(monkeypatch, tmp_path):
    default = tmp_path / "sessions"   # tmp_path itself is the writable parent
    fallback = tmp_path / "fallback" / "sessions"
    monkeypatch.setattr(capture_mod, "DEFAULT_STATE_DIR", str(default))
    monkeypatch.setattr(capture_mod, "DEV_FALLBACK_STATE_DIR", str(fallback))
    assert resolve_capture_dir(None) == str(default)


# -- lifecycle --------------------------------------------------------------

def test_start_creates_session_file_with_header_line(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path), engine_version="2.0.0.dev0",
                       instruments=["Kawai XD5"])
    result = sink.start()
    assert sink.is_recording is True
    path = sink.session_path(result["session_id"])
    assert os.path.exists(path)
    lines = read_jsonl(path)
    assert lines[0]["kind"] == "header"
    assert lines[0]["format"] == capture_mod.FORMAT_VERSION
    assert lines[0]["session_id"] == result["session_id"]
    assert lines[0]["engine_version"] == "2.0.0.dev0"
    assert lines[0]["instruments"] == ["Kawai XD5"]
    assert "started_ts" in lines[0]


def test_status_reports_not_recording_before_start(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    status = sink.status()
    assert status["recording"] is False
    assert status["session_id"] is None


def test_status_reports_recording_and_session_id_after_start(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    status = sink.status()
    assert status["recording"] is True
    assert status["session_id"] == result["session_id"]


def test_stop_when_not_recording_is_a_harmless_noop(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.stop()
    assert result == {"session_id": None, "counts": {}}
    assert sink.is_recording is False


def test_starting_again_while_recording_finalizes_the_prior_session_first(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    first = sink.start()
    second = sink.start()
    assert first["session_id"] != second["session_id"]
    rows = read_index(tmp_path)
    assert [r["id"] for r in rows] == [first["session_id"]]


def test_stop_writes_index_row_with_counts(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="note_on"))
    sink.record_event(make_event(type="note_off"))
    sink.record_event(make_event(type="note_on"))
    result = sink.stop()
    assert result["counts"] == {"note_on": 2, "note_off": 1}
    rows = read_index(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == result["session_id"]
    assert row["counts"] == {"note_on": 2, "note_off": 1}
    assert row["pinned"] is False
    assert row["ended_ts"] >= row["started_ts"]


# -- writer: queued, flushed on a cadence (loss window is documented, not
# tested -- see engine/capture.py's own module docstring) ------------------

def test_recorded_events_are_buffered_not_written_until_flush(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path), flush_interval_s=100.0)
    result = sink.start()
    path = sink.session_path(result["session_id"])
    # start() flushes its own header immediately (see module docstring) --
    # capture the post-header line count as the baseline.
    lines_after_header = read_jsonl(path)
    sink.record_event(make_event())
    sink.record_event(make_event(type="control_change", data1=1, data2=64))
    assert read_jsonl(path) == lines_after_header   # nothing new hit disk yet
    sink.flush()
    lines = read_jsonl(path)
    assert len(lines) == len(lines_after_header) + 2
    assert lines[-2]["type"] == "note_on"
    assert lines[-1]["type"] == "control_change"


def test_maybe_flush_respects_the_cadence_interval(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path), flush_interval_s=1.0)
    result = sink.start()
    path = sink.session_path(result["session_id"])
    baseline = len(read_jsonl(path))
    sink.record_event(make_event())
    sink.maybe_flush(now=sink._last_flush_ts + 0.1)   # too soon
    assert len(read_jsonl(path)) == baseline
    sink.maybe_flush(now=sink._last_flush_ts + 1.5)   # past the interval
    assert len(read_jsonl(path)) == baseline + 1


def test_maybe_flush_is_a_noop_when_not_recording(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.maybe_flush(now=time.time())   # must not raise with no open file


def test_stop_flushes_unconditionally_regardless_of_cadence(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path), flush_interval_s=9999.0)
    result = sink.start()
    sink.record_event(make_event())
    sink.stop()
    lines = read_jsonl(sink.session_path(result["session_id"]))
    assert any(line["kind"] == "event" for line in lines)


def test_record_event_is_a_noop_when_not_recording(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.record_event(make_event())   # must not raise, nothing to flush
    assert sink.status()["counts"] == {}


# -- event fields (clock_tick, sysex) ---------------------------------------

def test_record_event_captures_clock_tick_fields(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="clock_tick", ts=1.5, clock_batch_start=1.0,
                                 data1=24, data2=None))
    sink.flush()
    lines = [line for line in read_jsonl(sink.session_path(sink.status()["session_id"]))
             if line["kind"] == "event"]
    assert lines[0]["type"] == "clock_tick"
    assert lines[0]["clock_batch_start"] == 1.0


def test_record_event_captures_sysex_data(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="sysex", sysex_data=(0xF0, 0x01, 0xF7)))
    sink.flush()
    lines = [line for line in read_jsonl(sink.session_path(sink.status()["session_id"]))
             if line["kind"] == "event"]
    assert lines[0]["sysex_data"] == [0xF0, 0x01, 0xF7]


# -- tempo marks (v1 tempo-timeline port) ------------------------------------

def test_clock_tick_records_a_tempo_mark_on_first_computable_bpm(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    # 120 BPM: a 24-clock batch spanning exactly 0.5s.
    sink.record_event(make_event(type="clock_tick", ts=0.5, clock_batch_start=0.0))
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    tempo_marks = [line for line in lines if line["kind"] == "tempo"]
    assert len(tempo_marks) == 1
    assert tempo_marks[0]["bpm"] == pytest.approx(120.0)


def test_clock_tick_does_not_repeat_a_tempo_mark_for_unchanged_bpm(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="clock_tick", ts=0.5, clock_batch_start=0.0))
    sink.record_event(make_event(type="clock_tick", ts=1.0, clock_batch_start=0.5))
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    tempo_marks = [line for line in lines if line["kind"] == "tempo"]
    assert len(tempo_marks) == 1   # same 120bpm both times -- no repeat


def test_clock_tick_records_a_new_tempo_mark_when_bpm_changes(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="clock_tick", ts=0.5, clock_batch_start=0.0))    # 120bpm
    sink.record_event(make_event(type="clock_tick", ts=1.5, clock_batch_start=0.5))    # 60bpm
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    tempo_marks = [line["bpm"] for line in lines if line["kind"] == "tempo"]
    assert tempo_marks == [pytest.approx(120.0), pytest.approx(60.0)]


def test_clock_tick_with_no_batch_start_records_no_tempo_mark(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event(type="clock_tick", ts=0.5, clock_batch_start=None))
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    assert not [line for line in lines if line["kind"] == "tempo"]


# -- action / page_changed marks (provenance is exercised at the engine
# level in test_engine_core.py; this just proves the sink writes the shape
# it's told to) --------------------------------------------------------------

def test_record_action_writes_a_mark_with_origin(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_action("page.next", {}, "binding:b1")
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    marks = [line for line in lines if line["kind"] == "action"]
    assert marks[0]["name"] == "page.next"
    assert marks[0]["args"] == {}
    assert marks[0]["origin"] == "binding:b1"
    assert "ts" in marks[0]


def test_record_action_is_a_noop_when_not_recording(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.record_action("page.next", {}, "client")   # must not raise


def test_record_page_changed_writes_a_mark(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_page_changed("voices")
    sink.flush()
    lines = read_jsonl(sink.session_path(sink.status()["session_id"]))
    marks = [line for line in lines if line["kind"] == "page_changed"]
    assert marks[0]["page"] == "voices"


# -- pin ---------------------------------------------------------------------

def test_pin_marks_a_stopped_session_pinned(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.stop()
    pin_result = sink.pin(result["session_id"])
    assert pin_result == {"pinned": True, "id": result["session_id"]}
    rows = read_index(tmp_path)
    assert rows[0]["pinned"] is True


def test_pin_unknown_session_raises_value_error(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    with pytest.raises(ValueError, match="unknown capture session"):
        sink.pin("no-such-session")


def test_pin_a_still_recording_session_raises_value_error(tmp_path):
    # No index row exists yet for an in-progress session -- see module
    # docstring's "Retention" section for why pin only targets STOPPED
    # sessions.
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    with pytest.raises(ValueError):
        sink.pin(result["session_id"])


# -- retention ----------------------------------------------------------------

def test_retention_sweep_deletes_oldest_unpinned_beyond_the_cap(tmp_path):
    # retention=3: creating 6 sessions total (N+2 relative to the 4-session
    # "steady state" the brief's own task description names -- see
    # docs/phase5-notes.md's task-1 amplification) must leave exactly the
    # newest 3 resident, oldest 3 gone -- `_sweep_retention`'s own
    # docstring explains why the target is `retention - 1` BEFORE each new
    # session is created (so the resident count settles at exactly
    # `retention`, not `retention + 1`, once that new session lands).
    sink = CaptureSink(capture_dir=str(tmp_path), retention=3)
    ids = []
    for _ in range(6):
        result = sink.start()
        ids.append(result["session_id"])
        sink.stop()
        time.sleep(0.01)   # ensure distinct started_ts ordering
    rows = read_index(tmp_path)
    remaining_ids = {r["id"] for r in rows}
    assert len(remaining_ids) == 3
    for sid in ids[:3]:
        assert sid not in remaining_ids
        assert not os.path.exists(sink.session_path(sid))
    for sid in ids[3:]:
        assert sid in remaining_ids


def test_retention_sweep_never_deletes_a_pinned_session(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path), retention=2)
    result = sink.start()
    sink.stop()
    oldest_id = result["session_id"]
    sink.pin(oldest_id)
    for _ in range(3):
        time.sleep(0.01)
        sink.start()
        sink.stop()
    rows = read_index(tmp_path)
    remaining_ids = {r["id"] for r in rows}
    assert oldest_id in remaining_ids
    assert os.path.exists(sink.session_path(oldest_id))
    # Retention still caps the UNPINNED sessions at 2, on top of the
    # immune pinned one.
    unpinned = [r for r in rows if not r["pinned"]]
    assert len(unpinned) == 2


# -- header format versioning -------------------------------------------------

def test_header_carries_format_version_constant(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    header = read_jsonl(sink.session_path(result["session_id"]))[0]
    assert header["format"] == 1
    assert capture_mod.FORMAT_VERSION == 1


# -- malformed index recovery --------------------------------------------------

def test_malformed_index_json_is_rebuilt_from_directory_scan(tmp_path, caplog):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.stop()
    # Corrupt the index by hand.
    index_path = os.path.join(tmp_path, "index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("{not valid json")

    fresh = CaptureSink(capture_dir=str(tmp_path))
    with caplog.at_level("WARNING"):
        rows = fresh._load_index()
    assert any("malformed" in r.message.lower() or "index" in r.message.lower()
              for r in caplog.records)
    assert [r["id"] for r in rows] == [result["session_id"]]
    # Rebuild is persisted -- a second load doesn't warn again.
    caplog.clear()
    with caplog.at_level("WARNING"):
        fresh._load_index()
    assert not caplog.records


def test_malformed_index_that_is_valid_json_but_not_a_list_is_rebuilt(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.stop()
    index_path = os.path.join(tmp_path, "index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"oops": "not a list"}, f)

    fresh = CaptureSink(capture_dir=str(tmp_path))
    rows = fresh._load_index()
    assert [r["id"] for r in rows] == [result["session_id"]]


def test_missing_index_json_is_just_an_empty_list(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    assert sink._load_index() == []
