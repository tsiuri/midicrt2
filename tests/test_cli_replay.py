"""Subprocess-against-the-real-`midicrt`-executable tests for the `replay`
subcommand (Phase 5 Task 2, docs/phase5-notes.md). Split out from
test_daemon_cli.py -- unlike every OTHER subcommand tested there, `replay`
needs no running `midicrtd` daemon at all (it builds its own offline engine
in-process, see engine/replay.py), so there is no `start_daemon()` fixture
to share; unlike test_cli.py's pure argparse-helper unit tests, this DOES
need a real subprocess (proving the actual `python -m midicrt.clients.cli`
entry point, argument parsing, and stdout/exit-code contract end to end).
Library-level behavior (mark application, suppression, the timing model)
is exhaustively covered in test_replay.py against `engine/replay.py`
directly; these tests only prove the CLI plumbing on top of it.
"""
import json
import os
import subprocess
import sys

VENVPY = sys.executable
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "replay_session.jsonl")


def cli(*args):
    return subprocess.run([VENVPY, "-m", "midicrt.clients.cli", *args],
                          capture_output=True, text=True, check=False)


def test_replay_instant_prints_json_summary_and_exits_zero():
    r = cli("replay", FIXTURE, "--instant")
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout)
    assert summary["events_total"] > 0
    assert "voices" in summary["final_state"]


def test_replay_missing_file_exits_nonzero_with_readable_error():
    r = cli("replay", "tests/fixtures/does-not-exist.jsonl", "--instant")
    assert r.returncode != 0
    assert "midicrt:" in r.stderr


def test_replay_needs_no_running_daemon_or_socket_argument():
    # No --socket given, no daemon started anywhere in this test -- proves
    # `replay` truly never touches the socket-resolution path the rest of
    # this CLI shares.
    r = cli("replay", FIXTURE, "--instant")
    assert r.returncode == 0, r.stderr


def test_replay_twice_via_subprocess_yields_identical_stdout():
    # The end-to-end determinism proof, through the REAL executable (not
    # just the library function) -- task brief's own acceptance criterion.
    first = cli("replay", FIXTURE, "--instant")
    second = cli("replay", FIXTURE, "--instant")
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout == second.stdout


def test_replay_speed_and_instant_are_mutually_exclusive():
    r = cli("replay", FIXTURE, "--speed", "2.0", "--instant")
    assert r.returncode != 0
    assert "not allowed" in r.stderr.lower()


# -- review round (fix wave): --speed must be > 0 ----------------------------

def test_replay_rejects_zero_speed_with_a_clean_argparse_error():
    r = cli("replay", FIXTURE, "--speed", "0")
    assert r.returncode != 0
    assert "speed" in r.stderr.lower()


def test_replay_rejects_negative_speed_with_a_clean_argparse_error():
    r = cli("replay", FIXTURE, "--speed", "-1")
    assert r.returncode != 0
    assert "speed" in r.stderr.lower()
