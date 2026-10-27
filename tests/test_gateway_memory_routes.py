"""`/api/gateway/memory/*` over the runtime memory facade (memory transition S5, contract §6).

Real app, real auth middleware, real principals (user accounts on), real plane stores: a run started
through `/runs/start`, a ledger row appended to that plane's ledger, an artifact in its artifact store,
and two revisions written into the plane's memory HOME through `MemoryFacade` (the runtime facade; the
test never imports AbstractMemory either). Each test goes red when its route is removed."""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from test_gateway_principal_isolation_matrix import _client, _create_user, _write_min_bundle

pytestmark = pytest.mark.basic

ADMIN = {"Authorization": "Bearer admin-token"}
SESSION = "mem-session-1"


def _principal(user_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=user_id)


def _claim(aid: str, owner: str, scope: str = "session", s: str = "ex:laurent", o: str = "ex:ada") -> Dict[str, Any]:
    return {
        "op": "assert",
        "payload": {
            "assertion_id": aid, "subject": s, "predicate": "schema:knows", "object": o,
            "scope": scope, "owner_id": owner, "observed_at": "2026-10-01T00:00:00+00:00",
        },
    }


def _revision(rid: str, ops, *, owner: str, provenance: Dict[str, Any], scope: str = "session") -> Dict[str, Any]:
    return {
        "revision_id": rid, "scope": scope, "owner_id": owner, "cause": "ingest",
        "occurred_at": "2026-10-01T00:00:00+00:00", "ops": ops, "provenance": provenance,
    }


def _plane_facade(svc):
    from abstractgateway.memory_store import open_memory_home, resolve_memory_store_config

    return open_memory_home(
        resolve_memory_store_config(base_dir=Path(svc.stores.base_dir)),
        create=True,
        ledger_store=svc.stores.ledger_store,
        run_store=svc.stores.run_store,
        artifact_store=svc.stores.artifact_store,
    )


