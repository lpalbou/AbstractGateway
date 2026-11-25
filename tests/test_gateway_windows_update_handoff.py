"""Windows: Update of an installer install hands off to its own PowerShell window (2026-11-25).

Operator: on Windows the tray's Check for Updates answered "AbstractFramework 0.14.4 is
available, but not from here" and sent the person to a PowerShell line. Windows keeps a running
program's files locked, so the gateway cannot run the installer itself: the installer stops
the gateway and ends every process still running from its environment with `taskkill /T`.
Update now hands off: the checked install.ps1 and a small runner are written to
`<data dir>/update`, a short-lived launcher opens the runner in its own window and exits (so
the runner descends from nothing the installer ends), the hand-off is recorded in
`windows-update.json`, and the runner writes the installer's exit code next to it. The gateway
the installer starts again reports what happened (`windows_handoff_job`), and so do its tray
and consoles. Pins:

- detection, the check (install.ps1 from the same commit, its sha256 shown and required) and
  the action's confirmation say what happens;
- the hand-off writes exactly the checked script, records itself, refuses a second update
  while one runs, and leaves nothing behind when the window could not be opened;
- the recorded outcome: running, completed (what moved), failed (exit code or no report),
  forgotten after a day;
- on Windows, the real runner and launcher: a stand-in installer runs, outside this
  process's tree, and its exit code reaches the result file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway import self_update

pytestmark = pytest.mark.basic

UV_TOOL_PREFIX = "C:/Users/u/AppData/Roaming/uv/tools/abstractgateway"
SH = b"#!/bin/sh\n# AbstractFramework bootstrap installer (test double)\n"
PS1 = b"<#\r\n.SYNOPSIS\r\n    AbstractFramework bootstrap installer for Windows (test double)\r\n#>\r\nexit 0\r\n"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
PS1_SHA = hashlib.sha256(PS1).hexdigest()


@pytest.fixture(autouse=True)
def _reset_update_state():
    self_update._reset_for_tests()
    yield
    self_update._reset_for_tests()


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_extras", lambda: [])
    monkeypatch.setattr(self_update, "_in_docker", lambda: False)
    monkeypatch.setattr(self_update, "_direct_url_info", lambda: None)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.24.0")


def _state(data_dir: Path, framework: str = "0.14.4") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bootstrap.env").write_text(
        f"PORT=8080\nMODE=service\nPROFILE=gpu\nGATEWAY_VERSION=0.24.0\nFRAMEWORK_VERSION={framework}\nTRAY=1\n", encoding="utf-8"
    )


def _release(version: str = "0.14.5", gateway: str = "0.24.1", *, windows: bool = True) -> Dict[str, Any]:
    rel: Dict[str, Any] = {
        "ok": True,
        "offline": False,
        "commit": COMMIT,
        "version": version,
        "gateway_version": gateway,
        "python_packages": {"abstractgateway": gateway, "abstractcore": "2.39.1"},
        "manifest_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.FRAMEWORK_MANIFEST_PATH}",
        "installer_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.INSTALLER_SCRIPT_PATH}",
        "installer_bytes": SH,
    }
    if windows:
        rel["windows_installer_url"] = f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.INSTALLER_SCRIPT_PATH_WINDOWS}"
        rel["windows_installer_bytes"] = PS1
    return rel


def _win(data: Path) -> self_update.InstallInfo:
    return self_update.detect_install(executable="C:/py.exe", prefix=UV_TOOL_PREFIX, env={}, data_dir=data, platform="win32")


def _iso(seconds_ago: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)).isoformat()


# ------------------------------------------------------------------ detection, check, offer


def test_a_windows_installer_install_is_updatable_and_the_check_takes_install_ps1(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    inst = _win(data)
    assert inst.kind == "installer" and inst.upgradable and inst.windows and inst.reason is None
    check = self_update.check_for_update(force=True, info=inst, fetch_release=_release)
    assert check["update_available"] is True
    installer = check["release"]["installer"]
    # The script an Update runs on Windows, from the same commit as the manifest.
    assert installer["sha256"] == PS1_SHA and installer["url"] == self_update.INSTALLER_URL_WINDOWS
    assert installer["commit_url"].endswith(f"/{COMMIT}/scripts/install.ps1")

    view = self_update.update_view(inst, check, {"state": "idle"}, False)
    assert view["status"] == "available" and view["action"]["installer_sha256"] == PS1_SHA
    confirm = view["action"]["confirm"]
    assert "own PowerShell window" in confirm and "stops AbstractGateway" in confirm and "starts it again" in confirm
    assert view["action"]["command"].startswith("powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoOpen -NoModifyPath -AskWait 0 -DataDir ")


def test_a_check_without_the_windows_installer_is_an_error_never_an_offer(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    check = self_update.check_for_update(force=True, info=_win(data), fetch_release=lambda: _release(windows=False))
    assert check["update_available"] is None and "install.ps1" in check["error"]
    assert self_update.update_view(_win(data), check, {"state": "idle"}, False)["action"] is None


def test_the_release_fetch_takes_install_ps1_from_the_same_commit_and_checks_it_is_the_installer() -> None:
    asked: List[str] = []

    def get(url: str, accept: str, timeout_s: float) -> bytes:
        asked.append(url)
        if url == self_update.FRAMEWORK_COMMIT_API:
            return COMMIT.encode()
        if url.endswith(self_update.FRAMEWORK_MANIFEST_PATH):
            return json.dumps({"framework": {"version": "0.14.5"}, "bootstrap": {"gateway_version": "0.24.1"}}).encode()
        if url.endswith("/install.sh"):
            return SH
        if url.endswith("/install.ps1"):
            return PS1
        raise AssertionError(url)

    rel = self_update._fetch_framework_release(http_get=get, windows=True)
    assert rel["ok"] and rel["windows_installer_bytes"] == PS1
    assert rel["windows_installer_url"] == f"{self_update.FRAMEWORK_RAW}/{COMMIT}/scripts/install.ps1"
    assert all(f"/{COMMIT}/" in u for u in asked[1:])
    # Not on Windows: install.ps1 is not downloaded.
    asked.clear()
    assert "windows_installer_bytes" not in self_update._fetch_framework_release(http_get=get)
    assert not any(u.endswith(".ps1") for u in asked)

    def wrong(url: str, accept: str, timeout_s: float) -> bytes:
        return b"<html>not found</html>" if url.endswith(".ps1") else get(url, accept, timeout_s)

    bad = self_update._fetch_framework_release(http_get=wrong, windows=True)
    assert bad["ok"] is False and "is not the Windows installer script" in bad["error"]


# ------------------------------------------------------------------ the hand-off


def _start(data: Path, launched: List[Path], **kw: Any) -> Dict[str, Any]:
    inst = _win(data)
    self_update.check_for_update(force=True, info=inst, fetch_release=_release)
    return self_update.start_update(info=inst, installer_sha256=PS1_SHA, runner=lambda runner: launched.append(Path(runner)), **kw)


def test_update_writes_the_checked_script_and_its_runner_records_the_hand_off_and_opens_the_window(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    launched: List[Path] = []
    job = _start(data, launched, auto_restart=True)  # the installer restarts the gateway itself
    # "running" with the hand-off named: every client (the Rust terminal console too) follows a running job.
    assert job["state"] == "running" and job["handoff"]["id"] and "own window" in job["message"]
    assert len(launched) == 1
    runner = launched[0]
    scripts = list((data / "update").glob("install-*.ps1"))
    assert len(scripts) == 1 and scripts[0].read_bytes() == PS1, "exactly the script whose sha256 was confirmed"
    raw = runner.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "a byte-order mark, or PowerShell 5.1 garbles a non-ASCII path"
    text = raw.decode("utf-8-sig")
    assert f"'{scripts[0]}'" in text and "'-NoOpen', '-NoModifyPath', '-AskWait', '0', '-DataDir', " + f"'{data}'" in text
    assert "-NoStart" not in text, "the installer must start the gateway again"
    record = json.loads((data / "update" / self_update.WINDOWS_HANDOFF_FILE).read_text(encoding="utf-8"))
    assert record["id"] in runner.name and record["framework_before"] == "0.14.4" and record["target"] == "0.14.5"
    assert record["result_file"] in text and record["installer"]["sha256"] == PS1_SHA
    # Nothing waits on it here (no job thread); it runs until the installer reports.
    assert self_update._job_thread is None and self_update.job_running() is True
    # The runner checks the confirmed sha256 again right before running the script, records the
    # result whatever happens (finally), ignores Ctrl+C, and starts the gateway again after a failure.
    assert f"-ne '{PS1_SHA}'" in text and "Get-FileHash" in text
    assert "} finally {" in text and "TreatControlCAsInput" in text
    assert "/api/health" in text and "Start-Process -FilePath" in text
    view = self_update.update_view(_win(data), None, self_update.job_status(data_dir=str(data)), False)
    assert view["status"] == "running" and "in its own window" in view["line"]


def test_a_second_update_is_refused_while_one_runs_in_its_window(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    launched: List[Path] = []
    _start(data, launched)
    with pytest.raises(self_update.UpdateJobBusy):
        _start(data, launched)
    # A gateway that came back while the installer still finishes refuses it too.
    self_update._reset_for_tests()
    with pytest.raises(self_update.UpdateJobBusy):
        _start(data, launched)
    assert len(launched) == 1


def test_a_window_that_could_not_be_opened_leaves_no_record(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    inst = _win(data)
    self_update.check_for_update(force=True, info=inst, fetch_release=_release)

    def refuse(runner: Path) -> None:
        raise OSError("powershell.exe not found")

    with pytest.raises(self_update.UpdateNotPossible, match="could not be opened"):
        self_update.start_update(info=inst, installer_sha256=PS1_SHA, runner=refuse)
    assert not (data / "update" / self_update.WINDOWS_HANDOFF_FILE).exists()
    assert self_update.job_status(data_dir=str(data))["state"] == "idle"


def test_the_hand_off_runs_only_the_reviewed_installer(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    inst = _win(data)
    self_update.check_for_update(force=True, info=inst, fetch_release=_release)
    with pytest.raises(self_update.UpdateNotPossible, match="changed since it was checked"):
        self_update.start_update(info=inst, installer_sha256="0" * 64, runner=lambda r: None)
    with pytest.raises(self_update.UpdateNotPossible, match="check again first"):
        self_update.start_update(info=inst, installer_sha256=None, runner=lambda r: None)
    assert not (data / "update" / self_update.WINDOWS_HANDOFF_FILE).exists()


# ------------------------------------------------------------------ the outcome, after the restart


def _record(data: Path, *, started_ago: float = 60.0, framework: str = "0.14.4") -> Dict[str, Any]:
    folder = data / "update"
    folder.mkdir(parents=True, exist_ok=True)
    rec = {"id": "abc123def456", "started_at": _iso(started_ago), "framework_before": framework, "gateway_before": "0.24.0",
           "target": "0.14.5", "installer": {"sha256": PS1_SHA}, "command": ["powershell.exe"],
           "result_file": str(folder / "windows-update-abc123def456.result.json"), "log_dir": str(data / "logs")}
    (folder / self_update.WINDOWS_HANDOFF_FILE).write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _result(rec: Dict[str, Any], code: int, *, finished_ago: float = 5.0, rid: str = "") -> None:
    Path(rec["result_file"]).write_text(json.dumps({"id": rid or rec["id"], "exit_code": code, "finished_at": _iso(finished_ago)}), encoding="utf-8-sig")


def test_the_gateway_that_comes_back_reports_a_completed_update(tmp_path: Path, hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    _state(data, framework="0.14.5")  # the installer recorded the new release
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.24.1")
    rec = _record(data)
    _result(rec, 0)  # the runner writes UTF-8 with or without a byte-order mark: both read
    job = self_update.job_status(data_dir=str(data))
    assert job["state"] == "completed" and job["exit_code"] == 0 and job["handoff"]["id"] == rec["id"]
    assert job["framework_after"] == "0.14.5"
    assert {"name": "AbstractFramework", "from": "0.14.4", "to": "0.14.5"} in job["changes"]
    assert {"name": "abstractgateway", "from": "0.24.0", "to": "0.24.1"} in job["changes"]
    assert "is installed and running" in job["message"]
    view = self_update.update_view(_win(data), None, job, False)
    assert view["status"] == "up_to_date" and "updated:" in view["line"] and view["action"] is None


def test_a_failed_installer_and_a_window_that_never_reported_are_failures_with_the_log_folder(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    rec = _record(data)
    _result(rec, 1)
    job = self_update.job_status(data_dir=str(data))
    assert job["state"] == "failed" and job["exit_code"] == 1 and str(data / "logs") in job["error"]
    assert self_update.update_view(_win(data), None, job, False)["status"] == "failed"

    # No result after the time limit: the window was closed or the computer restarted.
    data2 = tmp_path / "data2"
    _state(data2)
    _record(data2, started_ago=self_update.WINDOWS_HANDOFF_TIMEOUT_S + 60)
    late = self_update.job_status(data_dir=str(data2))
    assert late["state"] == "failed" and "did not report back" in late["error"]

    # Still within the time limit: running (the installer may be finishing after the restart).
    data3 = tmp_path / "data3"
    _state(data3)
    _record(data3, started_ago=30)
    assert self_update.job_status(data_dir=str(data3))["state"] == "running"


def test_old_or_foreign_results_are_not_reported(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    rec = _record(data, started_ago=3 * 86400)
    _result(rec, 0, finished_ago=2 * 86400)
    assert self_update.job_status(data_dir=str(data))["state"] == "idle", "a day later the outcome is no longer news"
    rec = _record(data)
    _result(rec, 0, rid="another-run")
    assert self_update.job_status(data_dir=str(data))["state"] == "running", "a result of another hand-off is not this one's"
    # Not Windows, or no data dir: this process's own job only.
    assert self_update.job_status()["state"] == "idle"


# ------------------------------------------------------------------ the real runner and launcher


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows PowerShell")
def test_the_runner_runs_the_installer_outside_this_process_and_records_its_exit_code(tmp_path: Path) -> None:
    """The runner and launcher as the gateway uses them, with a stand-in installer that records
    its parent chain and exits 7. Hidden window and no pause, so nothing shows on the desktop."""
    folder = tmp_path / "update dir with spaces and José's name"
    folder.mkdir()
    chain = folder / "chain.json"
    fake = folder / "fake installer.ps1"
    fake.write_text(
        "param([string]$DataDir, [switch]$NoOpen, [switch]$NoModifyPath, [int]$AskWait = 25)\r\n"
        "$me = Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\"\r\n"
        "$runner = Get-CimInstance Win32_Process -Filter \"ProcessId=$($me.ParentProcessId)\"\r\n"
        "@{ data_dir = $DataDir; no_open = [bool]$NoOpen; ask_wait = $AskWait; runner_parent = [int]$runner.ParentProcessId } | ConvertTo-Json | Set-Content -LiteralPath $env:AF_TEST_CHAIN -Encoding UTF8\r\n"
        "exit 7\r\n",
        encoding="ascii",
    )
    result = folder / "windows-update-t1.result.json"
    command = self_update.windows_installer_command(fake, str(tmp_path / "data's dir"))
    runner = folder / "windows-update-t1.ps1"
    runner.write_bytes(self_update._windows_runner_script(
        handoff_id="t1", command=command, result_file=result, log_dir=tmp_path / "logs",
        before="AbstractFramework 0.14.4", after="AbstractFramework 0.14.5", close_after_s=0, start_delay_s=0,
    ).encode("utf-8-sig"))
    # A failed run waits for Enter; give it none (the result is written before that).
    env = dict(os.environ, AF_TEST_CHAIN=str(chain))
    self_update._default_windows_launcher(runner, window_style="Hidden", env=env)
    deadline = time.time() + 90
    while not result.exists() and time.time() < deadline:
        time.sleep(0.5)
    assert result.exists(), "the runner wrote no result"
    got = json.loads(result.read_text(encoding="utf-8-sig"))
    assert got["id"] == "t1" and got["exit_code"] == 7
    seen = json.loads(chain.read_text(encoding="utf-8-sig"))
    assert seen["data_dir"] == str(tmp_path / "data's dir") and seen["no_open"] is True and seen["ask_wait"] == 0
    # The runner's parent is the launcher, which has exited: neither this process nor a live
    # process whose tree the installer could end.
    assert seen["runner_parent"] != os.getpid()
    alive = subprocess.run(["tasklist", "/FI", f"PID eq {seen['runner_parent']}", "/NH"], capture_output=True, text=True).stdout
    assert str(seen["runner_parent"]) not in alive
    # The failed runner waits for Enter in its hidden window: end it.
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                    f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{runner.name}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                   capture_output=True, timeout=60)


# ------------------------------------------------------------------ adversarial review fixes


def test_a_hand_off_that_failed_while_the_gateway_still_ran_reports_failed_and_allows_a_retry(tmp_path: Path, hermetic: None) -> None:
    """The installer can fail before it stops the gateway (its lock, the network, a closed
    window): the gateway that started it must report the failure, not "running" forever."""
    data = tmp_path / "data"
    _state(data)
    launched: List[Path] = []
    job = _start(data, launched)
    record = json.loads((data / "update" / self_update.WINDOWS_HANDOFF_FILE).read_text(encoding="utf-8"))
    _result(record, 1)
    status = self_update.job_status()
    assert status["state"] == "failed" and status["handoff"]["id"] == job["handoff"]["id"]
    assert self_update.job_running() is False
    # The next check offers the update again, with a word about the failure, and it can start.
    inst = _win(data)
    check = self_update.check_for_update(force=True, info=inst, fetch_release=_release)
    view = self_update.update_view(inst, check, status, False)
    assert view["status"] == "available" and view["action"] and "didn't finish" in view["line"]
    again = self_update.start_update(info=inst, installer_sha256=PS1_SHA, runner=lambda runner: launched.append(Path(runner)))
    assert again["state"] == "running" and again["handoff"]["id"] != job["handoff"]["id"] and len(launched) == 2


def test_recovery_starts_serve_even_from_the_update_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import host_control

    data = tmp_path / "data"
    run_dir = data / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "gateway-serve.json").write_text(json.dumps({"url": "http://127.0.0.1:18099", "port": 18099, "pid": 1}), encoding="utf-8")
    # `abstractgateway update --yes`: its own arguments are not a gateway's.
    monkeypatch.setattr(host_control, "build_relaunch_command", lambda: ["C:/py/python.exe", "-m", "abstractgateway", "update", "--yes"])
    url, command = self_update._windows_recovery(data)
    assert command[1:5] == ["-m", "abstractgateway", "serve", "--data-dir"] and command[5] == str(data)
    assert command[-2:] == ["--port", "18099"] and "update" not in command
    # In the gateway: its own Restart command (a login start keeps its tray).
    monkeypatch.setattr(host_control, "build_relaunch_command", lambda: ["C:/py/python.exe", "-m", "abstractgateway", "serve", "--tray"])
    assert self_update._windows_recovery(data)[1] == ["C:/py/python.exe", "-m", "abstractgateway", "serve", "--tray"]


def test_a_slow_install_holding_its_lock_is_still_running_after_the_time_limit(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    _record(data, started_ago=self_update.WINDOWS_HANDOFF_TIMEOUT_S + 600)
    lock = data / "update" / "install.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")  # a live process holds it
    assert self_update.job_status(data_dir=str(data))["state"] == "running"
    (lock / "pid").write_text("999999", encoding="utf-8")  # no such process: a stale lock
    assert self_update.job_status(data_dir=str(data))["state"] == "failed"


def test_an_installer_checked_for_another_system_is_never_run(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    posix = self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={}, data_dir=data, platform="linux")
    self_update.check_for_update(force=True, info=posix, fetch_release=_release)  # downloaded install.sh
    with pytest.raises(self_update.UpdateNotPossible, match="another system"):
        self_update.start_update(info=_win(data), installer_sha256=hashlib.sha256(SH).hexdigest(), runner=lambda r: None)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows PowerShell")
def test_the_runner_refuses_an_installer_changed_after_the_check(tmp_path: Path) -> None:
    """The sha256 the admin confirmed is checked again by the runner, on the file it runs."""
    folder = tmp_path / "upd"
    folder.mkdir()
    ran = folder / "ran.txt"
    fake = folder / "install.ps1"
    fake.write_text(f"Set-Content -LiteralPath '{ran}' -Value ran\r\nexit 0\r\n", encoding="ascii")
    good = hashlib.sha256(fake.read_bytes()).hexdigest()
    for sha, expect_code, expect_ran in ((good, 0, True), ("0" * 64, 3, False)):
        if ran.exists():
            ran.unlink()
        result = folder / f"r-{expect_code}.json"
        runner = folder / f"runner-{expect_code}.ps1"
        runner.write_bytes(self_update._windows_runner_script(
            handoff_id=f"h{expect_code}", command=["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(fake)],
            result_file=result, log_dir=tmp_path, before="a", after="b", close_after_s=0, start_delay_s=0, installer_sha256=sha,
        ).encode("utf-8-sig"))
        self_update._default_windows_launcher(runner, window_style="Hidden")
        deadline = time.time() + 90
        while not result.exists() and time.time() < deadline:
            time.sleep(0.5)
        assert json.loads(result.read_text(encoding="utf-8-sig"))["exit_code"] == expect_code
        assert ran.exists() is expect_ran
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*runner-3.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   capture_output=True, timeout=60)
