"""Runs get the SAME "self" address the user's email settings show (0.7.0 Linux end-to-end F1a).

`GET /me/email` shows `registered_address` = the registered email, else the connected mailbox's
own address (AbstractCore `EmailSettings.self_address`). Runs read only the registered email
(user record / gateway knob), so an administrator without an email on their user record got no
`_runtime.operator_email` and every send to their own mailbox waited for approval. Now chat runs
(`host.start_run`) and automation definitions carry the settings' value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, write_echo_bundle
from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN_ADDR, connect_body

pytestmark = pytest.mark.integration


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path, runner=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _chat_operator_email(c: TestClient) -> object:
    from abstractgateway.service import get_gateway_service

    host = get_gateway_service().host
    bundle_id = c.bundle_ref.split("@", 1)[0]  # type: ignore[attr-defined]
    rid = host.start_run(flow_id=ECHO_FLOW_ID, bundle_id=bundle_id, input_data={"prompt": "hi"})
    return (host.run_store.load(rid).vars.get("_runtime") or {}).get("operator_email")


def _automation_operator_email(c: TestClient, request_id: str) -> object:
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": request_id, "title": "digest",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    d = c.get(f"/api/gateway/automations/{r.json()['automation_id']}", headers=HEADERS).json()["definition"]
    return (d["target"]["input_data"].get("_runtime") or {}).get("operator_email")


def test_runs_use_the_address_the_email_settings_show(live: TestClient, imap, smtp) -> None:
    # No registered email anywhere and no mailbox: no "self" (every send asks).
    assert _chat_operator_email(live) is None
    assert _automation_operator_email(live, "a0") is None

    r = live.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text
    shown = live.get("/api/gateway/me/email", headers=HEADERS).json()["registered_address"]
    assert shown == ADMIN_ADDR  # the mailbox fallback the settings show
    assert _chat_operator_email(live) == shown
    assert _automation_operator_email(live, "a1") == shown

    # A registered email set by the operator wins (and is what the settings show).
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(Path(live.data_dir), {"operator_email": "owner@example.test"}, actor="test")  # type: ignore[attr-defined]
    shown = live.get("/api/gateway/me/email", headers=HEADERS).json()["registered_address"]
    assert shown == "owner@example.test"
    assert _chat_operator_email(live) == shown
    assert _automation_operator_email(live, "a2") == shown
