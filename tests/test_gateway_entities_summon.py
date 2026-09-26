"""Summoning an entity over HTTP (a2a 0004, deliverable 2).

POST /api/gateway/entities/{name}/summon:
- renders the identity prelude (pure read) and REFUSES the summon when the
  budget cannot fit the core — 409, reasons verbatim, NO run started;
- on success starts a run whose vars carry a SIGNED stamp bound to that
  run's id and session (verified by the routing layer before any home
  opens), with the prelude leading the system prompt.
"""

from __future__ import annotations

import copy
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-summon-shared-secret"


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


def test_summon_starts_a_stamped_run(client: TestClient):
    r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()})
    assert r.status_code == 201, r.text
    # Newborn = sleep (c1503): wake so the summon reaches the gate under test.
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    r2 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hello there",
            "bundle_id": "min",
            "flow_id": "root",
            # The substrate chain refuses without an explicit choice
            # (request > home substrate.yaml > operator env > refusal) —
            # this summon declares it at the request step (flow c5253 P1-1).
            "input_data": {"provider": "mock", "model": "mock-model"},
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    run_id = body["run_id"]
    # The resolved substrate is caller-visible, never silent (P1-1).
    assert body["substrate"] == {"provider": "mock", "model": "mock-model", "source": "request"}
    assert body["entity_id"] == "entity:castor"  # clean keys, plan item 6
    assert body["channel"] == "workplace"
    assert "You are Castor." in body["prelude"]["text"]
    assert body["session_id"].startswith("entity-castor-")

    # The persisted run carries a VERIFYING stamp bound to this run, and the
    # actor was flipped to "gateway" (tickable) after the stamp was saved.
    from abstractgateway.entity_gate import read_stamp, verify_summon_stamp
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    run = svc.host.run_store.load(run_id)
    assert run is not None
    assert run.actor_id == "gateway"
    stamp = read_stamp(run)
    ok, err = verify_summon_stamp(stamp, data_dir=svc.entity_registry.data_dir, run=run)
    assert ok, err
    # WHO is stamped by the door: the verified principal PLUS the entity
    # itself (EXPLICIT co-presence, ruled a2a 0007 — owners are never
    # implied; the entity is present at its own session by construction).
    assert len(stamp["participants"]) == 2
    assert stamp["participants"][0].startswith("person:")
    assert stamp["participants"][1] == body["entity_id"]

    # The prelude leads the system prompt of the summoned run.
    assert str(run.vars.get("system") or "").startswith("<identity_prelude")

    # The run actually progresses (the runner ticks stamped runs normally).
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{run_id}")
        assert rr.status_code == 200, rr.text
        if rr.json().get("status") == "completed":
            break
        time.sleep(0.1)
    else:
        pytest.fail("summoned run did not complete")


def test_refused_prelude_aborts_the_summon(client: TestClient):
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    # Newborn = sleep (c1503): wake so the summon reaches the gate under test.
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    list_runs = getattr(svc.host.run_store, "list_runs", None)
    runs_before = len(list_runs(limit=1000) or []) if callable(list_runs) else None

    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hello", "bundle_id": "min", "flow_id": "root", "prelude_budget": 16},
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["refused"] is True
    assert any("#REFUSED" in reason for reason in detail["reasons"])
    assert any("truncated core is a different person" in reason for reason in detail["reasons"])

    # No fallback to a truncated header — and NO run was started.
    if runs_before is not None:
        runs_after = len(list_runs(limit=1000) or [])
        assert runs_after == runs_before, "a refused prelude must not start any run"


def test_summon_unknown_entity_404(client: TestClient):
    r = client.post("/api/gateway/entities/nobody/summon", json={"prompt": "hi"})
    assert r.status_code == 404


