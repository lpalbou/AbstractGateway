"""Session purpose (round 8, R8.3 S6): `kind` = conversation (default) | docs.

- `POST /runs/start` takes `kind: "docs"` (with a session_id) for a Docs
  assistant chat; anything else is refused;
- `GET /runs?root_only=true` (the list every app and the console uses for
  conversations) defaults to `kind=conversation`, so a docs chat never appears
  there; `kind=docs` is the drawer's history; `kind=all` both; every row
  carries `kind`;
- docs chats stay in the one pool: readable by session_id, archivable, and
  "Archived · N" counts the listed purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, gateway_env, save_runs


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _roots(c: TestClient, query: str = "") -> dict:
    r = c.get(f"/api/gateway/runs?root_only=true&include_ledger_len=false{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _start_docs(c: TestClient, session_id: str) -> None:
    body = {"registry_scope": "tenant_catalog", "bundle_id": "docs-qa", "flow_id": "docsqa001", "session_id": session_id, "kind": "docs",
            "input_data": {"prompt": "How?", "docs": "# Docs", "app": "AbstractCode", "use_session_history": True}}
    c.post("/api/gateway/runs/start", headers=HEADERS, json=body)  # the mark is written before the run starts


def test_a_docs_chat_never_appears_in_the_default_conversation_list(client: TestClient) -> None:
    conv = chat_run(session_id="s-conv", created_at="2026-10-04T08:00:00+00:00")
    docs = chat_run(session_id="code-docs-assistant:1", created_at="2026-10-04T09:00:00+00:00")
    save_runs(conv, docs)
    _start_docs(client, "code-docs-assistant:1")
    from abstractgateway.session_kinds import FILENAME

    stored = json.loads((client.data_dir / FILENAME).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert stored["sessions"]["code-docs-assistant:1"]["kind"] == "docs"

    default = _roots(client)
    assert {r["session_id"] for r in default["items"]} == {"s-conv"}
    assert all(r["kind"] == "conversation" for r in default["items"])
    assert {r["session_id"] for r in _roots(client, "&kind=conversation")["items"]} == {"s-conv"}
    history = _roots(client, "&kind=docs")
    assert {r["session_id"] for r in history["items"]} == {"code-docs-assistant:1"}
    assert all(r["kind"] == "docs" for r in history["items"])
    assert {r["session_id"] for r in _roots(client, "&kind=all")["items"]} == {"s-conv", "code-docs-assistant:1"}
    # Still the one pool: readable by its id.
    direct = client.get("/api/gateway/runs?session_id=code-docs-assistant:1&include_ledger_len=false", headers=HEADERS).json()
    assert docs.run_id in {r["run_id"] for r in direct["items"]} and all(r["kind"] == "docs" for r in direct["items"])


def test_a_docs_chat_is_archivable_and_counted_with_its_purpose(client: TestClient) -> None:
    save_runs(chat_run(session_id="s-conv"), chat_run(session_id="flow-docs-assistant:2"))
    _start_docs(client, "flow-docs-assistant:2")
    r = client.post("/api/gateway/sessions/flow-docs-assistant:2/archive", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _roots(client, "&kind=docs")["items"] == []
    assert _roots(client, "&kind=docs")["archived_sessions"] == 1
    assert _roots(client)["archived_sessions"] == 0  # the conversation list's "Archived · N"
    arch = _roots(client, "&kind=docs&archived_only=true")
    assert {r["session_id"] for r in arch["items"]} == {"flow-docs-assistant:2"} and all(r["archived"] for r in arch["items"])
    assert _roots(client, "&archived_only=true")["items"] == []


def test_kind_is_validated(client: TestClient) -> None:
    base = {"registry_scope": "tenant_catalog", "bundle_id": "docs-qa", "flow_id": "docsqa001", "input_data": {"prompt": "x"}}
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "session_id": "s", "kind": "notes"})
    assert r.status_code == 400 and "kind must be one of conversation, docs" in r.text
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "kind": "docs"})
    assert r.status_code == 400 and "needs a session_id" in r.text
    r = client.get("/api/gateway/runs?root_only=true&kind=notes", headers=HEADERS)
    assert r.status_code == 400 and "conversation|docs|all" in r.text