def _close(facade) -> None:
    facade.journal.close()
    facade.store.close()


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """alice's plane with: a run (session SESSION), ledger row #seq, artifact, memories mem-a (sourced,
    then closed) and mem-b (derived from mem-a), and mem-x owned by another owner."""
    from abstractruntime.core.models import StepRecord, StepStatus

    from abstractgateway.service import get_gateway_service_for_principal

    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / "alice" / "flows")
        r = client.post(
            "/api/gateway/runs/start", headers=alice,
            json={"bundle_id": "iso-bundle", "flow_id": "root", "session_id": SESSION, "input_data": {"prompt": "hi"}},
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        svc = get_gateway_service_for_principal(_principal("alice"))
        assert Path(svc.stores.base_dir) == (tmp_path / "runtime" / "users" / "default" / "alice" / "runtime").resolve()
        # A step ref addresses the step's STARTED row (the effect payload verbatim); its terminal row follows.
        seq = svc.stores.ledger_store.append(
            StepRecord(run_id=run_id, step_id="step-memory-1", node_id="remember", status=StepStatus.STARTED,
                       effect={"type": "memory_revise", "payload": {"said": "Laurent knows Ada"}, "result_key": None})
        )
        assert isinstance(seq, int) and seq >= 1
        svc.stores.ledger_store.append(
            StepRecord(run_id=run_id, step_id="step-memory-1", node_id="remember", status=StepStatus.COMPLETED,
                       result={"ok": True})
        )
        artifact = svc.stores.artifact_store.store(b"Laurent knows Ada (verbatim)", content_type="text/plain", run_id=run_id)
        artifact_id = artifact.artifact_id

        from abstractgateway.memory_store import session_memory_owner_id

        owner = session_memory_owner_id(SESSION)
        step_ref = {"kind": "step", "run_id": run_id, "seq": seq, "step_id": "step-memory-1"}
        art_ref = {"kind": "artifact", "artifact_id": artifact_id, "run_id": run_id}
        facade = _plane_facade(svc)
        try:
            r1 = facade.apply_revision(_revision(
                "rev-1", [_claim("mem-a", owner)], owner=owner,
                provenance={"sources": [step_ref, art_ref], "actor": "runtime"},
            ))
            assert r1["applied"] is True
            r2 = facade.apply_revision(_revision(
                "rev-2",
                [
                    {"op": "close", "payload": {"assertion_id": "mem-a", "valid_until": "2026-10-02T00:00:00+00:00",
                                                "kind": "supersede", "replacement_ids": ["mem-b"], "reason": "refined"}},
                    _claim("mem-b", owner, o="ex:ada-lovelace"),
                ],
                owner=owner,
                provenance={"derived_from": ["mem-a"], "actor": "abstractmemory:test"},
            ))
            assert r2["applied"] is True
            facade.apply_revision(_revision(
                "rev-x", [_claim("mem-x", "someone_else", scope="global", s="ex:secret")], owner="someone_else",
                scope="global", provenance={"sources": [step_ref], "actor": "runtime"},
            ))
        finally:
            _close(facade)
        yield {
            "client": client, "alice": alice, "run_id": run_id, "seq": seq, "artifact_id": artifact_id,
            "owner": owner, "svc": svc, "step_ref": step_ref, "art_ref": art_ref, "tmp": tmp_path,
        }


def test_query_returns_the_revisions_written_into_the_plane_home(world) -> None:
    c, alice = world["client"], world["alice"]
    r = c.post("/api/gateway/memory/query", headers=alice, json={"scope": "session", "session_id": SESSION, "order": "asc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_id"] == world["owner"]
    assert {i["assertion_id"] for i in body["items"]} == {"mem-a", "mem-b"}
    assert body["truncated"] is False
    assert body["home"]["path"].endswith("users/default/alice/runtime/abstractmemory/memory.sqlite3")
    by_id = {i["assertion_id"]: i for i in body["items"]}
    # Compared as instants: AbstractMemory may normalise the stored spelling (fractional seconds).
    assert datetime.fromisoformat(by_id["mem-a"]["valid_until"]) == datetime.fromisoformat("2026-10-02T00:00:00+00:00")

    # Bounded: limit cuts and says so.
    r = c.post("/api/gateway/memory/query", headers=alice, json={"scope": "session", "session_id": SESSION, "limit": 1})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1 and r.json()["truncated"] is True
    # No unbounded page on /memory/query (kg/query's 0/-1 "unlimited" is refused here).
    assert c.post("/api/gateway/memory/query", headers=alice, json={"scope": "session", "session_id": SESSION, "limit": -1}).status_code == 422
    # The same caller, by run id (run -> session owner through the runtime).
    r = c.post("/api/gateway/memory/query", headers=alice, json={"scope": "session", "run_id": world["run_id"]})
    assert r.status_code == 200 and {i["assertion_id"] for i in r.json()["items"]} == {"mem-a", "mem-b"}


def test_history_shows_asserted_then_closed(world) -> None:
    c, alice = world["client"], world["alice"]
    r = c.get("/api/gateway/memory/mem-a/history", headers=alice, params={"session_id": SESSION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["event"] for e in body["items"]] == ["asserted", "closed"]
    assert [e["revision_id"] for e in body["items"]] == ["rev-1", "rev-2"]
    assert body["owner_id"] == world["owner"] and body["truncated"] is False
    r = c.get("/api/gateway/memory/mem-a/history", headers=alice, params={"session_id": SESSION, "limit": 1})
    assert r.json()["count"] == 1 and r.json()["truncated"] is True


def test_sources_lists_the_refs_directly_and_transitively(world) -> None:
    c, alice = world["client"], world["alice"]
    r = c.get("/api/gateway/memory/mem-a/sources", headers=alice, params={"session_id": SESSION})
    assert r.status_code == 200, r.text
    keys = {i["key"] for i in r.json()["items"]}
    assert keys == {f"step:{world['run_id']}#{world['seq']}", f"artifact:{world['artifact_id']}"}

    # mem-b names no ledger source itself; transitively it inherits mem-a's.
    direct = c.get("/api/gateway/memory/mem-b/sources", headers=alice, params={"session_id": SESSION, "transitive": "false"})
    assert direct.status_code == 200 and direct.json()["items"] == []
    trans = c.get("/api/gateway/memory/mem-b/sources", headers=alice, params={"session_id": SESSION})
    assert {i["key"] for i in trans.json()["items"]} == keys


def test_reconstruct_step_and_artifact_then_gone_and_unsupported(world) -> None:
    c, alice = world["client"], world["alice"]
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": world["step_ref"], "session_id": SESSION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["kind"] == "step"
    assert body["content"]["effect"]["payload"] == {"said": "Laurent knows Ada"}
    assert body["metadata"]["status"] == "started" and body["metadata"]["effect_type"] == "memory_revise"
    assert body["metadata"]["seq"] == world["seq"] and body["metadata"]["step_id"] == "step-memory-1"

    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": world["art_ref"], "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["content"] == "Laurent knows Ada (verbatim)"

    assert world["svc"].stores.artifact_store.delete(world["artifact_id"]) is True
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": world["art_ref"], "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "source_gone", r.text

    # An admin reconstructs any ref of its plane (the root plane here): a kind with no indexed path is
    # unsupported, a ledger row that does not exist is gone, never an error.
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=ADMIN, json={"ref": {"kind": "message", "message_id": "m-1"}})
    assert r.status_code == 200 and r.json()["status"] == "unsupported", r.text
    gone = dict(world["step_ref"], seq=world["seq"] + 1000)
    gone.pop("step_id")
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=ADMIN, json={"ref": gone})
    assert r.status_code == 200 and r.json()["status"] == "source_gone", r.text


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "step", "run_id": "r"},  # no seq
        {"kind": "nope", "run_id": "r"},
        {"kind": "artifact"},
        {"kind": "run", "run_id": "r", "unknown_field": 1},
    ],
)
def test_reconstruct_refuses_a_malformed_ref_with_422(world, bad) -> None:
    r = world["client"].post("/api/gateway/memory/sources/reconstruct", headers=world["alice"], json={"ref": bad})
    assert r.status_code == 422, r.text


