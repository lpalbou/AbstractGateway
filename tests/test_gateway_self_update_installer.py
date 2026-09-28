"""Update of an AbstractFramework installer install = the installer (2026-09-28).

Operator: the gateway's Update (web console, terminal console, tray) must run the SAME
installer the one-line install runs, never a uv command of its own. Pins:

- `detect_install` classifies a uv tool whose data dir holds the installer's
  `bootstrap.env` as `installer`; its update never runs `uv tool upgrade` (a no-op on the
  installer's `==<pin>`). Windows shows the PowerShell line instead (running files are locked).
- The check compares AbstractFramework RELEASES (bootstrap.env vs the framework repo's
  install manifest), never PyPI's newest gateway; an install that recorded no release
  compares its gateway with the release's gateway pin.
- The job runs the checked install.sh, from the framework repo's `main` resolved to a
  commit, as `/bin/sh install.sh --yes --no-start --no-open --no-modify-path --data-dir
  <data dir>`; its output streams into the job log; the result is honest: what moved,
  "already up to date", or the exit code with where the full log is (never PyPI).
- pip / pipx / uv venv installs keep their package-manager path and PyPI comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway import self_update

pytestmark = pytest.mark.basic

UV_TOOL_PREFIX = "/home/u/.local/share/uv/tools/abstractgateway"
SCRIPT = b"#!/bin/sh\n# AbstractFramework bootstrap installer (test double)\necho installing\n"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _reset_update_state():
    self_update._reset_for_tests()
    yield
    self_update._reset_for_tests()


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_extras", lambda: ["apple"])
    monkeypatch.setattr(self_update, "_in_docker", lambda: False)
    monkeypatch.setattr(self_update, "_direct_url_info", lambda: None)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.1")


def _state(data_dir: Path, framework: str = "0.6.1") -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "bootstrap.env"
    path.write_text(
        "# written by AbstractFramework install.sh\nPORT=8080\nMODE=service\nPROFILE=apple\n"
        f"GATEWAY_VERSION=0.7.1\nFRAMEWORK_VERSION={framework}\nCONSOLE=1\nTRAY=1\n",
        encoding="utf-8",
    )
    return path


def _release(version: str = "0.6.2", gateway: str = "0.7.2", script: bytes = SCRIPT) -> Dict[str, Any]:
    return {
        "ok": True,
        "offline": False,
        "commit": COMMIT,
        "version": version,
        "gateway_version": gateway,
        "python_packages": {"abstractcore": "2.18.1", "abstractgateway": gateway, "abstractassistant": "0.9.1"},
        "manifest_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.FRAMEWORK_MANIFEST_PATH}",
        "installer_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.INSTALLER_SCRIPT_PATH}",
        "installer_bytes": script,
    }


def _installer_info(data_dir: Path, framework: str | None = "0.6.1") -> self_update.InstallInfo:
    return self_update.InstallInfo(
        kind="installer", upgradable=True, reason=None, command=None, display_command="x",
        python=sys.executable, prefix=UV_TOOL_PREFIX, version="0.7.1", data_dir=str(data_dir),
        framework_version=framework,
    )


def _pypi_must_not_be_asked() -> Dict[str, Any]:
    raise AssertionError("an installer install compares AbstractFramework releases, never PyPI's newest gateway")


def _wait_job() -> Dict[str, Any]:
    deadline = time.time() + 10
    while self_update.job_status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    return self_update.job_status()


# ------------------------------------------------------------------ detection


def test_an_installer_install_is_updated_by_the_installer_never_by_uv(tmp_path: Path, hermetic: None) -> None:
    data = tmp_path / "data"
    _state(data)
    info = self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={}, data_dir=data, platform="darwin")
    assert info.kind == "installer" and info.upgradable is True
    assert info.framework_version == "0.6.1" and info.data_dir == str(data)
    # No package-manager command of its own: the job runs the checked install.sh.
    assert info.command is None
    assert "uv tool upgrade" not in str(info.display_command)
    assert info.display_command.startswith(self_update.INSTALLER_ONE_LINER)
    assert "--yes --no-start --no-open --no-modify-path --data-dir" in info.display_command
    assert info.as_dict()["path"] == "installer"

    # Linux: the same path.
    assert self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={}, data_dir=data, platform="linux").kind == "installer"

    # Windows: the installer must stop the gateway before files change, so it is shown, not run.
    win = self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={}, data_dir=data, platform="win32")
    assert win.kind == "installer" and win.upgradable is False and win.command is None
    assert win.display_command == self_update.INSTALLER_ONE_LINER_WINDOWS and "locked" in str(win.reason)


def test_the_running_gateways_data_dir_decides_and_uv_tool_upgrade_never_runs(tmp_path: Path, hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the gateway sees at run time (no test-only argument): its data dir holds the
    installer's state, so the update is not `uv tool upgrade abstractgateway` (a no-op on
    the installer's ==pin: "Nothing to upgrade")."""
    data = tmp_path / "data"
    _state(data)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    info = self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={})
    assert info.command != ["/bin/uv", "tool", "upgrade", "abstractgateway"]
    assert info.kind == "installer" and info.command is None


