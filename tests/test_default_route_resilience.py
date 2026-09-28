"""A default text route that names a deleted endpoint profile must not take
the gateway down (found live 2026-09-28: every `/automations` call answered
500 "Gateway provider endpoint profile 'endpoint:<id>' is not configured or is
disabled" after the profile was deleted). Loading a bundle calls no model; the
broken route is reported next to the control that fixes it, and a call that
uses it fails at the call, naming it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle

LLM_BUNDLE = Path(__file__).resolve().parents[1] / "flows" / "bundles" / "basic-agent@0.0.5.flow"


def _break_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the text default to an endpoint profile, then delete the profile (one gateway life)."""
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    shutil.copy2(LLM_BUNDLE, tmp_path / "bundles" / LLM_BUNDLE.name)  # a bundle WITH llm nodes
    from abstractgateway.app import app

    with TestClient(app) as c:
        r = c.post("/api/gateway/config/provider-endpoint-profiles", headers=HEADERS, json={
            "id": "gone", "display_name": "Gone", "base_url": "http://127.0.0.1:9/v1", "api_key": "k",
            "scope": "gateway", "capabilities": ["text"], "allowed_models": ["m"]})
        assert r.status_code == 200, r.text
        r = c.put("/api/gateway/config/capability-defaults/output/text", headers=HEADERS, json={"provider": "endpoint:gone", "model": "m"})
        assert r.status_code == 200, r.text
        r = c.delete("/api/gateway/config/provider-endpoint-profiles/gone", headers=HEADERS)
        assert r.status_code == 200, r.text
    return ref


def test_a_deleted_default_profile_breaks_no_unrelated_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = _break_the_default(tmp_path, monkeypatch)
    from abstractgateway.app import app

    with TestClient(app) as c:  # a fresh process life: the host loads with the broken default
        assert c.get("/api/gateway/automations", headers=HEADERS).status_code == 200
        assert c.get("/api/gateway/runs", headers=HEADERS).status_code == 200
        # Where it matters: the console's defaults say why the default cannot be used.
        warnings = c.get("/api/gateway/config/capability-defaults", headers=HEADERS).json().get("warnings") or []
        assert any("endpoint:gone" in w and "cannot be used" in w for w in warnings), warnings
        # A run that needs no model still runs.
        r = c.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "x"}})
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        assert wait_until(lambda: c.get(f"/api/gateway/runs/{rid}", headers=HEADERS).json().get("status") == "completed")
        # A run that DOES use the default fails at the call, naming the profile.
        r = c.post("/api/gateway/runs/start", headers=HEADERS, json={
            "bundle_id": "basic-agent@0.0.5", "flow_id": "81795ea9", "input_data": {"prompt": "hi", "tools": []}})
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]

        def _done():
            run = c.get(f"/api/gateway/runs/{rid}", headers=HEADERS).json()
            return run if run.get("status") in ("failed", "completed", "cancelled") else None

        done = wait_until(_done, timeout_s=60)
        ledger = c.get(f"/api/gateway/runs/{rid}/ledger?after=0&limit=500", headers=HEADERS).text
        assert "Unknown provider: endpoint:gone" in (str(done) + ledger), done
