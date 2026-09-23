"""Desktop tray supervisor + self-update (2026-09-05).

Pins:
- THERE IS NO TRAY SETTING (operator ruling 2026-09-06). The icon is shown
  whenever the process and the desktop can hold it; the retired `desktop_tray`
  knob refuses loudly instead of accepting a write that would do nothing.
- The start decision table names every "no" with a reason and a hint, and every
  entry in it is a FACT about this machine, never a preference.
- `detect_install()` classifies editable / docker / pipx / uv tool / uv venv /
  pip / unknown, and only the reproducible kinds are upgradable.
- The update check is offline-honest and cached; the upgrade job is
  one-at-a-time with a bounded log and `restart_recommended` on success.
- HTTP: `/host/tray` is a user-level read; `/host/update*` are admin.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway import self_update
from abstractgateway.tray_supervisor import TrayDecision, display_available, tray_decision

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _reset_update_state():
    self_update._reset_for_tests()
    yield
    self_update._reset_for_tests()


# ------------------------------------------------------------------ setting


def test_there_is_no_tray_setting_and_the_retired_one_refuses(tmp_path: Path) -> None:
    """The icon is the gateway's presence on the desktop; it has no switch.

    A retired knob that is merely IGNORED is worse than one that refuses: every
    other unknown key here is dropped silently, so a stale console or script
    would get a 200 and believe it had turned the icon off.
    """

    from abstractgateway.runtime_config import RuntimeConfigError, read_runtime_config, write_runtime_config
    import abstractgateway.runtime_config as rc

    assert "desktop_tray" not in read_runtime_config(tmp_path)
    assert not hasattr(rc, "resolve_desktop_tray_enabled")

    with pytest.raises(RuntimeConfigError) as exc:
        write_runtime_config(tmp_path, {"desktop_tray": False}, actor="person:admin")
    assert "always shown" in str(exc.value)
    # ...and it refused BEFORE writing: nothing about the store changed.
    assert "desktop_tray" not in read_runtime_config(tmp_path)


# ------------------------------------------------------------ decision table


def test_tray_decision_table_names_every_refusal() -> None:
    deps_ok = (True, None)
    linux_desktop = {"DISPLAY": ":0"}
    assert tray_decision(platform="linux", env=linux_desktop, dependencies=deps_ok) == TrayDecision(True, "ok", None)

    d = tray_decision(reload=True, platform="linux", env=linux_desktop, dependencies=deps_ok)
    assert d.reason == "dev_reload"

    d = tray_decision(runner_only=True, platform="linux", env=linux_desktop, dependencies=deps_ok)
    assert d.reason == "runner_only"

    d = tray_decision(platform="linux", env={}, dependencies=deps_ok)
    assert d.reason == "headless" and "DISPLAY" in str(d.hint)

    d = tray_decision(platform="darwin", env={"SSH_CONNECTION": "1"}, dependencies=deps_ok)
    assert d.reason == "headless"

    d = tray_decision(platform="darwin", env={}, dependencies=(False, "pystray is not installed"))
    assert d.reason == "missing_dependency" and 'abstractgateway[tray]' in str(d.hint)

    # No argument can turn the icon off: what is left is only what the machine
    # can or cannot do.
    with pytest.raises(TypeError):
        tray_decision(enabled_setting=False)  # type: ignore[call-arg]

    assert display_available(platform="win32", env={}, probes={"win_session_zero": False})[0] is True
    assert display_available(platform="win32", env={}, probes={"win_session_zero": True})[0] is False
    assert display_available(platform="darwin", env={}, probes={"mac_gui": False})[0] is False
    assert display_available(platform="linux", env={"DISPLAY": ":0"}, probes={"sni": False})[1].startswith("no system tray")
    assert display_available(platform="linux", env={"DISPLAY": ":10", "SSH_CONNECTION": "1"})[0] is False
    assert display_available(platform="darwin", env={"CI": "true"}, probes={"mac_gui": True})[1] == "test or CI environment"


# ----------------------------------------------------------- install detect


def _fake_direct_url(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    monkeypatch.setattr(self_update, "_direct_url_info", lambda: payload)


def test_detect_install_classifies_editable_docker_pipx_uv_and_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_extras", lambda: ["apple"])
    monkeypatch.setattr(self_update, "_in_docker", lambda: False)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: f"/bin/{name}")

    _fake_direct_url(monkeypatch, {"dir_info": {"editable": True}, "url": "file:///src"})
    info = self_update.detect_install(executable="/py", prefix="/venv", env={})
    assert info.kind == "editable" and info.upgradable is False and "git pull" in str(info.reason)

    _fake_direct_url(monkeypatch, None)
    monkeypatch.setattr(self_update, "_in_docker", lambda: True)
    info = self_update.detect_install(executable="/py", prefix="/venv", env={})
    assert info.kind == "docker" and info.upgradable is False
    monkeypatch.setattr(self_update, "_in_docker", lambda: False)

    info = self_update.detect_install(executable="/py", prefix="/home/u/.local/pipx/venvs/abstractgateway", env={})
    assert info.kind == "pipx" and info.command == ["/bin/pipx", "upgrade", "abstractgateway"]

    info = self_update.detect_install(executable="/py", prefix="/home/u/.local/share/uv/tools/abstractgateway", env={})
    assert info.kind == "uv-tool" and info.command == ["/bin/uv", "tool", "upgrade", "abstractgateway"]

    uv_venv = tmp_path / "uvenv"
    uv_venv.mkdir()
    (uv_venv / "pyvenv.cfg").write_text("home = /x\nuv = 0.5.1\n", encoding="utf-8")
    info = self_update.detect_install(executable="/py", prefix=str(uv_venv), env={})
    assert info.kind == "uv-venv" and info.command == ["/bin/uv", "pip", "install", "--python", "/py", "--upgrade", "abstractgateway[apple]"]

    plain = tmp_path / "venv"
    plain.mkdir()
    monkeypatch.setattr(self_update, "_dist_installed", lambda name: name == "pip")
    # Hermetic: the suite's own venv may have been populated by uv, which the
    # INSTALLER record would otherwise report for this "plain pip" case.
    monkeypatch.setattr(self_update, "_dist_installer", lambda: "pip")
    info = self_update.detect_install(executable="/py", prefix=str(plain), env={})
    assert info.kind == "pip" and info.command == ["/py", "-m", "pip", "install", "--upgrade", "abstractgateway[apple]"]
    assert info.extras == ["apple"] and info.display_command

    monkeypatch.setattr(self_update, "_dist_installed", lambda name: False)
    info = self_update.detect_install(executable="/py", prefix=str(plain), env={})
    assert info.kind == "unknown" and info.upgradable is False


def test_extras_detection_follows_nested_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    reqs = {
        "abstractgateway": ["abstractruntime[apple]>=1; extra == 'apple'", "torchx>=1; extra == 'gpu'", "fastapi"],
        "abstractruntime": ["mlx-lm>=0.1; extra == 'apple'"],
    }
    installed = {"abstractruntime", "mlx-lm", "fastapi"}
    import importlib.metadata as md

    monkeypatch.setattr(md, "requires", lambda name: reqs.get(name.lower()))
    monkeypatch.setattr(self_update, "_dist_installed", lambda name: name in installed)
    assert self_update.extra_installed("abstractgateway", "apple") is True
    assert self_update.extra_installed("abstractgateway", "gpu") is False
    assert self_update.extra_installed("abstractgateway", "embeddings") is False  # no requirement = not installed


def test_version_compare_handles_prereleases_and_junk() -> None:
    assert self_update.is_newer("0.2.30", "0.2.29") is True
    assert self_update.is_newer("0.2.29", "0.2.29") is False
    assert self_update.is_newer("0.3.0rc1", "0.2.29") is True
    assert self_update.is_newer("0.3.0", "0.3.0rc1") is True
    assert self_update._version_key("garbage") is None


# ------------------------------------------------------------- update check


def test_check_for_update_is_offline_honest_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.2.29")
    calls: List[int] = []

    def offline() -> Dict[str, Any]:
        calls.append(1)
        return {"ok": False, "offline": True, "error": "could not reach PyPI (timeout)"}

    out = self_update.check_for_update(force=True, fetch=offline)
    assert out["offline"] is True and out["latest"] is None and out["update_available"] is None
    assert "PyPI" in out["error"]

    def online() -> Dict[str, Any]:
        calls.append(2)
        return {"ok": True, "offline": False, "latest": "0.2.31"}

    out = self_update.check_for_update(force=True, fetch=online, now=1000.0)
    assert out["update_available"] is True and out["latest"] == "0.2.31" and out["current"] == "0.2.29"
    # Within the TTL the cached answer is served without a fetch.
    again = self_update.check_for_update(fetch=online, now=1000.0 + 60)
    assert again["latest"] == "0.2.31" and calls == [1, 2]
    # Past the TTL it refreshes.
    self_update.check_for_update(fetch=online, now=1000.0 + self_update.CHECK_CACHE_TTL_S + 1)
    assert calls == [1, 2, 2]


def test_update_job_runs_once_logs_and_recommends_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    info = self_update.InstallInfo(kind="pip", upgradable=True, reason=None, command=["/py", "-m", "pip", "install", "-U", "abstractgateway"], display_command="x", python="/py", prefix="/v", extras=[], version="0.2.29")
    monkeypatch.setattr(self_update, "_installed_version_fresh", lambda python: "0.2.31")

    def runner(command: List[str], on_line: Any) -> int:
        for i in range(250):
            on_line(f"line {i}")
        return 0

    st = self_update.start_update(info=info, runner=runner)
    assert st["state"] in {"running", "succeeded"}
    deadline = time.time() + 5
    while self_update.job_status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    st = self_update.job_status()
    assert st["state"] == "succeeded" and st["restart_recommended"] is True and st["version_after"] == "0.2.31"
    assert len(st["log_tail"]) == self_update.LOG_TAIL_LINES and st["log_tail"][-1] == "line 249"

    failing = self_update.InstallInfo(kind="pip", upgradable=True, reason=None, command=["/py"], display_command="x", python="/py", prefix="/v", extras=[], version="0.2.29")
    self_update.start_update(info=failing, runner=lambda c, o: 1)
    deadline = time.time() + 5
    while self_update.job_status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    st = self_update.job_status()
    assert st["state"] == "failed" and "exited with code 1" in str(st["error"]) and st["restart_recommended"] is False

    with pytest.raises(self_update.UpdateNotPossible):
        self_update.start_update(info=self_update.InstallInfo(kind="editable", upgradable=False, reason="checkout", command=None, display_command=None, python="/py", prefix="/v"))


# -------------------------------------------------------------------- HTTP


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    token = "operator-token-for-tests"
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_tray_and_update_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import tray_supervisor

    # Hermetic: a serve test earlier in the session may have recorded a serve
    # context in this process; this test pins the "not serving" answer.
    monkeypatch.setattr(tray_supervisor, "_serve_context", {})
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.2.29")
    monkeypatch.setattr(self_update, "_fetch_pypi_latest", lambda timeout_s=5.0: {"ok": True, "offline": False, "latest": "0.2.30"})
    c, h = _client(tmp_path, monkeypatch)
    with c:
        r = c.get("/api/gateway/host/tray", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert "enabled_setting" not in body, "the tray surface carries no setting"
        # Not started by `abstractgateway serve` in tests: the decision says so.
        assert body["decision"]["reason"] == "not_serving" and body["can_control"] is False
        # `show` is the retry for a crashed helper; there is no `hide`.
        assert c.post("/api/gateway/host/tray/show", headers=h).status_code == 409
        assert c.post("/api/gateway/host/tray/hide", headers=h).status_code == 404

        # The retired knob refuses rather than pretending to have applied.
        r = c.post("/api/gateway/admin/runtime-config", headers=h, json={"desktop_tray": False})
        assert r.status_code == 400 and "always shown" in r.json()["detail"]

        assert c.get("/api/gateway/host/update").status_code == 401
        r = c.get("/api/gateway/host/update", headers=h)
        assert r.status_code == 200 and r.json()["install"]["kind"] and r.json()["check"] is None
        r = c.post("/api/gateway/host/update/check", headers=h)
        assert r.status_code == 200
        chk = r.json()["check"]
        assert chk["latest"] == "0.2.30" and chk["update_available"] is True and chk["offline"] is False
        # This test process is an editable checkout (or at least not a
        # reproducible install we would upgrade blindly): start refuses.
        monkeypatch.setattr(self_update, "detect_install", lambda **kw: self_update.InstallInfo(kind="editable", upgradable=False, reason="checkout", command=None, display_command=None, python="/py", prefix="/v"))
        r = c.post("/api/gateway/host/update/start", headers=h)
        assert r.status_code == 409 and "checkout" in r.json()["detail"]
