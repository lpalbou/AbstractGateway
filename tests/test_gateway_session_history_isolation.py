"""Session history never crosses sessions (operator report 2026-10-01, Mac mini).

A new conversation (session B) must start with NO messages from another
conversation (session A) of the same user, of another user, or after a gateway
restart. The seed half runs in the host before `runtime.start`; this pins the
boundary the runtime hands the model: `context.messages` and the recorded
`_runtime.session_history` receipt.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore as JsonFileLedgerStore

from tests.test_gateway_session_history_seed import _write_min_bundle

pytestmark = pytest.mark.basic

SESSION_A = "session-aaaa1111-leak-source"
SESSION_B = "session-bbbb2222-fresh"
SECRET = "SCREENSHOT-OF-THE-GATEWAY-UI-from-session-A"


def _turn(run_id: str, session_id: str, prompt: str, answer: str, created_at: str, *, actor: str = "admin") -> RunState:
    return RunState(
        run_id=run_id,
        workflow_id="wf_chat",
        status=RunStatus.COMPLETED,
        current_node="done",
        vars={"prompt": prompt, "context": {"task": prompt, "messages": []}},
        output={"response": answer},
        error=None,
        created_at=created_at,
        updated_at=created_at,
        actor_id=actor,
        session_id=session_id,
        parent_run_id=None,
        waiting=None,
    )


def _seed_session_a(run_store: Any, *, actor: str = "admin") -> None:
    run_store.save(_turn("a-1", SESSION_A, "can you see my screenshot?", f"Yes, I can see it: {SECRET}.", "2026-10-01T09:00:00+00:00", actor=actor))
    run_store.save(_turn("a-2", SESSION_A, "send me an email", "I have no email tool.", "2026-10-01T09:05:00+00:00", actor=actor))


def _host(tmp_path: Path, run_store: Any, ledger_store: Any, *, user_id: str = "admin"):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "bundles"
    if not (bundles_dir / "history-demo.flow").exists():
        _write_min_bundle(bundles_dir=bundles_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        catalog_user_id=user_id,
        run_store=run_store,
        ledger_store=ledger_store,
        artifact_store=InMemoryArtifactStore(),
    )


def _messages_of(run: RunState) -> List[Dict[str, Any]]:
    ctx = run.vars.get("context")
    assert isinstance(ctx, dict)
    msgs = ctx.get("messages")
    return list(msgs) if isinstance(msgs, list) else []


def _assert_no_leak(run: RunState) -> None:
    blob = json.dumps(run.vars, default=str)
    assert SECRET not in blob, "session A's content reached session B's run vars"
    assert _messages_of(run) == [], f"session B was seeded with {_messages_of(run)!r}"
    receipt = (run.vars.get("_runtime") or {}).get("session_history") or {}
    assert int(receipt.get("seeded") or 0) == 0


def test_same_user_new_session_sees_nothing_from_the_other_session(tmp_path: Path) -> None:
    run_store = InMemoryRunStore()
    _seed_session_a(run_store)
    host = _host(tmp_path, run_store, InMemoryLedgerStore())
    # Session A still replays its own turns (the feature works)…
    rid_a = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "again"}, session_id=SESSION_A)
    run_a = run_store.load(rid_a)
    assert run_a is not None and SECRET in json.dumps(_messages_of(run_a))
    # …and a NEW session starts clean.
    rid_b = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "same question"}, session_id=SESSION_B)
    run_b = run_store.load(rid_b)
    assert run_b is not None
    _assert_no_leak(run_b)


def test_other_user_session_sees_nothing(tmp_path: Path) -> None:
    run_store = InMemoryRunStore()
    _seed_session_a(run_store, actor="admin")
    host = _host(tmp_path, run_store, InMemoryLedgerStore(), user_id="lpalbou")
    rid_b = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "hello"}, session_id=SESSION_B)
    run_b = run_store.load(rid_b)
    assert run_b is not None
    _assert_no_leak(run_b)


def test_new_session_after_restart_sees_nothing(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    run_store = JsonFileRunStore(str(store_dir / "runs"))
    ledger_store = JsonFileLedgerStore(str(store_dir / "ledgers"))
    _seed_session_a(run_store)
    host1 = _host(tmp_path, run_store, ledger_store)
    rid_a = host1.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "again"}, session_id=SESSION_A)
    assert SECRET in json.dumps(_messages_of(run_store.load(rid_a)))
    # "Restart": fresh store objects over the same files, a fresh host.
    run_store2 = JsonFileRunStore(str(store_dir / "runs"))
    ledger_store2 = JsonFileLedgerStore(str(store_dir / "ledgers"))
    host2 = _host(tmp_path, run_store2, ledger_store2)
    rid_b = host2.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "same question"}, session_id=SESSION_B)
    run_b = run_store2.load(rid_b)
    assert run_b is not None
    _assert_no_leak(run_b)


def test_session_b_ledger_carries_nothing_from_session_a(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    run_store = JsonFileRunStore(str(store_dir / "runs"))
    ledger_store = JsonFileLedgerStore(str(store_dir / "ledgers"))
    _seed_session_a(run_store)
    host = _host(tmp_path, run_store, ledger_store)
    rid_b = host.start_run(flow_id="root", bundle_id="history-demo", input_data={"use_session_history": True, "prompt": "same question"}, session_id=SESSION_B)
    text = json.dumps(run_store.load(rid_b).vars, default=str)
    for p in (store_dir / "ledgers").rglob("*"):
        if p.is_file() and rid_b in p.name:
            text += p.read_text(errors="replace")
    assert SECRET not in text
