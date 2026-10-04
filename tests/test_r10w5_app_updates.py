"""R10.6 (round 10): every Apps row shows an available update; the Assistant
row offers one-click Update like the browser apps.

1. ONE registry cache (`_registry_cached`) for npm, PyPI and GitHub: an
   answer is reused for REGISTRY_CACHE_TTL_S, then asked again — a newer
   published version flips the row within one gateway process (clock
   injected through `apps_manager._now`).
2. The Assistant row: `latest_version` from PyPI
   (`<pypi_url>/abstractassistant/json`), `update_available` =
   version_newer(latest, installed), action `update`; the update job is the
   install job with `abstractassistant==<latest>` and the gateway's pins
   (never the app pinned to itself); a running Assistant THIS gateway
   started is quit and opened again on the new version; any other running
   Assistant (started elsewhere, or the other artifact of R10.5) is left
   alone with "Quit it and open it again to run x.y.z".
3. External rows: `update_available` computed and shown with "Started
   outside the gateway — update it where it was installed"; no action.
4. Label and tooltip come from the row (`update_label`, `update_tip`): both
   consoles show the gateway's words.

Nothing real is fetched, installed or launched: the registries are a fake
urlopen, pip and the launcher are recorders, the machine is DesktopProbes."""

from __future__ import annotations

import json
import types
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_desktop as desk
from abstractgateway import apps_manager as am

