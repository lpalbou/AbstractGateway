"""The redesigned tray menu (`tray/menu_v2.py`, 2026-11-01) read off the pure
model, and the switch between it and the classic menu (`tray/menu_model.py`,
kept whole for a roll-back).

- The shape: status lines, the two doors, *Needs you* rows at the top level,
  Conversations / Models / Apps, Connect / Settings / Help, the lifecycle
  rows; the product name on the header and Quit only.
- Nothing lost: every action the classic menu produces, the redesign produces
  too (ADR-0026), and every action it produces has a handler.
- The switch: launch flag > machine preference > the redesign; Settings ▸
  *Classic menu* and Help ▸ *New menu* write the preference and rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway.tray import menu_model as mm
from abstractgateway.tray import menu_v2 as v2
from abstractgateway.tray.apps import AppEntry
from abstractgateway.tray.sampler import AutomationRow, ModelRow, RunRow, Snapshot

pytestmark = pytest.mark.basic

GB = 1 << 30


def _snap(**over: Any) -> Snapshot:
    base: Dict[str, Any] = dict(
        ts=0.0, gateway_state="running", reachable=True, paused=False, inflight_ticks=0, paused_by=None, pause_reason=None,
        gpu_supported=True, gpu_reason=None, gpu_pct=20.0, gpu_name="Apple M5 Max", gpu_history=(20.0,), mem_supported=True, mem_reason=None,
        mem_used=int(40.6 * GB), mem_total=128 * GB, mem_pct=32.0, mem_history=(32.0,), process_rss=int(10.9 * GB), process_history=(10.9,),
        device_backend="metal", models=(), models_total_bytes=None, models_error=None, can_restart=True, can_shutdown=True, restart_block_reason=None,
        update_job_running=False, step_gate_supported=True, version="0.18.0", last_error=None, consecutive_failures=0, unauthorized=False,
    )
    base.update(over)
    return Snapshot(**base)


def _loaded(name="mlx-community/Qwen1.5-0.5B-Chat-4bit", provider="mlx", size=249 * (1 << 20), locked=False) -> ModelRow:
    rid = f"local:text_generation:{provider}:{name}"
    return ModelRow(key=rid, name=name, provider=provider, size_bytes=size, size_source="reported", locked=locked, lockable=True, resident=True, target={"runtime_id": rid})


RUNNING = RunRow("r1", "coding-agent:coder", "running", 4, 130.0, 1.0, observer_path="/apps/observer/#run/r1", title="Refactor the sampler", title_source="prompt")
APPROVAL = RunRow("r2", "coding-agent:coder", "waiting", 3, 40.0, 1.0, observer_path="/apps/observer/#run/r2", title="Clean the build folder", title_source="prompt", wait_kind="tool_approval", wait_summary="execute_command", wait_tool="execute_command", wait_key="k2", wait_run_id="r2")
QUESTION = RunRow("r3", "deep-research:main", "waiting", 2, 20.0, 1.0, observer_path="/apps/observer/#run/r3", title="Plan the trip", title_source="prompt", wait_kind="ask_user", wait_summary="Which dates?", wait_key="k3", wait_run_id="r3")
DONE = RunRow("r4", "deep-research:main", "completed", 7, 102.0, 1.0, observer_path="/apps/observer/#run/r4", title="Write the changelog", title_source="prompt")
DAILY = AutomationRow("a1", "Daily digest", "active", schedule_text="Every day at 09:00 · next Fri 17 Oct 09:00", capabilities=("run_now", "pause"), trigger_text="Every day at 09:00")


def _apps(assistant_status="available", assistant_installable=False) -> tuple:
    return (
        AppEntry("observer", "Observer", "running", "gateway", url="http://127.0.0.1:3001"),
        AppEntry("continuum", "Continuum", "stopped", "gateway"),
        AppEntry("code", "Code", "not_installed", "", install_available=True),
        AppEntry("entity", "Entity", "not_installed", ""),
        AppEntry("flow", "Flow", "not_installed", "", install_available=True),
        AppEntry("assistant", "Assistant", assistant_status, "python", install_available=assistant_installable),
    )


def _inputs(**over: Any) -> mm.MenuInputs:
    kw: Dict[str, Any] = dict(snap=_snap(), apps=_apps(), apps_fetched=True, tk_available=True)
    kw.update(over)
    return mm.MenuInputs(**kw)


def _labels(nodes) -> List[str]:
    return [n.label for n in nodes if not n.separator]


def _sub(nodes, label: str):
    for n in nodes:
        if n.label == label and n.children is not None:
            return n.children
    raise AssertionError(f"no submenu {label!r} in {_labels(nodes)}")


# ---------------------------------------------------------------- the shape


def test_the_top_level_reads_act_look_set_up_lifecycle() -> None:
    snap = _snap(runs=(RUNNING, APPROVAL, DONE), automations=(DAILY,), models=(_loaded(),), models_total_bytes=249 * (1 << 20))
    nodes = v2.build_menu(_inputs(snap=snap))
    labels = _labels(nodes)
    assert labels == [
        "AbstractGateway 0.18.0",
        "1 conversation running · 1 needs you · 1 model loaded",
        "Running",
        "Memory 32% · this gateway 10.9 GB · GPU 20%",
        "Open Console",
        "Open Assistant",
        "Needs you (1)",
        "🟡 Clean the build folder · approve `execute_command`",
        "Conversations",
        "Automations",
        "Models",
        "Apps",
        "Connect",
        "Settings",
        "Help",
        "Restart…",
        "Quit AbstractGateway…",
    ]
    # The default (bold) item is the console door; the header lines are information,
    # the Running row is the switch (ticked = running; a click pauses).
    assert [n.label for n in nodes if n.default] == ["Open Console"]
    assert all(not n.enabled and n.action is None for n in nodes[:2])
    assert nodes[2].checked is True and nodes[2].action == ("pause",) and nodes[2].enabled
    assert not any(n.label in {"⏸ Pause", "▶ Resume"} for n in mm.iter_nodes(nodes))
    # Separators: status | doors | needs you | look | set up | lifecycle.
    assert sum(1 for n in nodes if n.separator) == 5


def test_the_product_name_appears_on_the_header_and_quit_only() -> None:
    nodes = v2.build_menu(_inputs(snap=_snap(runs=(RUNNING,))))
    named = [n.label for n in mm.iter_nodes(nodes) if "AbstractGateway" in n.label]
    assert named == ["AbstractGateway 0.18.0", "About AbstractGateway…", "Quit AbstractGateway…"]


def test_the_activity_line_is_always_there_and_says_what_the_machine_does() -> None:
    assert v2.activity_line(_snap()) == "Idle · no model loaded"
    assert v2.activity_line(_snap(models=(_loaded(), _loaded("b")))) == "Idle · 2 models loaded"
    assert v2.activity_line(_snap(runs=(RUNNING, APPROVAL, QUESTION))) == "1 conversation running · 2 need you · no model loaded"
    assert v2.activity_line(_snap(slow=True, runs=(RUNNING,))) == "Slow to answer · 1 conversation running · no model loaded"
    assert v2.activity_line(_snap(gateway_state="paused", paused=True)) == "Paused — conversations wait until you resume"
    assert v2.activity_line(_snap(gateway_state="pausing", inflight_ticks=2)) == "Finishing 2 runs at the next step, then pausing"
    assert v2.activity_line(_snap(gateway_state="unreachable", reachable=False)) == "Can't reach it on this computer · retrying"
    assert v2.header_line(_snap(version="")) == "AbstractGateway — Running"
    assert v2.header_line(_snap(gateway_state="paused")) == "AbstractGateway 0.18.0 — Paused"


def test_the_meters_line_is_percentages_and_models_leads_with_memory() -> None:
    nodes = v2.build_menu(_inputs(snap=_snap(models=(_loaded(),), models_total_bytes=249 * (1 << 20))))
    assert "Memory 32% · this gateway 10.9 GB · GPU 20%" in _labels(nodes)
    models = _sub(nodes, "Models")
    labels = _labels(models)
    assert labels == [
        "Memory 40.6 GB of 128 GB (32%) · this gateway 10.9 GB",
        "GPU 20% busy · Apple M5 Max",
        "In memory (1) · 249 MB",
        "Eject Qwen1.5-0.5B-Chat-4bit (249 MB, MLX)…",
        "Load a Model",
        "Show Activity Window…",
        "Open Resources in Console…",
    ]
    by = {n.label: n for n in models if not n.separator}
    assert by["Eject Qwen1.5-0.5B-Chat-4bit (249 MB, MLX)…"].action == ("eject", _loaded().key)
    assert by["Load a Model"].children is not None
    assert by["Open Resources in Console…"].action == ("open_console_tab", "models")
    # Several loaded: one Eject row each, then Eject All with what it frees.
    two = (_loaded(), _loaded("mlx-community/Qwen3.6-27B-4bit", size=16 * GB))
    labels = _labels(_sub(v2.build_menu(_inputs(snap=_snap(models=two, models_total_bytes=16 * GB + 249 * (1 << 20)))), "Models"))
    assert "In memory (2) · 16.2 GB" in labels
    assert "Eject Qwen3.6-27B-4bit (16.0 GB, MLX)…" in labels
    assert "Eject All — frees 16.2 GB…" in labels and labels.index("Eject All — frees 16.2 GB…") == labels.index("Load a Model") - 1
    # One being ejected: its row says so and Eject All waits.
    ej = {n.label: n for n in _sub(v2.build_menu(_inputs(snap=_snap(models=two), models=mm.ModelsView(ejecting=(two[0].key,)))), "Models") if not n.separator}
    assert "Ejecting Qwen1.5-0.5B-Chat-4bit…" in ej and not ej["Ejecting Qwen1.5-0.5B-Chat-4bit…"].enabled
    assert not ej["Eject All…"].enabled
    # Nothing loaded: said plainly; a locked model says so in its row.
    assert "Nothing in memory" in _labels(_sub(v2.build_menu(_inputs()), "Models"))
    locked = _labels(_sub(v2.build_menu(_inputs(snap=_snap(models=(_loaded(locked=True),)))), "Models"))
    assert "Eject Qwen1.5-0.5B-Chat-4bit (249 MB, MLX, kept in memory)…" in locked
    # Without tkinter the Activity window row is absent, the console door stays.
    no_tk = _labels(_sub(v2.build_menu(_inputs(tk_available=False)), "Models"))
    assert "Show Activity Window…" not in no_tk and no_tk[-1] == "Open Resources in Console…"
    # A machine without meters has no meters line at all (no empty row).
    bare = v2.build_menu(_inputs(snap=_snap(mem_supported=False, mem_pct=None, mem_used=None, gpu_supported=False, gpu_pct=None, process_rss=None)))
    assert _labels(bare)[3] == "Open Console"


def test_automations_are_their_own_section_and_conversations_keep_only_conversations() -> None:
    paused = AutomationRow("a2", "Inbox triage", "paused", schedule_text="", capabilities=("run_now", "resume"), trigger_text="When mail arrives in me@example.org")
    nodes = v2.build_menu(_inputs(snap=_snap(runs=(RUNNING, DONE), automations=(DAILY, paused), observer_automations_path="/apps/observer/#automations")))
    autos = _sub(nodes, "Automations")
    labels = _labels(autos)
    assert labels == [
        "Active (1)",
        "⏱ Daily digest · Every day at 09:00 · next Fri 17 Oct 09:00",
        "Paused (1)",
        "⏸ Inbox triage · paused · When mail arrives in me@example.org",
        "Open Automations in Observer",
    ]
    daily = _sub(autos, "⏱ Daily digest · Every day at 09:00 · next Fri 17 Oct 09:00")
    assert [n.label for n in daily] == ["Active · Every day at 09:00 · next Fri 17 Oct 09:00", "Open in Observer", "Run now", "Pause"]
    assert daily[2].action == ("auto_run_now", "a1")
    assert autos[-1].action == ("open_automations_observer",) and autos[-1].enabled
    conv = _labels(_sub(nodes, "Conversations"))
    assert conv == ["Running (1)", "🟢 Refactor the sampler · step 4 · 2m 10s so far", "Recent — 1 done", "✅ Write the changelog · 7 steps · 1m 42s", "Open Conversations in Console"]
    assert not any("Scheduled" in l or "⏱" in l for l in conv)
    # No automations: said; Observer absent: the door is greyed.
    none = v2.build_menu(_inputs(snap=_snap(), apps=()))
    labels = _labels(_sub(none, "Automations"))
    assert labels == ["No automations yet", "Open Automations in Observer"]
    assert not _sub(none, "Automations")[-1].enabled
    err = _labels(_sub(v2.build_menu(_inputs(snap=_snap(automations_error="boom"))), "Automations"))
    assert err[0] == "Automation list unavailable" and err[1] == "boom"


# ------------------------------------------------------------- needs you


def test_needs_you_rows_are_promoted_with_their_classic_submenus_then_capped() -> None:
    snap = _snap(runs=(APPROVAL, QUESTION))
    nodes = v2.build_menu(_inputs(snap=snap))
    labels = _labels(nodes)
    i = labels.index("Needs you (2)")
    assert labels[i + 1 : i + 3] == ["🟡 Clean the build folder · approve `execute_command`", "🟡 Plan the trip · a question"]
    approval = _sub(nodes, "🟡 Clean the build folder · approve `execute_command`")
    assert [n.label for n in approval] == ["execute_command", "Show and approve…", "Deny", "Open in Observer"]
    assert approval[1].action == ("run_approve_show", "r2") and approval[2].action == ("run_deny", "r2")
    question = _sub(nodes, "🟡 Plan the trip · a question")
    assert [n.label for n in question] == ["Which dates?", "Open in Observer"]
    # Observer is running through the gateway: Open in Observer is live.
    assert approval[3].enabled and approval[3].action == ("open_run_observer", "r2")
    # Five waiting: three rows, one line for the rest, and Conversations keeps all five.
    many = tuple(RunRow(f"w{i}", "x", "waiting", 1, 1.0, 1.0, title=f"Ask {i}", wait_kind="ask_user", wait_summary="?") for i in range(5))
    nodes = v2.build_menu(_inputs(snap=_snap(runs=many)))
    labels = _labels(nodes)
    assert labels[labels.index("Needs you (5)") + 4] == "…and 2 more under Conversations"
    assert sum(1 for n in nodes if n.label.startswith("🟡")) == v2.ATTENTION_ROWS
    conv = _labels(_sub(nodes, "Conversations"))
    assert "Need attention (5)" in conv and sum(1 for l in conv if l.startswith("🟡")) == 5


def test_no_needs_you_section_when_nothing_waits() -> None:
    nodes = v2.build_menu(_inputs(snap=_snap(runs=(RUNNING, DONE))))
    assert not any(n.label.startswith("Needs you") or n.label.startswith("🟡") for n in nodes)
    assert sum(1 for n in nodes if n.separator) == 4


# ------------------------------------------------------------------- doors


def test_the_assistant_door_follows_the_app_and_apps_lists_it_only_when_greyed() -> None:
    # Installed: Open Assistant at the top, not under Apps.
    nodes = v2.build_menu(_inputs())
    assert "Open Assistant" in _labels(nodes)
    assert nodes[[n.label for n in nodes].index("Open Assistant")].action == ("assistant_launch",)
    apps = _labels(_sub(nodes, "Apps"))
    assert not any("Assistant" in l for l in apps)
    assert apps == ["Open Observer", "Open Continuum", "Install Code…", "Entity", "Install Flow…", "Installs unavailable now · Console → Apps", "Manage Apps in Console…"]
    # Installable: Install Assistant… at the top.
    nodes = v2.build_menu(_inputs(apps=_apps("not_installed", True)))
    assert "Install Assistant…" in _labels(nodes) and "Open Assistant" not in _labels(nodes)
    assert not any("Assistant" in l for l in _labels(_sub(nodes, "Apps")))
    # Neither: nothing at the top, the greyed name under Apps as before.
    nodes = v2.build_menu(_inputs(apps=_apps("not_installed", False)))
    assert not any("Assistant" in l for l in _labels(nodes))
    assert "Assistant" in _labels(_sub(nodes, "Apps"))
    # Not read yet: no door and no crash.
    nodes = v2.build_menu(_inputs(apps=(), apps_fetched=False))
    assert not any("Assistant" in l for l in _labels(nodes))


# --------------------------------------------------------- connect & co


def test_connect_merges_addresses_and_who_can_reach_it() -> None:
    nv = mm.parse_network(
        {
            "configured": {"mode": "lan", "port": 8080},
            "effective": {"mode": "lan", "bind_host": "0.0.0.0", "port": 8080},
            "addresses": [{"kind": "loopback", "url": "http://127.0.0.1:8080"}, {"kind": "lan", "url": "http://192.168.1.23:8080", "interface": "Wi-Fi"}],
            "restart_required": True,
            "warnings": ["Anyone on your network can reach the console"],
        }
    )
    nodes = v2.build_menu(_inputs(network=nv))
    connect = _sub(nodes, "Connect — restart required")
    labels = _labels(connect)
    assert labels == [
        "Now: local network · 0.0.0.0:8080",
        "Addresses — click one to copy it",
        "http://127.0.0.1:8080 · this computer",
        "http://192.168.1.23:8080 · local network (Wi-Fi)",
        "Who can reach it",
        "Localhost only",
        "Local network",
        "Internet…",
        "Restart to apply (local network)",
        "⚠ Anyone on your network can reach the console",
        "Open Network in Console…",
    ]
    by = {n.label: n for n in connect if not n.separator}
    assert by["http://192.168.1.23:8080 · local network (Wi-Fi)"].action == ("copy_address", "http://192.168.1.23:8080")
    assert by["Local network"].checked is True and by["Local network"].radio and by["Localhost only"].checked is False
    assert by["Internet…"].action == ("network_set", "internet")
    assert by["Restart to apply (local network)"].action == ("network_restart",)
    assert by["Open Network in Console…"].action == ("open_console_tab", "network")
    # No Addresses ▸ and no Network ▸ at the top level any more.
    assert "Addresses" not in _labels(nodes) and "Network" not in _labels(nodes)
    # Without the network route: the tray's own address is still offered, no mode choice.
    plain = _labels(_sub(v2.build_menu(_inputs(network=mm.NetworkView(error="older gateway"))), "Connect"))
    assert "http://127.0.0.1:8080 · this computer" in plain and "Who can reach it" not in plain


def test_settings_holds_the_switches_with_short_labels() -> None:
    nodes = v2.build_menu(_inputs(autostart=mm.AutostartView("on"), memory_warn=False))
    settings = _sub(nodes, "Settings")
    by = {n.label: n for n in settings if not n.separator}
    assert list(by) == ["Start at login", "Warn when memory is almost full", "Classic menu"]
    assert by["Start at login"].checked is True and by["Start at login"].action == ("toggle_autostart",)
    assert by["Warn when memory is almost full"].checked is False
    assert by["Classic menu"].action == ("set_menu_style", "classic") and by["Classic menu"].checked is False
    # A broken login item keeps its repair wording and its reason line.
    broken = _labels(_sub(v2.build_menu(_inputs(autostart=mm.AutostartView("broken", problems=("the program it starts is gone",)))), "Settings"))
    assert broken[0] == "Start at login — needs repair" and "the program it starts is gone" in broken[1]
    # The classic label is not used anywhere in the redesign.
    assert not any(mm.AUTOSTART_LABEL in n.label for n in mm.iter_nodes(nodes))


def test_updates_live_in_help_until_one_is_known_then_at_the_top() -> None:
    idle = v2.build_menu(_inputs())
    assert "Check for Updates…" in _labels(_sub(idle, "Help")) and "Check for Updates…" not in _labels(idle)
    help_labels = _labels(_sub(idle, "Help"))
    assert help_labels == ["Documentation", "Report a Problem…", "Developer API Reference", "Copy Console Link", "Check for Updates…", "About AbstractGateway…"]
    for phase, label in (("checking", "Checking for Updates…"), ("available", "Update to 0.19.0…"), ("updating", "Updating…"), ("installed", "Restart to Finish Update…")):
        nodes = v2.build_menu(_inputs(update_phase=phase, update_latest="0.19.0"))
        top = _labels(nodes)
        assert label in top and top.index(label) == top.index("Restart…") - 1, phase
        assert not any("Update" in l for l in _labels(_sub(nodes, "Help")))
    # A failed check falls back to Help.
    failed = v2.build_menu(_inputs(update_phase="failed"))
    assert "Check for Updates…" in _labels(_sub(failed, "Help")) and "Check for Updates…" not in _labels(failed)


def test_the_lifecycle_rows_follow_the_state() -> None:
    paused = v2.build_menu(_inputs(snap=_snap(gateway_state="paused", paused=True)))
    running_row = next(n for n in paused if n.label == "Running")
    assert running_row.checked is False and running_row.action == ("resume",) and running_row.enabled
    assert _labels(paused)[1] == "Paused — conversations wait until you resume"
    pausing = next(n for n in v2.build_menu(_inputs(snap=_snap(gateway_state="pausing", inflight_ticks=1))) if n.label == "Running")
    assert pausing.checked is False and not pausing.enabled
    down = v2.build_menu(_inputs(snap=_snap(gateway_state="unreachable", reachable=False)))
    by = {n.label: n for n in down if not n.separator}
    assert "Force Quit AbstractGateway…" in by and "Quit AbstractGateway…" not in by
    assert by["Open Console"].enabled and not by["Restart…"].enabled and not by["Conversations"].enabled
    assert by["Running"].enabled is False and by["Running"].action is None
    no_restart = {n.label: n for n in v2.build_menu(_inputs(snap=_snap(can_restart=False)))}
    assert no_restart["Restart…"].enabled is False


# ------------------------------------------------------------ nothing lost


def test_every_classic_action_is_still_produced_and_every_action_has_a_handler(tmp_path: Path) -> None:
    from abstractgateway.tray import app as tray_app

    snap = _snap(runs=(RUNNING, APPROVAL, QUESTION, DONE), automations=(DAILY,), models=(_loaded(),), observer_automations_path="/apps/observer/#automations")
    nv = mm.parse_network({"configured": {"mode": "localhost"}, "effective": {"mode": "localhost", "bind_host": "127.0.0.1", "port": 8080}, "addresses": [{"kind": "loopback", "url": "http://127.0.0.1:8080"}], "restart_required": True})
    installed, err = mm.parse_installed({"rows": [{"provider": "mlx", "artifact": "mlx-community/Qwen3.6-27B-4bit", "size_bytes": 16 * GB, "catalog_id": "qwen3.6-27b"}]})
    models = mm.ModelsView(installed=installed, installed_error=err, fetched=True)
    inputs = _inputs(snap=snap, network=nv, models=models, autostart=mm.AutostartView("off"), update_phase="available", update_latest="0.19.0", pending_label="Quit")
    classic = v2.action_names(mm.build_menu(inputs))
    new = v2.action_names(v2.build_menu(inputs))
    assert classic <= new, classic - new
    assert new - classic == {"set_menu_style"}
    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path)})
    assert new <= set(app.dispatch_table())


def test_the_flat_degrade_keeps_every_row() -> None:
    nodes = v2.build_menu(_inputs(snap=_snap(runs=(RUNNING, APPROVAL)), autostart=mm.AutostartView("on")))
    flat = mm.flatten(nodes)
    assert not any(n.children for n in flat)
    wanted = {n.label for n in mm.iter_nodes(nodes) if not n.separator}
    got = {n.label.rsplit(mm.PATH_SEP, 1)[-1] for n in flat if not n.separator}
    assert wanted <= got
    assert any(n.label == "Connect › Addresses — click one to copy it" for n in flat)


def test_render_text_draws_the_redesign() -> None:
    text = mm.render_text(v2.build_menu(_inputs(snap=_snap(runs=(RUNNING, APPROVAL), models=(_loaded(),)))))
    assert text.splitlines()[0] == "[AbstractGateway 0.18.0]"
    assert "Open Console   (default)" in text and "Connect ▸" in text and "Quit AbstractGateway…" in text


# ---------------------------------------------------------------- the switch


def test_menu_style_precedence_flag_then_prefs_then_the_redesign() -> None:
    assert v2.menu_style(flag=None, prefs=None) == "v2"
    assert v2.menu_style(flag=None, prefs="classic") == "classic"
    assert v2.menu_style(flag="v2", prefs="classic") == "v2"
    assert v2.menu_style(flag="classic", prefs="v2") == "classic"
    assert v2.menu_style(flag="CLASSIC ", prefs=None) == "classic"
    assert v2.menu_style(flag="bogus", prefs="also bogus") == "v2"


def test_the_app_picks_the_menu_and_the_switch_is_remembered(tmp_path: Path) -> None:
    import json

    from abstractgateway.tray import app as tray_app

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path)})
    app._snap = _snap(runs=(APPROVAL,))
    app._apps, app._apps_fetched = _apps(), True
    rebuilt: List[int] = []
    app._force_menu_rebuild = lambda: rebuilt.append(1)  # type: ignore[method-assign]
    app._info = lambda *a, **k: None  # type: ignore[method-assign]
    assert app.menu_style() == "v2"
    app._native_header = False
    assert _labels(app.menu_nodes())[4] == "Open Console" and "Needs you (1)" in _labels(app.menu_nodes())
    # Settings ▸ Classic menu: the preference is written, the menu rebuilt, the classic shape shows…
    app.dispatch(("set_menu_style", "classic"))
    assert rebuilt == [1]
    assert json.loads((tmp_path / "tray" / "prefs.json").read_text())["menu_style"] == "classic"
    assert app.menu_style() == "classic"
    classic = app.menu_nodes()
    assert "⏸ Pause AbstractGateway" in _labels(classic) and "Addresses" in _labels(classic)
    # …with ONE extra row, Help ▸ New menu, the way back; menu_model.build_menu itself is untouched.
    help_kids = _sub(classic, "Help")
    assert "New menu" in _labels(help_kids)
    assert [n.label for n in help_kids if not n.separator] == ["Documentation", "Report a Problem…", "Developer API Reference", "Copy Console Link", "Warn when memory is almost full", "New menu", "About AbstractGateway…"]
    assert "New menu" not in _labels(_sub(mm.build_menu(app.menu_inputs()), "Help"))
    app.dispatch(("set_menu_style", "v2"))
    assert app.menu_style() == "v2" and "Needs you (1)" in _labels(app.menu_nodes())
    # A launch flag pins this run whatever the preference says.
    pinned = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path), "menu_style": "classic"})
    pinned._snap = _snap()
    assert pinned.menu_style() == "classic"
    # An unknown style from a menu action is a bug, loud.
    with pytest.raises(ValueError):
        app.set_menu_style("legacy")


def test_the_serve_flag_reaches_the_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve --tray-menu classic` hands `menu_style` to the helper (the
    supervisor's `extra`), and the parser refuses any other word."""
    import argparse

    from abstractgateway import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:  # the parser is built inside main(): read the argument off the source
        src = Path(cli.__file__).read_text(encoding="utf-8")
        assert '"--tray-menu"' in src and 'choices=["v2", "classic"]' in src
        assert 'extra = {"menu_style": str(args.tray_menu)}' in src
        return
    ns = parser.parse_args(["serve", "--tray-menu", "classic"])
    assert ns.tray_menu == "classic"
    with pytest.raises((SystemExit, argparse.ArgumentError)):
        parser.parse_args(["serve", "--tray-menu", "legacy"])


