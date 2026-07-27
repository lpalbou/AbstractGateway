"""Grant-mode API pins (tool-tiers cycle-3 build).

The contract: vocabulary served with teaching lines + version; the DEFAULT
grant is a recorded act (ledger attribution); destroy refuses as a standing
default; grant_mode is provenance never an enforcement branch; run-start
injection sends the ceiling/name-list ONLY when the client sent nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abstractgateway.tool_grants import (
    TIER_VOCABULARY,
    ToolGrantError,
    grant_ceiling_rank,
    inject_default_grant,
    read_tool_grants,
    write_default_grant,
)


def test_vocabulary_shape_and_builtin_default(tmp_path: Path) -> None:
    posture = read_tool_grants(tmp_path)
    ids = [t["id"] for t in posture["tier_vocabulary"]]
    assert ids == ["observe", "act", "outreach", "destroy"]
    for t in posture["tier_vocabulary"]:
        assert t["teaching_line"].strip(), f"{t['id']} must teach what it grants"
    assert posture["default"] == {"mode": "preset", "tier_id": "act"}
    assert posture["default_source"] == "builtin-default"
    assert posture["tier_vocabulary_version"] == 1


def test_write_default_is_a_recorded_act(tmp_path: Path) -> None:
    out = write_default_grant(tmp_path, {"mode": "preset", "tier_id": "outreach"}, actor="admin")
    assert out["default"] == {"mode": "preset", "tier_id": "outreach"}
    assert out["default_source"] == "stored"
    assert out["applied_by"] == "admin"
    ledger = (tmp_path / "config" / "tool_grants_ledger.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.strip().splitlines()[-1])
    assert entry["actor"] == "admin" and entry["new"]["tier_id"] == "outreach"
    # First act records the EFFECTIVE prior state (the builtin), never None
    # (adversary F8: attribution honesty).
    assert entry["old"]["tier_id"] == "act" and entry["old"]["source"] == "builtin-default"


def test_destroy_refuses_as_standing_default(tmp_path: Path) -> None:
    """DESTRUCTIVE IS NEVER A DEFAULT (room-converged): the top tier cannot
    be a standing preset grant — per-tool custom or per-run only."""
    with pytest.raises(ToolGrantError, match="destroy"):
        write_default_grant(tmp_path, {"mode": "preset", "tier_id": "destroy"}, actor="admin")


def test_unknown_tier_and_mode_refuse_loudly(tmp_path: Path) -> None:
    """Vocabulary drift => fail-to-ask, never fuzzy-map (continuum's rule)."""
    with pytest.raises(ToolGrantError, match="unknown tier_id"):
        write_default_grant(tmp_path, {"mode": "preset", "tier_id": "tier3"}, actor="a")
    with pytest.raises(ToolGrantError, match="unknown grant mode"):
        write_default_grant(tmp_path, {"mode": "band", "tier_id": "act"}, actor="a")


def test_custom_grant_records_custom_even_when_it_equals_a_band(tmp_path: Path) -> None:
    """semantics c4505: provenance records what the operator DID."""
    out = write_default_grant(
        tmp_path, {"mode": "custom", "tools": ["read_file", "list_files", "web_search"]}, actor="admin"
    )
    assert out["default"]["mode"] == "custom"
    assert out["default"]["tools"] == ["list_files", "read_file", "web_search"]
    assert grant_ceiling_rank(tmp_path) is None  # a name list has no single rank


def test_builtin_default_never_injects(tmp_path: Path) -> None:
    """Adversary F1 (P0): grants are RECORDED ACTS — the builtin display
    default must NOT inject (it would silently flip ask->auto for writes on
    every bridge/scheduled run). No stored grant = no injection = the
    2026-02-21 static approval defaults stay authoritative."""
    rt: dict = {}
    assert inject_default_grant(tmp_path, rt) is None
    assert "tool_policy" not in rt


def test_corrupt_store_never_widens_the_grant(tmp_path: Path) -> None:
    """Adversary F2: corruption serves builtin (source != stored) — so the
    injector stays silent; one bad byte can never revert an operator's
    narrow grant to auto-writes."""
    write_default_grant(tmp_path, {"mode": "preset", "tier_id": "observe"}, actor="a")
    (tmp_path / "config" / "tool_grants.json").write_text("{broken", encoding="utf-8")
    rt: dict = {}
    assert inject_default_grant(tmp_path, rt) is None


def test_client_empty_policy_is_an_explicit_statement(tmp_path: Path) -> None:
    """Adversary F6: tool_policy: {} means "static defaults, no overrides" —
    key PRESENCE suppresses injection, not truthiness."""
    write_default_grant(tmp_path, {"mode": "preset", "tier_id": "observe"}, actor="a")
    rt = {"tool_policy": {}}
    assert inject_default_grant(tmp_path, rt) is None
    assert rt["tool_policy"] == {}


def test_injection_preset_sends_ceiling_only_when_client_silent(tmp_path: Path) -> None:
    write_default_grant(tmp_path, {"mode": "preset", "tier_id": "observe"}, actor="admin")
    rt: dict = {}
    note = inject_default_grant(tmp_path, rt)
    assert rt["tool_policy"]["auto_approve_max_risk_rank"] == 1
    assert rt["tool_policy"]["source"] == "gateway-default"
    assert "observe" in note

    # CLIENT POLICY WINS: an existing tool_policy is never overwritten.
    rt2 = {"tool_policy": {"auto_approve_tools": ["read_file"]}}
    assert inject_default_grant(tmp_path, rt2) is None
    assert rt2["tool_policy"] == {"auto_approve_tools": ["read_file"]}


def test_injection_custom_sends_name_list(tmp_path: Path) -> None:
    write_default_grant(tmp_path, {"mode": "custom", "tools": ["read_file", "web_search"]}, actor="admin")
    rt: dict = {}
    note = inject_default_grant(tmp_path, rt)
    assert rt["tool_policy"]["auto_approve_tools"] == ["read_file", "web_search"]
    assert rt["tool_policy"]["grant_mode"] == "custom"
    assert "custom" in note


def test_corrupt_store_refuses_writes_serves_default_reads(tmp_path: Path) -> None:
    p = tmp_path / "config" / "tool_grants.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    posture = read_tool_grants(tmp_path)
    assert posture["default_source"] == "builtin-default"
    assert any("unreadable" in w for w in posture.get("warnings", []))
    with pytest.raises(ToolGrantError, match="unreadable"):
        write_default_grant(tmp_path, {"mode": "preset", "tier_id": "act"}, actor="a")


def test_routes_serve_and_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "grant-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer grant-token"}) as client:
        r = client.get("/api/gateway/tool-grants")
        assert r.status_code == 200, r.text
        assert r.json()["default"]["tier_id"] == "act"

        w = client.put(
            "/api/gateway/tool-grants/default",
            json={"mode": "preset", "tier_id": "outreach"},
        )
        assert w.status_code == 200, w.text
        assert w.json()["default"]["tier_id"] == "outreach"

        bad = client.put(
            "/api/gateway/tool-grants/default",
            json={"mode": "preset", "tier_id": "destroy"},
        )
        assert bad.status_code == 400
        assert "destroy" in bad.json()["detail"]
