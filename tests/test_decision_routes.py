"""The `decision` capability surface (AbstractCore 2.32.0 `decision` output modality,
AbstractRuntime 0.16.0 `run_facade.decide`): `/v1/decisions` served and open to the
Guest, the run-scoped `POST /runs/{run_id}/decisions` over a fake facade, the
capabilities entries, and the console / catalog / tray lists that name the route.

Hermetic: Core stubbed (test_openai_api's `gw`), the facade faked, no model loaded."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from abstractgateway import core_endpoint as ce
from test_gateway_console_parity_web_fixes import _console_script, _run, _slice_line_const
from test_generated_media_gateway_contract import _wait_until, _write_image_bundle
from test_openai_api import ADMIN, _peer, _set, gw  # noqa: F401 - gw is a fixture
from test_openai_api_access import ALICE, _open

pytestmark = pytest.mark.basic

ROOT = Path(__file__).resolve().parents[1]

DECISION = {
    "model": "mlx/kev-4b",
    "state": "Subject: invoice overdue\n\nHi, the March invoice is still unpaid; please settle it this week.",
    "questions": {"urgent": "Does the sender need an answer today?", "topic": ["billing", "support", "other"]},
}
SYSTEM_ONE = {
    "model": "mlx/kev-4b",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.91},
        "topic": {"type": "choice", "choice": "billing", "confidence": 0.88,
                  "probabilities": {"billing": 0.88, "support": 0.09, "other": 0.03}},
    },
    "usage": {"input_tokens": 41, "output_tokens": 0},
    "latency_ms": 12,
}


# ---- the OpenAI API door: served, and model use for the Guest -------------------------

def test_v1_decisions_is_served_and_listed() -> None:
    assert ce.SERVING_ROUTES["/v1/decisions"] == "POST"
    assert "/v1/decisions" in ce.GUEST_PATHS
    assert "POST /v1/decisions" in ce.SUPPORT["served"]
    assert "`POST /v1/decisions`" in (ROOT / "docs" / "openai-api.md").read_text(encoding="utf-8")


def test_v1_decisions_passes_through_to_core_with_the_body_intact(gw):
    _set(gw, enabled=True)
    r = gw.admin.post("/v1/decisions", headers=ALICE, json=DECISION)
    assert r.status_code == 200, r.text
    call = gw.stub.calls[-1]
    assert call["path"] == "/v1/decisions"
    assert json.loads(call["body"]) == DECISION  # state and questions reach Core unchanged
    assert b"authorization" in call["headers"]  # a keyed caller is forwarded with the gateway's credential
    # The model is chosen by `model` alone: a routing field is refused before Core.
    refused = gw.admin.post("/v1/decisions", headers=ALICE, json={**DECISION, "provider": "mlx"})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "unsupported_parameter"
    assert gw.admin.get("/v1/decisions", headers=ALICE).status_code == 404  # POST only


def test_guest_may_decide_without_a_key_but_not_with_tools(gw):
    _open(gw)
    lan = _peer(gw, "192.168.1.20")
    r = lan.post("/v1/decisions", json=DECISION)
    assert r.status_code == 200, r.text
    assert gw.stub.calls[-1]["path"] == "/v1/decisions"
    assert json.loads(gw.stub.calls[-1]["body"]) == DECISION
    assert b"authorization" not in gw.stub.calls[-1]["headers"]  # anonymous towards Core: local models only
    r = lan.post("/v1/decisions", json={**DECISION, "tools": [{"type": "function", "function": {"name": "f"}}]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "guest_not_allowed"
    # a hosted decision model spends the operator's key: local models only for a guest
    for model in ("typesafe/jev-latest", "systemone/jev"):
        r = lan.post("/v1/decisions", json={**DECISION, "model": model})
        assert r.status_code == 403 and r.json()["error"]["code"] == "guest_not_allowed", r.text


# ---- the run-scoped route over a fake facade ---------------------------------------------

def _hermetic_gateway(tmp_path: Path, monkeypatch, bundle_id: str) -> str:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id=bundle_id, flow_id="root")
    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    return token


def _start_run(client: TestClient, headers: Dict[str, str], bundle_id: str) -> str:
    start = client.post(
        "/api/gateway/runs/start",
        json={"bundle_id": bundle_id, "bundle_version": "0.0.0", "flow_id": "root", "input_data": {}},
        headers=headers,
    )
    assert start.status_code == 200, start.text
    run_id = start.json()["run_id"]
    _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")
    return run_id


def test_gateway_direct_decision_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    token = _hermetic_gateway(tmp_path, monkeypatch, "decision-contract")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    seen: Dict[str, Any] = {}

    class StubRunFacade:
        def decide(self, parent_run_id: str, *, state: str, questions: Dict[str, Any], model=None, provider=None,
                   truncate=None, params=None, child_vars=None):
            seen.update({"parent": parent_run_id, "state": state, "questions": questions, "model": model,
                         "provider": provider, "truncate": truncate, "params": params, "child_vars": child_vars})
            return SimpleNamespace(
                run_id="child-decision-1",
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "content": json.dumps(SYSTEM_ONE["answers"]),
                        "decision": SYSTEM_ONE,
                        "answers": SYSTEM_ONE["answers"],
                        "outputs": {},
                        "resources": [],
                        "warnings": [],
                        "errors": [],
                        "provider": "mlx",
                        "model": "kev-4b",
                        "metadata": {"decision": SYSTEM_ONE},
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        run_id = _start_run(client, headers, "decision-contract")

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        body = caps.json()["capabilities"]
        assert body["capability_plugins"]["capabilities"]["decision"] == {"available": True}
        assert body["multimodal"]["capabilities"]["decision"] is True
        contracts = body["contracts"]
        for surface in ("common", "flow_editor", "assistant"):
            direct = contracts[surface]["decision"]["direct_endpoint"]
            assert direct["available"] is True and direct["route_available"] is True
            assert direct["endpoint"] == "/api/gateway/runs/{run_id}/decisions"
            assert direct["openai_endpoint"] == "/v1/decisions"
            assert direct["capability_route"] == "output.decision"
            assert direct["durability"] == "runtime_child_run" and direct["returns_child_run_id"] is True
            workflow = contracts[surface]["decision"]["workflow"]
            assert workflow == {"available": True, "node": "decide", "effect": "LLM_CALL", "output_modality": "decision"}
        assert "decision" not in contracts["assistant"]["media"]  # a decision is not media

        decided = client.post(
            f"/api/gateway/runs/{run_id}/decisions",
            json={**DECISION, "truncate": True, "request_id": "decision-1"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
        out = decided.json()
        assert out["ok"] is True and out["supported"] is True, out
        assert out["run_id"] == run_id and out["request_id"] == "decision-1"
        assert out["child_run_id"] == "child-decision-1"
        assert out["decision"] == SYSTEM_ONE
        assert out["answers"] == SYSTEM_ONE["answers"]
        assert out["provider"] == "mlx" and out["model"] == "kev-4b"
        # What reached the facade: the contract's keyword signature, nothing renamed.
        assert seen["parent"] == run_id
        assert seen["state"] == DECISION["state"] and seen["questions"] == DECISION["questions"]
        assert seen["model"] == "mlx/kev-4b" and seen["provider"] is None and seen["truncate"] is True
        assert seen["params"]["trace_metadata"]["request_id"] == "decision-1"
        assert seen["params"]["trace_metadata"]["run_id"] == run_id

        # Input shape errors are the gateway's 400s, before any child run.
        assert client.post(f"/api/gateway/runs/{run_id}/decisions", json={"state": "  ", "questions": {"q": "x?"}},
                           headers=headers).status_code == 400
        assert client.post(f"/api/gateway/runs/{run_id}/decisions", json={"state": "text", "questions": {}},
                           headers=headers).status_code == 400
        assert client.post(f"/api/gateway/runs/{run_id}/decisions", json={"state": "text"},
                           headers=headers).status_code == 422
        assert client.post(f"/api/gateway/runs/{run_id}/decisions", json=DECISION).status_code == 401


def test_gateway_direct_decision_reports_facade_errors_like_the_media_routes(tmp_path: Path, monkeypatch) -> None:
    token = _hermetic_gateway(tmp_path, monkeypatch, "decision-errors")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    class RefusingFacade:
        def decide(self, parent_run_id: str, **kw):
            raise ValueError("No decision model is configured: set output.decision or pass model.")

    class AnswerlessFacade:
        def decide(self, parent_run_id: str, **kw):
            return SimpleNamespace(run_id="child-decision-2", status=RunStatus.COMPLETED, error=None,
                                   output={"result": {"content": "{}", "outputs": {}, "resources": {}}})

    class FailedChildFacade:
        """The model refused inside the durable child run (the facade never raises)."""

        def decide(self, parent_run_id: str, **kw):
            return SimpleNamespace(run_id="child-decision-3", status=RunStatus.FAILED,
                                   error="Effect failed after 1 attempts: state is over the 65,536-token limit", output=None)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        run_id = _start_run(client, headers, "decision-errors")

        monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (None, "not wired"))
        r = client.post(f"/api/gateway/runs/{run_id}/decisions", json=DECISION, headers=headers)
        assert r.status_code == 200 and r.json()["ok"] is False and r.json()["code"] == "decision_unavailable", r.text
        caps = client.get("/api/gateway/discovery/capabilities", headers=headers).json()["capabilities"]
        assert caps["capability_plugins"]["capabilities"]["decision"] == {"available": False}
        direct = caps["contracts"]["assistant"]["decision"]["direct_endpoint"]
        assert direct["available"] is False and direct["config_hint"] == "not wired"

        monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (RefusingFacade(), None))
        r = client.post(f"/api/gateway/runs/{run_id}/decisions", json=DECISION, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False and r.json()["code"] == "decision_error"
        assert "No decision model is configured" in r.json()["error"]

        monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (AnswerlessFacade(), None))
        r = client.post(f"/api/gateway/runs/{run_id}/decisions", json=DECISION, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False and r.json()["code"] == "decision_no_answers"
        assert r.json()["child_run_id"] == "child-decision-2"

        monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (FailedChildFacade(), None))
        r = client.post(f"/api/gateway/runs/{run_id}/decisions", json=DECISION, headers=headers)
        assert r.status_code == 200, r.text  # a failed child run is an answer, not a 500
        assert r.json()["ok"] is False and r.json()["supported"] is True and r.json()["code"] == "decision_error"
        assert "65,536-token limit" in r.json()["error"] and r.json()["child_run_id"] == "child-decision-3"


# ---- the lists that name the route -----------------------------------------------------

def test_console_offers_the_route_catalog_and_names_the_capability() -> None:
    source = _console_script()
    catalog = _run(
        ["defaultCatalogForRow", "defaultRowKindModality", "defaultRowKey", "defaultRowParentKey",
         "canonicalDefaultRouteTask", "textCatalog", "withQuery", "visionCatalog", "audioCatalog"],
        "const c = defaultCatalogForRow({ key: 'output.decision', kind: 'output', modality: 'decision' });"
        " return [c.scope, c.providerPath(), c.modelPath('mlx')];",
    )
    assert catalog[0] == "decision"
    assert catalog[1] == "/api/gateway/discovery/providers"
    assert catalog[2] == "/api/gateway/discovery/providers/mlx/models?capability_route=output.decision"
    # The badge/row lists (simple and advanced model pages) and the route label maps.
    outputs = re.search(r"const DM_OUTPUTS = \[(.*?)\n    \];", source, flags=re.S)
    assert outputs and 'route: "output.decision"' in outputs.group(1) and 'label: "Decisions"' in outputs.group(1)
    rows = re.search(r"const DEFAULT_MODEL_ROWS = \[(.*?)\n    \];", source, flags=re.S)
    assert rows and 'route: "output.decision"' in rows.group(1) and 'label: "Decisions"' in rows.group(1)
    assert re.search(r"const DM_ICONS = \{.*?\n\s+decision: '", source, flags=re.S)
    assert 'decision:"Decision"' in source


def test_catalog_derives_the_decision_capability_from_the_row() -> None:
    source = _console_script()
    extra = "\n".join(_slice_line_const(source, n) for n in ("MC_CAPS",))
    tagged, flagged, plain = _run(
        ["mcRowCaps"],
        "return [mcRowCaps({ capabilities: { text: false }, tags: ['decision', 'classification'] }),"
        " mcRowCaps({ capabilities: { decision: true } }),"
        " mcRowCaps({ capabilities: { text: true }, tags: ['classification'] })];",
        extra,
    )
    assert tagged == ["decision"] and flagged == ["decision"]
    assert plain == ["text"], "only the decision tag or capability makes a decision model"
    chip = _run(["mcParseHash", "mcDefaultFilters", "mcHfMode"], "return [MC_CAPS.find((c) => c[0] === 'decision'),"
                " mcParseHash('#catalog?cap=decision').cap];", extra)
    assert chip == [["decision", "Decision"], "decision"]


def test_tray_lists_the_decision_route() -> None:
    from abstractgateway.tray.menu_model import ROUTE_TASKS

    assert ROUTE_TASKS["output.decision"] == ("Decision", "decision")


def test_rust_console_labels_the_decision_route() -> None:
    routes = (ROOT / "console-tui" / "src" / "ui" / "routes.rs").read_text(encoding="utf-8")
    assert '("output", "decision") => "decision",' in routes
    models = (ROOT / "console-tui" / "src" / "ui" / "models.rs").read_text(encoding="utf-8")
    assert 'Some("decision") => (Tone::Accent, "Decision")' in models
