"""Queued visits at the entity door — decision:summon-queue-v1 pins.

The sealed contract, exercised over HTTP: explicit queue:true opt-in (409
unchanged without it), 202 + poll, the DOOR executes the stored summon at
admission (client never resubmits), park entries survive poll-silence (they
are the mailbox), admission is idempotent per queue_id, and every queue act
lands its census marker with the words absent.
"""

from __future__ import annotations

import copy
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-queue-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "min", flow_id: str = "root") -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 200.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}
        ],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-21T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as c:
        yield c


_PAYLOAD = {
    "prompt": "hi",
    "bundle_id": "min",
    "flow_id": "root",
    "input_data": {"provider": "mock", "model": "mock-model"},
}


def _setup_entity(client: TestClient, name: str = "Castor") -> None:
    assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201
    assert client.post(f"/api/gateway/entities/{name}/state", json={"state": "awake"}).status_code == 200


def _hold_seat(client: TestClient) -> dict:
    """First summon takes the seat (live run or TTL-held after — both hold
    against an undeclared second caller)."""
    r = client.post("/api/gateway/entities/Castor/summon", json=_PAYLOAD)
    assert r.status_code == 200, r.text
    return r.json()


def _expire_seat_ttl(client: TestClient) -> None:
    """Rewind the seat's renewed_at past the TTL and wait out the holder's
    run — the deterministic 'seat frees' lever (the unit-test _age_seat
    pattern, over the served store)."""
    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_seat import SEAT_IDLE_TTL_S, seat_path

    svc = get_gateway_service()
    entities_dir = svc.entity_registry.entities_dir
    path = seat_path(entities_dir, "castor")
    rec = json.loads(path.read_text(encoding="utf-8"))
    # The holding run must be terminal too (run-live holds regardless of TTL).
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{rec['run_id']}")
        if rr.status_code == 200 and rr.json().get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    rec["renewed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=SEAT_IDLE_TTL_S + 30)).isoformat()
    path.write_text(json.dumps(rec), encoding="utf-8")


def test_queue_opt_in_gets_202_and_markers_carry_no_words(client: TestClient):
    """Contract §1/§2/§14: queue:true on a held seat answers 202 with
    queue_id/position/poll; without it the 409 is unchanged; the enqueue
    marker carries the act, never the prompt words."""
    _setup_entity(client)
    _hold_seat(client)

    # Without queue:true — today's 409, byte-unchanged semantics.
    r_plain = client.post("/api/gateway/entities/Castor/summon", json=_PAYLOAD)
    assert r_plain.status_code == 409

    secret = "the-words-that-must-never-mark"
    r_q = client.post(
        "/api/gateway/entities/Castor/summon",
        json={**_PAYLOAD, "prompt": secret, "queue": True},
    )
    assert r_q.status_code == 202, r_q.text
    body = r_q.json()
    assert body["queued"] is True
    assert body["queue_id"].startswith("q-")
    assert body["position"] == 1
    assert body["poll"].endswith(f"/queue/{body['queue_id']}")
    # current_idle_deadline: a fact or null, never invented (§2).
    assert "current_idle_deadline" in body

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    svc = get_gateway_service()
    markers = read_host_markers(svc.entity_registry.entities_dir, "castor")
    enq = [m for m in markers if (m.get("payload") or {}).get("kind") == "queue_enqueued"]
    assert enq, "an enqueue must land its census marker"
    assert secret not in json.dumps(markers), "queue markers must never carry message words"

    # The seat read serves the door's depth (§20).
    seat = client.get("/api/gateway/entities/Castor/seat").json()
    assert seat["queue_depth"] == 1


def test_poll_drives_admission_when_the_seat_frees(client: TestClient):
    """Contract §3/§4: the waiter's own poll admits it at the head — the
    DOOR executes the stored summon (the client never resubmits) and the
    poll answers {admitted, run_id}; a queue_admitted marker lands."""
    _setup_entity(client)
    _hold_seat(client)

    r_q = client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True})
    assert r_q.status_code == 202, r_q.text
    qid = r_q.json()["queue_id"]

    # While held: still queued, position honest.
    p1 = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p1.status_code == 200, p1.text
    assert p1.json()["state"] == "queued"
    assert p1.json()["position"] == 1

    _expire_seat_ttl(client)

    p2 = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p2.status_code == 200, p2.text
    body = p2.json()
    assert body["state"] == "admitted", body
    assert body["run_id"], "admission must name the executed run"
    # The run is REAL — the door executed the stored payload.
    rr = client.get(f"/api/gateway/runs/{body['run_id']}")
    assert rr.status_code == 200

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    markers = read_host_markers(get_gateway_service().entity_registry.entities_dir, "castor")
    adm = [m for m in markers if (m.get("payload") or {}).get("kind") == "queue_admitted"]
    assert adm and adm[-1]["payload"].get("run_id") == body["run_id"]

    # Idempotent per queue_id (invariant 12): repolling an admitted entry
    # never mints a second run.
    p3 = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p3.json()["run_id"] == body["run_id"]


