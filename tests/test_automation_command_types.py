"""One command-type source (automations contract C8): the door, the runner and
the capabilities all read `abstractgateway.automation_command_types`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.automation_command_types import (
    AUTOMATION_COMMAND_TYPES,
    AUTOMATION_SUMMARY_CAPABILITIES,
    COMMAND_TYPES,
    LEGACY_COMMAND_TYPES,
)
from automations_fixtures import HEADERS, chat_run, controller_run, gateway_env, save_runs

SRC = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"


def test_constants_are_the_contract_lists() -> None:
    assert AUTOMATION_COMMAND_TYPES == (
        "automation.revise",
        "automation.pause",
        "automation.resume",
        "automation.run_now",
        "automation.stop_current",
        "automation.archive",
    )
    assert set(LEGACY_COMMAND_TYPES) == {
        "pause", "resume", "cancel", "conclude", "emit_event", "update_schedule", "compact_memory", "inject_guidance",
    }
    assert COMMAND_TYPES == LEGACY_COMMAND_TYPES + AUTOMATION_COMMAND_TYPES
    assert AUTOMATION_SUMMARY_CAPABILITIES == ("revise", "pause", "resume", "run_now", "stop_current", "archive", "discuss")


def _string_collections(path: Path) -> list[set[str]]:
    """Every set/list/tuple literal made only of strings in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        ):
            out.append({e.value for e in node.elts})
    return out


@pytest.mark.parametrize("relpath", ["routes/gateway.py", "runner.py"])
def test_no_second_hand_written_allowlist(relpath: str) -> None:
    """A literal collection holding most of the command types is a second
    source of truth waiting to drift (the pre-0928 door, runner and
    capabilities each had one)."""
    for literal in _string_collections(SRC / relpath):
        overlap = literal & set(COMMAND_TYPES)
        # (subsets like the runner's 5-type priority lane are legitimate)
        assert len(overlap) < 6, f"{relpath} hard-codes command types {sorted(overlap)}; import COMMAND_TYPES instead"


def test_door_runner_and_capabilities_import_the_module() -> None:
    for relpath in ("routes/gateway.py", "runner.py"):
        tree = ast.parse((SRC / relpath).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("automation_command_types")
            for alias in node.names
        }
        assert "COMMAND_TYPES" in imported, relpath


def test_capabilities_advertise_exactly_the_constants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as client:
        r = client.get("/api/gateway/discovery/capabilities", headers=HEADERS)
        assert r.status_code == 200, r.text
        common = r.json()["capabilities"]["contracts"]["common"]
    assert common["runs"]["commands"]["types"] == list(COMMAND_TYPES)
    autos = common["automations"]
    assert autos["available"] is True
    assert autos["command_types"] == list(AUTOMATION_COMMAND_TYPES)
    assert autos["trigger_sources_endpoint"] == "/api/gateway/trigger-sources"
    assert autos["automations_endpoint"] == "/api/gateway/automations"
    assert "session_kind" in common["runs"]["list"]["filters"]


def test_door_accepts_every_type_and_refuses_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as client:
        controller = controller_run()
        chat = chat_run(session_id="s1")
        save_runs(controller, chat)
        for i, typ in enumerate(AUTOMATION_COMMAND_TYPES):
            r = client.post("/api/gateway/commands", headers=HEADERS,
                            json={"command_id": f"c{i}", "run_id": controller.run_id, "type": typ, "payload": {}})
            assert r.status_code == 200, (typ, r.text)
            assert r.json()["accepted"] is True
        # An automation command at a run that is not an automation: refused now, not later.
        r = client.post("/api/gateway/commands", headers=HEADERS,
                        json={"command_id": "x1", "run_id": chat.run_id, "type": "automation.pause", "payload": {}})
        assert r.status_code == 404, r.text
        r = client.post("/api/gateway/commands", headers=HEADERS,
                        json={"command_id": "x2", "run_id": chat.run_id, "type": "automation.explode", "payload": {}})
        assert r.status_code == 400
        assert "automation.archive" in r.json()["detail"]


def test_runner_dispatches_automation_types_to_the_runtime_applier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner accepts every constant; automation.* go to _apply_automation_command."""
    from abstractruntime.storage.commands import CommandRecord

    from abstractgateway.runner import GatewayRunner

    seen: list[str] = []
    runner = GatewayRunner.__new__(GatewayRunner)
    monkeypatch.setattr(GatewayRunner, "_apply_automation_command", lambda self, rec, **kw: seen.append(kw["typ"]), raising=True)
    for typ in AUTOMATION_COMMAND_TYPES:
        runner._apply_command(CommandRecord(command_id=typ, run_id="a1", type=typ, payload={}, ts="", client_id=None, seq=1))
    assert seen == list(AUTOMATION_COMMAND_TYPES)
    with pytest.raises(ValueError):
        runner._apply_command(CommandRecord(command_id="z", run_id="a1", type="automation.explode", payload={}, ts="", client_id=None, seq=2))


def test_gateway_types_equal_the_runtime_appliers_types() -> None:
    """The door must accept exactly what abstractruntime's applier applies."""
    from abstractruntime.automations.commands import AUTOMATION_COMMAND_TYPES as RUNTIME_TYPES

    assert set(AUTOMATION_COMMAND_TYPES) == set(RUNTIME_TYPES)
