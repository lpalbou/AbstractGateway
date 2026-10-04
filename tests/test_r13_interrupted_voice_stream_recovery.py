"""R13.1 recovery: a streamed-speech wait left by a dead gateway is closed at boot, never stuck.

The incident's ledger (run a2abbf65, workflow wf_abstractcore_run_facade_tts_stream):
the Read-aloud child run parked on `WAIT_EVENT abstractcore.voice.tts.stream:<uuid>`;
the watchdog killed the process mid-stream; after the restart nothing could
send that event, the child stayed WAITING, and clients showed the run as
"Waiting for an event › Streaming voice synthesis is running." with
"Event routing unavailable". Runtimes since R13.1 never create that wait; the
gateway closes the ones older processes left, with a sentence.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

import pytest

from abstractgateway.runner import INTERRUPTED_VOICE_STREAM_SENTENCE, GatewayRunner, GatewayRunnerConfig
from abstractruntime import Effect, EffectType, Runtime, RunStatus, StepPlan, WorkflowSpec
from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore

pytestmark = pytest.mark.basic

LEGACY_KEY = "abstractcore.voice.tts.stream:c8f04c59-7819-48b4-87b7-b03e2f921ab2"


def _legacy_stream_workflow() -> WorkflowSpec:
    def wait(run, ctx):
        return StepPlan(
            node_id="wait",
            effect=Effect(
                type=EffectType.WAIT_EVENT,
                payload={"wait_key": LEGACY_KEY, "resume_to_node": "done", "prompt": "Streaming voice synthesis is running.",
                         "allow_free_text": False, "details": {"mode": "abstractcore_voice_stream", "text": "Found it!"}},
                result_key="_abstractcore_result",
            ),
            next_node="done",
        )

    def done(run, ctx):
        return StepPlan(node_id="done", complete_output={"result": run.vars.get("_abstractcore_result")})

    return WorkflowSpec("wf_abstractcore_run_facade_tts_stream", "wait", {"wait": wait, "done": done})


def _parent_workflow() -> WorkflowSpec:
    def ask(run, ctx):
        return StepPlan(node_id="ask", effect=Effect(type=EffectType.ASK_USER, payload={"prompt": "next?"}, result_key="a"), next_node="done")

    def done(run, ctx):
        return StepPlan(node_id="done", complete_output={"ok": True})

    return WorkflowSpec("basic-agent@0.0.5:81795ea9", "ask", {"ask": ask, "done": done})


def _seed(data_dir: Path, *, created_before_boot: bool = True) -> tuple[str, str]:
    """Write, with the gateway's own file stores, what the incident left on disk."""
    data_dir.mkdir(parents=True, exist_ok=True)
    rs, ls = JsonFileRunStore(data_dir), JsonlLedgerStore(data_dir)
    rt = Runtime(run_store=rs, ledger_store=ls)
    parent_wf, child_wf = _parent_workflow(), _legacy_stream_workflow()
    parent = rt.start(workflow=parent_wf, session_id="c1f908d4-d70d-41b7-b32e-7b17d4280041", actor_id="gateway")
    rt.tick(workflow=parent_wf, run_id=parent)
    child = rt.start(workflow=child_wf, session_id="c1f908d4-d70d-41b7-b32e-7b17d4280041", actor_id="gateway", parent_run_id=parent)
    rt.tick(workflow=child_wf, run_id=child)
    if created_before_boot:
        st = rs.load(child)
        # The incident's own timestamp (run a2abbf65): created by the process the watchdog killed,
        # i.e. before this process (and its runner module) started.
        st.created_at = "2026-10-04T19:23:22.399245+00:00"
        rs.save(st)
    assert rs.load(child).status == RunStatus.WAITING
    return parent, child


class _Host:
    def __init__(self, data_dir: Path) -> None:
        self.run_store = JsonFileRunStore(data_dir)
        self.ledger_store = JsonlLedgerStore(data_dir)
        self.artifact_store = None
        self.runtime = Runtime(run_store=self.run_store, ledger_store=self.ledger_store)

    def runtime_and_workflow_for_run(self, run_id: str):  # the facade workflow is not in any registry
        raise KeyError(run_id)


def test_the_close_pass_finishes_a_legacy_stream_wait_with_a_sentence(tmp_path: Path) -> None:
    data = tmp_path / "data"
    parent, child = _seed(data)
    host = _Host(data)
    runner = GatewayRunner(base_dir=data, host=host, config=GatewayRunnerConfig())
    waiting = host.run_store.list_runs(status=RunStatus.WAITING, limit=100)

    closed = runner._close_interrupted_voice_streams(waiting=waiting)

    assert closed == [child]
    st = host.run_store.load(child)
    assert st.status == RunStatus.COMPLETED and st.waiting is None
    assert st.output["result"]["errors"] == [{"message": INTERRUPTED_VOICE_STREAM_SENTENCE, "code": "interrupted"}]
    assert "Read aloud was interrupted because the gateway restarted" in INTERRUPTED_VOICE_STREAM_SENTENCE
    # The caller's run is untouched (still on its own question).
    p = host.run_store.load(parent)
    assert p.status == RunStatus.WAITING and p.waiting.reason.value == "user"
    # Idempotent: nothing left to close.
    assert runner._close_interrupted_voice_streams(waiting=host.run_store.list_runs(status=RunStatus.WAITING, limit=100)) == []


def test_a_stream_wait_created_by_this_process_is_left_alone(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _parent, child = _seed(data, created_before_boot=False)
    host = _Host(data)
    runner = GatewayRunner(base_dir=data, host=host, config=GatewayRunnerConfig())
    assert runner._close_interrupted_voice_streams(waiting=host.run_store.list_runs(status=RunStatus.WAITING, limit=100)) == []
    assert host.run_store.load(child).status == RunStatus.WAITING


def test_a_restarted_gateway_closes_the_stuck_read_aloud_wait_at_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real boot: seeded data dir, the gateway app starts with its runner, the wait is gone."""
    from fastapi.testclient import TestClient

    data = tmp_path / "runtime"
    parent, child = _seed(data)
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "r13-boot")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "1")
    from abstractgateway.app import app

    with TestClient(app) as client:
        client.get("/api/gateway/runs/" + parent, headers={"Authorization": "Bearer r13-boot"})  # waits for boot
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = json.loads((data / f"run_{child}.json").read_text())["status"]
            if status != "waiting":
                break
            time.sleep(0.2)
        body = client.get(f"/api/gateway/runs/{child}", headers={"Authorization": "Bearer r13-boot"}).json()
    assert status == "completed", f"the stuck read-aloud wait was not closed at boot (status {status})"
    assert body.get("waiting") in (None, {}), body
    assert json.loads((data / f"run_{parent}.json").read_text())["status"] == "waiting"  # its own question
