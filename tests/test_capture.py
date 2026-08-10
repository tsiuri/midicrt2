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
import contextlib
import fcntl
import json
import os
import threading
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


# -- status(): live counts vs last_session (Minor fix-wave finding) --------

def test_status_counts_is_empty_and_last_session_is_none_before_any_session(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    status = sink.status()
    assert status["counts"] == {}
    assert status["last_session"] is None


def test_status_reports_last_session_counts_after_stop_not_under_live_counts(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.record_event(make_event(type="note_on"))
    sink.stop()
    status = sink.status()
    # Live `counts` reflects the (now idle) active session -- empty, not a
    # stale carryover from the session that just ended.
    assert status["counts"] == {}
    assert status["last_session"]["id"] == result["session_id"]
    assert status["last_session"]["counts"] == {"note_on": 1}
    assert status["last_session"]["ended_ts"] >= status["last_session"]["started_ts"]


def test_status_last_session_persists_across_a_later_idle_period(tmp_path):
    # last_session is sticky -- it's not cleared by anything except a NEW
    # stop()/fail() (there's no "clear history" operation).
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.stop()
    first_status = sink.status()
    second_status = sink.status()
    assert first_status["last_session"] == second_status["last_session"]
    assert second_status["last_session"]["id"] == result["session_id"]


# -- fail(): write-failure containment (Critical fix-wave finding) ---------

def test_fail_disables_recording_and_clears_the_buffer(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event())   # buffered, never flushed
    result = sink.fail(OSError(28, "No space left on device"))
    assert sink.is_recording is False
    assert result["error"] == "[Errno 28] No space left on device"
    assert len(sink._buffer) == 0


def test_fail_notes_the_session_in_index_json_with_an_error_field(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.fail(OSError(28, "No space left on device"))
    rows = read_index(tmp_path)
    assert len(rows) == 1
    assert rows[0]["id"] == result["session_id"]
    assert "No space left" in rows[0]["error"]


def test_fail_reports_last_session_with_error(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.start()
    sink.fail(OSError(28, "No space left on device"))
    status = sink.status()
    assert status["recording"] is False
    assert status["last_session"]["id"] == result["session_id"]
    assert "No space left" in status["last_session"]["error"]


def test_fail_when_nothing_was_recording_is_harmless(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    result = sink.fail(OSError(28, "No space left on device"))
    assert result["session_id"] is None
    assert sink.is_recording is False
    # Nothing to note -- no session was active, so index.json is never
    # even touched (not written as an empty list either).
    assert not os.path.exists(os.path.join(tmp_path, "index.json"))


def test_fail_never_raises_even_if_the_index_write_also_fails(tmp_path, monkeypatch):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()

    def broken_save_index(rows):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sink, "_save_index", broken_save_index)
    result = sink.fail(OSError(28, "No space left on device"))   # must not raise
    assert sink.is_recording is False
    assert result["error"]


def test_a_fresh_start_after_fail_works_normally(tmp_path):
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.fail(OSError(28, "No space left on device"))
    result = sink.start()
    assert sink.is_recording is True
    sink.record_event(make_event())
    sink.flush()
    lines = read_jsonl(sink.session_path(result["session_id"]))
    assert any(line["kind"] == "event" for line in lines)


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


# -- THIRD review round (Critical finding, deterministically live-reproduced
# by an independent reviewer against an isolated engine): stop()'s own
# index-write lock-wait (engine/capture.py::_try_index_write_lock, Task 6
# SECOND review round) can now run for up to `capture_mod.
# ENGINE_LOCK_TIMEOUT_S` seconds OFF the event loop (`asyncio.to_thread`,
# engine/core.py) -- the whole point of that fix. The SECOND round's own
# `stop()` shipped the state reset (`self._recording = False`/`self._fh =
# None`) AFTER that lock-wait, not before -- a real MIDI event landing
# DURING the wait saw `recording=True` with an ALREADY-CLOSED `self._fh`,
# and the next tick-flush's `self._fh.write(...)` raised `ValueError: I/O
# operation on closed file` -- NOT an `OSError`, escaping every existing
# containment path and killing the whole engine task (MIDI processing
# stops forever, systemd still shows the daemon "active"). These tests
# hold `.index.lock` externally (a raw `os.open`+`flock`, exactly
# simulating a concurrent `midicrt sessions repair-index`/`trim`/`delete`)
# so `stop()`'s/`start()`'s own lock-wait genuinely takes real wall-clock
# time on a BACKGROUND THREAD (mirroring `asyncio.to_thread`'s actual
# concurrency shape) while THIS thread fires a real `record_event` +
# forced `maybe_flush` DURING that window -- the reviewer's exact repro,
# automated, for BOTH the `stop()` and `start()` paths (the coordinator's
# own explicitly-named coverage gap).

def _hold_index_lock(capture_dir):
    """Opens+locks `<capture_dir>/.index.lock` on THIS (test) thread's own
    fd and returns it for the caller to unlock/close later -- simulates
    "another process/operation" holding the lock, exactly like `engine/
    sessions.py`'s own blocking `_index_write_lock` would from a real
    `midicrt sessions ...` CLI invocation."""
    os.makedirs(capture_dir, exist_ok=True)
    lock_path = os.path.join(capture_dir, capture_mod._INDEX_LOCK_FILE)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_index_lock(fd):
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_stop_flips_recording_state_before_its_own_lock_wait_not_after(tmp_path):
    """Direct proof of the ORDERING fix: self._recording/self._fh must
    already reflect "stopped" WHILE stop()'s own index-write lock-wait is
    still in progress on another thread -- not only after it returns."""
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()

    lock_fd = _hold_index_lock(str(tmp_path))
    try:
        stop_thread = threading.Thread(target=sink.stop)
        stop_thread.start()
        time.sleep(0.15)   # stop() should be well into its lock-wait by now

        # Observed WHILE stop() is still blocked waiting on the
        # externally-held lock, on a SEPARATE thread:
        assert sink.is_recording is False, (
            "self._recording must already be False DURING stop()'s own "
            "lock-wait, not only after it returns -- this is the ordering "
            "fix itself")
        assert sink._fh is None
    finally:
        _release_index_lock(lock_fd)
        stop_thread.join(timeout=5)


def test_stop_lock_wait_survives_a_concurrent_event_and_forced_tick_flush(tmp_path):
    """The reviewer's exact repro, automated: a real event + a forced
    tick-flush landing DURING stop()'s own lock-wait window must never
    raise -- this was a deterministic ValueError (closed-file write)
    before the ordering fix."""
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()
    sink.record_event(make_event())
    sink.flush()

    lock_fd = _hold_index_lock(str(tmp_path))
    stop_errors = []
    tick_errors = []
    try:
        stop_thread = threading.Thread(
            target=lambda: stop_errors.append(_call_capturing_exception(sink.stop)))
        stop_thread.start()
        time.sleep(0.1)   # let stop() get past flush()/fh.close() into the lock-wait

        # A real event + forced tick-flush, repeatedly, DURING the window.
        for _ in range(5):
            exc = _call_capturing_exception(
                lambda: (sink.record_event(make_event(data1=99)),
                        sink.maybe_flush(now=time.time() + 9999)))
            if exc is not None:
                tick_errors.append(exc)
            time.sleep(0.05)
    finally:
        _release_index_lock(lock_fd)
        stop_thread.join(timeout=5)

    stop_errors = [e for e in stop_errors if e is not None]
    assert not stop_errors, f"stop() itself raised: {stop_errors}"
    assert not tick_errors, (
        f"a concurrent record_event/maybe_flush during stop()'s lock-wait "
        f"raised: {tick_errors}")
    # The event(s) recorded DURING the window (after the state reset) were
    # honestly DROPPED, not silently carried into whatever session starts
    # next -- see stop()'s own docstring for why this is the deliberate,
    # documented choice, not an oversight.
    assert len(sink._buffer) == 0


def _call_capturing_exception(fn):
    try:
        fn()
        return None
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: we want to
                              # catch AND report exactly what stop()/flush()
                              # raise here, including a bare ValueError.
        return exc


def test_start_retention_sweep_lock_wait_never_lets_a_concurrent_event_corrupt_state(tmp_path):
    """The coordinator's own explicitly-named coverage gap: start()'s own
    lock-wait (inside _sweep_retention) happens BEFORE self._recording
    ever flips True -- a record_event/maybe_flush landing during that
    window must be a harmless no-op (recording is False throughout),
    never touching a file handle that doesn't exist yet, and the event
    must be dropped, not carried into the session that starts moments
    later."""
    sink = CaptureSink(capture_dir=str(tmp_path))

    lock_fd = _hold_index_lock(str(tmp_path))
    start_errors = []
    try:
        start_thread = threading.Thread(
            target=lambda: start_errors.append(_call_capturing_exception(sink.start)))
        start_thread.start()
        time.sleep(0.15)   # start() should be inside _sweep_retention's lock-wait

        assert sink.is_recording is False   # not yet flipped True

        exc = _call_capturing_exception(
            lambda: (sink.record_event(make_event()),
                    sink.maybe_flush(now=time.time() + 9999)))
        assert exc is None, f"concurrent record_event/maybe_flush during start() raised: {exc}"
    finally:
        _release_index_lock(lock_fd)
        start_thread.join(timeout=5)

    start_errors = [e for e in start_errors if e is not None]
    assert not start_errors, f"start() itself raised: {start_errors}"
    # The event recorded before recording ever went True must be dropped.
    assert sink.status()["counts"] == {}


# -- lifecycle-lock serialization (Phase 9 close-out fix wave, item 2's
# "fuller cure") -------------------------------------------------------------
#
# Independent of engine/core.py's own `Engine.stop()` suppress-widening
# (test_engine_core.py's own proof), this is the CaptureSink-level "fuller
# cure": `start`/`stop`/`fail` now serialize against each other via
# `self._lifecycle_lock` (a `threading.RLock`), so the SIGTERM-vs-worker-
# stop race (Task 6 second review round's own `asyncio.to_thread` offload
# made this reachable in the first place) can never let two concurrent
# calls interleave their state mutations.

def test_concurrent_capture_stop_calls_are_serialized_by_the_lifecycle_lock(
        tmp_path, monkeypatch):
    """Two THREADS both calling `sink.stop()` for the SAME recording
    session, forced to genuinely overlap (a monkeypatched `flush()` sleeps
    on its first call, holding the lock the whole time) -- must never
    raise, and exactly ONE of the two actually stops a real session (the
    other cleanly sees "nothing recording", not a torn/interleaved
    partial state). Verified RED against a version of this fix with the
    lock removed (see task report for the transcript) before landing."""
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()

    real_flush = sink.flush
    call_state = {"n": 0}

    def slow_flush():
        call_state["n"] += 1
        if call_state["n"] == 1:
            time.sleep(0.2)
        return real_flush()

    monkeypatch.setattr(sink, "flush", slow_flush)

    results = []
    errors = []

    def do_stop():
        try:
            results.append(sink.stop())
        except Exception as exc:   # noqa: BLE001 -- deliberately broad, see assertion below
            errors.append(exc)

    t1 = threading.Thread(target=do_stop)
    t1.start()
    time.sleep(0.05)   # let t1 get past the recording-check and into flush()
    t2 = threading.Thread(target=do_stop)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"concurrent stop() calls raised: {errors}"
    assert len(results) == 2
    real_stops = [r for r in results if r["session_id"] is not None]
    noop_stops = [r for r in results if r["session_id"] is None]
    assert len(real_stops) == 1, (
        f"expected exactly one real stop, got {len(real_stops)}: {results}")
    assert len(noop_stops) == 1


def test_concurrent_start_and_stop_do_not_corrupt_state(tmp_path, monkeypatch):
    """A second flavor of the same race: `start()`'s own inline `self.
    stop()` (finalizing a prior session) racing a SEPARATE, concurrent
    direct `stop()` call for that SAME prior session -- must never raise,
    and the final state is consistent (a real session recording, with a
    fresh id, once both threads finish)."""
    sink = CaptureSink(capture_dir=str(tmp_path))
    sink.start()

    real_flush = sink.flush
    call_state = {"n": 0}

    def slow_flush():
        call_state["n"] += 1
        if call_state["n"] == 1:
            time.sleep(0.2)
        return real_flush()

    monkeypatch.setattr(sink, "flush", slow_flush)

    errors = []

    def do_stop():
        try:
            sink.stop()
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    def do_start():
        try:
            sink.start()
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=do_stop)
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=do_start)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"concurrent start()/stop() raised: {errors}"
    # Exactly one session ended up recording, with a real, non-None id --
    # not a corrupted half-state from the two threads interleaving.
    assert sink.is_recording is True
    assert sink._session_id is not None
    assert sink._fh is not None
    assert not sink._fh.closed