def test_a_plain_uv_tool_keeps_uv_and_a_pinned_one_says_why_it_cannot(tmp_path: Path, hermetic: None) -> None:
    empty = tmp_path / "no-installer"
    empty.mkdir()
    info = self_update.detect_install(executable="/py", prefix=UV_TOOL_PREFIX, env={}, data_dir=empty)
    assert info.kind == "uv-tool" and info.command == ["/bin/uv", "tool", "upgrade", "abstractgateway"]

    tool = tmp_path / "tools" / "abstractgateway"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "abstractgateway", extras = ["apple"], specifier = "==0.7.1" }]\n', encoding="utf-8"
    )
    pinned = self_update.detect_install(executable="/py", prefix=str(tool), env={}, data_dir=empty)
    if sys.version_info >= (3, 11):
        assert pinned.kind == "uv-tool" and pinned.upgradable is False and pinned.command is None
        assert "==0.7.1" in str(pinned.reason) and "PyPI" not in str(pinned.reason)


# ---------------------------------------------------------------- the check


def test_the_check_compares_framework_releases_not_the_newest_gateway(tmp_path: Path) -> None:
    info = _installer_info(tmp_path / "data")
    out = self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release("0.6.2", "0.7.2"))
    assert out["source"] == "framework-release" and out["update_available"] is True
    rel = out["release"]
    assert rel["version"] == "0.6.2" and rel["installed"] == "0.6.1" and rel["commit"] == COMMIT
    # Shown: the one-liner's URL (main), and the exact snapshot that runs (commit + sha256).
    assert rel["installer"]["url"] == "https://raw.githubusercontent.com/lpalbou/AbstractFramework/main/scripts/install.sh"
    assert rel["installer"]["commit_url"].endswith(f"/{COMMIT}/scripts/install.sh")
    assert rel["installer"]["sha256"] == hashlib.sha256(SCRIPT).hexdigest()

    # The same release as installed: up to date, whatever PyPI's newest gateway is.
    same = self_update.check_for_update(force=True, info=_installer_info(tmp_path / "data", "0.6.2"), fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release("0.6.2", "0.7.2"))
    assert same["update_available"] is False

    # No release recorded (--pin/--from, or an old install): the gateway vs the release's pin.
    newer_gateway = dict(vars(_installer_info(tmp_path / "data", None)), version="0.7.3")
    unrecorded = self_update.InstallInfo(**newer_gateway)
    out = self_update.check_for_update(force=True, info=unrecorded, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release("0.6.2", "0.7.2"))
    assert out["update_available"] is False, "a gateway newer than the release is never offered the release"
    older = self_update.InstallInfo(**dict(newer_gateway, version="0.7.0"))
    out = self_update.check_for_update(force=True, info=older, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release("0.6.2", "0.7.2"))
    assert out["update_available"] is True

    # Offline / a broken release: in-band, and it names GitHub, not the package index.
    down = self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: {"ok": False, "offline": True, "error": "could not reach GitHub (the AbstractFramework release) (URLError)"})
    assert down["offline"] is True and down["update_available"] is None and "GitHub" in down["error"]