# ---------------------------------------------------------------- the header


def test_the_native_header_carries_logo_name_subtitle_and_switch() -> None:
    snap = _snap(runs=(RUNNING, APPROVAL), models=(_loaded(),))
    nodes = v2.build_menu(_inputs(snap=snap), native_header=True)
    head = nodes[0]
    assert isinstance(head, v2.HeaderNode) and head.view is not None
    assert head.label == "AbstractGateway 0.18.0 — Running"  # the text VoiceOver reads / the fallback
    assert not head.enabled and head.action is None
    assert head.view.title == "AbstractGateway"
    assert head.view.subtitle == "1 conversation running · 1 needs you · 1 model loaded"
    assert head.view.switch_on is True and head.view.switch_enabled
    # ONE header row: the activity line is the subtitle, not a second row; the meters follow.
    assert _labels(nodes)[1] == "Memory 32% · this gateway 10.9 GB · GPU 20%"
    # Text header (Windows, Linux): the name row (a HeaderNode too — Linux puts the logo on it),
    # the subtitle, and the Running toggle in place of the switch.
    text = v2.build_menu(_inputs(snap=snap), native_header=False)
    assert isinstance(text[0], v2.HeaderNode) and text[0].label == "AbstractGateway 0.18.0" and not text[0].enabled
    assert _labels(text)[:3] == ["AbstractGateway 0.18.0", "1 conversation running · 1 needs you · 1 model loaded", "Running"]
    assert text[2].checked is True and text[2].action == ("pause",)
    # The native menu has no Running row and no Pause row: the switch is the control.
    assert "Running" not in _labels(nodes)


