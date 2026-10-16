"""`context_used_tokens` on the automation summary (Observer board, 2026-10-15).

The number is READ from the last occurrence's own run — the history-window
receipt the controller froze with its inputs (`vars._runtime.session_history`,
ADR-0026: "newest whole turns up to the budget, recorded") — never computed by
the gateway. No receipt = null.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from automations_fixtures import (  # noqa: E402
    HEADERS,
    ECHO_FLOW_ID,
    controller_run,
    gateway_env,
    occurrence_run,
    save_runs,
    wait_until,
    write_echo_bundle,
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def test_context_used_tokens_reads_replayed_tokens_of_the_receipt_not_its_budget() -> None:
    """Occurrences are listed from the automation ledger, so the store-seeded path
    cannot carry a last occurrence; the field read is pinned here, the wire in
    the live test below."""
    from abstractgateway.routes.automations import context_used_tokens

    class Run:
        vars = {"_runtime": {"session_history": {"policy": "most_recent_whole_turns", "max_tokens": 50_000, "replayed_tokens": 31_000,
                                                 "replayed_messages": 6, "dropped_messages": 0, "dropped_tokens": 0, "strict": True,
                                                 "session_kind": "automation"}}}

    assert context_used_tokens(Run()) == 31_000

    class Zero:
        vars = {"_runtime": {"session_history": {"max_tokens": 50_000, "replayed_tokens": 0}}}

    assert context_used_tokens(Zero()) == 0


def test_context_used_tokens_is_null_without_a_receipt(client: TestClient) -> None:
    bare = controller_run()
    with_plain_child = controller_run()
    child = occurrence_run(with_plain_child.run_id, index=1, session_id=f"automation-session:{with_plain_child.run_id}")
    assert "_runtime" not in child.vars
    save_runs(bare, with_plain_child, child)
    by_id = {s["automation_id"]: s for s in client.get("/api/gateway/automations", headers=HEADERS).json()["items"]}
    # Always present on automation rows: null, never absent and never a computed number.
    assert by_id[bare.run_id]["context_used_tokens"] is None
    assert by_id[with_plain_child.run_id]["context_used_tokens"] is None
    assert "last_occurrence" not in by_id[bare.run_id]


@pytest.mark.parametrize("bad", [True, False, -1, "31000", 1.5, None])
def test_context_used_tokens_rejects_non_counts(bad) -> None:
    from abstractgateway.routes.automations import context_used_tokens

    class Run:
        vars = {"_runtime": {"session_history": {"replayed_tokens": bad}}}

    assert context_used_tokens(Run()) is None
    assert context_used_tokens(None) is None


def _create(c: TestClient, *, request_id: str, mode: str) -> str:
    body = {
        "request_id": request_id,
        "title": f"ctx {mode}",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": mode, "growing": {"max_tokens": 50_000}} if mode == "growing" else {"mode": mode},
    }
    r = c.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def _run_now_and_wait(c: TestClient, aid: str, cid: str, *, index: int) -> dict:
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"type": "automation.run_now", "command_id": cid})
    assert r.status_code == 200, r.text

    def done():
        rows = [o for o in c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"] if o["index"] == index]
        return rows[0] if rows and rows[0]["status"] in ("completed", "failed") else None

    return wait_until(done, timeout_s=20)


def test_live_growing_context_serves_the_recorded_replay_size_and_independent_serves_null(live: TestClient) -> None:
    from abstractgateway.service import get_gateway_service

    gro = _create(live, request_id="ctx-gro", mode="growing")
    ind = _create(live, request_id="ctx-ind", mode="independent")
    _run_now_and_wait(live, gro, "g1", index=1)
    second = _run_now_and_wait(live, gro, "g2", index=2)
    _run_now_and_wait(live, ind, "i1", index=1)

    note = get_gateway_service().host.run_store.load(second["run_id"]).vars["_runtime"]["session_history"]
    assert note["replayed_messages"] == 2 and note["replayed_tokens"] > 0
    summary = live.get(f"/api/gateway/automations/{gro}", headers=HEADERS).json()["summary"]
    assert summary["last_occurrence"]["run_id"] == second["run_id"]
    assert summary["context_used_tokens"] == note["replayed_tokens"]
    assert summary["growing_max_tokens"] == 50_000
    assert live.get(f"/api/gateway/automations/{ind}", headers=HEADERS).json()["summary"]["context_used_tokens"] is None
