"""A test gateway never puts an icon on the person's desktop.

`serve --no-tray` starts no tray child; the hermetic test start shape (`--host 127.0.0.1`, a
`--port` other than the configured one, `--print-token`) starts none either, and says why in one
line. Both proven on a REAL `abstractgateway serve` subprocess (scratch HOME and data folder,
loopback scratch port): the tray decision it reports and its child processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from abstractgateway.tray_supervisor import hermetic_start_reason, tray_decision


def test_the_test_start_shape() -> None:
    assert hermetic_start_reason(cli_host="127.0.0.1", cli_port=18120, print_token=True, configured_port=8080)
    assert hermetic_start_reason(cli_host="127.0.0.1", cli_port=8080, print_token=True, configured_port=8080) is None
    assert hermetic_start_reason(cli_host="127.0.0.1", cli_port=18120, print_token=None, configured_port=8080) is None
    assert hermetic_start_reason(cli_host="127.0.0.1", cli_port=18120, print_token=False, configured_port=8080) is None
    assert hermetic_start_reason(cli_host="0.0.0.0", cli_port=18120, print_token=True, configured_port=8080) is None
    assert hermetic_start_reason(cli_host=None, cli_port=18120, print_token=True, configured_port=8080) is None
    assert hermetic_start_reason(cli_host="127.0.0.1", cli_port=9000, print_token=True, configured_port=9000) is None
    d = tray_decision(no_tray=True, no_tray_reason="a test gateway start")
    assert (d.start, d.reason) == (False, "test_gateway")
    assert tray_decision(no_tray=True).reason == "no_tray_flag"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _children(pid: int) -> list:
    out = subprocess.run(["ps", "-A", "-o", "ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == str(pid):
            rows.append(parts[1])
    return rows


def _serve(tmp_path: Path, extra: list) -> tuple:
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]
            + [str(Path(x).resolve()) for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]
        ),
        "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_RUNNER": "0",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "NO_COLOR": "1",
    }
    log_path = tmp_path / "serve.log"
    log = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), *extra],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(tmp_path),
    )
    return proc, port, data, log, log_path


def _stop(proc, log) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def _tray_state(port: int, data: Path, log_path: Path) -> dict:
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        raise AssertionError(log_path.read_text()[-3000:])
    token = (data / "auth" / "bootstrap-admin-token").read_text().strip()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/gateway/host/tray", headers={"Authorization": f"Bearer {token}"})
    time.sleep(1.0)  # a tray would be launched right after the bind: give it the chance to appear
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


@pytest.mark.parametrize(
    "extra, reason",
    [
        (["--no-tray", "--print-token"], "no_tray_flag"),
        (["--print-token"], "test_gateway"),  # the test start shape, without the flag
    ],
)
def test_a_test_gateway_spawns_no_tray(tmp_path: Path, extra: list, reason: str) -> None:
    proc, port, data, log, log_path = _serve(tmp_path, extra)
    try:
        state = _tray_state(port, data, log_path)
        assert state["decision"]["start"] is False and state["decision"]["reason"] == reason, state
        assert not state["supervisor"].get("running"), state
        assert not [c for c in _children(proc.pid) if "abstractgateway.tray" in c], _children(proc.pid)
        assert "Desktop tray: not started" in log_path.read_text()
    finally:
        _stop(proc, log)
