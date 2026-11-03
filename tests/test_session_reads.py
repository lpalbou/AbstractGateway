"""Conversation read receipts are durable account state, never execution steps.

Exercise the public API and restart/outbox/concurrent-writer behavior. Tests use
isolated gateway stores; none of the operator's sessions is read or marked seen.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient
from automations_fixtures import HEADERS, chat_run, controller_run, discussion_run, gateway_env, occurrence_run, save_runs
from abstractgateway.session_reads import SessionReadLedger

URL = "/api/gateway/me/session-reads"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app
    with TestClient(app) as c:
        c.data_dir = data_dir
        yield c


def receipt(run, status="completed"):
    return {"session_id": run.session_id, "run_id": run.run_id, "status": status}


def post(c, *items):
    return c.post(URL, headers=HEADERS, json={"receipts": list(items)})


def test_read_receipt_survives_clients_and_does_not_modify_execution(client):
    run = chat_run(session_id="chat")
    save_runs(run)
    from abstractgateway.service import get_gateway_service
    store = get_gateway_service().host.run_store
    before = asdict(store.load(run.run_id))
    empty = client.get(URL, headers=HEADERS)
    assert empty.status_code == 200 and empty.json() == {"items": [], "next_after": 0, "more": False}
    assert not (client.data_dir / "session_reads.sqlite3").exists()
    first = post(client, receipt(run))
    assert first.status_code == 200, first.text
    event = first.json()["items"][0]
    assert event["run_id"] == run.run_id and event["status"] == "completed"
    assert event["sequence"] > 0 and event["event_id"].startswith("session.read:")
    assert post(client, receipt(run)).json()["items"] == [event]
    # A second HTTP client and a reopened SQLite connection replay the same fact.
    page = client.get(URL, headers=HEADERS).json()
    assert page["items"] == [event] and page["next_after"] == event["sequence"] and not page["more"]
    ledger = SessionReadLedger(client.data_dir, "default", "admin")
    assert ledger.page()["items"] == [event]
    assert asdict(store.load(run.run_id)) == before
    from abstractgateway.events_stream import stream_for
    streamed = [e for e in stream_for(client.data_dir, "default", "admin").read() if e["type"] == "session.read"]
    assert len(streamed) == 1 and streamed[0]["data"] == event


def test_request_does_not_acknowledge_later_answer_and_late_older_turn_stays_independent(client):
    old = chat_run(session_id="chat", created_at="2026-10-01T00:00:00Z")
    new = chat_run(session_id="chat", created_at="2026-10-01T01:00:00Z")
    from abstractruntime.core.models import RunStatus
    old.status = RunStatus.RUNNING
    save_runs(old, new)
    assert post(client, receipt(old, "request"), receipt(new)).status_code == 200
    old.status = RunStatus.COMPLETED
    save_runs(old)
    statuses = {(e["run_id"], e["status"]) for e in client.get(URL, headers=HEADERS).json()["items"]}
    assert (old.run_id, "completed") not in statuses and (old.run_id, "request") in statuses
    assert post(client, receipt(old)).status_code == 200
    assert len(client.get(URL, headers=HEADERS).json()["items"]) == 3


def test_batch_validates_all_members_before_any_write(client):
    run = chat_run(session_id="chat")
    save_runs(run)
    bad = {**receipt(run), "session_id": "foreign"}
    assert post(client, receipt(run), bad).status_code == 404
    assert client.get(URL, headers=HEADERS).json()["items"] == []
    assert post(client, {**receipt(run), "status": "failed"}).status_code == 409
    assert post(client, {**receipt(run), "run_id": "../escape"}).status_code == 422
    assert post(client).status_code == 422
    assert post(client, *([receipt(run)] * 101)).status_code == 422


def test_roots_only_and_automation_attention_is_separate(client):
    root = chat_run(session_id="chat")
    child = chat_run(session_id="chat", parent_run_id=root.run_id)
    controller = controller_run()
    independent = occurrence_run(controller.run_id, index=1, session_id="auto")
    growing = occurrence_run(controller.run_id, index=2, session_id="auto", session_kind="automation")
    wake = occurrence_run(controller.run_id, index=3, session_id="chat", session_kind="chat")
    discussion = discussion_run(controller.run_id, index=1)
    save_runs(root, child, controller, independent, growing, wake, discussion)
    for run in (child, controller, independent, growing):
        response = post(client, receipt(run))
        assert response.status_code == 422, (run.run_id, response.text)
    assert post(client, receipt(root), receipt(wake), receipt(discussion)).status_code == 200


def test_filtered_pagination_has_stable_cursor_and_rejects_large_requests(client):
    runs = [chat_run(session_id="chat") for _ in range(4)]
    save_runs(*runs)
    assert post(client, *(receipt(run) for run in runs)).status_code == 200
    page = client.get(URL, headers=HEADERS, params={"limit": 2}).json()
    assert page["more"] and len(page["items"]) == 2
    second = client.get(URL, headers=HEADERS, params={"after": page["next_after"], "limit": 2}).json()
    assert not second["more"] and len(second["items"]) == 2
    assert set(e["run_id"] for e in page["items"] + second["items"]) == {r.run_id for r in runs}
    filtered = client.get(URL, headers=HEADERS, params=[("run_id", runs[0].run_id), ("run_id", runs[2].run_id)]).json()
    assert {e["run_id"] for e in filtered["items"]} == {runs[0].run_id, runs[2].run_id}
    assert client.get(URL, headers=HEADERS, params={"after": -1}).status_code == 422
    assert client.get(URL, headers=HEADERS, params={"limit": 1001}).status_code == 422
    assert client.get(URL, headers=HEADERS, params=[("run_id", "r")] * 101).status_code == 422


def test_principal_isolation_and_filtered_restart_replay(tmp_path):
    alice = SessionReadLedger(tmp_path, "tenant", "alice")
    bob = SessionReadLedger(tmp_path, "tenant", "bob")
    tenant = SessionReadLedger(tmp_path, "other", "alice")
    event = {"session_id": "s", "run_id": "r", "status": "completed"}
    row = alice.append([event])[0]
    assert bob.page()["items"] == [] and tenant.page()["items"] == []
    bob_row = bob.append([event])[0]
    assert row["event_id"] != bob_row["event_id"]
    assert SessionReadLedger(tmp_path, "tenant", "alice").page(run_ids=["r"])["items"] == [row]
    assert alice.page(run_ids=["missing"])["items"] == []


def test_concurrent_writers_are_idempotent_and_monotone(tmp_path):
    event = {"session_id": "s", "run_id": "r", "status": "completed"}
    def write(n):
        ledger = SessionReadLedger(tmp_path, "tenant", "alice")
        return ledger.append([event, {**event, "run_id": f"r-{n}"}])
    with ThreadPoolExecutor(max_workers=8) as pool:
        written = list(pool.map(write, range(24)))
    common = [pair[0] for pair in written]
    assert all(item == common[0] for item in common)
    rows = SessionReadLedger(tmp_path, "tenant", "alice").page(limit=100)["items"]
    assert len(rows) == 25 and len({row["sequence"] for row in rows}) == 25
    assert [row["sequence"] for row in rows] == sorted(row["sequence"] for row in rows)


def test_outbox_retries_after_delivery_crash_without_duplicating_stream_event(tmp_path):
    from abstractgateway.events_stream import UserEventStream
    ledger = SessionReadLedger(tmp_path, "tenant", "alice")
    event = ledger.append([{"session_id": "s", "run_id": "r", "status": "completed"}])[0]
    stream = UserEventStream(tmp_path, "tenant", "alice")
    class DeliveredThenCrashed:
        def append(self, *args, **kwargs):
            stream.append(*args, **kwargs)
            raise OSError("after durable stream append, before SQL delivered flag")
    with pytest.raises(OSError):
        ledger.publish_pending(DeliveredThenCrashed())
    reopened = SessionReadLedger(tmp_path, "tenant", "alice")
    assert reopened.page()["items"] == [event]
    reopened.publish_pending(stream)
    reopened.publish_pending(stream)
    assert len(stream.read()) == 1
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT published FROM read_events").fetchone()[0] == 1


def test_stream_outage_does_not_forge_or_lose_committed_read(client, monkeypatch):
    run = chat_run(session_id="chat")
    save_runs(run)
    from abstractgateway.events_stream import UserEventStream
    original = UserEventStream.append
    def fail(self, *args, **kwargs):
        raise OSError("delivery unavailable")
    monkeypatch.setattr(UserEventStream, "append", fail)
    response = post(client, receipt(run))
    assert response.status_code == 200 and response.json()["items"][0]["run_id"] == run.run_id
    monkeypatch.setattr(UserEventStream, "append", original)
    assert client.get(URL, headers=HEADERS).json()["items"] == response.json()["items"]
    from abstractgateway.events_stream import stream_for
    assert len([e for e in stream_for(client.data_dir, "default", "admin").read() if e["type"] == "session.read"]) == 1


def test_corrupt_receipt_database_raises_instead_of_pretending_everything_is_unread(tmp_path):
    ledger = SessionReadLedger(tmp_path, "tenant", "alice")
    ledger.path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        ledger.page()


def test_http_receipts_are_shared_for_same_account_and_isolated_from_other_accounts(tmp_path, monkeypatch):
    gateway_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    from abstractgateway.users import GatewayUserRegistry
    from abstractgateway.service import get_gateway_service_for_principal
    from abstractgateway.app import app
    registry = GatewayUserRegistry()
    _a, alice_token = registry.create_user(user_id="alice", roles=["user"])
    _b, bob_token = registry.create_user(user_id="bob", roles=["user"])
    alice = {"Authorization": f"Bearer {alice_token}"}
    bob = {"Authorization": f"Bearer {bob_token}"}
    with TestClient(app) as client:
        owner = get_gateway_service_for_principal(registry.authenticate(alice_token))
        run = chat_run(session_id="alice-session")
        owner.host.run_store.save(run)
        written = client.post(URL, headers=alice, json={"receipts": [receipt(run)]})
        assert written.status_code == 200, written.text
        assert client.get(URL, headers=alice).json()["items"] == written.json()["items"]
        assert client.get(URL, headers=bob).json()["items"] == []
        foreign = client.post(URL, headers=bob, json={"receipts": [receipt(run)]})
        assert foreign.status_code == 404, foreign.text
        assert client.get(URL, headers=alice).json()["items"] == written.json()["items"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failure_and_cancellation_are_distinct_observed_outcomes(client, status):
    from abstractruntime.core.models import RunStatus
    run = chat_run(session_id="chat")
    run.status = RunStatus(status)
    save_runs(run)
    assert post(client, receipt(run)).status_code == 409
    response = post(client, receipt(run, status))
    assert response.status_code == 200 and response.json()["items"][0]["status"] == status
    assert post(client, receipt(run, "running")).status_code == 422


def test_user_stream_retention_cannot_erase_canonical_read_receipts(tmp_path):
    from datetime import datetime, timedelta, timezone
    from abstractgateway.events_stream import UserEventStream
    ledger = SessionReadLedger(tmp_path, "tenant", "alice")
    row = ledger.append([{"session_id": "s", "run_id": "r", "status": "completed"}])[0]
    stream = UserEventStream(tmp_path, "tenant", "alice")
    notified = []
    off = stream.add_listener(lambda: notified.append(True))
    ledger.publish_pending(stream)
    off()
    assert notified == [True]
    pruned = stream.inbox.prune(now=datetime.now(timezone.utc) + timedelta(days=365))
    assert pruned["removed"] == 1 and stream.read() == []
    assert SessionReadLedger(tmp_path, "tenant", "alice").page()["items"] == [row]
