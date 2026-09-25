"""The tray as a control centre (2026-09-24): the pure menu model, app
detection by presence, signed-in console links, and the actions the menu
calls — all without a display, pystray, a gateway or a real login item.

- Menu model: given a snapshot + extras → nodes. Loaded models each carry an
  Eject submenu; "Load a Model" lists defaults first, then every installed
  model grouped by engine (recognised first, then A–Z), long lists chunked,
  NOTHING dropped; embeddings and loaded rows say why they are not clickable.
- Start at login: one check item whose state is the truthful status.
- Apps: gateway-managed → Open/Start/Install; a global install → Start
  (global); the Assistant → Launch; a namespace-only `abstractassistant`
  (a folder in the working directory) is NOT an install.
- Flat degrade: same rows, no submenus, path-prefixed.
- Actions: every node action has a handler; load / eject / toggle / open go
  through the same functions the menu calls.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from abstractgateway.tray import apps as tray_apps
from abstractgateway.tray import menu_model as mm
from abstractgateway.tray.client import Result
from abstractgateway.tray.sampler import ModelRow, Snapshot

pytestmark = pytest.mark.basic

GB = 1 << 30


def _snap(**over: Any) -> Snapshot:
    base: Dict[str, Any] = dict(
        ts=0.0, gateway_state="running", reachable=True, paused=False, inflight_ticks=0, paused_by=None, pause_reason=None,
        gpu_supported=True, gpu_reason=None, gpu_pct=35.0, gpu_name="GPU", gpu_history=(35.0,), mem_supported=True, mem_reason=None,
        mem_used=38 * GB, mem_total=128 * GB, mem_pct=29.0, mem_history=(29.0,), process_rss=3 * GB, process_history=(2.0,),
        device_backend="metal", models=(), models_total_bytes=None, models_error=None, can_restart=True, can_shutdown=True, restart_block_reason=None,
        update_job_running=False, step_gate_supported=True, version="0.3.0", last_error=None, consecutive_failures=0, unauthorized=False,
    )
    base.update(over)
    return Snapshot(**base)


def _loaded(name="Jundot/Qwen3.8-27B-oQ4e-mtp", provider="mlx", size=int(14.7 * GB), locked=False) -> ModelRow:
    rid = f"local:text_generation:{provider}:{name}"
    return ModelRow(key=rid, name=name, provider=provider, size_bytes=size, size_source="estimated", locked=locked, lockable=True, resident=True, target={"runtime_id": rid})


INSTALLED_PAYLOAD = {
    "schema": "models_installed_v1",
    "rows": [
        {"provider": "mlx", "artifact": "Jundot/Qwen3.8-27B-oQ4e-mtp", "size_bytes": 17 * GB, "catalog_id": None},
        {"provider": "mlx", "artifact": "mlx-community/Qwen3.6-27B-4bit", "size_bytes": 16 * GB, "catalog_id": "qwen3.6-27b"},
        {"provider": "mlx", "artifact": "mlx-community/Qwen1.5-0.5B-Chat-4bit", "size_bytes": GB // 3, "catalog_id": None},
        {"provider": "mlx", "artifact": "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", "size_bytes": 106 * GB, "catalog_id": None},
        {"provider": "lmstudio", "artifact": "qwen/qwen3.8-27b@q4_k_m", "size_bytes": 18 * GB, "catalog_id": "qwen3.8-27b", "type": "llm"},
        {"provider": "lmstudio", "artifact": "text-embedding-nomic-embed-text-v1.5", "size_bytes": 80 << 20, "catalog_id": None, "type": "embedding"},
        {"provider": "ollama", "artifact": "gemma3:1b", "size_bytes": 800 << 20, "catalog_id": "gemma-3-1b"},
    ]
    + [{"provider": "huggingface", "artifact": f"org{i:03d}/model-{chr(97 + i % 26)}{i:03d}", "size_bytes": (i + 1) << 20, "catalog_id": ("known" if i % 40 == 0 else None)} for i in range(75)],
    "errors": {},
}

AVAILABILITY_PAYLOAD = {
    "routes": [
        {"key": "input.text", "configured": True, "provider": "mlx", "model": "mlx-community/Qwen1.5-0.5B-Chat-4bit", "availability": {"status": "installed"}},
        {"key": "output.text", "configured": True, "provider": "mlx", "model": "mlx-community/Qwen1.5-0.5B-Chat-4bit", "availability": {"status": "installed"}},
        {"key": "output.image", "configured": True, "provider": "huggingface", "model": "AbstractFramework/flux.2-klein-4b-8bit", "availability": {"status": "missing"}},
        {"key": "input.image", "configured": False, "provider": "", "model": ""},
    ]
}


def _inputs(**over: Any) -> mm.MenuInputs:
    installed, err = mm.parse_installed(INSTALLED_PAYLOAD)
    kw: Dict[str, Any] = dict(
        snap=_snap(models=(_loaded(),), models_total_bytes=int(14.7 * GB)),
        autostart=mm.AutostartView("off", "Off"),
        apps=(),
        apps_fetched=True,
        models=mm.ModelsView(installed=installed, installed_error=err, defaults=mm.parse_defaults(AVAILABILITY_PAYLOAD), fetched=True),
    )
    kw.update(over)
    return mm.MenuInputs(**kw)


def _find(nodes, label_prefix: str) -> mm.Node:
    for n in mm.iter_nodes(nodes):
        if n.label.startswith(label_prefix):
            return n
    raise AssertionError(f"no node starting with {label_prefix!r} in:\n{mm.render_text(nodes)}")


# ------------------------------------------------------------------ menu


def test_top_level_order_matches_the_design() -> None:
    nodes = mm.build_menu(_inputs())
    labels = [n.label for n in nodes if not n.separator]
    assert labels[:2] == ["AbstractGateway — Running", "Ready · 1 model loaded · 14.7 GB"]
    order = ["Open Console", "Apps", "Workflows", "Pause Workflows", "Models", "Check for Updates…", "Restart AbstractGateway…", "Start AbstractGateway at login", "Help", "Quit AbstractGateway…"]
    positions = [labels.index(x) for x in order]
    assert positions == sorted(positions), labels
    assert not any(label.startswith("Loaded Models") for label in labels), "the old flat list is gone"
    assert _find(nodes, "Open Console").default is True


def test_models_leads_with_what_is_in_memory_and_eject_is_a_submenu_action() -> None:
    nodes = mm.build_menu(_inputs())
    models = _find(nodes, "Models")
    kids = [n for n in models.children if not n.separator]
    assert kids[0].label == "Loaded: 1 · 14.7 GB · 90.0 GB free" and kids[0].enabled is False
    row = kids[1]
    assert row.label == "✓ Qwen3.8-27B-oQ4e-mtp · 14.7 GB · MLX" and row.action is None and row.children
    eject = row.children[0]
    assert eject.label == "Eject — frees 14.7 GB" and eject.action == ("eject", "local:text_generation:mlx:Jundot/Qwen3.8-27B-oQ4e-mtp")
    assert "Size estimated from the weights" in [c.label for c in row.children]
    assert [n.label for n in models.children if not n.separator][-2:] == ["Load a Model", "Manage Models in Console…"]


def test_models_never_say_no_models_loaded_over_held_gateway_memory() -> None:
    """2026-09-25: the tray said "No models loaded" while the gateway process
    held 92 GB of MLX buffers the list could not attribute. With held memory
    above the noise floor and nothing listed, the header and the Models menu
    say what is held and the two ways out; below it (or unknown) nothing changes."""
    held = _snap(models=(), device_held_bytes=92 * GB)
    nodes = mm.build_menu(_inputs(snap=held))
    labels = [n.label for n in mm.iter_nodes(nodes) if not n.separator]
    assert "Ready · gateway still holds 92.0 GB (no model listed); eject or restart" in labels
    assert not any("no models loaded" in lab.lower() for lab in labels), labels
    models = [n.label for n in _find(nodes, "Models").children if not n.separator]
    assert models[0].endswith("gateway holds 92.0 GB")
    assert "Gateway still holds 92.0 GB (no model listed)" in models
    assert "Eject the held model in the Console, or restart the gateway to free it" in models

    for quiet in (None, 32 << 20):
        nodes = mm.build_menu(_inputs(snap=_snap(models=(), device_held_bytes=quiet)))
        labels = [n.label for n in mm.iter_nodes(nodes) if not n.separator]
        assert "Ready · no models loaded" in labels and "No models loaded" in labels
        assert not any("still holds" in lab for lab in labels)


def test_ejecting_row_is_disabled_and_says_so() -> None:
    row = _loaded()
    nodes = mm.build_menu(_inputs(models=mm.ModelsView(fetched=True, ejecting=(row.key,))))
    eject = _find(nodes, "Ejecting…")
    assert eject.enabled is False


def test_load_menu_defaults_first_then_engines_recognised_first_and_nothing_dropped() -> None:
    nodes = mm.build_menu(_inputs())
    load = _find(nodes, "Load a Model")
    top = [n.label for n in load.children if not n.separator]
    assert top[0] == "Your defaults"
    # input.text + output.text name the same model: ONE row.
    assert top[1] == "Text: Qwen1.5-0.5B-Chat-4bit · 341 MB · MLX"
    assert _find(load.children, "Text: ").action == ("load", "mlx", "mlx-community/Qwen1.5-0.5B-Chat-4bit", "text_generation")
    # A configured default that is not downloaded says so instead of failing on click.
    image = _find(load.children, "Image: ")
    assert image.enabled is False and image.label.endswith("missing")
    engines = [label for label in top if label.endswith(")") and label.split(" (")[0] in {"MLX", "LM Studio", "Ollama", "Hugging Face"}]
    assert engines == ["MLX (4)", "LM Studio (2)", "Ollama (1)", "Hugging Face (75)"]

    mlx = [n for n in _find(load.children, "MLX (").children if not n.separator]
    assert mlx[0].label.startswith("Qwen3.6-27B-4bit"), "a recognised catalog model leads"
    assert [n.label.split(" · ")[0] for n in mlx[1:]] == ["Qwen1.5-0.5B-Chat-4bit", "Qwen3.8-27B-oQ4e-mtp", "Qwen3.8-Flash-Next-oQ4e-mtp"]
    loaded = _find(load.children, "Qwen3.8-27B-oQ4e-mtp · 17.0 GB")
    assert loaded.checked is True and loaded.enabled is False and loaded.label.endswith("loaded")
    big = _find(load.children, "Qwen3.8-Flash-Next-oQ4e-mtp")
    assert big.label.endswith("more than free memory") and big.action[0] == "load"

    embed = _find(load.children, "text-embedding-nomic")
    assert embed.enabled is False and "embeddings" in embed.label
    lms = _find(load.children, "qwen3.8-27b@q4_k_m")
    assert lms.action == ("load", "lmstudio", "qwen/qwen3.8-27b@q4_k_m", "text_generation")

    # Every installed model has exactly one row somewhere under Load a Model.
    leaf_labels = [n.label for n in mm.iter_nodes(load.children) if n.children is None and not n.separator]
    for r in INSTALLED_PAYLOAD["rows"]:
        short = mm.short_model_name(r["artifact"])
        assert sum(1 for lab in leaf_labels if lab.startswith(short + " · ")) == 1, short


def test_a_long_engine_list_is_chunked_into_ranges_never_truncated() -> None:
    load = _find(mm.build_menu(_inputs()), "Load a Model")
    hf = _find(load.children, "Hugging Face (")
    labels = [n.label for n in hf.children]
    assert labels[0] == "Recognised models (2)"
    ranges = [n for n in hf.children[1:]]
    assert all(n.children for n in ranges) and [len(n.children) for n in ranges] == [30, 30, 13]
    assert [n.label for n in ranges] == ["model-a – model-k (30)", "model-k – model-u (30)", "model-u – model-z (13)"]
    assert sum(len(n.children) for n in hf.children) == 75


def test_loading_and_unfetched_states_are_visible() -> None:
    inp = _inputs(models=mm.ModelsView(fetched=False))
    load = _find(mm.build_menu(inp), "Load a Model")
    assert [n.label for n in load.children] == ["Reading the installed models…"]
    installed, _ = mm.parse_installed(INSTALLED_PAYLOAD)
    inp = _inputs(models=mm.ModelsView(installed=installed, fetched=True, loading=("mlx/mlx-community/Qwen1.5-0.5B-Chat-4bit",)))
    nodes = mm.build_menu(inp)
    assert _find(nodes, "Loading Qwen1.5-0.5B-Chat-4bit…").enabled is False
    assert _find(nodes, "Qwen1.5-0.5B-Chat-4bit · loading…").enabled is False


def test_engine_errors_are_one_visible_line() -> None:
    installed, err = mm.parse_installed({"rows": [], "errors": {"lmstudio": "lms not found", "ollama": ""}})
    assert installed == () and err == "LM Studio: lms not found"
    load = _find(mm.build_menu(_inputs(models=mm.ModelsView(installed=(), installed_error=err, fetched=True))), "Load a Model")
    assert "Not listed: LM Studio: lms not found" in [n.label for n in load.children]


def test_unreachable_gateway_disables_model_actions() -> None:
    nodes = mm.build_menu(_inputs(snap=_snap(gateway_state="unreachable", reachable=False, models=(_loaded(),))))
    assert _find(nodes, "Models").enabled is False
    assert _find(nodes, "Eject —").enabled is False


# ------------------------------------------------------------- autostart


@pytest.mark.parametrize(
    "state,label,checked,action",
    [
        ("on", "Start AbstractGateway at login", True, ("toggle_autostart",)),
        ("off", "Start AbstractGateway at login", False, ("toggle_autostart",)),
        ("broken", "Start AbstractGateway at login — needs repair", False, ("toggle_autostart",)),
        ("other", "Start AbstractGateway at login (another gateway is registered)", False, ("toggle_autostart",)),
        ("unknown", "Start AbstractGateway at login", False, None),
    ],
)
def test_the_login_item_reflects_the_truthful_state(state: str, label: str, checked: bool, action: Any) -> None:
    view = mm.AutostartView(state, "s", ("the program it starts is gone: /gone/abstractgateway (the gateway was moved, reinstalled elsewhere or removed)",) if state == "broken" else ())
    nodes = mm.autostart_nodes(view)
    assert nodes[0].label == label and nodes[0].checked is checked and nodes[0].action == action
    if state == "broken":
        assert nodes[1].label.startswith("   the program it starts is gone: /gone/abstractgateway") and nodes[1].enabled is False
    busy = mm.autostart_nodes(mm.replace(view, busy=True))
    assert busy[0].enabled is False and busy[0].label.endswith("…")


# ------------------------------------------------------------------ apps


def _apps_payload(install_allowed: bool = True, **rows: Dict[str, Any]) -> Dict[str, Any]:
    base = {a[0]: {"id": a[0], "installed": False, "running": False, "status": "not_installed", "install_available": install_allowed} for a in tray_apps.WEB_APPS}
    for k, v in rows.items():
        base[k].update(v)
    return {"apps": list(base.values()), "install_allowed": install_allowed}


def test_app_rows_cover_every_state_and_the_six_apps_in_order() -> None:
    # Mission HH: stack order, one short line per app, never a reason.
    payload = _apps_payload(
        install_allowed=False,
        observer={"installed": True, "running": True, "status": "running", "url": "http://127.0.0.1:18841/", "version": "0.4.1"},
        flow={"installed": True, "status": "stopped", "last_error": "exited 1"},
        code={"installed": True, "status": "starting"},
        continuum={"install_available": False, "install_blocked_reason": "Installing software on the gateway host is turned off for this gateway."},
    )
    globals_found = {"entity": {"found_by": ["path:/usr/local/bin/abstractentity"], "launch": ["/usr/local/bin/abstractentity"]}}
    entries = tray_apps.build_app_entries(payload, None, globals_found=globals_found, assistant={"found_by": ["bundle:/Applications/AbstractAssistant.app"], "launch": ["open", "-a", "/Applications/AbstractAssistant.app"], "source": "bundle"}, local_running={})
    assert [e.name for e in entries] == ["Observer", "Continuum", "Code", "Entity", "Flow", "Assistant"]
    apps = _find(mm.build_menu(_inputs(apps=entries)), "Apps")
    labels = [n.label for n in apps.children if not n.separator]
    assert labels == [
        "Open Observer",
        "Continuum",
        "Code — starting…",
        "Open Entity",
        "Open Flow",
        "Launch Assistant",
        mm.INSTALLS_OFF_LINE,
        "Manage Apps in Console…",
    ]
    assert _find(mm.build_menu(_inputs(apps=entries)), "Continuum").enabled is False
    actions = {n.label: n.action for n in apps.children if n.action}
    assert actions["Open Observer"] == ("app_open", "observer") and actions["Open Flow"] == ("app_launch", "flow")
    assert actions["Open Entity"] == ("app_launch_global", "entity") and actions["Launch Assistant"] == ("assistant_launch",)
    assert actions["Manage Apps in Console…"] == ("open_console_tab", "apps")


def test_not_installed_apps_offer_install_and_a_missing_list_is_named() -> None:
    entries = tray_apps.build_app_entries(_apps_payload(), None, globals_found={}, assistant={}, local_running={})
    apps = _find(mm.build_menu(_inputs(apps=entries)), "Apps")
    labels = [n.label for n in apps.children if not n.separator]
    assert labels[:5] == ["Install Observer…", "Install Continuum…", "Install Code…", "Install Entity…", "Install Flow…"]
    assert labels[5] == "Assistant" and mm.INSTALLS_OFF_LINE not in labels
    down = tray_apps.build_app_entries(None, "unreachable: connection refused", globals_found={}, assistant={}, local_running={})
    assert all(e.status == "unknown" and "connection refused" in e.detail for e in down[:5])
    # The menu names the apps it could not read, greyed, without the error.
    down_labels = [n.label for n in _find(mm.build_menu(_inputs(apps=down)), "Apps").children if not n.separator]
    assert down_labels[:5] == ["Observer", "Continuum", "Code", "Entity", "Flow"]
    assert "Apps" == _find(mm.build_menu(_inputs(apps_fetched=False)), "Apps").label
    assert _find(mm.build_menu(_inputs(apps_fetched=False)), "Looking for apps…").enabled is False


def _external(app_id: str, port: int, version: Optional[str] = None) -> Dict[str, Any]:
    return {
        "installed": True, "running": True, "status": "running", "managed": False, "source": "external",
        "url": f"http://127.0.0.1:{port}/", "port": port, "version": version, "actions": ["open"],
        "external": {"port": port, "version": version, "detail": f"Started outside the gateway on port {port}"},
    }


def test_external_apps_open_through_the_gateway_and_a_blocked_install_is_one_bottom_line() -> None:
    """The operator's screen (2026-09-24): five apps running from the dev
    stack, installs off for a LAN-bound gateway. Every running app is "Open X"
    (the gateway's handover), no per-app reason, one footer line."""
    payload = _apps_payload(
        install_allowed=False,
        observer=_external("observer", 3001, "0.1.12"),
        continuum=_external("continuum", 3002, "0.2.0"),
        code=dict(_external("code", 3003, "0.4.2"), interfaces=[{"kind": "web"}, {"kind": "tui", "installed": True, "launch_available": True}]),
        entity=_external("entity", 3004, "0.1.0"),
    )
    entries = tray_apps.build_app_entries(payload, None, globals_found={}, assistant={"launch": ["open", "-a", "/Applications/AbstractAssistant.app"], "source": "bundle"}, local_running={})
    assert [(e.id, e.status, e.source) for e in entries[:5]] == [
        ("observer", "running", "external"), ("continuum", "running", "external"), ("code", "running", "external"),
        ("entity", "running", "external"), ("flow", "not_installed", ""),
    ]
    assert entries[0].detail == "started outside the gateway on port 3001"
    apps = _find(mm.build_menu(_inputs(apps=entries)), "Apps")
    labels = [n.label for n in apps.children if not n.separator]
    assert labels == [
        "Open Observer", "Open Continuum", "Open Code", "Open Code in Terminal", "Open Entity", "Flow",
        "Launch Assistant", "Installs are off for this gateway · Console → Apps", "Manage Apps in Console…",
    ]
    actions = {n.label: n.action for n in apps.children if n.action}
    assert actions["Open Observer"] == ("app_open", "observer") and actions["Open Entity"] == ("app_open", "entity")
    assert not any("can't install" in l or "turned off" in l for l in labels)
    assert all(len(l) <= 40 for l in labels if l != mm.INSTALLS_OFF_LINE), labels


def _probes(tmp_path: Path, *, which: Optional[Dict[str, str]] = None, files: Optional[List[str]] = None, spec: Any = None, platform: str = "darwin") -> tray_apps.Probes:
    present = set(files or [])
    return tray_apps.Probes(
        which=lambda name: (which or {}).get(name),
        exists=lambda p: p in present,
        find_spec=lambda name: spec,
        npm_root=lambda: None,
        platform=platform,
        home=tmp_path,
        scripts_dir=str(tmp_path / "venv" / "bin"),
        read_text=lambda p: '{"bin": {"abstractobserver": "./bin/cli.js"}}',
    )


def test_assistant_detection_bundle_script_spec_and_the_namespace_trap(tmp_path: Path) -> None:
    ns = types.SimpleNamespace(origin=None, submodule_search_locations=["/cwd/abstractassistant"])
    real = types.SimpleNamespace(origin="/site/abstractassistant/__init__.py")
    # A folder named abstractassistant in the working directory is NOT an install.
    got = tray_apps.detect_assistant(_probes(tmp_path, spec=ns), entry_point=lambda: "abstractassistant.cli:main")
    assert got["launch"] is None and any(f.startswith("namespace-only") for f in got["found_by"])
    got = tray_apps.detect_assistant(_probes(tmp_path, spec=real), entry_point=lambda: "abstractassistant.cli:main", python="/py")
    assert got["source"] == "python" and got["launch"][:2] == ["/py", "-c"] and "from abstractassistant.cli import main" in got["launch"][2]
    got = tray_apps.detect_assistant(_probes(tmp_path, which={"abstractassistant": "/bin/abstractassistant"}, spec=real), entry_point=lambda: None)
    assert got["source"] == "script" and got["launch"] == ["/bin/abstractassistant"]
    bundle = "/Applications/AbstractAssistant.app"
    got = tray_apps.detect_assistant(_probes(tmp_path, files=[bundle], which={"abstractassistant": "/bin/abstractassistant"}), entry_point=lambda: None)
    assert got["source"] == "bundle" and got["launch"] == ["open", "-a", bundle]
    assert got["found_by"] == [f"bundle:{bundle}", "script:/bin/abstractassistant"]
    # The gateway's own scripts folder counts even when it is not on PATH.
    script = str(tmp_path / "venv" / "bin" / "abstractassistant")
    got = tray_apps.detect_assistant(_probes(tmp_path, files=[script], platform="linux"), entry_point=lambda: None)
    assert got["launch"] == [script]
    assert tray_apps.detect_assistant(_probes(tmp_path), entry_point=lambda: None)["launch"] is None


def test_global_web_app_detection_uses_the_app_command_not_the_terminal_one(tmp_path: Path) -> None:
    # `abstractcode` on PATH is the TERMINAL app (a cargo binary here): it is not Code (web).
    got = tray_apps.detect_global_web_app("code", _probes(tmp_path, which={"abstractcode": "/c/abstractcode"}), npm_root=None)
    assert got == {"found_by": [], "launch": None}
    got = tray_apps.detect_global_web_app("code", _probes(tmp_path, which={"abstractcode-web": "/n/abstractcode-web"}), npm_root=None)
    assert got["launch"] == ["/n/abstractcode-web"]
    root = str(tmp_path / "lib" / "node_modules")
    pkg_json = str(Path(root) / "@abstractframework" / "observer" / "package.json")
    got = tray_apps.detect_global_web_app("observer", _probes(tmp_path, which={"node": "/n/node"}, files=[pkg_json]), npm_root=root)
    assert got["found_by"] == [f"npm-global:{Path(root) / '@abstractframework' / 'observer'}"]
    assert got["launch"] == ["/n/node", str(Path(root) / "@abstractframework" / "observer" / "bin" / "cli.js")]


def test_launched_apps_never_inherit_tokens_or_gateway_settings() -> None:
    env = tray_apps.scrubbed_env({"PATH": "/bin", "HOME": "/h", "ABSTRACTGATEWAY_AUTH_TOKEN": "x", "ABSTRACTCORE_CONFIG_FILE": "c", "OPENAI_API_KEY": "k", "HUGGINGFACE_HUB_TOKEN": "t", "DB_PASSWORD": "p", "SSH_KEY": "s", "LANG": "C"})
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C"}


# ----------------------------------------------------------------- flat


def test_flat_degrade_keeps_every_row_and_drops_every_submenu() -> None:
    nodes = mm.build_menu(_inputs())
    flat = mm.flatten(nodes)
    assert all(n.children is None for n in flat)
    acts = lambda ns: sorted(repr(n.action) for n in mm.iter_nodes(ns) if n.action)  # noqa: E731
    assert acts(flat) == acts(nodes), "the flat menu can do exactly what the tree can"
    eject = next(n for n in flat if n.action and n.action[0] == "eject")
    assert eject.label.startswith("Models › ✓ Qwen3.8-27B-oQ4e-mtp · 14.7 GB · MLX › Eject")
    assert any(n.label.startswith("Models › Load a Model › Hugging Face (75) › model-a – model-k (30) › model-a026 · ") for n in flat)


# ----------------------------------------------------------- render text


def test_render_text_marks_submenus_checks_and_disabled_rows() -> None:
    text = mm.render_text(mm.build_menu(_inputs(autostart=mm.AutostartView("on", "On"))))
    assert "Models ▸" in text and "☑ Start AbstractGateway at login" in text
    assert "[AbstractGateway — Running]" in text and "Open Console   (default)" in text


# ------------------------------------------------------------------ actions


class FakeClient:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.load_result = Result(True, 200, {"ok": True, "success": True})
        self.unload_result = Result(True, 200, {"ok": True, "success": True})

    def load_model(self, **kw):
        self.calls.append(("load", kw))
        return self.load_result

    def unload_model(self, target, force=False):
        self.calls.append(("unload", target, force))
        return self.unload_result

    def app_open(self, app_id):
        self.calls.append(("open", app_id))
        return Result(True, 200, {"ok": True, "open_url": f"/apps/handover/code-{app_id}"})

    def app_launch(self, app_id):
        self.calls.append(("launch", app_id))
        return Result(True, 200, {"ok": True})


@pytest.fixture()
def tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway.tray import app as tray_app

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path)})
    app.client = FakeClient()  # type: ignore[assignment]
    app._snap = _snap(models=(_loaded(),))
    events: List[tuple] = []
    app._notify = lambda title, msg: events.append(("notify", title, msg))  # type: ignore[assignment]
    app._info = lambda title, body, style="informational": events.append(("info", title, body))  # type: ignore[assignment]
    app._bg = lambda fn, name="": fn()  # type: ignore[assignment]
    app._force_menu_rebuild = lambda: None  # type: ignore[assignment]
    app.sampler.poke = lambda: None  # type: ignore[assignment]
    opened: List[str] = []
    monkeypatch.setattr(tray_app.plat, "open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda *a, **k: True)
    app.events, app.opened = events, opened  # type: ignore[attr-defined]
    return app


def test_every_action_in_the_menu_has_a_handler(tray) -> None:
    """Every action any menu state can produce maps to a callable (read from
    the table, not by calling them: some start processes)."""
    entries = tray_apps.build_app_entries(
        {"apps": [{"id": "observer", "installed": True, "running": True, "url": "u"}, {"id": "flow", "installed": True, "status": "stopped"}, {"id": "code", "installed": False, "install_available": True}]},
        None,
        globals_found={"entity": {"found_by": ["path:/x"], "launch": ["/x"]}},
        assistant={"launch": ["/a"], "source": "script"},
        local_running={"continuum": "http://127.0.0.1:3004/"},
    )
    names = set()
    for snap in (_snap(models=(_loaded(),)), _snap(gateway_state="unreachable", reachable=False), _snap(paused=True, gateway_state="paused")):
        for pending in (None, "Quit AbstractGateway"):
            nodes = mm.build_menu(_inputs(snap=snap, apps=entries, pending_label=pending, tk_available=True, network=mm.parse_network(_net(restart_required=True))))
            names |= {n.action[0] for n in mm.iter_nodes(nodes) if n.action}
    table = tray.dispatch_table()
    assert names <= set(table), names - set(table)
    assert {"app_open", "app_launch", "app_install", "app_launch_global", "app_open_url", "assistant_launch", "eject", "load", "toggle_autostart", "network_set", "network_restart", "copy_address"} <= names
    assert all(callable(fn) for fn in table.values())
    with pytest.raises(KeyError):
        tray.dispatch(("no_such_action",))


def test_load_success_and_failure_are_both_visible_with_the_reason(tray) -> None:
    installed, _ = mm.parse_installed(INSTALLED_PAYLOAD)
    tray._models_view = mm.ModelsView(installed=installed, fetched=True)
    tray.dispatch(("load", "mlx", "mlx-community/Qwen1.5-0.5B-Chat-4bit", "text_generation"))
    assert tray.client.calls[-1] == ("load", {"provider": "mlx", "model": "mlx-community/Qwen1.5-0.5B-Chat-4bit", "task": "text_generation"})
    titles = [e[1] for e in tray.events]
    assert titles == ["Loading Qwen1.5-0.5B-Chat-4bit", "Loaded Qwen1.5-0.5B-Chat-4bit"]
    assert tray._loading == {}

    tray.events.clear()
    tray.client.load_result = Result(True, 200, {"ok": True, "success": False, "error": {"code": "model_residency_error", "message": "MLX model not found in the HF cache"}})
    tray.dispatch(("load", "mlx", "mlx-community/Qwen1.5-0.5B-Chat-4bit", "text_generation"))
    kind, title, body = tray.events[-1]
    assert kind == "info" and title == "Couldn't load Qwen1.5-0.5B-Chat-4bit"
    assert "MLX model not found in the HF cache" in body and "model_residency_error" in body


def test_loading_more_than_free_memory_asks_first(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    installed, _ = mm.parse_installed(INSTALLED_PAYLOAD)
    tray._models_view = mm.ModelsView(installed=installed, fetched=True)
    asked: List[str] = []
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda title, body, **k: asked.append(body) or False)
    tray.dispatch(("load", "mlx", "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", "text_generation"))
    assert asked and "about 106 GB" in asked[0] and "90.0 GB is free" in asked[0]
    assert not any(c[0] == "load" for c in tray.client.calls), "declined: nothing loaded"


def test_eject_goes_through_unload_with_the_runtime_target_and_reports_what_it_freed(tray) -> None:
    row = _loaded()
    tray.dispatch(("eject", row.key))
    assert tray.client.calls == [("unload", {"runtime_id": row.key}, False)]
    assert tray.events[-1] == ("notify", "Model ejected", "Jundot/Qwen3.8-27B-oQ4e-mtp · freed 14.7 GB")
    assert tray._ejecting == set()
    tray.client.unload_result = Result(True, 200, {"ok": True, "success": False, "error": {"message": "a call would not stop"}})
    tray.dispatch(("eject", row.key))
    assert tray.events[-1][0] == "info" and "a call would not stop" in tray.events[-1][2]


def test_eject_confirmation_mentions_running_work(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    seen: List[str] = []
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda title, body, **k: seen.append(body) or False)
    tray._snap = _snap(models=(_loaded(),), inflight_ticks=2)
    tray.dispatch(("eject", _loaded().key))
    assert "stopped first" in seen[0] and tray.client.calls == []


def test_toggle_registers_this_gateways_port_and_reads_back(tray, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway import autostart

    seen: Dict[str, Any] = {}

    def _enable(**kw):
        seen.update(kw)
        return {"ok": True, "after": {"state": "on", "summary": "On — a LaunchAgent starts the gateway at login on port 18840"}}

    monkeypatch.setattr(autostart, "enable_autostart", _enable)
    monkeypatch.setattr(autostart, "autostart_status", lambda **kw: {"state": "on", "summary": "On", "problems": []})
    tray._autostart = mm.AutostartView("off", "Off")
    tray.dispatch(("toggle_autostart",))
    # No host/port (2026-09-24): the login item runs plain `serve` and the
    # Network setting binds it; this tray's 127.0.0.1 URL must not be written
    # into the setting (it would undo a "Local network" choice).
    assert seen == {"data_dir": tmp_path, "actor": "tray"}
    assert tray._autostart.state == "on" and tray.events[-1][1] == "Starts at login"

    monkeypatch.setattr(autostart, "disable_autostart", lambda **kw: {"ok": False, "error": "permission denied: ~/Library/LaunchAgents", "after": {"state": "on"}})
    tray.dispatch(("toggle_autostart",))
    assert tray.events[-1] == ("info", "Couldn't change Start at login", "permission denied: ~/Library/LaunchAgents")


def test_open_app_uses_the_signed_in_handover(tray) -> None:
    tray.dispatch(("app_open", "observer"))
    assert tray.opened == ["http://127.0.0.1:18840/apps/handover/code-observer"]
    tray.dispatch(("app_launch", "flow"))
    assert ("launch", "flow") in tray.client.calls and tray.opened[-1].endswith("/apps/handover/code-flow")


def test_open_console_mints_a_claim_link_not_the_plain_url(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import signin

    monkeypatch.setattr(signin, "_user_auth_enabled", lambda d: True)
    monkeypatch.setattr(signin, "_mint_default", lambda d: {"code": "agclaim_" + "x" * 30})
    tray.dispatch(("open_console",))
    assert tray.opened == ["http://127.0.0.1:18840/console#claim=agclaim_" + "x" * 30]
    tray.dispatch(("open_console_tab", "models"))
    assert tray.opened[-1].endswith("#claim=agclaim_" + "x" * 30 + "&tab=models")


def test_console_link_falls_back_to_plain_and_says_why(tmp_path: Path) -> None:
    from abstractgateway.tray.signin import console_link

    token_mode = console_link("http://127.0.0.1:8080", "/console", tmp_path, user_auth=lambda d: False, mint=lambda d: pytest.fail("minted in token mode"))
    assert token_mode.url == "http://127.0.0.1:8080/console" and not token_mode.signed_in and "static token" in (token_mode.note or "")

    def _boom(d):
        raise PermissionError("auth/claims is read-only")

    failed = console_link("http://127.0.0.1:8080", "/console", tmp_path, tab="runtimes", user_auth=lambda d: True, mint=_boom)
    assert failed.url == "http://127.0.0.1:8080/console#runtimes" and "read-only" in (failed.note or "")
    ok = console_link("http://127.0.0.1:8080/", "/console", tmp_path, user_auth=lambda d: None, mint=lambda d: {"code": "agclaim_abc"})
    assert ok.signed_in and ok.url == "http://127.0.0.1:8080/console#claim=agclaim_abc"


def test_real_claim_minting_writes_a_redeemable_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default minter is the same code as `abstractgateway claim`: the
    code it returns is redeemed once by first_run.redeem_claim."""
    from abstractgateway.first_run import ClaimError, redeem_claim
    from abstractgateway.tray.signin import TRAY_CLAIM_TTL_S, _mint_default

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    minted = _mint_default(tmp_path)
    assert minted["ttl_s"] == TRAY_CLAIM_TTL_S
    rec = redeem_claim(minted["code"], data_dir=tmp_path)
    assert rec["created_by"] == "tray" and rec["user_id"] == "admin"
    with pytest.raises(ClaimError):
        redeem_claim(minted["code"], data_dir=tmp_path)


def test_a_broken_registration_is_repaired_by_the_switch_not_removed(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import autostart

    called: List[str] = []
    monkeypatch.setattr(autostart, "enable_autostart", lambda **kw: called.append("enable") or {"ok": True, "after": {"state": "on", "summary": "On"}})
    monkeypatch.setattr(autostart, "disable_autostart", lambda **kw: called.append("disable") or {"ok": True, "after": {"state": "off"}})
    monkeypatch.setattr(autostart, "autostart_status", lambda **kw: {"state": "on", "summary": "On", "problems": []})
    tray._autostart = mm.AutostartView("broken", "Registered but it will not start", ("the program it starts is gone: /x",))
    tray.dispatch(("toggle_autostart",))
    tray._autostart = mm.AutostartView("other", "Registered for another gateway")
    tray.dispatch(("toggle_autostart",))
    assert called == ["enable", "enable"]


# ------------------------------------------------------------------ network
# Mission R's contract (GET/POST /api/gateway/network). Built against the
# contract with a fake: R's backend had not landed when this was written.

NETWORK_LOCAL = {
    "configured": {"mode": "localhost", "port": 8080},
    "effective": {"mode": "localhost", "bind_host": "127.0.0.1", "port": 8080, "overridden_by_cli": False},
    "restart_required": False,
    "auth": {"user_auth": True, "token_auth": False, "ok_for_mode": True},
    "addresses": [
        {"kind": "loopback", "url": "http://127.0.0.1:8080", "host": "127.0.0.1", "port": 8080},
        {"kind": "lan", "url": "http://192.168.1.23:8080", "host": "192.168.1.23", "port": 8080, "interface": "Wi-Fi", "note": "after restart"},
        {"kind": "hostname", "url": "http://mymac.local:8080", "host": "mymac.local", "port": 8080},
    ],
    "warnings": [],
    "copy_hint": "http://127.0.0.1:8080",
}


def _net(**over: Any) -> Dict[str, Any]:
    import copy

    d = copy.deepcopy(NETWORK_LOCAL)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


def test_network_line_under_the_status_and_the_radio_submenu() -> None:
    nodes = mm.build_menu(_inputs(network=mm.parse_network(NETWORK_LOCAL)))
    labels = [n.label for n in nodes if not n.separator]
    assert labels[2] == "http://127.0.0.1:8080 · localhost only"
    net = _find(nodes, "Network")
    kids = [n for n in net.children if not n.separator]
    assert kids[0].label == "Now: localhost only · 127.0.0.1:8080" and kids[0].enabled is False
    radios = [(n.label, n.checked, n.radio, n.action) for n in kids[1:4]]
    assert radios == [
        ("Localhost only", True, True, ("network_set", "localhost")),
        ("Local network", False, True, ("network_set", "lan")),
        ("Internet…", False, True, ("network_set", "internet")),
    ]
    assert not any(n.label.startswith("Restart to apply") for n in net.children)
    text = mm.render_text((net,))
    assert "● Localhost only" in text and "○ Local network" in text and "○ Internet…" in text
    order = [n.label for n in nodes if not n.separator]
    assert order.index("Start AbstractGateway at login") < order.index("Network") < order.index("Help")


def test_a_pending_change_says_restart_required_everywhere_and_offers_the_restart() -> None:
    nv = mm.parse_network(_net(configured={"mode": "lan"}, restart_required=True, warnings=["Other devices on this network can reach the sign-in page."]))
    nodes = mm.build_menu(_inputs(network=nv))
    assert [n.label for n in nodes if not n.separator][2] == "http://127.0.0.1:8080 · localhost only · restart required"
    net = _find(nodes, "Network — restart required")
    assert _find(net.children, "Local network").checked is True, "the radio shows what is SET; 'Now:' shows what runs"
    assert _find(net.children, "Restart to apply (local network)").action == ("network_restart",)
    assert _find(net.children, "⚠ Other devices").enabled is False


def test_auth_not_ok_for_mode_is_shown_with_the_fix() -> None:
    nv = mm.parse_network(_net(auth={"ok_for_mode": False, "fix": "abstractgateway-config bootstrap-admin"}))
    net = _find(mm.build_menu(_inputs(network=nv)), "Network")
    assert _find(net.children, "⚠ Sign-in is not set up for this mode: abstractgateway-config bootstrap-admin").enabled is False


def test_copy_address_lists_every_address_url_first() -> None:
    nodes = mm.build_menu(_inputs(network=mm.parse_network(NETWORK_LOCAL)))
    copy = _find(nodes, "Copy Address")
    assert [(n.label, n.action) for n in copy.children] == [
        ("http://127.0.0.1:8080", ("copy_address", "http://127.0.0.1:8080")),
        ("http://192.168.1.23:8080 (Wi-Fi)", ("copy_address", "http://192.168.1.23:8080")),
        ("http://mymac.local:8080", ("copy_address", "http://mymac.local:8080")),
    ]


def test_without_the_network_route_the_menu_says_so_and_still_copies_this_address() -> None:
    nv = mm.NetworkView(available=False, error="this gateway has no network settings yet")
    nodes = mm.build_menu(_inputs(network=nv, base_url="http://127.0.0.1:18840"))
    assert [n.label for n in nodes if not n.separator][2] != "http://127.0.0.1:18840 · localhost only"
    assert _find(nodes, "Network").children[0].label == "this gateway has no network settings yet"
    copy = _find(nodes, "Copy Address")
    assert copy.children[0].action == ("copy_address", "http://127.0.0.1:18840")
    assert copy.children[1].label == "Other addresses: this gateway has no network settings yet"


class NetClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.state = _net()
        self.post_result: Optional[Result] = None
        self.restart_route = True

    def network(self):
        return Result(True, 200, self.state)

    def set_network(self, mode, port=None, acknowledge_internet=False):
        self.calls.append(("set_network", mode, acknowledge_internet))
        if self.post_result is not None:
            return self.post_result
        self.state = _net(configured={"mode": mode}, restart_required=True)
        return Result(True, 200, {"ok": True, "configured": self.state["configured"], "restart_required": True, "warnings": []})

    def network_restart(self):
        self.calls.append(("network_restart",))
        return Result(True, 200, {"ok": True}) if self.restart_route else Result(False, 404, {"detail": "Not Found"}, error="Not Found")

    def restart(self, reason=None):
        self.calls.append(("restart", reason))
        return Result(True, 200, {"ok": True})


@pytest.fixture()
def net_tray(tray):
    tray.client = NetClient()
    tray.sampler.set_override = lambda s: None
    tray._refresh_network()
    return tray


def test_switching_to_local_network_posts_and_reports_restart_required(net_tray) -> None:
    net_tray.dispatch(("network_set", "lan"))
    assert ("set_network", "lan", False) in net_tray.client.calls
    assert net_tray._network.restart_required and net_tray._network.configured_mode == "lan"
    assert net_tray.events[-1][1] == "Network: local network" and "Restart to apply" in net_tray.events[-1][2]


def test_internet_needs_an_explicit_acknowledgement(net_tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    net_tray.client.state = _net(warnings=["Port 8080 is forwarded by your router."])
    net_tray._refresh_network()
    shown: List[str] = []
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda title, body, **k: shown.append(body) or False)
    net_tray.dispatch(("network_set", "internet"))
    assert not any(c[0] == "set_network" for c in net_tray.client.calls), "declined: nothing posted"
    assert "Port 8080 is forwarded by your router." in shown[0]
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda *a, **k: True)
    net_tray.dispatch(("network_set", "internet"))
    assert ("set_network", "internet", True) in net_tray.client.calls


def test_a_refused_switch_shows_the_reason_and_the_fix(net_tray) -> None:
    net_tray.client.post_result = Result(False, 409, {"ok": False, "refused_reason": "Internet mode needs user sign-in; this gateway runs without it.", "auth": {"fix": "abstractgateway-config bootstrap-admin"}}, error="refused")
    net_tray.dispatch(("network_set", "lan"))
    kind, title, body = net_tray.events[-1]
    assert kind == "info" and title == "Couldn't switch to local network"
    assert "needs user sign-in" in body and "To fix: abstractgateway-config bootstrap-admin" in body


def test_restart_to_apply_uses_the_network_route_else_the_normal_restart(net_tray) -> None:
    net_tray.dispatch(("network_restart",))
    assert ("network_restart",) in net_tray.client.calls and not any(c[0] == "restart" for c in net_tray.client.calls)
    net_tray.client.restart_route = False
    net_tray.dispatch(("network_restart",))
    assert ("restart", "network") in net_tray.client.calls


def test_copy_address_copies_and_says_what(net_tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    copied: List[str] = []
    monkeypatch.setattr(tray_app.plat, "copy_to_clipboard", lambda t: copied.append(t) or True)
    net_tray.dispatch(("copy_address", "http://192.168.1.23:8080"))
    assert copied == ["http://192.168.1.23:8080"] and net_tray.events[-1][1] == "Copied http://192.168.1.23:8080"
    monkeypatch.setattr(tray_app.plat, "copy_to_clipboard", lambda t: False)
    net_tray.dispatch(("copy_address", "http://mymac.local:8080"))
    assert net_tray.events[-1] == ("info", "Address", "http://mymac.local:8080")


def test_a_missing_network_route_is_named_not_hidden(tray) -> None:
    class Old(FakeClient):
        def network(self):
            return Result(False, 404, {"detail": "Not Found"}, error="Not Found")

    tray.client = Old()
    tray._refresh_network()
    assert tray._network.available is False and "newer gateway" in (tray._network.error or "")
