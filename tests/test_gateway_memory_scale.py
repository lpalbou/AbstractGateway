"""Scale and contention of the `/memory/*` routes (architect review B6, C1).

- B6: the reconstruct owner gate must be one index read per request, and right whatever the number of
  memories naming a ref (1,201 here: 1,200 of one owner then 1 of another).
- C1: the plane's home is opened ONCE per service and reused, so a read never waits for a writer's lock on
  the home (the per-request open took a write lock)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from test_gateway_memory_routes import SESSION, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.basic


def test_reconstruct_gate_is_right_and_fast_above_a_thousand_memories(tmp_path: Path) -> None:
    from abstractgateway.memory_store import open_memory_home, resolve_memory_store_config
    from abstractgateway.routes.memory import _names_ref_for_caller

    facade = open_memory_home(resolve_memory_store_config(base_dir=tmp_path, backend="sqlite"), create=True)
    try:
        art = {"kind": "artifact", "artifact_id": "shared-import-file"}
        when = "2026-10-01T00:00:00+00:00"
        for k in range(240):
            facade.apply_revision({
                "revision_id": f"a-{k}", "scope": "global", "owner_id": "session_memory_A", "cause": "import",
                "occurred_at": when, "provenance": {"sources": [art], "actor": "importer"},
                "ops": [{"op": "assert", "payload": {"subject": f"ex:a{k}-{j}", "predicate": "ex:p", "object": "v"}}
                        for j in range(5)],
            })
        facade.apply_revision({
            "revision_id": "b-0", "scope": "global", "owner_id": "session_memory_B", "cause": "ingest",
            "occurred_at": when, "provenance": {"sources": [art], "actor": "b"},
            "ops": [{"op": "assert", "payload": {"subject": "ex:b", "predicate": "ex:p", "object": "v"}}],
        })
        for owners in ({"session_memory_B"}, {"session_memory_A"}):
            t0 = time.perf_counter()
            allowed = _names_ref_for_caller(facade, art, owners)
            elapsed = time.perf_counter() - t0
            assert allowed is True, owners
            assert elapsed < 0.05, f"{owners}: {elapsed * 1000:.1f} ms"
        # An owner with no memory naming the ref is refused, as fast.
        assert _names_ref_for_caller(facade, art, {"session_memory_C"}) is False
    finally:
        facade.journal.close()
        facade.store.close()


def test_a_read_does_not_wait_for_a_writer_holding_the_home(world) -> None:  # noqa: F811
    c, alice = world["client"], world["alice"]
    params = {"session_id": SESSION}
    assert c.get("/api/gateway/memory/mem-a/history", headers=alice, params=params).status_code == 200  # warm

    from abstractgateway.memory_store import resolve_memory_home, resolve_memory_store_config

    home = resolve_memory_home(resolve_memory_store_config(base_dir=Path(world["svc"].stores.base_dir)))
    holding, release = threading.Event(), threading.Event()

    def writer() -> None:
        conn = sqlite3.connect(str(home), isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _writer_probe (x)")
        conn.execute("INSERT INTO _writer_probe VALUES (1)")
        holding.set()
        release.wait(5)
        conn.execute("ROLLBACK")
        conn.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert holding.wait(5)
        t0 = time.perf_counter()
        r = c.get("/api/gateway/memory/mem-a/history", headers=alice, params=params)
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200, r.text
        assert [e["event"] for e in r.json()["items"]] == ["asserted", "closed"]
        assert elapsed < 0.1, f"a read waited {elapsed * 1000:.0f} ms behind a writer"
    finally:
        release.set()
        t.join()