def test_step_away_dequeues_politely(client: TestClient):
    """Contract §6: the explicit leave verb marks stepped_away + census."""
    _setup_entity(client)
    _hold_seat(client)
    qid = client.post(
        "/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}
    ).json()["queue_id"]

    left = client.post(f"/api/gateway/entities/Castor/queue/{qid}/leave")
    assert left.status_code == 200, left.text
    assert left.json()["state"] == "stepped_away"

    # A stepped-away entry never admits.
    _expire_seat_ttl(client)
    p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p.json()["state"] == "stepped_away"

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    markers = read_host_markers(get_gateway_service().entity_registry.entities_dir, "castor")
    assert any((m.get("payload") or {}).get("kind") == "queue_stepped_away" for m in markers)


def test_park_survives_poll_silence_and_admits_by_sweep(client: TestClient):
    """Contract §5/§19: a park entry (the mailbox drop) is exempt from
    poll-silence reaping and admits when the seat frees — with NO client
    polling; a plain queued entry with the same silence reaps."""
    from abstractgateway.entity_queue import queue_path, reap_poll_silent
    from abstractgateway.routes.entities import _queue_sweep
    from abstractgateway.service import get_gateway_service

    _setup_entity(client)
    _hold_seat(client)

    r_park = client.post(
        "/api/gateway/entities/Castor/summon",
        json={**_PAYLOAD, "prompt": "leave this with her", "queue": True, "park": True},
    )
    assert r_park.status_code == 202, r_park.text
    park_id = r_park.json()["park"] is True and r_park.json()["queue_id"]
    r_poll = client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True})
    assert r_poll.status_code == 202
    poll_id = r_poll.json()["queue_id"]

    # Simulate poll silence: rewind both entries' last_poll_at past the reap
    # window; the park entry must survive, the poll-mode one must reap.
    entities_dir = get_gateway_service().entity_registry.entities_dir
    qpath = queue_path(entities_dir, "castor")
    data = json.loads(qpath.read_text(encoding="utf-8"))
    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    for e in data["entries"]:
        e["last_poll_at"] = past
    qpath.write_text(json.dumps(data), encoding="utf-8")

    _expire_seat_ttl(client)
    _queue_sweep("castor")  # the backstop clock's pass, called directly

    p_park = client.get(f"/api/gateway/entities/Castor/queue/{park_id}")
    assert p_park.json()["state"] == "admitted", p_park.text
    p_poll = client.get(f"/api/gateway/entities/Castor/queue/{poll_id}")
    assert p_poll.json()["state"] == "reaped", p_poll.text
    assert "poll-silent" in str(p_poll.json().get("reason") or "")


def test_queued_attempt_that_keeps_losing_stays_queued(client: TestClient):
    """Invariant 9 (attempt-not-grant): while the seat is genuinely held,
    admission attempts leave the entry queued at head — retrying is the
    design working, never an error."""
    from abstractgateway.routes.entities import _attempt_queue_admission

    _setup_entity(client)
    _hold_seat(client)
    qid = client.post(
        "/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}
    ).json()["queue_id"]

    _attempt_queue_admission("Castor")
    _attempt_queue_admission("Castor")
    p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p.json()["state"] == "queued"
    assert p.json()["attempts"] >= 1


def test_queue_marker_kinds_are_registered():
    from abstractgateway.entity_replay import HOST_MARKER_KINDS

    for kind in ("queue_enqueued", "queue_admitted", "queue_stepped_away", "queue_reaped"):
        assert kind in HOST_MARKER_KINDS, kind


def test_queued_attempts_never_write_refusal_markers(client: TestClient):
    """Audit P0-1: a retry is not an act — polls against a held seat must
    not flood the append-only biography with summon_refused markers (the
    enqueue writes queue_enqueued INSTEAD of a refusal marker; attempts
    write nothing until the admission act)."""
    _setup_entity(client)
    _hold_seat(client)

    r_q = client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True})
    assert r_q.status_code == 202
    qid = r_q.json()["queue_id"]
    for _ in range(3):
        client.get(f"/api/gateway/entities/Castor/queue/{qid}")

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    markers = read_host_markers(get_gateway_service().entity_registry.entities_dir, "castor")
    kinds = [str((m.get("payload") or {}).get("kind") or "") for m in markers]
    assert kinds.count("summon_refused") == 0, "queued waits/attempts must not mark refusals"
    assert kinds.count("queue_enqueued") == 1


