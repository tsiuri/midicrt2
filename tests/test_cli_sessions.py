"""Subprocess-against-the-real-`midicrt`-executable tests for `midicrt
sessions ...` (Phase 9 Task 6, capture editor). Mirrors test_cli_replay.py's
own split rationale: library-level behavior (drift handling, trim windowing,
stage-don't-delete) is exhaustively covered in test_sessions.py against
engine/sessions.py directly; these tests prove the actual CLI entry point
(argparse, `--config` resolution, exit codes, stdout/stderr contract) end to
end, PLUS the one thing that genuinely needs a real subprocess daemon: the
liveness refusal (trim/delete must never touch the session a REAL running
`midicrtd` is actively recording).

`_write_isolated_daemon_config`/`start_daemon` below are copies of test_
daemon_cli.py's own helpers (not imported from there -- that module has no
public API surface meant for cross-file reuse, and duplicating ~15 lines
here keeps this file runnable in isolation, matching how test_fb_render.py
already carries its own independent copy too)."""
import json
import os
import subprocess
import sys
import time

from midicrt.engine.capture import CaptureSink

VENVPY = sys.executable


def cli(*args, config=None):
    argv = [VENVPY, "-m", "midicrt.clients.cli"]
    if config is not None:
        argv += ["--config", config]
    argv += list(args)
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _write_config(tmp_path, *, capture_dir=None, socket_path=None) -> str:
    config_path = tmp_path / "isolated-config.toml"
    capture_dir = capture_dir if capture_dir is not None else (tmp_path / "sessions")
    lines = [
        "capture_auto_start = false\n",
        f'capture_dir = "{capture_dir}"\n',
    ]
    if socket_path is not None:
        lines.append(f'socket_path = "{socket_path}"\n')
    config_path.write_text("".join(lines))
    return str(config_path)


def _write_isolated_daemon_config(tmp_path, sock) -> str:
    return _write_config(tmp_path, capture_dir=tmp_path / "sessions", socket_path=sock)


