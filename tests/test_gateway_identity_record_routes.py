"""Track record, reputation and acquaintances through the door (ADR 0052 sections 5, 6, 11; backlog 1119).

Routes: `GET /entities/{name}/track-record|reputation|acquaintances` (the entity's home stores, its
home files, its diary head) and `GET /sessions/{id}/track-record|reputation|acquaintances` (a regular
agent's session in the caller's plane).

Pinned here, each red when its rule is removed:
- the answer IS the runtime's typed output (same fields, same values; only `computed_at` differs
  between two computations), with `basis` and `not_counted` kept, and the numbers of a known fixture;
- the entity variant reads the entity's own home store and diary head (the fingerprint's `book_head`);
- `since` is normalised to UTC before the runtime compares it as text (`Z` reads like `+00:00`), and a
  value that is not ISO-8601 is a 400;
- who: a hidden entity answers like a missing one (404) for all three routes; an admin sees every
  entity; another account's session is 404 `session_not_found`; no credential is refused;
- a held home answers the maintenance 409; an unreadable `identity.json` is a 500
  `record_unreadable`, never an empty record.

Real stores (the entity's own SQLite home store; the plane's host store), real security middleware;
nothing of the runtime is replaced. Runs are written in the runtime's own fixture style.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures (gateway: alice, bob, the static admin token)
from email_fixtures import ADMIN

pytestmark = pytest.mark.integration


def _ts(minute: int) -> str:
    return f"2026-11-04T10:{minute:02d}:00+00:00"


def _run(
    run_id: str,
    *,
    minute: int,
    status: str = "completed",
    session: str = "s1",
    workflow: str = "chat-flow",
    origin: Optional[str] = "person",
    actor: str = "default:alice",
    parent: Optional[str] = None,
    stamp: Optional[Dict[str, Any]] = None,
) -> Any:
    from abstractruntime.core.models import RunState, RunStatus

    meta: Dict[str, Any] = {}
    if origin is not None:
        meta["origin"] = {"kind": origin, "actor": actor, "client": "console", "ref": {}}
    vars_obj: Dict[str, Any] = {"_meta": meta}
    if stamp is not None:
        vars_obj["_runtime"] = {"entity": stamp}
    return RunState(
        run_id=run_id,
        workflow_id=workflow,
        status=RunStatus(status),
        current_node="end",
        vars=vars_obj,
        created_at=_ts(minute),
        updated_at=_ts(minute + 1),
        session_id=session,
        parent_run_id=parent,
        actor_id="gateway",
    )


def _llm(run_id: str, step: str, *, inp: int, out: int) -> Any:
    from abstractruntime.core.models import StepRecord, StepStatus

    return StepRecord(
        run_id=run_id,
        step_id=step,
        node_id="llm",
        status=StepStatus.COMPLETED,
        effect={"type": "llm_call", "payload": {}, "result_key": "_tmp"},
        result={"usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}},
    )


def _seed(run_store: Any, ledger: Any, *, session_prefix: str = "s") -> None:
    """2 sessions, 3 counted turn roots (2 completed, 1 failed), 1 sub-run (tokens count with its root),
    1 run another run started (never counted); one visit with stamped participants."""
    s1, s2 = f"{session_prefix}1", f"{session_prefix}2"
    for run in (
        _run("r1", minute=1, session=s1),
        _run("r1-sub", minute=2, session=s1, parent="r1", origin=None),
        _run(
            "r2",
            minute=5,
            session=s2,
            workflow="visit-flow",
            stamp={"phase": "visit", "participants": ["person:alice", "entity:pollux", "agent:helper"]},
        ),
        _run("r3", minute=7, session=s2, status="failed"),
        _run("r-agent", minute=9, session=s1, origin="agent"),
    ):
        run_store.save(run)
    for rec in (
        _llm("r1", "a", inp=100, out=20),
        _llm("r1-sub", "b", inp=10, out=5),
        _llm("r2", "c", inp=200, out=40),
        _llm("r-agent", "d", inp=999, out=999),
    ):
        ledger.append(rec)


def _without_computed_at(body: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(body)
    out.pop("computed_at", None)
    return out


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def world(gateway, monkeypatch):
    """alice created Aster (seeded with runs and a diary entry), bob created Borea (empty)."""
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    import abstractgateway.routes.entities as entities_routes
    from abstractgateway.app import _maintenance_hold_409
    from abstractgateway.entities import EntityRegistry, MaintenanceHoldActive
    from abstractgateway.routes import entities_router

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    app.add_exception_handler(MaintenanceHoldActive, _maintenance_hold_409)
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    for who, name in (("alice", "Aster"), ("bob", "Borea")):
        r = c.post("/api/gateway/entities", headers=gateway[who], json={"name": name, "spark": _spark(name), "skills": []})
        assert r.status_code == 201, r.text
    er = registry.get_entity_runtime("aster")
    _seed(er.run_store, er.ledger_store)
    from abstractruntime.identity import DiaryEntry

    er.home.diary.append_entry(
        DiaryEntry(entry_id="diary_first", author=er.entity_id, text="First day.", written_at=_ts(0))
    )
    assert er.home.diary.head(), "the fixture needs a diary head to prove book_head"
    return {"c": c, "alice": gateway["alice"], "bob": gateway["bob"], "registry": registry, "er": er}


def _runtime_record(world, since: Optional[str] = None) -> Dict[str, Any]:
    from abstractruntime.identity import entity_track_record

    er = world["er"]
    home = world["registry"].entities_dir / "aster"
    return entity_track_record(home, er.run_store, er.ledger_store, book_head=er.home.diary.head(), since=since).to_dict()


# --- entities -------------------------------------------------------------------------------------


def test_entity_track_record_is_the_runtime_answer_from_the_home_store(world) -> None:
    r = world["c"].get("/api/gateway/entities/aster/track-record", headers=world["alice"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert _without_computed_at(body) == _without_computed_at(_runtime_record(world))
    # The fixture's numbers (proves the HOME store was read, not the plane's host store).
    assert body["subject"] == "entity:aster"
    assert (body["sessions"], body["runs"], body["successes"], body["failures"]) == (2, 3, 2, 1)
    assert body["tokens"] == {"input": 310, "output": 65, "total": 375}
    assert body["basis"] == "completion-rate only" and body["assessed"] == 0
    assert body["fingerprint"]["book_head"] == world["er"].home.diary.head()
    assert sorted(body["by_task_kind"]) == ["chat/chat-flow", "visit/visit-flow"]


def test_entity_track_record_since_is_normalised_to_utc(world) -> None:
    c, alice = world["c"], world["alice"]
    # r2 starts at exactly 10:05:00+00:00. As raw text "...10:05:00Z" sorts AFTER it ('Z' > '+'),
    # so only the normalised value keeps r2.
    body = c.get("/api/gateway/entities/aster/track-record?since=2026-11-04T10:05:00Z", headers=alice).json()
    assert body["since"] == "2026-11-04T10:05:00+00:00"
    assert (body["runs"], body["successes"], body["failures"]) == (2, 1, 1)
    assert _without_computed_at(body) == _without_computed_at(_runtime_record(world, since="2026-11-04T10:05:00+00:00"))
    r = c.get("/api/gateway/entities/aster/track-record?since=yesterday", headers=alice)
    assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "invalid_since"


def test_entity_reputation_is_the_runtime_answer(world) -> None:
    from abstractruntime.identity import compute_reputation, entity_track_record

    r = world["c"].get("/api/gateway/entities/aster/reputation", headers=world["alice"])
    assert r.status_code == 200, r.text
    body = r.json()
    er = world["er"]
    expected = compute_reputation(
        entity_track_record(world["registry"].entities_dir / "aster", er.run_store, er.ledger_store, book_head=er.home.diary.head())
    ).to_dict()
    assert _without_computed_at(body) == _without_computed_at(expected)
    assert body["overall"]["support"] == 3 and body["overall"]["successes"] == 2
    assert body["by_task_kind"]["chat/chat-flow"]["trust"] == pytest.approx(2 / 4)  # 1 success, 1 failure
    assert [e["run_id"] for e in body["evidence"]] == ["r1", "r2", "r3"]
    assert body["basis"] == "completion-rate only"


def test_entity_acquaintances_are_the_runtime_answer_with_not_counted(world) -> None:
    from abstractruntime.identity import entity_acquaintances

    r = world["c"].get("/api/gateway/entities/aster/acquaintances", headers=world["alice"])
    assert r.status_code == 200, r.text
    body = r.json()
    expected = entity_acquaintances(world["registry"].entities_dir / "aster", world["er"].run_store).to_dict()
    assert _without_computed_at(body) == _without_computed_at(expected)
    assert [a["id"] for a in body["entities"]] == ["entity:pollux"]
    assert {a["id"] for a in body["humans"]} == {"person:alice", "default:alice"}
    assert body["not_counted"] == {"participant of another kind (not an entity or a human)": 1}


def test_an_entity_with_no_runs_answers_the_empty_record_without_added_words(world) -> None:
    c, bob = world["c"], world["bob"]
    rec = c.get("/api/gateway/entities/borea/track-record", headers=bob).json()
    assert (rec["runs"], rec["sessions"], rec["successes"], rec["failures"]) == (0, 0, 0, 0)
    rep = c.get("/api/gateway/entities/borea/reputation", headers=bob).json()
    assert rep["overall"]["support"] == 0 and rep["overall"]["trust"] == 0.5 and rep["evidence"] == []
    acq = c.get("/api/gateway/entities/borea/acquaintances", headers=bob).json()
    assert acq["entities"] == [] and acq["humans"] == [] and acq["not_counted"] == {}
    # Exactly the runtime's fields: the door adds no copy of its own.
    assert set(rec) == set(_runtime_record(world))


@pytest.mark.parametrize("route", ["track-record", "reputation", "acquaintances"])
def test_a_hidden_entity_reads_like_a_missing_one_and_an_admin_sees_all(world, route) -> None:
    c = world["c"]
    missing = c.get(f"/api/gateway/entities/nobody-here/{route}", headers=world["bob"])
    hidden = c.get(f"/api/gateway/entities/aster/{route}", headers=world["bob"])
    assert missing.status_code == 404 and hidden.status_code == 404, (missing.text, hidden.text)
    assert hidden.json()["detail"] == missing.json()["detail"].replace("nobody-here", "aster")
    assert c.get(f"/api/gateway/entities/aster/{route}", headers=ADMIN).status_code == 200
    assert c.get(f"/api/gateway/entities/aster/{route}").status_code == 401


def test_a_held_home_answers_the_maintenance_409(world) -> None:
    world["registry"].open_maintenance_window("aster", reason="test", actor="admin")
    for route in ("track-record", "reputation", "acquaintances"):
        r = world["c"].get(f"/api/gateway/entities/aster/{route}", headers=world["alice"])
        assert r.status_code == 409 and "maintenance window" in r.json()["detail"], (route, r.text)


def test_an_unreadable_identity_is_an_error_never_an_empty_record(world) -> None:
    (world["registry"].entities_dir / "aster" / "identity.json").write_text("{not json", encoding="utf-8")
    for route in ("track-record", "reputation", "acquaintances"):
        r = world["c"].get(f"/api/gateway/entities/aster/{route}", headers=world["alice"])
        assert r.status_code == 500, (route, r.text)
        assert r.json()["detail"]["reason_code"] == "record_unreadable"


# --- a regular agent's session --------------------------------------------------------------------


@pytest.fixture()
def two_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    (tmp_path / "bundles").mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from fastapi.testclient import TestClient

    from abstractgateway.service import get_gateway_service_for_principal, reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"])
    _b, bob = reg.create_user(user_id="bob", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        principal = reg.authenticate(alice)
        assert principal is not None and principal.user_id == "alice"
        host = get_gateway_service_for_principal(principal).host
        _seed(host.run_store, host.ledger_store, session_prefix="sa-")
        yield {"c": c, "alice": {"Authorization": f"Bearer {alice}"}, "bob": {"Authorization": f"Bearer {bob}"}, "host": host}


def test_session_track_record_and_reputation_are_the_runtime_answer(two_users) -> None:
    from abstractruntime.identity import compute_reputation, session_track_record

    c, alice, host = two_users["c"], two_users["alice"], two_users["host"]
    r = c.get("/api/gateway/sessions/sa-2/track-record", headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    expected = session_track_record("sa-2", host.run_store, host.ledger_store)
    assert _without_computed_at(body) == _without_computed_at(expected.to_dict())
    assert body["subject"] == "session:sa-2" and body["fingerprint"] is None
    assert (body["runs"], body["successes"], body["failures"]) == (2, 1, 1)
    assert body["basis"] == "completion-rate only"
    since = c.get("/api/gateway/sessions/sa-2/track-record?since=2026-11-04T10:06:00Z", headers=alice).json()
    assert since["since"] == "2026-11-04T10:06:00+00:00" and since["runs"] == 1
    rep = c.get("/api/gateway/sessions/sa-2/reputation", headers=alice).json()
    assert _without_computed_at(rep) == _without_computed_at(compute_reputation(expected).to_dict())
    assert rep["overall"]["support"] == 2


def test_session_acquaintances_are_the_runtime_answer(two_users) -> None:
    from abstractruntime.identity import session_acquaintances

    c, host = two_users["c"], two_users["host"]
    body = c.get("/api/gateway/sessions/sa-2/acquaintances", headers=two_users["alice"]).json()
    assert _without_computed_at(body) == _without_computed_at(session_acquaintances("sa-2", host.run_store).to_dict())
    assert [a["id"] for a in body["entities"]] == ["entity:pollux"]
    assert body["not_counted"] == {"participant of another kind (not an entity or a human)": 1}


@pytest.mark.parametrize("route", ["track-record", "reputation", "acquaintances"])
def test_another_accounts_session_is_not_found(two_users, route) -> None:
    c = two_users["c"]
    for headers, sid in ((two_users["bob"], "sa-2"), (two_users["alice"], "no-such-session")):
        r = c.get(f"/api/gateway/sessions/{sid}/{route}", headers=headers)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["reason_code"] == "session_not_found"
    assert c.get(f"/api/gateway/sessions/sa-2/{route}").status_code == 401
