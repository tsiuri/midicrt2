import json
import subprocess
import sys
import time

VENVPY = sys.executable


def start_daemon(sock):
    p = subprocess.Popen(
        [VENVPY, "-m", "midicrt.daemon", "--socket", sock, "--no-midi", "--no-audio"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if subprocess.run(
            [VENVPY, "-m", "midicrt.clients.cli", "--socket", sock, "status"],
            capture_output=True, check=False).returncode == 0:
            return p
        time.sleep(0.1)
    p.terminate()
    raise RuntimeError("daemon did not come up")


def cli(sock, *args):
    return subprocess.run(
        [VENVPY, "-m", "midicrt.clients.cli", "--socket", sock, *args],
        capture_output=True, text=True, check=False)


def test_daemon_cli_roundtrip(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        st = json.loads(cli(sock, "status").stdout)
        assert st["page"] == "eventlog" and st["clients"] >= 1
        d = json.loads(cli(sock, "describe").stdout)
        assert "eventlog.clear" in d["actions"]
        r = cli(sock, "action", "eventlog.clear")
        assert r.returncode == 0
        r = cli(sock, "action", "bogus.action")
        assert r.returncode != 0 and "unknown action" in r.stderr
    finally:
        p.terminate()
        p.wait(timeout=5)


# -- `midicrt bind ...` (Phase 4 Task 3, docs/phase4-notes.md) ---------------
#
# `bind learn` genuinely CAPTURING a MIDI event needs a real MidiEvent to
# reach the daemon's engine queue -- there is no wire-level command that
# injects one, and this daemon subprocess runs `--no-midi` (no real ALSA
# hardware exists on the CI runner these tests execute on, see
# .github/workflows/ci.yml: ubuntu-latest, no MIDI Through port). That full
# arm->capture->persist->event proof lives in test_engine_core.py (real
# Engine, real eng.queue.put) and test_server.py (real ProtocolServer, same
# in-process queue injection, over a real socket) -- both have direct access
# to the running Engine object a separate OS process never has. What CAN be
# proven here, against the REAL `midicrt` executable/argparse/wire plumbing,
# is the arm/cancel/list/remove round trip: `bind cancel` (issued from a
# SECOND cli invocation) is the one thing that can reach an ARMED learn
# subprocess without any MIDI hardware at all.

def test_cli_bind_list_empty(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        r = cli(sock, "bind", "list")
        assert r.returncode == 0
        assert json.loads(r.stdout) == {"bindings": []}
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_cli_bind_remove_unknown_id_fails(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        r = cli(sock, "bind", "remove", "nope")
        assert r.returncode != 0
        assert "unknown binding id" in r.stderr.lower()
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_cli_bind_cancel_with_nothing_armed_reports_not_cancelled(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        r = cli(sock, "bind", "cancel")
        assert r.returncode == 0
        assert json.loads(r.stdout) == {"cancelled": False}
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_cli_bind_learn_unknown_action_fails_immediately(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        r = cli(sock, "bind", "learn", "bogus.action")
        assert r.returncode != 0
        assert "unknown action" in (r.stdout + r.stderr).lower()
    finally:
        p.terminate()
        p.wait(timeout=5)


def _bind_learn_bg(sock, *extra_args):
    return subprocess.Popen(
        [VENVPY, "-m", "midicrt.clients.cli", "--socket", sock,
         "bind", "learn", *extra_args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cli_bind_learn_arm_then_cancel_round_trip(tmp_path):
    """The one MIDI-hardware-free way to exercise `midicrt bind learn`'s
    real wait-for-event loop end to end: arm it as a background process,
    cancel it from a second `midicrt bind cancel` invocation, and confirm
    the FIRST process observes its own arm being cancelled and exits
    non-zero with a readable message -- proves the reader-thread wait,
    the learn_cancelled event delivery, and the exit-code contract all
    work through the real socket/argparse path."""
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        learn_proc = _bind_learn_bg(sock, "page.next", "--timeout", "10")
        time.sleep(0.5)   # let the arm request land before cancelling
        cancel = cli(sock, "bind", "cancel")
        assert cancel.returncode == 0
        assert json.loads(cancel.stdout) == {"cancelled": True}

        _out, err = learn_proc.communicate(timeout=5)
        assert learn_proc.returncode != 0
        assert "cancel" in err.lower()
    finally:
        learn_proc.kill()
        p.terminate()
        p.wait(timeout=5)


def test_cli_bind_learn_continuous_mode_arms_with_auto_detected_fill_arg(tmp_path):
    """`--mode continuous` with no `--arg` at all must still arm
    successfully against an action with exactly one declared float arg
    (`pianoroll.zoom_level`) -- the auto-detected-fill-key path, exercised
    through the real CLI flag parsing rather than a direct engine-level
    dispatch call."""
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        learn_proc = _bind_learn_bg(
            sock, "pianoroll.zoom_level", "--mode", "continuous", "--timeout", "10")
        time.sleep(0.5)
        cancel = cli(sock, "bind", "cancel")
        assert json.loads(cancel.stdout) == {"cancelled": True}   # proves something WAS armed

        _out, err = learn_proc.communicate(timeout=5)
        assert learn_proc.returncode != 0
        assert "cancel" in err.lower()
    finally:
        learn_proc.kill()
        p.terminate()
        p.wait(timeout=5)


def test_cli_bind_learn_trigger_with_arg_flag_arms_successfully(tmp_path):
    sock = str(tmp_path / "ctl.sock")
    p = start_daemon(sock)
    try:
        learn_proc = _bind_learn_bg(
            sock, "page.goto", "--arg", "name=harmony", "--timeout", "10")
        time.sleep(0.5)
        cancel = cli(sock, "bind", "cancel")
        assert json.loads(cancel.stdout) == {"cancelled": True}

        _out, err = learn_proc.communicate(timeout=5)
        assert learn_proc.returncode != 0
        assert "cancel" in err.lower()
    finally:
        learn_proc.kill()
        p.terminate()
        p.wait(timeout=5)