def test_derived_by_ref_key(world) -> None:
    c, alice = world["client"], world["alice"]
    key = f"step:{world['run_id']}#{world['seq']}"
    r = c.get(f"/api/gateway/memory/derived?ref_key={urllib.parse.quote(key, safe=':')}&session_id={SESSION}", headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    # mem-a names the step, mem-b derives from it; mem-x (another owner) names it too and is WITHHELD, counted.
    assert body["items"] == ["mem-a", "mem-b"]
    assert body["withheld"] == 1 and body["truncated"] is False
    assert body["ref"]["kind"] == "step" and body["ref"]["seq"] == world["seq"]

    r = c.get(f"/api/gateway/memory/derived?ref_key={urllib.parse.quote(key, safe=':')}&limit=1&session_id={SESSION}", headers=alice)
    assert r.json()["truncated"] is True

    for bad in ("step:" + world["run_id"], "nope:x", "artifact"):
        r = c.get("/api/gateway/memory/derived", headers=alice, params={"ref_key": bad})
        assert r.status_code == 422, (bad, r.text)


def test_a_non_admin_caller_is_refused_another_owner(world) -> None:
    c, alice = world["client"], world["alice"]
    r = c.post("/api/gateway/memory/query", headers=alice, json={"scope": "global", "owner_id": "someone_else"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "owner_forbidden"
    r = c.post("/api/gateway/memory/query", headers=alice, json={"scope": "all", "all_owners": True})
    assert r.status_code == 403, r.text
    r = c.get("/api/gateway/memory/mem-x/history", headers=alice)
    assert r.status_code == 403, r.text
    r = c.get("/api/gateway/memory/mem-x/sources", headers=alice)
    assert r.status_code == 403, r.text
    # Without naming its session, alice does not own the session owner either.
    r = c.get("/api/gateway/memory/mem-a/history", headers=alice)
    assert r.status_code == 403, r.text
    # An unknown id is 404 for a non-admin (no probing of what exists).
    assert c.get("/api/gateway/memory/nope/history", headers=alice).status_code == 404


def test_an_admin_reads_any_owner_of_its_plane(world) -> None:
    """The admin's plane is the root data folder; it holds its own home."""
    from abstractgateway.service import get_gateway_service_for_principal
    from abstractgateway.security.principal import local_admin_principal

    c = world["client"]
    admin_svc = get_gateway_service_for_principal(local_admin_principal())
    assert Path(admin_svc.stores.base_dir) == (world["tmp"] / "runtime").resolve()
    facade = _plane_facade(admin_svc)
    try:
        facade.apply_revision(_revision(
            "rev-admin", [_claim("mem-other", "someone_else", scope="global")], owner="someone_else", scope="global",
            provenance={"sources": [{"kind": "run", "run_id": "r-admin"}], "actor": "runtime"},
        ))
    finally:
        _close(facade)
    r = c.post("/api/gateway/memory/query", headers=ADMIN, json={"scope": "global", "owner_id": "someone_else"})
    assert r.status_code == 200, r.text
    assert [i["assertion_id"] for i in r.json()["items"]] == ["mem-other"]
    r = c.get("/api/gateway/memory/mem-other/history", headers=ADMIN)
    assert r.status_code == 200 and [e["event"] for e in r.json()["items"]] == ["asserted"]
    r = c.post("/api/gateway/memory/query", headers=ADMIN, json={"scope": "all", "all_owners": True})
    assert r.status_code == 200 and [i["assertion_id"] for i in r.json()["items"]] == ["mem-other"]


@pytest.mark.parametrize(
    "backend,leaf,same_as_kg",
    [("sqlite", "kg.sqlite3", True), ("lancedb", "memory.sqlite3", False), ("memory", "memory.sqlite3", False)],
)
def test_the_plane_home_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, leaf: str, same_as_kg: bool) -> None:
    """One file per plane: with `sqlite` the home IS the KG store the bundle host hands the runtime's
    memory effects (`open_gateway_memory_store`), so `/kg/query`, `/memory/*` and the effects agree."""
    from abstractgateway.memory_store import resolve_memory_home, resolve_memory_store_config

    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_PATH", raising=False)
    cfg = resolve_memory_store_config(base_dir=tmp_path, backend=backend)
    home = resolve_memory_home(cfg)
    assert home == tmp_path.resolve() / "abstractmemory" / leaf
    assert (home == cfg.path) is same_as_kg


def test_memory_revise_through_the_bundle_host_store_lands_in_the_home_the_routes_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the `sqlite` KG backend the bundle host's wiring (`build_gateway_memory_kg_handlers`, the function
    `_build_runtime` calls) hands MEMORY_REVISE the plane home, its journal, the plane's runtime id and artifact
    store: an assert + close applied through the runtime's handler is what `/memory/*` serves, the close is
    journaled, and the refs it stamped reconstruct through the route."""
    from abstractruntime.core.models import Effect, EffectType

    from abstractgateway.hosts.bundle_host import build_gateway_memory_kg_handlers
    from abstractgateway.memory_store import resolve_memory_home

    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_PATH", raising=False)
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post(
            "/api/gateway/runs/start", headers=ADMIN,
            json={"bundle_id": "iso-bundle", "flow_id": "root", "session_id": SESSION, "input_data": {"prompt": "hi"}},
        )
        assert r.status_code == 200, r.text
        svc = get_gateway_service_for_admin()
        run = svc.host.run_store.load(r.json()["run_id"])
        artifact_id = svc.stores.artifact_store.store(b"verbatim", content_type="text/plain", run_id=run.run_id).artifact_id

        handlers, resolution = build_gateway_memory_kg_handlers(
            data_root=Path(svc.stores.base_dir), run_store=svc.host.run_store,
            artifact_store=svc.stores.artifact_store, runtime_id=svc.host.catalog_runtime_id,
        )
        try:
            assert resolution.config.path == resolve_memory_home(resolution.config)
            assert resolution.journal is not None
            revise = handlers[EffectType.MEMORY_REVISE]
            outcome = revise(run, Effect(type=EffectType.MEMORY_REVISE, payload={
                "ops": [_claim("mem-r", "global_memory", scope="global")],
                "scope": "global", "owner_id": "global_memory", "artifact_ids": [artifact_id], "revision_id": "rev-r",
            }), None)
            assert outcome.status == "completed", outcome.error
            outcome = revise(run, Effect(type=EffectType.MEMORY_REVISE, payload={
                "ops": [{"op": "close", "payload": {"assertion_id": "mem-r", "valid_until": "2026-10-03T00:00:00+00:00",
                                                    "kind": "retract", "replacement_ids": [], "reason": "wrong"}}],
                "scope": "global", "owner_id": "global_memory", "artifact_ids": [artifact_id], "revision_id": "rev-r-close",
            }), None)
            assert outcome.status == "completed", outcome.error
            # The close is in the journal the store shares its file with (ranked recall folds closures from it).
            assert [c.assertion_id for c in resolution.journal.closures(assertion_id="mem-r")] == ["mem-r"]
        finally:
            resolution.journal.close()
            resolution.store.close()

        h = client.get("/api/gateway/memory/mem-r/history", headers=ADMIN)
        assert h.status_code == 200, h.text
        events = h.json()["items"]
        assert [(e["event"], e["revision_id"]) for e in events] == [("asserted", "rev-r"), ("closed", "rev-r-close")]
        assert events[1]["detail"]["closure_id"]
        s = client.get("/api/gateway/memory/mem-r/sources", headers=ADMIN)
        items = s.json()["items"]
        # Called outside a runtime step there is no seq: the runtime stamps the (real) run, never a guessed step,
        # and every ref carries the plane's runtime id.
        assert {i["key"] for i in items} == {f"artifact:{artifact_id}", f"run:{run.run_id}"}
        assert {i.get("runtime_id") for i in items} == {svc.host.catalog_runtime_id}
        art = next(i for i in items if i["kind"] == "artifact")
        art = {k: v for k, v in art.items() if k != "key"}
        rec = client.post("/api/gateway/memory/sources/reconstruct", headers=ADMIN, json={"ref": art})
        assert rec.status_code == 200 and rec.json()["status"] == "ok" and rec.json()["content"] == "verbatim", rec.text


def get_gateway_service_for_admin():
    from abstractgateway.security.principal import local_admin_principal
    from abstractgateway.service import get_gateway_service_for_principal

    return get_gateway_service_for_principal(local_admin_principal())


def _cross_owner_lineage(world) -> None:
    """mem-y (another owner) names a secret artifact; alice's mem-own derives from her own mem-a. A derivation
    from mem-y is refused at write by AbstractMemory (lineage never crosses owners)."""
    facade = _plane_facade(world["svc"])
    try:
        facade.apply_revision(_revision(
            "rev-y", [_claim("mem-y", "someone_else", scope="global", s="ex:secret2")], owner="someone_else",
            scope="global", provenance={"sources": [{"kind": "artifact", "artifact_id": "SECRET-ARTIFACT"}], "actor": "runtime"},
        ))
        with pytest.raises(ValueError, match="lineage never crosses owners"):
            facade.apply_revision(_revision(
                "rev-own-cross", [_claim("mem-own-cross", world["owner"], o="ex:d3")], owner=world["owner"],
                provenance={"derived_from": ["mem-y"], "actor": "abstractmemory:test"},
            ))
        facade.apply_revision(_revision(
            "rev-own", [_claim("mem-own", world["owner"], o="ex:d2")], owner=world["owner"],
            provenance={"derived_from": ["mem-a"], "actor": "abstractmemory:test"},
        ))
    finally:
        _close(facade)


def test_transitive_sources_stay_inside_the_callers_memories(world) -> None:
    _cross_owner_lineage(world)
    c, alice = world["client"], world["alice"]
    assert c.get("/api/gateway/memory/mem-y/sources", headers=alice).status_code == 403
    r = c.get("/api/gateway/memory/mem-own/sources", headers=alice, params={"session_id": SESSION})
    assert r.status_code == 200, r.text
    keys = {i["key"] for i in r.json()["items"]}
    assert "artifact:SECRET-ARTIFACT" not in keys
    # Her own parent mem-a is followed (her own lineage walk); nothing foreign to withhold.
    assert keys == {f"step:{world['run_id']}#{world['seq']}", f"artifact:{world['artifact_id']}"}
    assert r.json()["withheld"] == 0


def test_history_names_only_readable_ids(world) -> None:
    _cross_owner_lineage(world)
    c, alice = world["client"], world["alice"]
    h = c.get("/api/gateway/memory/mem-own/history", headers=alice, params={"session_id": SESSION})
    assert h.status_code == 200, h.text
    assert h.json()["items"][0]["detail"]["derived_from"] == ["mem-a"]
    assert h.json()["withheld"] == 0


def test_the_refusal_does_not_name_the_other_owner(world) -> None:
    c, alice = world["client"], world["alice"]
    for r in (
        c.get("/api/gateway/memory/mem-x/history", headers=alice),
        c.post("/api/gateway/memory/query", headers=alice, json={"scope": "global", "owner_id": "someone_else"}),
    ):
        assert r.status_code == 403, r.text
        assert "someone_else" not in r.text


def test_reconstruct_applies_the_owner_rule(world) -> None:
    """A non-admin reconstructs only a ref that one of ITS memories names: not a ref named only by another
    owner's memory, nor one reached through its own memory derived from another owner's."""
    _cross_owner_lineage(world)
    c, alice = world["client"], world["alice"]
    secret = {"kind": "artifact", "artifact_id": "SECRET-ARTIFACT"}
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": secret, "session_id": SESSION})
    assert r.status_code == 403, r.text
    assert "someone_else" not in r.text
    # A ref nobody names is refused too (no probing the ledger by id).
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": {"kind": "artifact", "artifact_id": "nope"}, "session_id": SESSION})
    assert r.status_code == 403, r.text
    # Her own memory's ref is served when she names the session that owns it, refused when she does not.
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": world["art_ref"], "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": world["art_ref"]})
    assert r.status_code == 403, r.text


def test_reconstruct_bounds_a_run_ref_and_reads_only_this_planes_runtime(world) -> None:
    c, alice = world["client"], world["alice"]
    facade = _plane_facade(world["svc"])
    try:
        facade.apply_revision(_revision(
            "rev-run", [_claim("mem-run", world["owner"], o="ex:run-level")], owner=world["owner"],
            provenance={"sources": [{"kind": "run", "run_id": world["run_id"]}], "actor": "runtime"},
        ))
    finally:
        _close(facade)
    run_ref = {"kind": "run", "run_id": world["run_id"]}
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": run_ref, "session_id": SESSION, "limit": 1})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    meta = r.json()["metadata"]
    assert len(r.json()["content"]["ledger"]) == 1 and meta["ledger_returned"] == 1
    assert meta["ledger_left_out"] == meta["ledger_records"] - 1 and meta["ledger_records"] >= 2 and meta["truncated"] is True
    assert c.post("/api/gateway/memory/sources/reconstruct", headers=alice,
                  json={"ref": run_ref, "session_id": SESSION, "limit": 1001}).status_code == 422

    # The plane's runtime id is the facade's: a ref stamped with it resolves, one of another runtime does not.
    own_runtime = world["svc"].config.runtime_id
    assert own_runtime
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice,
               json={"ref": dict(world["step_ref"], runtime_id=own_runtime), "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    foreign = dict(world["step_ref"], runtime_id="entity-home-elsewhere")
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=alice, json={"ref": foreign, "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "unsupported", r.text
    assert "entity-home-elsewhere" in r.json()["reason"]
