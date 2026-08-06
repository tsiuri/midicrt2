import json
import subprocess
import sys
import time

VENVPY = sys.executable


def start_daemon(sock):
    p = subprocess.Popen(
        [VENVPY, "-m", "midicrt.daemon", "--socket", sock, "--no-midi"],
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
