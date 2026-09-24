"""Mission LL (2026-09-24): one Install, and the Assistant as an app.

1. Install installs the browser app AND, when the app has a terminal version
   with a prebuilt download for this computer (Code), the terminal app too, as
   ONE job with two child rows (`parts`); it starts nothing; Cancel stops both.
2. AbstractAssistant (a Python/Qt desktop app) is the sixth app card
   (`kind: "desktop"`): presence detection shared with the tray, install into
   the gateway's own Python with the `abstract*` packages pinned, launch on
   the gateway computer only.

Nothing real is found, installed, downloaded or launched: the web install is
a stand-in, GitHub is a fake urlopen, pip and the launcher are recorders, and
the machine is `DesktopProbes` fakes."""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_desktop as desk
from abstractgateway import apps_manager as am
from abstractgateway.tray import apps as tray_apps
from abstractgateway.tray import menu_model

from test_gateway_apps_tui import FakeNet, _publish  # the fake GitHub release

_TOKEN = "apps-ll-test-token-0123456789abcdef"
BUNDLE = "/Applications/AbstractAssistant.app"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(am, "release_target", lambda **kw: "aarch64-apple-darwin")
    monkeypatch.setattr(am, "_is_script", lambda p: False)  # the fake terminal app stands in for a native binary
    return h


def _fake_web_install(m: am.AppsManager, calls: List[str], *, cancel_after: bool = False):
    """Stands in for the npm download: marks the app installed at 1.2.3."""

    def _install_app(job, spec, version, phase):
        calls.append(spec.id)
        job.step("install", f"Installing {spec.package}…")
        pkg = m.package_dir(spec.id, "1.2.3")
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"name": spec.package, "version": "1.2.3", "bin": "bin/cli.js"}), encoding="utf-8")
        m._update_app_state(spec.id, version="1.2.3")
        job.say(f"{spec.name} 1.2.3 is installed.", percent=phase.hi)
        if cancel_after:
            job.cancel_event.set()
        return {"version": "1.2.3", "previous_version": None}

    return _install_app


