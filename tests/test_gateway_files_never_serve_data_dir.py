"""The server-workspace file routes never serve the gateway data folder (D3: administrators never
read mail).

Found on a hermetic 0.9.0 gateway (day review 2026-09-30, W3): the server workspace root defaults
to the gateway's working directory, which is the data folder itself under the OS service
(launchd `WorkingDirectory`) and contains it after a launch from a parent folder. An
administrator then read another user's email through `GET /files/read` on
`users/<tenant>/<user>/runtime/event_inbox/events/*.json` and on the user's run ledgers. The
run workspace browser and every run's tools already deny the data folder; `/files/*` did not.

Red without `_server_file_blocked_roots` in the file routes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CANARY = "BOB-MAIL-CANARY-7c2"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    data = ws / "gateway-data"  # the data folder INSIDE the server workspace root
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    events = data / "users" / "default" / "bob" / "runtime" / "event_inbox" / "events"
    events.mkdir(parents=True)
    (events / "000000000001.json").write_text(json.dumps({"subject": CANARY, "text": CANARY}), encoding="utf-8")
    (data / "users" / "default" / "bob" / "runtime" / "ledger_r1.jsonl").write_text(json.dumps({"prompt": CANARY}) + "\n", encoding="utf-8")
    (ws / "notes.txt").write_text("a workspace file\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.ws, c.data = ws, data  # type: ignore[attr-defined]
        yield c


H = {"Authorization": "Bearer t"}


def test_admin_file_routes_do_not_serve_another_users_mail(client: TestClient) -> None:
    ws, data = client.ws, client.data  # type: ignore[attr-defined]
    event = data / "users" / "default" / "bob" / "runtime" / "event_inbox" / "events" / "000000000001.json"
    ledger = data / "users" / "default" / "bob" / "runtime" / "ledger_r1.jsonl"

    # Positive control: the workspace itself is served.
    ok = client.get("/api/gateway/files/read", params={"path": "notes.txt"}, headers=H)
    assert ok.status_code == 200 and "a workspace file" in ok.text

    for path in (str(event), str(ledger), str(event.relative_to(ws)), "gateway-data/users/default/bob/runtime/ledger_r1.jsonl"):
        for route in ("/api/gateway/files/read", "/api/gateway/files/skim"):
            r = client.get(route, params={"path": path}, headers=H)
            assert r.status_code == 403, (route, path, r.status_code, r.text[:200])
            assert CANARY not in r.text
    for path in (str(data), "gateway-data", "gateway-data/users/default/bob/runtime/event_inbox/events"):
        r = client.get("/api/gateway/files/list", params={"path": path}, headers=H)
        assert r.status_code == 403, (path, r.status_code, r.text[:200])

    # Listing the workspace root never descends into the data folder; search never finds it.
    r = client.get("/api/gateway/files/list", params={"path": "", "recursive": True}, headers=H)
    assert r.status_code == 200, r.text
    assert "000000000001.json" not in r.text and "ledger_r1" not in r.text
    r = client.get("/api/gateway/files/search", params={"query": "ledger_r1"}, headers=H)
    assert r.status_code == 200 and "ledger_r1" not in r.text
    r = client.get("/api/gateway/files/search", params={"query": "000000000001"}, headers=H)
    assert r.status_code == 200 and "000000000001" not in r.text