def test_enqueue_cap_refuses_loudly(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Contract §19: the per-home cap refuses at enqueue, never silently."""
    import abstractgateway.entity_queue as eq

    monkeypatch.setattr(eq, "QUEUE_MAX_PER_HOME", 1)
    _setup_entity(client)
    _hold_seat(client)
    assert client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}).status_code == 202
    full = client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True})
    assert full.status_code == 409
    assert "full" in json.dumps(full.json())


def test_admission_uses_the_stored_declaration_not_the_ticker(client: TestClient):
    """Contract §4: the door executes the stored payload under the
    ENQUEUER's engraved identity/declaration — the admitted seat carries
    the entry's caller_kind, whoever's request ticked the queue."""
    _setup_entity(client)
    _hold_seat(client)
    r_q = client.post(
        "/api/gateway/entities/Castor/summon",
        json={**_PAYLOAD, "queue": True, "caller_kind": "agent"},
    )
    assert r_q.status_code == 202
    qid = r_q.json()["queue_id"]
    _expire_seat_ttl(client)
    p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p.json()["state"] == "admitted", p.text
    seat = client.get("/api/gateway/entities/Castor/seat").json()
    assert seat["holder_kind"] == "agent", "the seat must carry the ENQUEUER's declaration"
    assert seat["session_id"] == p.json()["session_id"]


def test_paused_entity_fails_the_entry_instead_of_retrying_forever(client: TestClient):
    """Audit P1-1: a 409 is not automatically contention — the paused kill
    switch can never admit, so the entry marks FAILED with the reason
    served, instead of re-attempting (and re-marking) at sweep cadence."""
    from abstractgateway.routes.entities import _attempt_queue_admission

    _setup_entity(client)
    _hold_seat(client)
    qid = client.post(
        "/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}
    ).json()["queue_id"]

    _expire_seat_ttl(client)
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "paused"}).status_code == 200
    _attempt_queue_admission("Castor")
    p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p.json()["state"] == "failed", p.text
    assert "paused" in str(p.json().get("reason") or "").lower()


def test_admitting_crash_reconciles_to_one_run(client: TestClient):
    """Audit P1-2 / contract §12 across process death: an entry left in
    'admitting' whose run WAS minted (the seat carries its deterministic
    session + holder) reconciles to admitted — never a second execution of
    the same stored prompt."""
    from abstractgateway.entity_queue import queue_path
    from abstractgateway.routes.entities import _attempt_queue_admission
    from abstractgateway.service import get_gateway_service

    _setup_entity(client)
    _hold_seat(client)
    qid = client.post(
        "/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}
    ).json()["queue_id"]
    _expire_seat_ttl(client)
    p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p.json()["state"] == "admitted"
    run_1 = p.json()["run_id"]

    # Simulate the crash window: the run + seat exist, but the entry's
    # admitted write was lost (state rolled back to 'admitting').
    entities_dir = get_gateway_service().entity_registry.entities_dir
    qpath = queue_path(entities_dir, "castor")
    data = json.loads(qpath.read_text(encoding="utf-8"))
    for e in data["entries"]:
        if e["queue_id"] == qid:
            e["state"] = "admitting"
            e["admitted_run_id"] = None
            e["admitted_session_id"] = None
    qpath.write_text(json.dumps(data), encoding="utf-8")

    _attempt_queue_admission("Castor")
    p2 = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
    assert p2.json()["state"] == "admitted"
    assert p2.json()["run_id"] == run_1, "reconciliation must adopt the minted run, never execute twice"


def test_waiting_behind_visit_is_legible(client: TestClient):
    """Contract §3 + entity room#12: an attempt refused by the visiting
    posture leaves the entry queued with waiting_behind='visit' — the card
    can say WHY the free-looking seat is not advancing."""
    from abstractruntime.identity.life import write_entity_state
    from abstractgateway.routes.entities import _attempt_queue_admission
    from abstractgateway.service import get_gateway_service

    _setup_entity(client)
    _hold_seat(client)
    qid = client.post(
        "/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "queue": True}
    ).json()["queue_id"]
    _expire_seat_ttl(client)

    home_dir = get_gateway_service().entity_registry.entities_dir / "castor"
    write_entity_state(home_dir, "asleep", reason="in conversation [visit chat-test]", mode="visiting", written_by="visit-door")
    try:
        _attempt_queue_admission("Castor")
        p = client.get(f"/api/gateway/entities/Castor/queue/{qid}")
        assert p.json()["state"] == "queued", p.text
        assert p.json()["waiting_behind"] == "visit"
    finally:
        write_entity_state(home_dir, "awake", reason="test cleanup")


def test_human_takes_ttl_held_seat_without_cancel(client: TestClient):
    """Sealed seat contract, the idle-preempt face (audit missing-pin 6):
    a human taking an agent's TTL-held seat cancels NOTHING (the holder run
    already ended) — marker says holding_status=ttl_held, cancelled_runs=[]
    and the holder's terminal status is untouched."""
    _setup_entity(client)
    first = _hold_seat(client)
    run_1 = first["run_id"]
    # Wait out the holder's run so the seat is TTL-held, not live.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{run_1}")
        if rr.status_code == 200 and rr.json().get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    status_before = client.get(f"/api/gateway/runs/{run_1}").json().get("status")

    r_h = client.post("/api/gateway/entities/Castor/summon", json={**_PAYLOAD, "caller_kind": "human"})
    assert r_h.status_code == 200, r_h.text

    assert client.get(f"/api/gateway/runs/{run_1}").json().get("status") == status_before

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    markers = read_host_markers(get_gateway_service().entity_registry.entities_dir, "castor")
    pre = [m for m in markers if (m.get("payload") or {}).get("kind") == "seat_preempted"]
    assert pre, "the takeover must mark"
    assert pre[-1]["payload"].get("holding_status") == "ttl_held"
    assert pre[-1]["payload"].get("cancelled_runs") == []