@pytest.fixture()
def mgr(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    net = FakeNet()
    m = am.AppsManager(tmp_path / "data", urlopen=net)
    web_calls: List[str] = []
    m._install_app = _fake_web_install(m, web_calls)  # type: ignore[method-assign]
    monkeypatch.setattr(m, "node_status", lambda refresh=False: {"available": True, "path": "/usr/bin/node", "version": "24.0.0", "message": "Node.js 24"})
    launched: List[str] = []
    monkeypatch.setattr(m, "launch", lambda app_id, **kw: launched.append(app_id) or {"url": None})
    return m, net, web_calls, launched


# ---------------------------------------------------------------------------
# 1. One Install: browser + terminal, one job
# ---------------------------------------------------------------------------


def test_install_installs_the_browser_and_the_terminal_app_as_one_job(mgr) -> None:
    m, net, web_calls, launched = mgr
    _publish(net)
    assert m.app_row(am.spec_for("code"))["install_parts"] == ["web", "tui"]
    assert m.app_row(am.spec_for("flow"))["install_parts"] == ["web"]  # no terminal version
    job, created = m.start_install("code", run_inline=True, same_machine=True)
    d = job.to_dict()
    assert created and d["state"] == "succeeded", d
    assert web_calls == ["code"] and launched == []  # installs; starts nothing
    assert [(p["id"], p["state"]) for p in d["parts"]] == [("install", "done"), ("install-tui", "done")]
    assert [p["label"] for p in d["parts"]] == ["Code in the browser", "Code in the terminal"]
    assert d["result"]["terminal"]["version"] == "0.9.9"
    assert "with its terminal app 0.9.9" in d["message"]
    assert m.tui_status(am.CODE_TUI)["installed"] and m.managed_tui_path(am.CODE_TUI).is_file()
    row = m.app_row(am.spec_for("code"))
    tui = next(i for i in row["interfaces"] if i["kind"] == "tui")
    assert row["installed"] and tui["installed"] and row["install_parts"] == ["web"]


def test_install_without_a_prebuilt_terminal_app_installs_the_browser_app_only(mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    m, net, web_calls, _ = mgr
    monkeypatch.setattr(am, "release_target", lambda **kw: None)  # e.g. linux/riscv64: only `cargo install`
    assert m.app_row(am.spec_for("code"))["install_parts"] == ["web"]
    job, _ = m.start_install("code", run_inline=True, same_machine=True)
    d = job.to_dict()
    assert d["state"] == "succeeded" and d["parts"] == [] and "terminal" not in d["result"]
    assert web_calls == ["code"] and not any("github" in u for u in net.calls)
    tui = next(i for i in m.app_row(am.spec_for("code"))["interfaces"] if i["kind"] == "tui")
    assert tui["installed"] is False and tui["install_method"] == "cargo"  # the command stays under Technical details


def test_a_terminal_part_that_fails_keeps_the_browser_app_and_says_so(mgr) -> None:
    m, net, _, _ = mgr  # no release published: GitHub answers 404
    job, _ = m.start_install("code", run_inline=True, same_machine=True)
    d = job.to_dict()
    assert d["state"] == "failed"
    assert d["error"]["message"].startswith("Code is installed for the browser, but its terminal app did not install:"), d["error"]
    assert [(p["id"], p["state"]) for p in d["parts"]] == [("install", "done"), ("install-tui", "failed")]
    assert m.installed_version("code") == "1.2.3" and not m.tui_status(am.CODE_TUI)["installed"]


def test_cancel_covers_both_parts(mgr) -> None:
    m, net, web_calls, _ = mgr
    _publish(net)
    m._install_app = _fake_web_install(m, web_calls, cancel_after=True)  # type: ignore[method-assign]
    job, _ = m.start_install("code", run_inline=True, same_machine=True)
    d = job.to_dict()
    assert d["state"] == "cancelled"
    assert [(p["id"], p["state"]) for p in d["parts"]] == [("install", "done"), ("install-tui", "cancelled")]
    assert not m.tui_status(am.CODE_TUI)["installed"] and not any("releases/download" in u for u in net.calls)


def test_with_terminal_false_and_update_install_the_browser_app_only(mgr) -> None:
    m, net, _, _ = mgr
    _publish(net)
    job, _ = m.start_install("code", run_inline=True, same_machine=True, with_terminal=False)
    assert job.to_dict()["parts"] == [] and not m.tui_status(am.CODE_TUI)["installed"]


# ---------------------------------------------------------------------------
# 2. The Assistant: detection (one function for console + tray)
# ---------------------------------------------------------------------------


def _probes(tmp_path: Path, *, files=(), which=None, spec=None, platform="darwin", procs=(), version="0.5.0", plist="0.4.9") -> desk.DesktopProbes:
    present = set(files)
    return desk.DesktopProbes(
        which=lambda name: (which or {}).get(name),
        exists=lambda p: p in present,
        find_spec=lambda name: spec,
        platform=platform,
        home=tmp_path,
        script_dirs=[str(tmp_path / "venv" / "bin")],
        python="/venv/bin/python",
        dist_version=lambda name: version,
        plist_version=lambda path: plist if path.startswith(BUNDLE) else None,
        entry_point=lambda script: "abstractassistant.cli:main",
        processes=lambda: list(procs),
    )


def test_detection_bundle_script_and_neither(tmp_path: Path) -> None:
    script = str(tmp_path / "venv" / "bin" / "abstractassistant")
    both = desk.detect_assistant(_probes(tmp_path, files=[BUNDLE, script]))
    assert both["installed"] and both["source"] == "bundle" and both["launch"] == ["open", "-a", BUNDLE]
    assert both["found_by"] == [f"bundle:{BUNDLE}", f"script:{script}"] and both["version"] == "0.5.0"  # package metadata first
    only_bundle = desk.detect_assistant(_probes(tmp_path, files=[BUNDLE], version=None))
    assert only_bundle["version"] == "0.4.9" and only_bundle["location"] == BUNDLE  # Info.plist
    only_script = desk.detect_assistant(_probes(tmp_path, files=[script], platform="linux"))
    assert only_script["source"] == "script" and only_script["launch"] == [script] and only_script["location"] == script
    neither = desk.detect_assistant(_probes(tmp_path, version=None))
    assert neither["installed"] is False and neither["launch"] is None and neither["version"] is None
    # A folder named abstractassistant in the working directory is not an install.
    ns = desk.detect_assistant(_probes(tmp_path, spec=types.SimpleNamespace(origin=None)))
    assert ns["installed"] is False and ns["found_by"][0].startswith("namespace-only")
    pkg = desk.detect_assistant(_probes(tmp_path, spec=types.SimpleNamespace(origin="/site/abstractassistant/__init__.py")))
    assert pkg["source"] == "python" and "from abstractassistant.cli import main" in pkg["launch"][2]


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["/venv/bin/python", "-m", "abstractassistant.cli"], True),  # how the operator's runs
        (["/venv/bin/python3.12", "-m", "abstractassistant"], True),
        (["/venv/bin/abstractassistant"], True),
        (["/venv/bin/python", "/venv/bin/abstractassistant", "--gateway-url", "x"], True),
        ([f"{BUNDLE}/Contents/MacOS/AbstractAssistant"], True),
        (["/venv/bin/python", "-c", "import sys; from abstractassistant.cli import main as _m; sys.exit(_m())"], True),
        (["/x/abstractassistant/.venv/bin/python", "-m", "pytest", "tests"], False),
        (["/venv/bin/python", "-m", "pytest", "abstractassistant/tests"], False),
        (["git", "-C", "abstractassistant", "status"], False),
        (["vim", "abstractassistant/cli.py"], False),
        ([], False),
    ],
)
def test_running_detection_reads_the_command_line(argv, expected) -> None:
    assert desk.is_assistant_argv(argv) is expected


