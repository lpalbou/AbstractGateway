"""Tray: "Open <app> in Terminal" (mission Y).

The Apps submenu offers it only when the gateway reports the app's terminal
version on this machine (`interfaces[kind=tui].installed`, found by presence);
the action goes through the gateway's `POST /apps/{id}/launch-tui`, the same
path as the console's button. No display, no pystray, no gateway."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from abstractgateway.tray import apps as tray_apps
from abstractgateway.tray import menu_model as mm
from abstractgateway.tray.client import Result


def _row(app_id: str, *, installed: bool = False, tui: Dict[str, Any] | None = None, **over: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": app_id, "installed": installed, "install_available": True, "status": "stopped" if installed else "not_installed"}
    row["interfaces"] = [{"kind": "web", "installed": installed}] + ([dict(kind="tui", **tui)] if tui is not None else [])
    row.update(over)
    return row


def _entries(rows: List[Dict[str, Any]]):
    return tray_apps.build_app_entries({"apps": rows}, None, globals_found={}, assistant={}, local_running={})


def _menu(entries, *, reachable: bool = True) -> mm.Node:
    # apps_section reads only these two MenuInputs fields
    return mm.apps_section(SimpleNamespace(apps=entries, apps_fetched=True), reachable=reachable)


def _labels(node: mm.Node) -> List[str]:
    return [c.label for c in node.children or ()]


def test_installed_terminal_app_gets_an_open_in_terminal_entry() -> None:
    entries = _entries([_row("code", tui={"installed": True, "launch_available": True}), _row("flow")])
    code = next(e for e in entries if e.id == "code")
    assert code.tui_installed and code.tui_launch_available
    node = _menu(entries)
    item = next(c for c in node.children if c.label == "Open Code in Terminal")
    assert item.action == ("app_launch_tui", "code") and item.enabled
    # the web entry for Code is still there, unchanged
    assert "Install Code…" in _labels(node)
    # browser-only apps never get one
    assert not any("Flow in Terminal" in l for l in _labels(node))


def test_no_terminal_entry_when_it_is_not_installed_or_the_gateway_does_not_say() -> None:
    node = _menu(_entries([_row("code", tui={"installed": False, "launch_available": False, "install_available": True})]))
    assert not any("in Terminal" in l for l in _labels(node))
    # an older gateway (no interfaces[]) → nothing guessed
    old = _entries([{"id": "code", "installed": True, "status": "stopped"}])
    assert not any("in Terminal" in l for l in _labels(_menu(old)))


def test_terminal_entry_disabled_when_the_gateway_is_unreachable_and_explained_when_refused() -> None:
    entries = _entries([_row("code", tui={"installed": True, "launch_available": True})])
    item = next(c for c in _menu(entries, reachable=False).children if c.label == "Open Code in Terminal")
    assert item.enabled is False
    refused = _entries([_row("code", tui={"installed": True, "launch_available": False, "launch_blocked_reason": "Only an admin can open a terminal on the gateway computer."})])
    # Refused here: the entry is greyed, with no reason on the menu (mission HH).
    item = next(c for c in _menu(refused).children if c.label == "Open Code in Terminal")
    assert item.enabled is False
    assert not any("Only an admin" in l for l in _labels(_menu(refused)))


class _Client:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.calls: List[tuple] = []

    def app_launch_tui(self, app_id):
        self.calls.append(("launch_tui", app_id))
        return self.result


@pytest.fixture()
def tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway.tray import app as tray_app

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18890", "token": "t", "data_dir": str(tmp_path)})
    events: List[tuple] = []
    app._notify = lambda title, msg: events.append(("notify", title, msg))  # type: ignore[assignment]
    app._info = lambda title, body, style="informational": events.append(("info", title, body))  # type: ignore[assignment]
    app._bg = lambda fn, name="": fn()  # type: ignore[assignment]
    app.poke_extras = lambda: None  # type: ignore[assignment]
    app.events = events  # type: ignore[attr-defined]
    return app


def test_the_menu_action_goes_through_launch_tui(tray) -> None:
    tray.client = _Client(Result(True, 200, {"ok": True, "terminal": "Terminal"}))
    assert "app_launch_tui" in tray.dispatch_table()
    tray.dispatch(("app_launch_tui", "code"))
    assert tray.client.calls == [("launch_tui", "code")]
    assert tray.events == [("notify", "Code is opening in Terminal", "Signed in to this gateway.")]


def test_a_refused_launch_shows_the_gateways_reason(tray) -> None:
    tray.client = _Client(Result(False, 409, {"ok": False, "reason": "not_installed", "message": "Code's terminal app is not installed on this computer."}))
    tray.dispatch(("app_launch_tui", "code"))
    kind, title, body = tray.events[-1]
    assert kind == "info" and title == "Couldn't open Code in a terminal"
    assert "not installed" in body
