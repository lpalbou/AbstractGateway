"""Durable session conversation replay — gateway seed half (agora `durable-sessions` v1).

When a run starts with `use_session_history` truthy and a session_id, the host
seeds `context.messages` from the session's prior COMPLETED root runs before
`runtime.start`. Client-provided messages always win; failures degrade to
no-seed with a labeled note — never a blocked start.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

pytestmark = pytest.mark.basic

_SESSION = "sess-history-demo"


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "history-demo", flow_id: str = "root") -> Path:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": flow_id,
        "entryNode": "start",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {"outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {"inputs": [{"id": "exec-in", "label": "", "type": "execution"}]},
            },
        ],
        "edges": [
            {
                "id": "e-start-end",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
    }
    manifest = {
        "bundle_format_version": 1,
        "bundle_id": bundle_id,
        "bundle_version": "0.0.1",
        "created_at": "2026-07-16T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return path


def _prior_turn(run_id: str, prompt: str, answer: str, created_at: str) -> RunState:
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
        actor_id="tester",
        session_id=_SESSION,
        parent_run_id=None,
        waiting=None,
    )


def _build_host(tmp_path: Path, run_store: InMemoryRunStore):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=run_store,
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )


def _start(
    tmp_path: Path,
    *,
    input_data: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = _SESSION,
    with_prior_turns: bool = True,
) -> RunState:
    run_store = InMemoryRunStore()
    if with_prior_turns:
        run_store.save(_prior_turn("turn-1", "analyze the report", "It concludes X.", "2026-01-01T00:00:00+00:00"))
        run_store.save(_prior_turn("turn-2", "explain simply", "In short: X.", "2026-01-01T00:05:00+00:00"))
    host = _build_host(tmp_path, run_store)
    run_id = host.start_run(
        flow_id="root",
        bundle_id="history-demo",
        input_data=dict(input_data or {}),
        session_id=session_id,
    )
    run = run_store.load(run_id)
    assert run is not None
    return run


def test_seed_opt_in_folds_prior_session_turns_into_context(tmp_path: Path) -> None:
    run = _start(tmp_path, input_data={"prompt": "and diagrams?", "use_session_history": True})

    messages = run.vars["context"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "analyze the report"
    assert messages[-1]["content"] == "In short: X."
    assert all(m["metadata"]["kind"] == "session_turn" for m in messages)

    note = run.vars["_runtime"]["session_history"]
    assert note["seeded"] == 4


def test_seed_requires_opt_in(tmp_path: Path) -> None:
    run = _start(tmp_path, input_data={"prompt": "and diagrams?", "context": {"task": "and diagrams?", "messages": []}})

    assert run.vars["context"]["messages"] == []
    assert "session_history" not in (run.vars.get("_runtime") or {})


def test_seed_never_overwrites_client_messages(tmp_path: Path) -> None:
    client_msgs = [{"role": "user", "content": "client-authored history"}]
    run = _start(
        tmp_path,
        input_data={
            "prompt": "next",
            "use_session_history": True,
            "context": {"task": "next", "messages": client_msgs},
        },
    )

    assert run.vars["context"]["messages"] == client_msgs
    note = run.vars["_runtime"]["session_history"]
    assert note["seeded"] == 0
    assert "client" in str(note.get("skipped") or "")


def test_seed_skipped_without_session_id(tmp_path: Path) -> None:
    run = _start(
        tmp_path,
        input_data={"prompt": "hi", "use_session_history": True},
        session_id=None,
    )

    ctx = run.vars.get("context")
    assert not (isinstance(ctx, dict) and ctx.get("messages"))
    assert "session_history" not in (run.vars.get("_runtime") or {})


def _strip_drop_marker(content: str) -> str:
    """Drop Runtime's leading `[#TRUNCATION: N earlier turn(s) ...]` line."""
    if content.startswith("[#TRUNCATION:") and "]\n" in content:
        return content.split("]\n", 1)[1]
    return content


def test_seed_honors_input_message_cap(tmp_path: Path) -> None:
    run = _start(
        tmp_path,
        input_data={
            "prompt": "next",
            "use_session_history": True,
            "session_history_max_messages": 2,
        },
    )

    messages = run.vars["context"]["messages"]
    # Newest turn only, never split: [user, assistant] of turn-2. The replay
    # labels the cut on the first kept message (Runtime #TRUNCATION marker).
    assert [_strip_drop_marker(m["content"]) for m in messages] == ["explain simply", "In short: X."]
    assert messages[0]["content"].startswith("[#TRUNCATION: 1 earlier turn(s)")
    assert run.vars["_runtime"]["session_history"]["max_messages"] == 2


def test_seed_honors_env_message_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES", "2")
    run = _start(tmp_path, input_data={"prompt": "next", "use_session_history": True})

    messages = run.vars["context"]["messages"]
    assert [_strip_drop_marker(m["content"]) for m in messages] == ["explain simply", "In short: X."]


def test_seed_zero_cap_disables_replay_but_normalizes_context(tmp_path: Path) -> None:
    """Audit #9: an explicit 0 means 'no replay for this run' — and the
    context still gets its messages list so turn classification stays
    stable (audit #5)."""
    run = _start(
        tmp_path,
        input_data={
            "prompt": "next",
            "use_session_history": True,
            "session_history_max_messages": 0,
        },
    )

    assert run.vars["context"]["messages"] == []
    note = run.vars["_runtime"]["session_history"]
    assert note["seeded"] == 0
    assert "disabled" in str(note.get("skipped") or "")


def test_seed_skips_non_dict_client_context(tmp_path: Path) -> None:
    """Audit #10: a non-dict client context is never stomped by the seed."""
    run = _start(
        tmp_path,
        input_data={
            "prompt": "next",
            "use_session_history": True,
            "context": "opaque-client-string",
        },
    )

    assert run.vars["context"] == "opaque-client-string"
    note = run.vars["_runtime"]["session_history"]
    assert note["seeded"] == 0
    assert "not an object" in str(note.get("skipped") or "")


def test_seed_normalizes_empty_history_to_messages_list(tmp_path: Path) -> None:
    """Audit #5: the session's FIRST turn (no prior history) must still get
    context.messages=[] so it classifies as a chat turn for later reads —
    otherwise the chat-preference filter hides turn 1 from turn 3 onward."""
    run = _start(
        tmp_path,
        input_data={"prompt": "first message", "use_session_history": True},
        with_prior_turns=False,
    )

    assert run.vars["context"]["messages"] == []
    assert run.vars["_runtime"]["session_history"]["seeded"] == 0


def test_seed_records_char_budget_in_note(tmp_path: Path) -> None:
    run = _start(
        tmp_path,
        input_data={
            "prompt": "next",
            "use_session_history": True,
            "session_history_max_chars": 5000,
        },
    )

    note = run.vars["_runtime"]["session_history"]
    assert note["max_total_chars"] == 5000
    assert note["seeded"] == 4


def test_seed_failure_degrades_with_labeled_note_and_run_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import abstractruntime.session_history as session_history_module

    def _boom(**_kwargs: Any):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(session_history_module, "session_chat_messages", _boom)

    run = _start(tmp_path, input_data={"prompt": "next", "use_session_history": True})

    note = run.vars["_runtime"]["session_history"]
    assert note["seeded"] == 0
    assert str(note.get("error") or "").startswith("#FALLBACK")