def test_the_tray_uses_the_same_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []

    def spy(probes, **kw):
        seen.append(probes)
        return {"installed": True, "found_by": ["spy"], "source": "script", "launch": ["/s/abstractassistant"], "running": True, "pid": 7}

    monkeypatch.setattr(desk, "detect_assistant", spy)
    probes = tray_apps.Probes(which=lambda n: None, exists=lambda p: False, find_spec=lambda n: None, npm_root=lambda: None, platform="darwin", home=tmp_path, scripts_dir=str(tmp_path), processes=lambda: [])
    got = tray_apps.detect_assistant(probes)
    assert got["found_by"] == ["spy"] and len(seen) == 1 and isinstance(seen[0], desk.DesktopProbes)


def test_tray_entries_and_menu_for_the_assistant() -> None:
    running = {"launch": ["/s/abstractassistant"], "source": "script", "found_by": ["script:/s"], "running": True}
    entries = tray_apps.build_app_entries({"apps": []}, None, globals_found={}, assistant=running, local_running={})
    a = entries[-1]
    assert a.id == "assistant" and a.status == "running"
    missing = tray_apps.build_app_entries(
        {"apps": [{"id": "assistant", "kind": "desktop", "installed": False, "install_available": True}]}, None, globals_found={}, assistant={"launch": None}, local_running={}
    )
    assert missing[-1].status == "not_installed" and missing[-1].install_available
    for ents, want in ((entries, ("Launch Assistant", ("assistant_launch",))), (missing, ("Install Assistant…", ("app_install", "assistant")))):
        section = menu_model.apps_section(types.SimpleNamespace(apps=ents, apps_fetched=True), reachable=True)
        labels = {n.label: n.action for n in section.children if getattr(n, "label", None)}
        assert labels.get(want[0]) == want[1], labels


def test_tray_install_asks_for_install_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray.client import GatewayClient

    c = GatewayClient("http://127.0.0.1:18990", "t")
    sent: List[Any] = []
    monkeypatch.setattr(c, "_request", lambda method, path, **kw: sent.append((method, path, kw.get("body"))) or None)
    c.app_install("code")
    assert sent == [("POST", "/apps/code/install", {})]  # no launch: nothing opens by itself


# ---------------------------------------------------------------------------
# 2b. The Assistant card, install job and launch
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture()
def amgr(tmp_path: Path, home: Path):
    m = am.AppsManager(tmp_path / "data", urlopen=FakeNet())
    state: Dict[str, Any] = {"files": set(), "procs": [], "version": None}
    script = str(tmp_path / "venv" / "bin" / "abstractassistant")
    state["script"] = script
    m.desktop_probes = lambda: _probes(tmp_path, files=state["files"], procs=state["procs"], version=state["version"], plist=None)
    m.desktop_find_uv = lambda: "/fake/uv"
    m.desktop_python = "/venv/bin/python"
    spawned: List[Dict[str, Any]] = []

    def spawner(argv, *, env, log_path):
        spawned.append({"argv": list(argv), "env": dict(env), "log": log_path})
        return _Proc(state.get("exit"))

    m.desktop_spawner = spawner
    m.desktop_wait = lambda proc: None if proc.code in (None, 0) else proc.code
    return m, state, spawned


