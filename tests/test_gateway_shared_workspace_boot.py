"""Round 9 follow-up (V18): the default shared workspace is decided from the data folder AS IT WAS
before this boot wrote anything. Runs the REAL `abstractgateway serve` on an empty data folder
(hermetic: scratch HOME and data dir, runner off, no tray, loopback, no provider keys) and checks a
fresh folder gets `<data_dir>/workspace`; a folder that already held a run or a real account freezes."""

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

pytestmark = pytest.mark.basic


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve_once(tmp_path: Path, data: Path) -> dict:
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = {
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LANG": "en_US.UTF-8",
        "HF_HOME": str(home / "hf"),
        "HF_HUB_OFFLINE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data),
        "ABSTRACTGATEWAY_RUNNER": "0",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "NO_COLOR": "1",
    }
    log = (tmp_path / f"serve-{port}.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(tmp_path),
    )
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError((tmp_path / f"serve-{port}.log").read_text()[-3000:])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return json.loads((data / "config" / "runtime_config.json").read_text())


def test_the_real_boot_of_an_empty_data_folder_is_fresh(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    stored = _serve_once(tmp_path, data)
    record = stored["_migrated"]["shared_workspace_v1"]
    fresh = str((data / "workspace").resolve())
    assert record["source"] == "fresh" and record["value"] == fresh, record
    assert stored["workspace_policy"]["shared_workspace"] == fresh
    from abstractgateway.workspace_policy import gateway_policy

    assert gateway_policy(data)["shared_workspace"] == fresh and (data / "workspace").is_dir()  # created on first use
    # The boot wrote its own admin account and audit trail: a second start still changes nothing.
    assert _serve_once(tmp_path, data)["_migrated"]["shared_workspace_v1"] == record


def test_a_data_folder_with_work_freezes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import runtime_config
    from abstractgateway.workspace_policy import ensure_shared_workspace

    before = tmp_path / "before"
    before.mkdir()
    monkeypatch.setattr(runtime_config, "_workspace_root_fallback", lambda: str(before))
    monkeypatch.delenv("ABSTRACTGATEWAY_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_WORKSPACE_DIR", raising=False)

    run_dir = tmp_path / "with-run"
    run_dir.mkdir()
    (run_dir / "run_0001.json").write_text("{}")
    assert ensure_shared_workspace(run_dir) == "frozen"

    real_user = tmp_path / "with-user"
    (real_user / "auth").mkdir(parents=True)
    (real_user / "auth" / "users.json").write_text(json.dumps({"users": [
        {"tenant_id": "default", "user_id": "admin"}, {"tenant_id": "default", "user_id": "alice"}]}))
    assert ensure_shared_workspace(real_user) == "frozen"

    # Only the bootstrap operator (what a fresh boot writes itself) is NOT work.
    admin_only = tmp_path / "admin-only"
    (admin_only / "auth").mkdir(parents=True)
    (admin_only / "auth" / "users.json").write_text(json.dumps({"runtime_reservations": [], "updated_at": "x", "users": [
        {"tenant_id": "default", "user_id": "admin"}], "version": 1}))
    (admin_only / "audit_log.jsonl").write_text("{}\n")
    assert ensure_shared_workspace(admin_only) == "fresh"