def start_daemon(sock, tmp_path, config_path=None):
    config_path = config_path or _write_isolated_daemon_config(tmp_path, sock)
    p = subprocess.Popen(
        [VENVPY, "-m", "midicrt.daemon", "--socket", sock, "--no-midi", "--no-audio",
         "--config", config_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if subprocess.run(
            [VENVPY, "-m", "midicrt.clients.cli", "--socket", sock, "status"],
            capture_output=True, check=False).returncode == 0:
            return p
        time.sleep(0.1)
    p.terminate()
    raise RuntimeError("daemon did not come up")


# -- sessions list / repair-index: no daemon required at all -----------------

def test_sessions_list_with_no_daemon_and_empty_store_is_empty(tmp_path):
    config_path = _write_config(tmp_path)
    r = cli("sessions", "list", config=config_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["sessions"] == []


def test_sessions_repair_index_with_no_daemon_and_empty_store_is_a_noop(tmp_path):
    config_path = _write_config(tmp_path)
    r = cli("sessions", "repair-index", config=config_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data == {"kept": [], "adopted": [], "dropped": [], "skipped_live": []}


def test_sessions_show_unknown_id_exits_nonzero_with_readable_error(tmp_path):
    config_path = _write_config(tmp_path)
    r = cli("sessions", "show", "no-such-session", config=config_path)
    assert r.returncode != 0
    assert "midicrt:" in r.stderr


def test_sessions_delete_unknown_id_exits_nonzero(tmp_path):
    config_path = _write_config(tmp_path)
    r = cli("sessions", "delete", "no-such-session", config=config_path)
    assert r.returncode != 0
    assert "midicrt:" in r.stderr


def test_sessions_trim_rejects_inverted_range_via_real_argv(tmp_path):
    config_path = _write_config(tmp_path)
    r = cli("sessions", "trim", "no-such-session", "--from", "5", "--to", "1",
           config=config_path)
    assert r.returncode != 0
    assert "midicrt:" in r.stderr


# -- a real, recorded session on disk (via a real daemon, then stopped) ------
#
# `capture.start`/`.stop` via a live subprocess daemon is the honest way to
# get a REAL, CaptureSink-written session file (rather than hand-rolling
# JSONL) -- mirrors test_daemon_cli.py's own "spawn a real daemon subprocess"
# discipline throughout this file.

def _record_and_stop_a_session(sock, tmp_path, config_path):
    r = cli("--socket", sock, "action", "capture.start", config=config_path)
    assert r.returncode == 0, r.stderr
    session_id = json.loads(r.stdout)["session_id"]
    r = cli("--socket", sock, "action", "eventlog.clear", config=config_path)
    assert r.returncode == 0, r.stderr
    r = cli("--socket", sock, "action", "capture.stop", config=config_path)
    assert r.returncode == 0, r.stderr
    return session_id


def test_sessions_list_shows_a_session_recorded_by_a_real_daemon(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        session_id = _record_and_stop_a_session(sock, tmp_path, config_path)
        r = cli("--socket", sock, "sessions", "list", config=config_path)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        ids = [row["id"] for row in data["sessions"]]
        assert session_id in ids
        row = next(row for row in data["sessions"] if row["id"] == session_id)
        assert row["status"] == "finished"
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_sessions_show_summarizes_a_real_session(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        session_id = _record_and_stop_a_session(sock, tmp_path, config_path)
        r = cli("--socket", sock, "sessions", "show", session_id, config=config_path)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["id"] == session_id
        assert data["replay"] is not None
        assert "action" in data["replay"]["marks_by_kind"]
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_sessions_trim_and_the_result_replays(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        session_id = _record_and_stop_a_session(sock, tmp_path, config_path)
        r = cli("--socket", sock, "sessions", "trim", session_id,
               "--from", "0", "--to", "999999", config=config_path)
        assert r.returncode == 0, r.stderr
        trim_data = json.loads(r.stdout)
        new_id = trim_data["id"]
        assert new_id != session_id

        r = cli("replay", trim_data["path"], "--instant")
        assert r.returncode == 0, r.stderr
        summary = json.loads(r.stdout)
        # This session (no real MIDI, --no-midi daemon) has no "event"
        # lines at all -- only action marks (capture.start/eventlog.clear/
        # capture.stop) -- so the meaningful "it replayed successfully and
        # kept content" proof is the marks count, not events_total.
        assert summary["marks_by_kind"].get("action", 0) >= 2

        # Original untouched: still present, still listed.
        r = cli("--socket", sock, "sessions", "list", config=config_path)
        ids = [row["id"] for row in json.loads(r.stdout)["sessions"]]
        assert session_id in ids
        assert new_id in ids
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_sessions_delete_stages_a_real_session_to_trash(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        session_id = _record_and_stop_a_session(sock, tmp_path, config_path)
        r = cli("--socket", sock, "sessions", "delete", session_id, config=config_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["deleted"] is True
        sessions_dir = tmp_path / "sessions"
        assert not (sessions_dir / f"{session_id}.jsonl").exists()
        assert (sessions_dir / "trash" / f"{session_id}.jsonl").exists()
    finally:
        p.terminate()
        p.wait(timeout=5)


# -- liveness: the ONE thing that genuinely needs a real, RUNNING daemon ----

def test_sessions_trim_refuses_the_daemons_currently_recording_session(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        r = cli("--socket", sock, "action", "capture.start", config=config_path)
        assert r.returncode == 0, r.stderr
        live_id = json.loads(r.stdout)["session_id"]

        r = cli("--socket", sock, "sessions", "trim", live_id,
               "--from", "0", "--to", "1", config=config_path)
        assert r.returncode != 0
        assert "recording" in r.stderr.lower()

        # No new file was created from the refused trim attempt.
        sessions_dir = tmp_path / "sessions"
        jsonl_files = [f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")]
        assert jsonl_files == [f"{live_id}.jsonl"]
    finally:
        cli("--socket", sock, "action", "capture.stop", config=config_path)
        p.terminate()
        p.wait(timeout=5)


def test_sessions_delete_refuses_the_daemons_currently_recording_session(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        r = cli("--socket", sock, "action", "capture.start", config=config_path)
        assert r.returncode == 0, r.stderr
        live_id = json.loads(r.stdout)["session_id"]

        r = cli("--socket", sock, "sessions", "delete", live_id, config=config_path)
        assert r.returncode != 0
        assert "recording" in r.stderr.lower()

        sessions_dir = tmp_path / "sessions"
        assert (sessions_dir / f"{live_id}.jsonl").exists()
    finally:
        cli("--socket", sock, "action", "capture.stop", config=config_path)
        p.terminate()
        p.wait(timeout=5)


def test_sessions_list_labels_the_live_session_as_recording_via_real_daemon(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    p = start_daemon(sock, tmp_path, config_path)
    try:
        r = cli("--socket", sock, "action", "capture.start", config=config_path)
        live_id = json.loads(r.stdout)["session_id"]

        r = cli("--socket", sock, "sessions", "list", config=config_path)
        assert r.returncode == 0, r.stderr
        row = next(row for row in json.loads(r.stdout)["sessions"] if row["id"] == live_id)
        assert row["status"] == "recording"
    finally:
        cli("--socket", sock, "action", "capture.stop", config=config_path)
        p.terminate()
        p.wait(timeout=5)


def test_sessions_trim_with_no_daemon_reachable_proceeds_normally(tmp_path):
    # No daemon at all -- the liveness probe must treat "unreachable" as
    # "nothing is live", not refuse everything defensively.
    sock = str(tmp_path / "ctl.sock")   # never started
    config_path = _write_isolated_daemon_config(tmp_path, sock)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    # Build a real session file directly via CaptureSink so this test needs
    # no daemon subprocess at all.
    sink = CaptureSink(capture_dir=str(sessions_dir))
    started = sink.start()
    sink.stop()

    r = cli("sessions", "trim", started["session_id"], "--from", "0", "--to", "9999",
           config=config_path)
    assert r.returncode == 0, r.stderr