_TOKEN = "r10w5-apps-test-token-0123456789abcdef"
PYPI = "https://pypi.org/pypi/abstractassistant/json"
BUNDLE = "/Applications/AbstractAssistant.app"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRegistries:
    """npm and PyPI in one fake urlopen; `answers` is changed between calls."""

    def __init__(self) -> None:
        self.answers: Dict[str, Any] = {}
        self.calls: List[str] = []
        self.offline = False

    def npm(self, package: str, latest: str) -> None:
        self.answers[f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}"] = {"name": package, "dist-tags": {"latest": latest}, "versions": {latest: {}}}

    def pypi(self, latest: str) -> None:
        self.answers[PYPI] = {"info": {"name": "abstractassistant", "version": latest}, "releases": {latest: []}}

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError("offline")
        if url not in self.answers:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return _Resp(json.dumps(self.answers[url]).encode())


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class _Proc:
    def __init__(self, pid: int, code=None) -> None:
        self.pid = pid
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("ABSTRACTGATEWAY_APPS_PYPI_URL", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_APPS_NPM_REGISTRY", raising=False)
    return h


def _probes(tmp_path: Path, state: Dict[str, Any]) -> desk.DesktopProbes:
    script = str(tmp_path / "venv" / "bin" / "abstractassistant")
    present = set(state.get("files") or ())
    return desk.DesktopProbes(
        which=lambda name: None,
        exists=lambda p: p in present,
        find_spec=lambda name: types.SimpleNamespace(origin="/venv/lib/abstractassistant/__init__.py") if script in present else None,
        platform="darwin",
        home=tmp_path,
        script_dirs=[str(tmp_path / "venv" / "bin")],
        python="/venv/bin/python",
        dist_version=lambda name: state.get("version"),
        plist_version=lambda path: "0.5.0" if path.startswith(BUNDLE) else None,
        entry_point=lambda s: "abstractassistant.cli:main",
        processes=lambda: list(state.get("procs") or ()),
    )


@pytest.fixture()
def world(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    net = FakeRegistries()
    clock = Clock()
    monkeypatch.setattr(am, "_now", clock)
    m = am.AppsManager(tmp_path / "data", urlopen=net)
    m.external_probe = lambda **kw: {}
    m.identity_probe = lambda *a, **kw: None  # no app answers on loopback here
    monkeypatch.setattr(m, "node_status", lambda refresh=False: {"available": True, "path": "/usr/bin/node", "version": "24.0.0", "message": "Node.js 24"})
    script = str(tmp_path / "venv" / "bin" / "abstractassistant")
    state: Dict[str, Any] = {"files": {script}, "version": "0.13.0", "procs": [], "script": script, "next_pid": 5000}
    m.desktop_probes = lambda: _probes(tmp_path, state)
    m.desktop_find_uv = lambda: "/fake/uv"
    m.desktop_python = "/venv/bin/python"
    m.desktop_pins = lambda python: {"abstractgateway": "0.13.0", "abstractcore": "2.25.0", "abstractassistant": "0.13.0"}
    spawned: List[Dict[str, Any]] = []
    quit_calls: List[int] = []
    ran: List[List[str]] = []

    def spawner(argv, *, env, log_path):
        state["next_pid"] += 1
        pid = state["next_pid"]
        spawned.append({"argv": list(argv), "pid": pid})
        state["procs"] = [p for p in state["procs"] if p[0] != pid] + [(pid, [state["script"]] + list(argv[1:]))]
        return _Proc(pid)

    def quitter(pid: int) -> bool:
        quit_calls.append(pid)
        state["procs"] = [p for p in state["procs"] if p[0] != pid]
        return True

    def runner(argv, *, on_line, cancelled, env=None):
        ran.append(list(argv))
        for line in ("Resolved 3 packages in 0.1s", "Downloading abstractassistant", "Installed 1 package in 0.2s"):
            on_line(line)
        req = [a for a in argv if a.startswith("abstractassistant==")]
        state["version"] = req[0].split("==", 1)[1] if req else "0.13.0"
        return 0

    m.desktop_spawner = spawner
    m.desktop_wait = lambda proc: None
    m.desktop_quit = quitter
    m.desktop_pip_runner = runner
    return types.SimpleNamespace(m=m, net=net, clock=clock, state=state, spawned=spawned, quit=quit_calls, ran=ran)


def _install_web(m: am.AppsManager, app_id: str, version: str) -> None:
    pkg = m.package_dir(app_id, version)
    pkg.mkdir(parents=True, exist_ok=True)
    spec = am.spec_for(app_id)
    (pkg / "package.json").write_text(json.dumps({"name": spec.package, "version": version, "bin": "bin/cli.js"}), encoding="utf-8")
    m._update_app_state(app_id, version=version)


def _row(ov: Dict[str, Any], app_id: str) -> Dict[str, Any]:
    return next(a for a in ov["apps"] if a["id"] == app_id)


CALLER = {"local": True, "same_machine": True, "admin": True}


# ---------------------------------------------------------------------------
# 1. TTL: a newer published version flips the row within one process
# ---------------------------------------------------------------------------


def test_ttl_flip_assistant_pypi_within_one_process(world) -> None:
    m, net, clock = world.m, world.net, world.clock
    net.pypi("0.13.0")
    a = _row(m.overview(caller=CALLER), "assistant")
    assert a["latest_version"] == "0.13.0" and a["update_available"] is False and "update" not in a["actions"]
    assert a["update_label"] is None and a["update_tip"] is None
    net.pypi("0.14.0")  # published now
    clock.t += am.REGISTRY_CACHE_TTL_S - 1
    a = _row(m.overview(caller=CALLER), "assistant")
    assert a["latest_version"] == "0.13.0" and a["update_available"] is False, "still inside the TTL: the cached answer"
    assert net.calls.count(PYPI) == 1
    clock.t += 2  # past the TTL
    a = _row(m.overview(caller=CALLER), "assistant")
    assert net.calls.count(PYPI) == 2
    assert a["latest_version"] == "0.14.0" and a["update_available"] is True and a["actions"] == ["open", "update"]
    assert a["update_label"] == "Update to 0.14.0"
    assert a["update_tip"] == "Install the newest Assistant (0.14.0); a running app restarts on it"


def test_ttl_flip_browser_app_npm_through_the_same_cache(world) -> None:
    m, net, clock = world.m, world.net, world.clock
    pkg = am.spec_for("code").package
    _install_web(m, "code", "0.10.0")
    net.npm(pkg, "0.10.0")
    net.pypi("0.13.0")
    c = _row(m.overview(caller=CALLER), "code")
    assert c["update_available"] is False
    net.npm(pkg, "0.10.1")
    clock.t += am.REGISTRY_CACHE_TTL_S + 1
    c = _row(m.overview(caller=CALLER), "code")
    assert c["update_available"] is True and "update" in c["actions"]
    assert c["update_label"] == "Update to 0.10.1"
    assert c["update_tip"] == f"Install the newest {am.spec_for('code').name} (0.10.1); a running app restarts on it"


def test_one_cache_for_npm_pypi_and_github(world, monkeypatch: pytest.MonkeyPatch) -> None:
    m = world.m
    keys: List[str] = []
    real = m._registry_cached

    def spy(key, fetch, *, use_cache=True):
        keys.append(key)
        return real(key, fetch, use_cache=use_cache)

    monkeypatch.setattr(m, "_registry_cached", spy)
    world.net.pypi("0.13.0")
    m.latest_version(desk.ASSISTANT)
    m.latest_version(am.spec_for("code"))
    with pytest.raises(am.AppsError):
        m.tui_release(am.TUI_BY_APP["code"])
    assert keys == ["pypi:abstractassistant", am.spec_for("code").package, "tui:" + am.TUI_BY_APP["code"].id]


def test_pypi_unreachable_says_so_and_asks_again_after_the_failure_ttl(world) -> None:
    m, net, clock = world.m, world.net, world.clock
    net.offline = True
    a = _row(m.overview(caller=CALLER), "assistant")
    assert a["latest_version"] is None and a["update_available"] is False
    assert a["desktop"]["latest_error"].startswith("PyPI is not reachable: Cannot reach pypi.org")
    net.offline = False
    net.pypi("0.14.0")
    clock.t += am.REGISTRY_FAIL_TTL_S + 1
    a = _row(m.overview(caller=CALLER), "assistant")
    assert a["latest_version"] == "0.14.0" and a["update_available"] and a["desktop"]["latest_error"] is None


# ---------------------------------------------------------------------------
# 2. The Assistant's update job
# ---------------------------------------------------------------------------


def test_update_job_installs_latest_with_the_gateway_pins(world) -> None:
    m, net = world.m, world.net
    net.pypi("0.14.0")
    job, created = m.start_install("assistant", update=True, run_inline=True, same_machine=True)
    d = job.to_dict()
    assert created and d["state"] == "succeeded", d
    assert d["kind"] == "app_update" and d["title"] == "Update Assistant"
    argv = world.ran[-1]
    assert argv[:5] == ["/fake/uv", "pip", "install", "--python", "/venv/bin/python"]
    assert argv[5] == "abstractassistant==0.14.0"
    # The gateway's own packages pinned; the Assistant never pinned to its old self.
    assert argv[6:] == ["abstractcore==2.25.0", "abstractgateway==0.13.0"]
    assert d["message"] == "Assistant 0.14.0 is installed." and d["result"]["previous_version"] == "0.13.0"
    assert [(st["name"], st["label"]) for st in d["steps"]] == [
        ("pins", "Keeping the gateway's own packages as they are…"),
        ("install", "Downloading and installing Assistant 0.14.0…"),
        ("check", "Checking that the Assistant is there…"),
    ]
    log = job.log_path.read_text(encoding="utf-8")
    assert "$ /fake/uv pip install --python /venv/bin/python abstractassistant==0.14.0 abstractcore==2.25.0 abstractgateway==0.13.0" in log
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["version"] == "0.14.0" and row["update_available"] is False and "update" not in row["actions"]


def test_update_restarts_the_assistant_this_gateway_started(world) -> None:
    m, net, state = world.m, world.net, world.state
    net.pypi("0.14.0")
    m.launch_desktop("assistant", same_machine=True)  # started by this gateway
    first_pid = world.spawned[-1]["pid"]
    m._desktop_cache.clear()
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["running"] and row["desktop"]["started_by_gateway"] is True
    assert row["update_tip"] == "Install the newest Assistant (0.14.0); a running app restarts on it"
    principal = types.SimpleNamespace(user_id="admin", tenant_id="default")
    minted: List[Any] = []
    m.mint_desktop_handover = lambda p, base_url: (minted.append(p) or ("code-1", Path("/tmp/handover-file")))  # type: ignore[method-assign]
    job, _ = m.start_install("assistant", update=True, run_inline=True, same_machine=True, principal=principal, gateway_url="http://127.0.0.1:18975")
    d = job.to_dict()
    assert d["state"] == "succeeded", d
    assert world.quit == [first_pid], "the old process was quit"
    relaunch = world.spawned[-1]
    assert relaunch["pid"] != first_pid and relaunch["argv"][0] == state["script"]
    assert "--gateway-handover-file" in relaunch["argv"] and minted == [principal], "reopened signed in"
    assert d["message"].startswith("Assistant 0.14.0 is installed and running again: ")
    assert [st["name"] for st in d["steps"]] == ["pins", "install", "check", "restart"]
    assert d["result"]["running"] is True
    m._desktop_cache.clear()
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["running"] and row["version"] == "0.14.0" and row["desktop"]["started_by_gateway"] is True
    assert row["desktop"]["restart_note"] is None


def test_update_leaves_an_assistant_started_elsewhere_alone(world) -> None:
    m, net, state = world.m, world.net, world.state
    net.pypi("0.14.0")
    state["procs"] = [(4242, ["/venv/bin/python", "-m", "abstractassistant.cli"])]  # start-local.sh's
    m._desktop_cache.clear()
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["running"] and row["desktop"]["started_by_gateway"] is False
    assert row["update_tip"] == "Install the newest Assistant (0.14.0). Quit it and open it again to run 0.14.0"
    job, _ = m.start_install("assistant", update=True, run_inline=True, same_machine=True)
    d = job.to_dict()
    assert d["state"] == "succeeded", d
    assert world.quit == [] and world.spawned == [], "not ours: never stopped, never started"
    assert d["message"] == "Assistant 0.14.0 is installed. Quit it and open it again to run 0.14.0."
    m._desktop_cache.clear()
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["desktop"]["restart_note"] == "Quit it and open it again to run 0.14.0"
    state["procs"] = []  # the person quit it
    m._desktop_cache.clear()
    assert m.desktop_row("assistant", caller=CALLER)["desktop"]["restart_note"] is None


def test_update_leaves_the_other_artifact_alone(world) -> None:
    m, net, state = world.m, world.net, world.state
    net.pypi("0.14.0")
    state["files"] = {state["script"], BUNDLE}
    state["procs"] = [(777, [f"{BUNDLE}/Contents/MacOS/AbstractAssistant"])]  # R10.5's stale bundle
    m._desktop_cache.clear()
    row = m.desktop_row("assistant", caller=CALLER)
    assert row["running"] is False and row["desktop"]["other_running"]["pid"] == 777
    assert row["update_tip"].endswith("Quit it and open it again to run 0.14.0")
    job, _ = m.start_install("assistant", update=True, run_inline=True, same_machine=True)
    assert job.to_dict()["message"] == "Assistant 0.14.0 is installed. Quit it and open it again to run 0.14.0."
    assert world.quit == [] and world.spawned == []
    m._desktop_cache.clear()
    assert m.desktop_row("assistant", caller=CALLER)["desktop"]["restart_note"] == "Quit it and open it again to run 0.14.0"


def test_update_without_pypi_is_refused_with_the_sentence(world) -> None:
    world.net.offline = True
    with pytest.raises(am.NetworkUnavailable) as ei:
        world.m.start_install("assistant", update=True, run_inline=True, same_machine=True)
    assert ei.value.message.startswith("PyPI is not reachable, so the newest Assistant is unknown: Cannot reach pypi.org")
    assert world.ran == []


def test_update_refused_when_installs_are_off(world) -> None:
    world.net.pypi("0.14.0")
    world.m.install_allowed = lambda same_machine=False: False  # type: ignore[method-assign]
    row = world.m.desktop_row("assistant", caller=CALLER)
    assert row["update_available"] is True and "update" not in row["actions"]


# ---------------------------------------------------------------------------
# 3. External rows: shown, never offered
# ---------------------------------------------------------------------------


def test_external_row_shows_latest_with_the_sentence_and_no_action(world) -> None:
    m, net = world.m, world.net
    pkg = am.spec_for("observer").package
    net.npm(pkg, "0.2.0")
    net.pypi("0.13.0")
    m.external_probe = lambda **kw: {"observer": am.ExternalApp("observer", 3001, "http://127.0.0.1:3001/", version="0.1.12", pid=77)}
    o = _row(m.overview(caller=CALLER), "observer")
    assert o["source"] == "external" and o["latest_version"] == "0.2.0" and o["update_available"] is True
    assert o["actions"] == ["open"] and o["update_label"] is None
    assert o["update_tip"] == "Started outside the gateway — update it where it was installed"
    # Not newer, or no version reported: nothing shown.
    net.npm(pkg, "0.1.12")
    world.clock.t += am.REGISTRY_CACHE_TTL_S + 1
    o = _row(m.overview(caller=CALLER), "observer")
    assert o["update_available"] is False and o["update_tip"] is None


# ---------------------------------------------------------------------------
# 4. Routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, world):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: world.m)
    monkeypatch.setattr(routes, "_same_machine", lambda request: True)
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_route_update_runs_the_desktop_update_signed_in(client, world, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Dict[str, Any]] = []
    real = world.m.start_desktop_install

    def spy(app_id, **kw):
        seen.append(dict(kw, app_id=app_id))
        return real(app_id, **dict(kw, run_inline=True))

    monkeypatch.setattr(world.m, "start_desktop_install", spy)
    world.net.pypi("0.14.0")
    r = client.post("/api/gateway/apps/assistant/update", json={})
    assert r.status_code == 200, r.text
    assert seen and seen[0]["app_id"] == "assistant" and seen[0]["update"] is True and seen[0]["principal"] is not None
    assert world.ran[-1][5] == "abstractassistant==0.14.0"
    ov = client.get("/api/gateway/apps").json()
    a = _row(ov, "assistant")
    assert a["version"] == "0.14.0" and a["update_available"] is False


# ---------------------------------------------------------------------------
# 5. The installer's pending upgrade never downgrades an in-place update
# ---------------------------------------------------------------------------


def test_installer_marker_never_downgrades_an_app_updated_in_place(world) -> None:
    m = world.m
    calls: List[tuple] = []
    installed = {"code": "0.10.4", "flow": "0.6.0"}  # code updated from the Apps page past the pin
    m.installed_version = lambda app_id: installed.get(app_id)  # type: ignore[method-assign]
    m.start_install = lambda app_id, **kw: (calls.append((app_id, kw.get("version"))) or (types.SimpleNamespace(state="succeeded", message="ok", error=None), True))  # type: ignore[method-assign]
    marker = m.data_dir / am.APPS_UPGRADE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("code 0.10.3\nflow 0.7.0\n", encoding="utf-8")
    out = m.apply_pending_upgrades()
    assert calls == [("flow", "0.7.0")], "code 0.10.4 stays: never down to the installer's 0.10.3"
    assert [o["app_id"] for o in out] == ["flow"]
