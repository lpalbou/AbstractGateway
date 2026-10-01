"""Account Logs: run starts name their run and workflow, notification kinds read as words
(adversary pass 4, G9 + copy).

A run id is created by the /runs/start handler, so it is in the RESPONSE, never in the request
path the audit middleware logs: the route writes it into its audit detail
(routes/gateway.py `_audit_run_started`). Proven end to end with a real run start through the
real app + security middleware (a model-free ask_user bundle), and on synthetic audit lines for
older lines and notification kinds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_http_api import _write_test_bundle


def _activity_line(tmp_path: Path, line: dict) -> dict:
    from abstractgateway.account_activity import account_activity

    (tmp_path / "audit_log.jsonl").write_text(json.dumps(line) + "\n")
    events = account_activity("alice", data_dir=tmp_path)["events"]
    assert len(events) == 1, events
    ev = events[0]
    ev.pop("ts_local")
    return ev


def test_a_real_run_start_records_its_run_id_and_workflow_on_the_audit_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from abstractcore.config.manager import ConfigurationManager

    assert ConfigurationManager().set_capability_default("output.text", provider="stub", model="stub-model")
    from abstractgateway.account_activity import account_activity
    from abstractgateway.app import app

    with TestClient(app) as client:
        r = client.post("/api/gateway/runs/start", json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
                        headers={"Authorization": "Bearer t"})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

    lines = [json.loads(x) for x in (runtime_dir / "audit_log.jsonl").read_text().splitlines() if x.strip()]
    starts = [x for x in lines if x.get("path") == "/api/gateway/runs/start"]
    assert len(starts) == 1, starts
    line = starts[0]
    assert line["run"] == {"run_id": run_id, "workflow": "test", "bundle_id": bundle_id, "bundle_version": "0.0.0",
                           "entrypoint": flow_id, "scheduled": False}

    account = line["principal_user_id"]
    events = account_activity(account, tenant_id=line.get("principal_tenant_id") or "default",
                              data_dir=runtime_dir, kinds=["run"])["events"]
    assert len(events) == 1, events
    ev = events[0]
    assert (ev["title"], ev["detail"], ev["run_id"], ev["observer_path"], ev["ok"]) == (
        "Run started", "test", run_id, f"/apps/observer/#run/{run_id}", True)


def test_an_older_run_line_without_a_run_id_says_so_and_has_no_link(tmp_path: Path) -> None:
    line = {"ts": "2026-09-27T07:58:26+00:00", "method": "POST", "path": "/api/gateway/runs/start", "status": 200,
            "principal_user_id": "alice", "principal_tenant_id": "default"}
    assert _activity_line(tmp_path, line) == {
        "ts": line["ts"], "kind": "run", "title": "Run started", "detail": "Run id not recorded (before this version)",
        "run_id": None, "observer_path": None, "ok": True}


def test_a_refused_run_start_says_refused_not_missing(tmp_path: Path) -> None:
    line = {"ts": "2026-09-27T07:58:26+00:00", "method": "POST", "path": "/api/gateway/runs/start", "status": 404,
            "principal_user_id": "alice", "principal_tenant_id": "default"}
    ev = _activity_line(tmp_path, line)
    assert (ev["detail"], ev["run_id"], ev["observer_path"], ev["ok"]) == ("Refused (HTTP 404).", None, None, False)


@pytest.mark.parametrize(
    ("kind", "words"),
    [("approval_needed", "Approval needed"), ("job_failed", "Job failed"), ("test", "Test notification"),
     ("job_finished", "Job finished"), ("automation_result", "Automation result"),
     ("automation_failed", "Automation failed"), ("some_new_kind", "some_new_kind")],
)
def test_notification_details_are_plain_words(tmp_path: Path, kind: str, words: str) -> None:
    line = {"ts": "2026-09-30T09:00:00+00:00", "event": "email.notification_sent", "user_id": "alice",
            "tenant_id": "default", "kind": kind, "outcome": "sent"}
    ev = _activity_line(tmp_path, line)
    assert (ev["title"], ev["detail"]) == ("Notification sent", words)