def test_the_release_is_read_from_one_commit_of_main(tmp_path: Path) -> None:
    manifest = {
        "framework": {"version": "0.6.2"},
        "bootstrap": {"gateway_version": "0.7.2"},
        "python_packages": [{"distribution": "abstractcore", "version": "2.18.1"}, {"distribution": "AbstractRuntime", "version": "0.7.1"}],
    }
    seen: List[str] = []

    def get(url: str, accept: str, timeout_s: float) -> bytes:
        seen.append(url)
        if url == self_update.FRAMEWORK_COMMIT_API:
            return (COMMIT + "\n").encode()
        if url.endswith(self_update.FRAMEWORK_MANIFEST_PATH):
            return json.dumps(manifest).encode()
        if url.endswith(self_update.INSTALLER_SCRIPT_PATH):
            return SCRIPT
        raise AssertionError(url)

    rel = self_update._fetch_framework_release(http_get=get)
    assert rel["ok"] and rel["version"] == "0.6.2" and rel["gateway_version"] == "0.7.2"
    assert rel["python_packages"] == {"abstractcore": "2.18.1", "abstractruntime": "0.7.1"}
    assert seen == [
        "https://api.github.com/repos/lpalbou/AbstractFramework/commits/main",
        f"https://raw.githubusercontent.com/lpalbou/AbstractFramework/{COMMIT}/docs/installers/install-manifest.json",
        f"https://raw.githubusercontent.com/lpalbou/AbstractFramework/{COMMIT}/scripts/install.sh",
    ]

    def not_a_script(url: str, accept: str, timeout_s: float) -> bytes:
        return b"<html>404</html>" if url.endswith(".sh") else get(url, accept, timeout_s)

    bad = self_update._fetch_framework_release(http_get=not_a_script)
    assert bad["ok"] is False and "not the installer script" in bad["error"]

    import urllib.error

    def offline(url: str, accept: str, timeout_s: float) -> bytes:
        raise urllib.error.URLError("no route")

    down = self_update._fetch_framework_release(http_get=offline)
    assert down == {"ok": False, "offline": True, "error": "could not reach GitHub (the AbstractFramework release) (URLError: <urlopen error no route>)"}


# ------------------------------------------------------------------ the job


def _snapshots(before: Dict[str, str], after: Dict[str, str]) -> Any:
    calls = {"n": 0}

    def snap(python: str) -> Dict[str, str]:
        calls["n"] += 1
        return dict(before if calls["n"] == 1 else after)

    return snap


def test_the_update_runs_the_checked_installer_streams_its_log_and_reports_what_moved(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _state(data, "0.6.1")
    info = _installer_info(data)
    self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release())
    ran: List[List[str]] = []

    def runner(command: List[str], on_line: Any) -> int:
        ran.append(list(command))
        assert Path(command[1]).read_bytes() == SCRIPT, "the job runs exactly the script the check downloaded"
        for line in ("AbstractFramework 0.6.1 found: upgrading to 0.6.2", "[3] AbstractGateway", "  abstractgateway 0.7.1 -> 0.7.2"):
            on_line(line)
        _state(data, "0.6.2")  # the installer rewrites its state
        return 0

    before = {"abstractgateway": "0.7.1", "abstractcore": "2.18.0", "httpx": "0.27.0", "abstractassistant": "x"}
    after = {"abstractgateway": "0.7.2", "abstractcore": "2.18.1", "httpx": "0.28.0", "abstractassistant": "x"}
    st = self_update.start_update(info=info, runner=runner, snapshot=_snapshots(before, after), installer_sha256=hashlib.sha256(SCRIPT).hexdigest())
    assert st["installer"]["url"].endswith("/main/scripts/install.sh") and st["installer"]["commit_url"].endswith(f"/{COMMIT}/scripts/install.sh")
    st = _wait_job()
    assert ran == [["/bin/sh", str(data / "update" / "install.sh"), "--yes", "--no-start", "--no-open", "--no-modify-path", "--data-dir", str(data)]]
    assert st["state"] == "succeeded" and st["restart_recommended"] is True and self_update.restart_pending()
    assert st["log_tail"] == ["AbstractFramework 0.6.1 found: upgrading to 0.6.2", "[3] AbstractGateway", "  abstractgateway 0.7.1 -> 0.7.2"]
    assert st["framework_before"] == "0.6.1" and st["framework_after"] == "0.6.2" and st["version_after"] == "0.7.2"
    assert st["changes"] == [
        {"name": "AbstractFramework", "from": "0.6.1", "to": "0.6.2"},
        {"name": "abstractgateway", "from": "0.7.1", "to": "0.7.2"},
        {"name": "abstractcore", "from": "2.18.0", "to": "2.18.1"},
    ]
    assert st["other_changes"] == 1
    assert st["message"] == "AbstractFramework 0.6.2 is installed (AbstractFramework 0.6.1 -> 0.6.2, abstractgateway 0.7.1 -> 0.7.2, abstractcore 2.18.0 -> 2.18.1, 1 other package)"