def test_overview_lists_the_assistant_after_the_browser_apps(amgr) -> None:
    m, state, _ = amgr
    ov = m.overview(check_latest=False, caller={"local": True, "same_machine": True, "admin": True})
    assert [a["id"] for a in ov["apps"]] == ["observer", "continuum", "code", "entity", "flow", "assistant"]
    a = ov["apps"][-1]
    assert a["kind"] == "desktop" and a["name"] == "Assistant" and a["url"] is None and a["port"] is None
    assert a["status"] == "not_installed" and a["actions"] == ["install"] and a["install_available"]
    assert a["desktop"]["install_command"].startswith("/fake/uv pip install --python /venv/bin/python abstractassistant")
    state["files"] = {BUNDLE, state["script"]}
    state["version"] = "0.5.0"
    m._desktop_cache.clear()
    a = m.desktop_row("assistant", caller={"same_machine": True, "admin": True})
    assert a["installed"] and a["status"] == "stopped" and a["actions"] == ["open"] and a["version"] == "0.5.0"
    assert a["desktop"]["launch_available"] and a["desktop"]["launch_command"] == f"open -a {BUNDLE}" and a["desktop"]["location"] == BUNDLE
    state["procs"] = [(4242, ["/venv/bin/python", "-m", "abstractassistant.cli"])]
    m._desktop_cache.clear()
    a = m.desktop_row("assistant", caller={"same_machine": True, "admin": True})
    assert a["running"] and a["status"] == "running" and a["pid"] == 4242
    remote = m.desktop_row("assistant", caller={"same_machine": False, "admin": True})
    assert remote["desktop"]["launch_available"] is False and remote["desktop"]["launch_blocked"] == "other_computer"
    assert remote["desktop"]["launch_blocked_reason"] == "The Assistant runs on the gateway's computer: open it there."


def test_install_job_pins_the_gateway_packages_and_checks_the_result(amgr) -> None:
    m, state, _ = amgr
    ran: List[List[str]] = []
    pins_seen: List[str] = []

    def runner(argv, *, on_line, cancelled, env=None):
        ran.append(list(argv))
        c = argv[argv.index("--constraint") + 1]
        pins_seen.append(Path(c).read_text(encoding="utf-8"))
        for line in ("Resolved 12 packages in 0.4s", "Downloading pyside6", "Installed 3 packages in 1.2s", " + abstractassistant==0.5.0"):
            on_line(line)
        state["files"] = {state["script"]}
        state["version"] = "0.5.0"
        return 0

    m.desktop_pip_runner = runner
    m.desktop_pins = lambda python: {"abstractgateway": "0.4.1", "abstractcore": "2.15.0"}
    job, created = m.start_install("assistant", run_inline=True, same_machine=True)
    d = job.to_dict()
    assert created and d["state"] == "succeeded", d
    assert ran[0][:5] == ["/fake/uv", "pip", "install", "--python", "/venv/bin/python"] and ran[0][5] == "abstractassistant"
    assert pins_seen == ["abstractcore==2.15.0\nabstractgateway==0.4.1\n"]
    assert d["message"] == "Assistant 0.5.0 is installed." and d["result"]["location"] == state["script"]
    assert m.desktop_row("assistant")["installed"]


def test_install_job_failure_and_cancel(amgr) -> None:
    m, state, _ = amgr
    m.desktop_pins = lambda python: {}
    m.desktop_pip_runner = lambda argv, *, on_line, cancelled, env=None: (on_line("error: No solution found when resolving dependencies"), 1)[1]
    d = m.start_install("assistant", run_inline=True, same_machine=True)[0].to_dict()
    assert d["state"] == "failed" and "No solution found" in d["error"]["message"] and "No solution found" in (d["details"] or "")

    def cancelling(argv, *, on_line, cancelled, env=None):
        m.jobs.active_for("app:assistant").cancel_event.set()  # the user's Cancel, mid-install
        assert cancelled()
        return -1

    m.desktop_pip_runner = cancelling
    d = m.start_install("assistant", run_inline=True, same_machine=True)[0].to_dict()
    assert d["state"] == "cancelled" and not m.desktop_row("assistant")["installed"]


def test_install_refused_when_installs_are_off(amgr) -> None:
    m, _, _ = amgr
    m._install_allowed_fn = lambda same_machine=False: same_machine
    m._install_fn_takes_caller = True
    with pytest.raises(am.InstallsNotAllowed):
        m.start_install("assistant", same_machine=False)
    row = m.desktop_row("assistant", caller={"same_machine": False, "admin": True})
    assert row["install_available"] is False and row["install_blocked_reason"] == am.INSTALLS_OFF_MESSAGE