def test_summon_without_substrate_refuses_loudly(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """flow c5253 P1-1: a summon with no provider/model used to get SILENT
    SUBSTRATE SUBSTITUTION (the gateway capability default answered, the
    home's substrate.yaml never consulted, warnings empty). The chain is
    the chat lane's: request > home substrate.yaml > operator env > LOUD
    REFUSAL — never a code/capability default."""
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root"},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["refused"] is True
    assert any("no mind substrate chosen" in reason for reason in detail["reasons"]), detail


def test_summon_resolves_home_substrate(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The home's persisted substrate.yaml is the second chain step, and the
    resolved pair is stamped caller-visible with its source."""
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
    r0 = client.put(
        "/api/gateway/entities/Castor/substrate",
        json={"provider": "mock", "model": "home-model"},
    )
    assert r0.status_code == 200, r0.text

    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["substrate"] == {"provider": "mock", "model": "home-model", "source": "home substrate.yaml"}


def test_summon_reasoning_effort_shown_equals_what_runs(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Reasoning-first-citizen + adversary cycle-1 D1: the response's
    substrate block and the run's _runtime.thinking must be the SAME value
    — even when the caller seeds _runtime.thinking directly (a request-level
    spelling, folded into resolution, never a silent survivor)."""
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
    # Home stores the triple with an effort.
    r0 = client.put(
        "/api/gateway/entities/Castor/substrate",
        json={"provider": "mock", "model": "home-model", "thinking": "high"},
    )
    assert r0.status_code == 200, r0.text

    # (1) Home effort applies and is shown.
    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["substrate"]["thinking"] == "high"
    run = client.get(f"/api/gateway/entities/Castor/runs/{body['run_id']}") if False else None
    # The run vars carry the SAME value (read through the run store).
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    loaded = svc.host.runtime.run_store.load(body["run_id"])
    assert (loaded.vars.get("_runtime") or {}).get("thinking") == "high"
    # (2) A caller-seeded _runtime.thinking is a REQUEST-level ask: it wins
    # over the home value AND the response shows the value that runs. Same
    # session = the seat's slide semantics let the conversation continue.
    r2 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi again",
            "bundle_id": "min",
            "flow_id": "root",
            "session_id": body["session_id"],
            "input_data": {"_runtime": {"thinking": "low"}},
        },
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["substrate"]["thinking"] == "low"
    loaded2 = svc.host.runtime.run_store.load(b2["run_id"])
    assert (loaded2.vars.get("_runtime") or {}).get("thinking") == "low"


def test_second_summon_of_a_live_life_refuses_409(client: TestClient):
    """flow c5260 P1-B foundation + conversation-seat slice 2: a second
    (agent) summon refuses 409 while the seat is held — live run OR the
    sliding TTL (the incident fix: the seat no longer frees between a
    conversation's turns). retry_after_s rides the refusal; a HUMAN-declared
    summon takes the agent-held seat (machinery yields to humans)."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    payload = {
        "prompt": "hi",
        "bundle_id": "min",
        "flow_id": "root",
        "input_data": {"provider": "mock", "model": "mock-model"},
    }
    r1 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r1.status_code == 200, r1.text
    run_1 = r1.json()["run_id"]

    # Immediately again (undeclared caller = agent): 409 whether the first
    # run is still live (one life) or already terminal (TTL hold — the
    # seat protects the conversation's inter-turn gaps, agent-vs-agent
    # first-wins included).
    r2 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["refused"] is True
    assert any("one life, one summon" in reason for reason in detail["reasons"])
    assert detail["live_run_id"] == run_1
    assert int(detail["retry_after_s"]) >= 1
    assert r2.headers.get("Retry-After") == str(detail["retry_after_s"])

    # The TTL-held seat still refuses agents after the run terminates
    # (the exact incident window: the operator's turn had COMPLETED when
    # the probe stole the seat between his messages).
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{run_1}")
        if rr.status_code == 200 and rr.json().get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    r3 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r3.status_code == 409, r3.text

    # MACHINERY YIELDS TO HUMANS: the same summon declared human preempts
    # the unknown-kind holder and proceeds.
    r4 = client.post(
        "/api/gateway/entities/Castor/summon", json={**payload, "caller_kind": "human"}
    )
    assert r4.status_code == 200, r4.text

    # The seat now belongs to the human; its record says so.
    seat = client.get("/api/gateway/entities/Castor/seat").json()
    assert seat["held"] is True
    assert seat["holder_kind"] == "human"
    assert seat["session_id"] == r4.json()["session_id"]


def test_human_preempts_a_live_agent_run_at_the_door(client: TestClient):
    """Conversation-seat slice 2, the incident's other face: an agent holds
    the seat with a run STILL LIVE; a human summon cancels the holder's run
    tree at the turn boundary (runtime's terminal-guarded cancel_run),
    lands a seat_preempted marker, and proceeds — the human's message is
    never lost server-side."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    payload = {"prompt": "hi", "bundle_id": "min", "flow_id": "root", "input_data": {"provider": "mock", "model": "mock-model"}}
    r1 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r1.status_code == 200, r1.text
    run_1 = r1.json()["run_id"]

    # Freeze the holder run as LIVE deterministically: WAITING with no due
    # wait is inert to the tick loop (never scheduled), non-terminal to the
    # seat, and legitimately cancellable (cancel_run cancels RUNNING/WAITING).
    # First let the runner finish its own tick of this run (the tiny flow ends
    # at once): freezing it while that tick is still saving let the tick's
    # final save overwrite the freeze under a loaded machine (flaky, REVIEW/19).
    from abstractruntime.core.models import RunStatus
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    deadline = time.time() + 30.0
    while time.time() < deadline:
        cur = svc.host.run_store.load(run_1)
        if cur is not None and getattr(cur.status, "value", cur.status) in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("the runner never finished the holder's first tick")
    run = svc.host.run_store.load(run_1)
    run.status = RunStatus.WAITING
    run.waiting = None
    svc.host.run_store.save(run)

    # An agent cannot displace the live holder...
    r_agent = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r_agent.status_code == 409, r_agent.text
    assert int(r_agent.json()["detail"]["retry_after_s"]) >= 1

    # ...the human preempts it.
    r_human = client.post("/api/gateway/entities/Castor/summon", json={**payload, "caller_kind": "human"})
    assert r_human.status_code == 200, r_human.text

    # The holder's run is honestly CANCELLED (terminal-guarded runtime-side).
    rr = client.get(f"/api/gateway/runs/{run_1}")
    assert rr.json().get("status") == "cancelled", rr.text

    # The preempt beat is in the biography.
    from abstractgateway.entity_replay import read_host_markers

    markers = read_host_markers(svc.entity_registry.entities_dir, "castor")
    preempts = [m for m in markers if (m.get("payload") or {}).get("kind") == "seat_preempted"]
    assert preempts, "a live preempt must land a seat_preempted marker"
    p = preempts[-1].get("payload") or {}
    assert p.get("preempted_run_id") == run_1
    assert p.get("holding_status") == "live"
    assert run_1 in (p.get("cancelled_runs") or [])
    assert p.get("preempting_principal")
    # Never any message text in a marker.
    assert "prompt" not in p and "text" not in p


def test_seat_read_surface_reports_occupancy(client: TestClient):
    """Conversation-seat plan item 6: GET /seat returns {held:false} before a
    summon and the held seat block (run/session/holder) while a run is live —
    the drawer's occupancy line. Pure read; never mutates."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    empty = client.get("/api/gateway/entities/Castor/seat")
    assert empty.status_code == 200, empty.text
    assert empty.json()["held"] is False
    assert empty.json()["entity_id"] == "entity:castor"

    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root", "input_data": {"provider": "mock", "model": "mock-model"}},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # While live, the seat reads held with the run/session + holder metadata.
    # (The minimal flow can complete fast; poll a moment for the live window.)
    seen_held = False
    for _ in range(20):
        seat = client.get("/api/gateway/entities/Castor/seat").json()
        if seat.get("held"):
            seen_held = True
            assert seat["run_id"] == run_id
            assert seat["session_id"] == r.json()["session_id"]
            assert seat["holder_kind"] == "unknown"  # GW-H makes it truthful later
            assert seat["idle_ttl_s"] == 300
            break
        time.sleep(0.02)
    # Either we caught the live window, or the run already finished (seat free).
    if not seen_held:
        assert client.get(f"/api/gateway/runs/{run_id}").json().get("status") in {
            "completed", "failed", "cancelled"
        }


def test_summon_refused_writes_a_host_marker(client: TestClient):
    """Conversation-seat plan item 3: a one-life-one-summon 409 lands a
    summon_refused host marker (the refusal census) naming the holding run +
    the refusing principal — NOT the refused message text."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    from abstractgateway.service import get_gateway_service
    from abstractgateway.entity_replay import read_host_markers

    payload = {"prompt": "hi", "bundle_id": "min", "flow_id": "root", "input_data": {"provider": "mock", "model": "mock-model"}}
    r1 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r1.status_code == 200, r1.text
    run_1 = r1.json()["run_id"]

    # Deterministic since slice 2: live run OR TTL hold — an undeclared
    # (agent) second summon always refuses while the seat is held.
    r2 = client.post("/api/gateway/entities/Castor/summon", json=payload)
    assert r2.status_code == 409, r2.text
    svc = get_gateway_service()
    markers = read_host_markers(svc.entity_registry.entities_dir, "castor")
    # Host markers carry kind + details flattened into `payload`.
    refused = [m for m in markers if (m.get("payload") or {}).get("kind") == "summon_refused"]
    assert refused, "a 409 must land a summon_refused marker"
    p = refused[-1].get("payload") or {}
    assert p.get("holding_run_id") == run_1
    assert p.get("refusing_principal")
    assert p.get("refused_caller_kind") == "agent"  # undeclared reads agent
    # The refused message text is never recorded (the marker holds the act).
    assert "prompt" not in p and "text" not in p


def test_summon_whitespace_prompt_refused_at_boundary(client: TestClient):
    """flow c5253 P1-3: an empty prompt used to be accepted and die 2.5s
    later inside the run as engine jargon ('MEMORY_PROBE op=probe requires
    payload.cue'). Boundary validation belongs to the boundary."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    # Pydantic min_length rejects the empty string (422 from the model).
    r_empty = client.post("/api/gateway/entities/Castor/summon", json={"prompt": ""})
    assert r_empty.status_code == 422, r_empty.text
    # Whitespace-only passes min_length; the route strip-check refuses it.
    r_ws = client.post("/api/gateway/entities/Castor/summon", json={"prompt": "   "})
    assert r_ws.status_code == 422, r_ws.text
    assert "whitespace" in r_ws.json()["detail"]


def test_context_recommendation_is_soft(client: TestClient):
    """Operator 2026-08-01 (both passes; superseding the round-8 hard
    floor): 50k is the RECOMMENDED working size and 200k the soft
    ACCEPTABLE ceiling ("it is acceptable to go to 200k context, but
    ideally, let's have a (soft) recommended target of 50k tokens");
    'more a soft than a hard limit ... if it needs to grow, it needs to
    grow' still governs. Declared windows below the recommendation PROCEED
    with a labeled #RECOMMENDED warning; windows above the acceptable
    ceiling PROCEED with the same warning class naming 200k; an undeclared
    window proceeds with a labeled #FALLBACK warning (the gateway checks
    what it can see, never guesses what it cannot)."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    # Newborn = sleep (c1503): wake so the summon reaches the gate under test.
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

    # Explicit declaration below the recommendation -> accepted, labeled.
    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi",
            "bundle_id": "min",
            "flow_id": "root",
            "context_window_tokens": 8192,
            "session_id": "ctx-soft-1",
            "input_data": {"provider": "mock", "model": "mock-model"},
        },
    )
    assert r.status_code == 200, r.text
    warns = r.json()["warnings"]
    assert any(w.startswith("#RECOMMENDED") and "8192" in w and "50000-token" in w for w in warns), warns
    assert any("recommendation, not a wall" in w for w in warns), warns

    # A flow pin declaring a small window -> the same soft warning (same
    # conversation slides the seat; a fresh session would 409 on the seat,
    # which is the ONE-LIFE gate, not the context gate).
    r2 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi",
            "bundle_id": "min",
            "flow_id": "root",
            "session_id": "ctx-soft-1",
            "input_data": {"max_in_tokens": 4096, "provider": "mock", "model": "mock-model"},
        },
    )
    assert r2.status_code == 200, r2.text
    assert any(w.startswith("#RECOMMENDED") and "4096" in w for w in r2.json()["warnings"])

    # Declared at/above the recommendation and within the acceptable
    # ceiling -> proceeds, no context warning (growth is never blocked:
    # 50k <= 65536 <= 200k passes clean).
    r3 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi",
            "bundle_id": "min",
            "flow_id": "root",
            "session_id": "ctx-soft-1",
            "context_window_tokens": 65536,
            "input_data": {"provider": "mock", "model": "mock-model"},
        },
    )
    assert r3.status_code == 200, r3.text
    assert not any("context window" in w for w in r3.json()["warnings"])

    # One life, one summon (c5260 P1-B): the next summon must wait for this
    # run to end or it refuses 409.
    r3_run = r3.json()["run_id"]
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{r3_run}")
        if rr.status_code == 200 and rr.json().get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)

    # Undeclared -> proceeds with the labeled #FALLBACK warning. Rides r3's
    # session (the seat's same-session SLIDE since slice 2 — a fresh session
    # would queue behind r3's TTL-held seat, which is the seat tests' lane,
    # not this floor test's).
    r4 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi",
            "bundle_id": "min",
            "flow_id": "root",
            "session_id": r3.json()["session_id"],
            "input_data": {"provider": "mock", "model": "mock-model"},
        },
    )
    assert r4.status_code == 200, r4.text
    assert any("#FALLBACK context window undeclared" in w for w in r4.json()["warnings"])

    # Above the 200k ACCEPTABLE ceiling -> proceeds with the soft
    # #RECOMMENDED-class warning naming 200000 (operator 2026-08-01: "it is
    # acceptable to go to 200k context" — beyond is guidance, never a block).
    r4_run = r4.json()["run_id"]
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{r4_run}")
        if rr.status_code == 200 and rr.json().get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    r5 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={
            "prompt": "hi",
            "bundle_id": "min",
            "flow_id": "root",
            "session_id": r4.json()["session_id"],
            "context_window_tokens": 262144,
            "input_data": {"provider": "mock", "model": "mock-model"},
        },
    )
    assert r5.status_code == 200, r5.text
    warns5 = r5.json()["warnings"]
    assert any(
        w.startswith("#RECOMMENDED") and "262144" in w and "200000-token" in w and "above" in w
        for w in warns5
    ), warns5
    # And never the below-recommendation text on an above-ceiling window.
    assert not any("below" in w and "context window" in w for w in warns5), warns5


