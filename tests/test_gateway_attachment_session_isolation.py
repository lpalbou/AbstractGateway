"""An attachment never crosses conversations, whatever a client sends (operator report 2026-10-01).

Mac mini, gateway 0.10.0: a new conversation's first turn referenced the screenshot uploaded in
another conversation (the client resent the reference, with its owner `run_id`), the start door let
it through because the reference named its owner run, and the model answered about the other
conversation's screenshot. Now every door — the HTTP run start and the host itself, so bridges,
entities and automations too — refuses a session-private artifact (an upload: owned by a session
memory run, or tagged `kind: attachment`) that the run's session does not own: typed 400
`artifact_not_in_session`, a warning in the log, no run started, so no llm_call ever carries it.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict

import pytest
from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from fastapi.testclient import TestClient

from tests.test_gateway_artifacts_endpoint import _write_test_bundle
from tests.test_gateway_session_history_seed import _write_min_bundle

pytestmark = pytest.mark.basic

SESSION_A = "conv-a-with-screenshot"
SESSION_B = "conv-b-fresh"


# ----------------------------------------------------------------------------------------------
# The HTTP door
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    with TestClient(app) as client:
        yield {"client": client, "headers": {"Authorization": "Bearer t"}, "bundle_id": bundle_id, "flow_id": flow_id}


def _upload(gw: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    r = gw["client"].post(
        "/api/gateway/attachments/upload",
        data={"session_id": session_id},
        files={"file": ("IMG_0155.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")},
        headers=gw["headers"],
    )
    assert r.status_code == 200, r.text
    ref = r.json().get("attachment") or r.json().get("artifact")
    assert isinstance(ref, dict) and ref.get("$artifact"), r.text
    return ref


def _start(gw: Dict[str, Any], session_id: str, input_data: Dict[str, Any]):
    return gw["client"].post(
        "/api/gateway/runs/start",
        json={"bundle_id": gw["bundle_id"], "flow_id": gw["flow_id"], "session_id": session_id, "input_data": input_data},
        headers=gw["headers"],
    )


def test_a_new_conversation_cannot_attach_another_conversations_upload(gateway) -> None:
    ref = _upload(gateway, SESSION_A)
    # The Mac mini shape: the client resends the reference WITH its owner run id.
    foreign = {**ref, "run_id": ref.get("run_id") or f"session_memory_{SESSION_A}"}
    r = _start(gateway, SESSION_B, {"prompt": "send an email", "context": {"task": "send an email", "attachments": [foreign]}})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "artifact_not_in_session"
    assert detail["artifact_id"] == ref["$artifact"] and detail["session_id"] == SESSION_B
    # No run exists for session B: nothing could have carried the screenshot to a model.
    runs = gateway["client"].get("/api/gateway/runs", params={"session_id": SESSION_B}, headers=gateway["headers"])
    assert runs.status_code == 200 and not [x for x in (runs.json().get("items") or runs.json().get("runs") or []) if x.get("session_id") == SESSION_B]


def test_the_same_reference_in_its_own_conversation_is_accepted(gateway) -> None:
    ref = _upload(gateway, SESSION_A)
    r = _start(gateway, SESSION_A, {"prompt": "what is in it?", "context": {"task": "what is in it?", "attachments": [ref]}})
    assert r.status_code == 200, r.text


def test_legacy_media_key_and_bare_ids_are_refused_the_same_way(gateway) -> None:
    ref = _upload(gateway, SESSION_A)
    bare = {"$artifact": ref["$artifact"]}
    for shape in (
        {"context": {"media": [bare]}},
        {"media": [bare]},
        {"attachments": [{**bare, "artifact_id": ref["$artifact"]}]},
    ):
        r = _start(gateway, SESSION_B, {"prompt": "x", **shape})
        assert r.status_code == 400, (shape, r.text)
        assert r.json()["detail"]["reason_code"] == "artifact_not_in_session"


# ----------------------------------------------------------------------------------------------
# The host (every in-process caller: bridges, entities, automations)
# ----------------------------------------------------------------------------------------------


def _host(tmp_path: Path, run_store: InMemoryRunStore, artifact_store: InMemoryArtifactStore):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=run_store,
        ledger_store=InMemoryLedgerStore(),
        artifact_store=artifact_store,
    )


def test_host_refuses_a_session_private_artifact_of_another_session(tmp_path: Path) -> None:
    from abstractgateway.artifact_scope import ForeignSessionArtifact

    artifacts = InMemoryArtifactStore()
    meta = artifacts.store(b"png-bytes", content_type="image/png", run_id=f"session_memory_{SESSION_A}", tags={"kind": "attachment", "filename": "IMG_0155.png"})
    host = _host(tmp_path, InMemoryRunStore(), artifacts)
    ref = {"$artifact": meta.artifact_id, "run_id": f"session_memory_{SESSION_A}", "content_type": "image/png"}
    with pytest.raises(ForeignSessionArtifact) as err:
        host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "hi", "context": {"attachments": [ref]}}, session_id=SESSION_B)
    assert err.value.artifact_id == meta.artifact_id and err.value.session_id == SESSION_B
    # Its own session: fine.
    rid = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "hi", "context": {"attachments": [ref]}}, session_id=SESSION_A)
    assert rid


def test_host_allows_an_artifact_a_run_of_this_session_produced_and_a_user_shared_one(tmp_path: Path) -> None:
    artifacts = InMemoryArtifactStore()
    run_store = InMemoryRunStore()
    run_store.save(
        RunState(
            run_id="run-of-b",
            workflow_id="wf",
            status=RunStatus.COMPLETED,
            current_node="done",
            vars={},
            output={},
            error=None,
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
            actor_id="admin",
            session_id=SESSION_B,
            parent_run_id=None,
            waiting=None,
        )
    )
    produced = artifacts.store(b"out", content_type="text/plain", run_id="run-of-b", tags={"kind": "attachment"})
    shared = artifacts.store(b"shared", content_type="text/plain", run_id=f"session_memory_{SESSION_A}", tags={"kind": "attachment", "shared": "user"})
    host = _host(tmp_path, run_store, artifacts)
    for aid in (produced.artifact_id, shared.artifact_id):
        rid = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "hi", "context": {"attachments": [{"$artifact": aid}]}}, session_id=SESSION_B)
        assert rid


def test_run_produced_artifacts_keep_the_documented_cross_session_handoff(tmp_path: Path) -> None:
    """Outputs a run generated are not session-private: a later run of another session may name
    them by `run_id` (docs/api.md, artifact hand-off). Only uploads are a conversation's own."""

    artifacts = InMemoryArtifactStore()
    produced_elsewhere = artifacts.store(b"report", content_type="text/plain", run_id="run-of-a", tags={"pin_id": "report"})
    host = _host(tmp_path, InMemoryRunStore(), artifacts)
    rid = host.start_run(
        flow_id="root",
        bundle_id="history-demo",
        input_data={"prompt": "hi", "report": {"$artifact": produced_elsewhere.artifact_id, "run_id": "run-of-a"}},
        session_id=SESSION_B,
    )
    assert rid