def test_already_up_to_date_is_said_and_no_restart_is_offered(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _state(data, "0.6.2")
    info = _installer_info(data, "0.6.2")
    self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release())
    same = {"abstractgateway": "0.7.2", "abstractcore": "2.18.1"}
    self_update.start_update(info=info, runner=lambda c, on_line: on_line("Already up to date") or 0, snapshot=_snapshots(same, same))
    st = _wait_job()
    assert st["state"] == "succeeded_no_change" and st["restart_recommended"] is False and not self_update.restart_pending()
    assert st["message"] == "already up to date: AbstractFramework 0.6.2; the installer changed nothing"
    view = self_update.update_view(info, self_update.last_check(), st, False)
    assert view["status"] == "no_change" and view["action"] is None and "already up to date" in view["line"]


def test_a_failed_installer_run_names_its_exit_code_and_log_never_the_package_index(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _state(data)
    info = _installer_info(data)
    self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release())

    def runner(command: List[str], on_line: Any) -> int:
        on_line("ERROR: uv tool install failed: no wheel for webrtcvad")
        return 1

    self_update.start_update(info=info, runner=runner, snapshot=_snapshots({}, {}))
    st = _wait_job()
    assert st["state"] == "failed" and st["exit_code"] == 1 and st["restart_recommended"] is False
    assert st["log_tail"] == ["ERROR: uv tool install failed: no wheel for webrtcvad"]
    assert "installer exited with code 1" in st["error"] and str(data / "logs") in st["error"]
    assert "install.sh | sh" in st["error"] and "PyPI" not in st["error"]
    view = self_update.update_view(info, self_update.last_check(), st, False)
    assert view["status"] == "failed" and "exited with code 1" in view["line"]


def test_a_changed_installer_is_refused_before_it_runs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _state(data)
    info = _installer_info(data)
    self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release())
    with pytest.raises(self_update.UpdateNotPossible, match="installer changed since it was checked"):
        self_update.start_update(info=info, runner=lambda c, o: 0, installer_sha256="f" * 64)
    assert self_update.job_status()["state"] == "idle"


def test_the_view_shows_what_runs_and_where_it_comes_from(tmp_path: Path) -> None:
    data = tmp_path / "data"
    info = _installer_info(data)
    chk = self_update.check_for_update(force=True, info=info, fetch=_pypi_must_not_be_asked, fetch_release=lambda: _release())
    view = self_update.update_view(info, chk, {"state": "idle"}, False)
    assert view["status"] == "available" and view["offer"] == "AbstractFramework 0.6.2"
    assert view["line"] == "AbstractFramework 0.6.1 · gateway 0.7.1 · AbstractFramework 0.6.2 available"
    action = view["action"]
    assert action["label"] == "Update to AbstractFramework 0.6.2"
    assert action["installer_sha256"] == hashlib.sha256(SCRIPT).hexdigest()
    assert action["command"] == f"/bin/sh install.sh --yes --no-start --no-open --no-modify-path --data-dir {data}"
    for text in (self_update.INSTALLER_URL, COMMIT[:12], action["installer_sha256"], action["command"], "asks nothing", "start at login"):
        assert text in action["confirm"], text
    assert "uv tool upgrade" not in json.dumps(view)


# ------------------------------------------------------------ pip/pipx unchanged