def test_the_header_switch_follows_the_pause_state() -> None:
    assert v2.header_view(_snap()).switch_on is True
    paused = v2.header_view(_snap(gateway_state="paused", paused=True))
    assert paused.switch_on is False and paused.switch_enabled and paused.subtitle == "Paused — conversations wait until you resume"
    pausing = v2.header_view(_snap(gateway_state="pausing", inflight_ticks=1))
    assert pausing.switch_on is False and not pausing.switch_enabled
    down = v2.header_view(_snap(gateway_state="unreachable", reachable=False))
    assert down.switch_on is None and not down.switch_enabled and down.subtitle.startswith("Not responding")
    starting = v2.header_view(_snap(gateway_state="starting", reachable=False))
    assert starting.switch_on is True and not starting.switch_enabled


def test_the_header_toggle_and_eject_all_go_through_the_same_handlers(tmp_path: Path) -> None:
    from abstractgateway.tray import app as tray_app

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path)})
    calls: List[str] = []
    app.pause = lambda: calls.append("pause")  # type: ignore[method-assign]
    app.resume = lambda: calls.append("resume")  # type: ignore[method-assign]
    app._snap = _snap()
    app.header_toggle(False)
    app.header_toggle(True)  # already running: nothing
    app._snap = _snap(gateway_state="paused", paused=True)
    app.header_toggle(True)
    app.header_toggle(False)  # already paused: nothing
    assert calls == ["pause", "resume"]
    assert "eject_all" in app.dispatch_table()
    # Eject All: one confirmation, then every row through the single-eject path.
    two = (_loaded(), _loaded("mlx-community/Qwen3.6-27B-4bit", size=16 * GB))
    app._snap = _snap(models=two, models_total_bytes=16 * GB)
    confirms: List[tuple] = []
    unloaded: List[str] = []
    app._confirm = lambda key, short, title, body, ok_label="", danger=False, action=None: confirms.append((key, title, body, ok_label)) or action()  # type: ignore[method-assign]
    app._bg = lambda fn, name="": fn()  # type: ignore[method-assign]
    app._unload_now = lambda row, force=False: unloaded.append(row.name)  # type: ignore[method-assign]
    app.dispatch(("eject_all",))
    assert confirms[0][0] == "unload:all" and confirms[0][1] == "Eject 2 models?" and "16.2 GB" in confirms[0][2]
    assert unloaded == [two[0].name, two[1].name]


def test_the_macos_header_view_builds_with_logo_and_switch() -> None:
    pytest.importorskip("AppKit")  # macOS with the pyobjc Cocoa bindings (the tray extra)
    from abstractgateway.tray.header_mac import build_header_view
    from abstractgateway.tray.logo import logo_png

    assert logo_png()[:8] == b"\x89PNG\r\n\x1a\n"
    toggles: List[bool] = []
    built = build_header_view(v2.header_view(_snap(models=(_loaded(),))), logo_png=logo_png(), on_toggle=toggles.append)
    assert built is not None
    view, keep = built
    import AppKit

    subviews = list(view.subviews())
    assert sum(isinstance(sv, AppKit.NSImageView) for sv in subviews) == 1
    assert sum(isinstance(sv, AppKit.NSSwitch) for sv in subviews) == 1
    assert sum(isinstance(sv, AppKit.NSTextField) for sv in subviews) == 2
    sw = next(sv for sv in view.subviews() if isinstance(sv, AppKit.NSSwitch))
    assert sw.state() == AppKit.NSControlStateValueOn and sw.isEnabled()
    sw.setState_(AppKit.NSControlStateValueOff)
    sw.target().toggled_(sw)
    assert toggles == [False]