def test_a_summoned_run_is_confined_and_cannot_name_the_data_folder(client: TestClient):
    """Entity summons take a client's input_data: the /runs/start workspace
    policy applies (400 for the data folder), and the run gets the host's
    built-in deny rule, so its tools refuse the data folder."""
    from pathlib import Path

    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope, rewrite_tool_arguments
    from abstractgateway.service import get_gateway_service

    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
    svc = get_gateway_service()
    data = Path(svc.host.data_dir).resolve()

    bad = client.post("/api/gateway/entities/Castor/summon", json={
        "prompt": "hi", "bundle_id": "min", "flow_id": "root",
        "input_data": {"provider": "mock", "model": "mock-model", "workspace_root": str(data)}})
    assert bad.status_code == 400 and "data folder" in str(bad.json()["detail"]), bad.text

    ok = client.post("/api/gateway/entities/Castor/summon", json={
        "prompt": "hi", "bundle_id": "min", "flow_id": "root", "caller_kind": "human",
        "input_data": {"provider": "mock", "model": "mock-model", "workspace_builtin_allow": ["/"]}})
    assert ok.status_code == 200, ok.text
    v = svc.host.run_store.load(ok.json()["run_id"]).vars
    assert str(data) in v["workspace_builtin_deny_prefixes"]
    assert v["workspace_builtin_allow"] == [v["workspace_root"]]
    scope = WorkspaceScope.from_input_data(v)
    for target in (data / "entities", data / "auth"):
        with pytest.raises(ValueError):
            rewrite_tool_arguments(tool_name="list_files", args={"directory_path": str(target)}, scope=scope)


def test_an_entity_visit_workspace_never_leaves_the_home(tmp_path):
    """Entity visits (the entity chat / own-time loop) use the runtime's
    identity tools, confined structurally to <home>/workspace: the data
    folder around the home is out of reach."""
    from abstractruntime.identity.tools import WorkspaceRoot

    home = tmp_path / "data" / "entities" / "castor"
    home.mkdir(parents=True)
    (tmp_path / "data" / "run_x.json").write_text("{}")
    ws = WorkspaceRoot(home)
    for escape in ("../../run_x.json", "../../../data", str(tmp_path / "data" / "run_x.json")):
        with pytest.raises(PermissionError):
            ws._route(escape)