def test_pip_and_pipx_keep_their_package_manager_and_pypi(tmp_path: Path, hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "no-installer"
    empty.mkdir()
    pipx = self_update.detect_install(executable="/py", prefix="/home/u/.local/pipx/venvs/abstractgateway", env={}, data_dir=empty)
    assert pipx.kind == "pipx" and pipx.command == ["/bin/pipx", "upgrade", "abstractgateway"]
    # A pipx venv next to an installer data dir is still pipx (not a uv tool).
    _state(tmp_path / "data")
    assert self_update.detect_install(executable="/py", prefix="/home/u/.local/pipx/venvs/abstractgateway", env={}, data_dir=tmp_path / "data").kind == "pipx"

    plain = tmp_path / "venv"
    plain.mkdir()
    monkeypatch.setattr(self_update, "_dist_installed", lambda name: name == "pip")
    monkeypatch.setattr(self_update, "_dist_installer", lambda: "pip")
    pip = self_update.detect_install(executable="/py", prefix=str(plain), env={}, data_dir=empty)
    assert pip.kind == "pip" and pip.command == ["/py", "-m", "pip", "install", "--upgrade", "abstractgateway[apple]"]

    def release_must_not_be_asked() -> Dict[str, Any]:
        raise AssertionError("a pip install compares with PyPI")

    chk = self_update.check_for_update(force=True, info=pip, fetch=lambda: {"ok": True, "offline": False, "latest": "0.7.2"}, fetch_release=release_must_not_be_asked)
    assert chk["source"] == "pypi" and chk["update_available"] is True and chk["latest"] == "0.7.2" and "release" not in chk
    view = self_update.update_view(pip, chk, {"state": "idle"}, False)
    assert view["action"]["label"] == "Update to 0.7.2" and view["action"]["installer_sha256"] is None
    assert view["action"]["command"] == pip.display_command

    ran: List[List[str]] = []
    monkeypatch.setattr(self_update, "_installed_version_fresh", lambda python: "0.7.2")
    self_update.start_update(info=pip, runner=lambda c, o: ran.append(list(c)) or 0)
    st = _wait_job()
    assert ran == [pip.command] and st["state"] == "succeeded" and st["installer"] is None


# -------------------------------------------------------------- HTTP, real sh


def test_the_http_update_runs_a_real_installer_script_admin_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over HTTP with a real /bin/sh: the start route runs the checked script with
    the update flags; the job log carries its output and the flags it received."""
    if os.name == "nt":
        pytest.skip("the installer path runs install.sh (macOS / Linux)")
    from fastapi.testclient import TestClient

    token = "operator-token-for-tests"
    data = tmp_path / "runtime"
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    _state(data, "0.6.1")
    script = (
        b"#!/bin/sh\n# AbstractFramework bootstrap installer (test double)\n"
        b'echo "args: $*"\necho "AbstractFramework 0.6.1 found: upgrading to 0.6.2"\n'
        b'while [ $# -gt 0 ]; do [ "$1" = --data-dir ] && sed -i.bak "s/^FRAMEWORK_VERSION=.*/FRAMEWORK_VERSION=0.6.2/" "$2/bootstrap.env"; shift; done\n'
        b"exit 0\n"
    )
    monkeypatch.setattr(self_update, "_fetch_framework_release", lambda: _release(script=script))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.1")
    # This suite runs from a checkout (an editable install): pose as the installer's uv tool.
    monkeypatch.setattr(self_update, "_direct_url_info", lambda: None)
    monkeypatch.setattr(self_update, "_in_docker", lambda: False)
    real_detect = self_update.detect_install
    monkeypatch.setattr(self_update, "detect_install", lambda **kw: real_detect(prefix=UV_TOOL_PREFIX, **kw))
    from abstractgateway.app import app

    c = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    with c:
        assert c.post("/api/gateway/host/update/start").status_code == 401
        r = c.post("/api/gateway/host/update/check", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["install"]["kind"] == "installer" and body["update"]["status"] == "available"
        sha = body["update"]["action"]["installer_sha256"]
        assert c.post("/api/gateway/host/update/start", headers=h, json={"installer_sha256": "0" * 64}).status_code == 409
        r = c.post("/api/gateway/host/update/start", headers=h, json={"installer_sha256": sha})
        assert r.status_code == 200 and r.json()["job"]["state"] in {"running", "succeeded"}
        st = _wait_job()
        assert st["log_tail"][0] == f"args: --yes --no-start --no-open --no-modify-path --data-dir {data.resolve()}"
        assert st["log_tail"][1] == "AbstractFramework 0.6.1 found: upgrading to 0.6.2"
        assert st["state"] == "succeeded" and st["framework_after"] == "0.6.2"
        view = c.get("/api/gateway/host/update", headers=h).json()["update"]
        assert view["status"] == "installed" and "restart to finish" in view["line"]