def test_launch_bundle_script_running_and_the_same_machine_rule(amgr, monkeypatch: pytest.MonkeyPatch) -> None:
    m, state, spawned = amgr
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    with pytest.raises(am.NotInstalled):
        m.launch_desktop("assistant", same_machine=True)
    state["files"] = {BUNDLE, state["script"]}
    with pytest.raises(am.NotOnGatewayMachine):
        m.launch_desktop("assistant", same_machine=False)
    assert spawned == []
    out = m.launch_desktop("assistant", same_machine=True)
    assert spawned[-1]["argv"] == ["open", "-a", BUNDLE] and out["message"].startswith("The Assistant is starting")
    env = spawned[-1]["env"]
    assert "ABSTRACTGATEWAY_AUTH_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert not any("must-not-leak" in str(a) for a in spawned[-1]["argv"])  # never a token on argv
    # Script only.
    state["files"] = {state["script"]}
    m.launch_desktop("assistant", same_machine=True)
    assert spawned[-1]["argv"] == [state["script"]]
    # Running from a script: not started twice.
    state["procs"] = [(99, [state["script"]])]
    n = len(spawned)
    out = m.launch_desktop("assistant", same_machine=True)
    assert len(spawned) == n and out["already_running"] and "menu bar" in out["message"]
    # Running from the bundle: `open -a` brings it forward.
    state["files"] = {BUNDLE}
    state["procs"] = [(98, [f"{BUNDLE}/Contents/MacOS/AbstractAssistant"])]
    out = m.launch_desktop("assistant", same_machine=True)
    assert spawned[-1]["argv"] == ["open", "-a", BUNDLE] and out["already_running"]


def test_a_launch_that_exits_is_a_failure(amgr) -> None:
    m, state, _ = amgr
    state["files"] = {state["script"]}
    state["exit"] = 2
    with pytest.raises(am.LaunchFailed) as ei:
        m.launch_desktop("assistant", same_machine=True)
    assert "exit code 2" in ei.value.message


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, amgr):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    m, state, spawned = amgr
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    where = {"same": True}
    monkeypatch.setattr(routes, "_same_machine", lambda request: where["same"])
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}), m, state, spawned, where


def test_routes_launch_open_and_install(client, monkeypatch: pytest.MonkeyPatch) -> None:
    c, m, state, spawned, where = client
    state["files"] = {BUNDLE}
    r = c.post("/api/gateway/apps/assistant/launch", json={})
    assert r.status_code == 200 and r.json()["app"]["kind"] == "desktop" and spawned[-1]["argv"] == ["open", "-a", BUNDLE]
    where["same"] = False
    n = len(spawned)
    r = c.post("/api/gateway/apps/assistant/launch", json={})
    assert r.status_code == 409 and r.json()["reason"] == "not_on_gateway_machine" and len(spawned) == n
    r = c.post("/api/gateway/apps/assistant/open", json={})
    assert r.status_code == 409 and r.json()["reason"] == "desktop_app"
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(m, "start_install", lambda app_id, **kw: seen.append(dict(kw, app_id=app_id)) or (types.SimpleNamespace(to_dict=lambda: {"id": "j"}), True))
    where["same"] = True
    assert c.post("/api/gateway/apps/code/install", json={}).status_code == 200
    assert c.post("/api/gateway/apps/code/install", json={"with_terminal": False}).status_code == 200
    assert [(s["app_id"], s["launch"], s["with_terminal"]) for s in seen] == [("code", False, True), ("code", False, False)]


def test_console_card_pins() -> None:
    """The plain view: ONE Install (never "Install and open", never a separate
    terminal install), and the desktop card's Open goes to /launch."""
    from abstractgateway.console_ui import CONSOLE_UI_JS

    card = CONSOLE_UI_JS[CONSOLE_UI_JS.index("function appCardMarkup(app) {"):CONSOLE_UI_JS.index("function appViewMarkup()")]
    assert 'b("install", "Install", "is-primary", what)' in card
    assert "Install and open" not in CONSOLE_UI_JS and "Install for Terminal" not in CONSOLE_UI_JS
    assert 'b("desktop-open", "Open", "is-primary"' in card and 'desk.launch_blocked === "other_computer"' in card
    assert "appPartsMarkup(job)" in card
    act = CONSOLE_UI_JS[CONSOLE_UI_JS.index("async function appAction(action, id, button) {"):CONSOLE_UI_JS.index("async function appTuiAction(action, id) {")]
    assert "{ launch: true }" not in act and "/launch`" in act
