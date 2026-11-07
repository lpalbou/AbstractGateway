"""Backlog 1103, booted for real: a polite stop (SIGTERM) while a model call is in flight, then a new
start. The run is interrupted, never failed, and continues where it stood.

Opt-in (`ABSTRACT_E2E_MODEL_HOST=1`, like `test_model_host_kill_e2e.py`, whose bundle and helpers it
reuses). Hermetic: 127.0.0.1, scratch HOME / data dir, `--no-tray`, `--model-host process`, the text
model is `model_host_fakes.FakeOpenAIServer` answering after 40 s, longer than the 30 s runner drain.

The destructive path (reproduced on 0.19.0, `untracked/release-2026-11-03/repro-restart-suspend.md`):
SIGTERM -> runner drain times out -> serve's `finally: model_host.stop()` closes the child's stdin ->
the call broke as `ModelHostCrashed` 424 -> the runtime recorded the step FAILED. A SIGKILL before that
point left the step STARTED and the next boot replayed it; a polite stop must now behave the same:
1. a plain run: RUNNING on disk after the stop, no FAILED record, no incident; the next start re-sends
   the call (started, started, completed, all attempt 1);
2. a chat turn (the shipped basic-agent, `kind: conversation`): the turn is NOT completed with the
   failure sentence as its answer; it resumes and answers;
3. an automation `run_now`: the occurrence completes with `attempts: 1`, no `automation.retry_scheduled`
   is written and no notification is sent.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from model_host_fakes import FakeOpenAIServer
from test_model_host_kill_e2e import TOKEN, _bundle, _free_port, _wait

pytestmark = pytest.mark.skipif(os.environ.get("ABSTRACT_E2E_MODEL_HOST") != "1",
                                reason="opt-in: ABSTRACT_E2E_MODEL_HOST=1 boots a real gateway")

REPO = Path(__file__).resolve().parents[1]
LONG_CALL_DELAY_S = 10.0  # x 4 chunks = 40 s: longer than the 30 s runner drain
FAILURE_WORDS = ("stopped while answering", "failed before producing", "The incident is recorded")


class _Gateway:
    """One scratch data dir served by successive `serve` processes (a stop, then a new start)."""

    def __init__(self, tmp_path: Path, fake: FakeOpenAIServer) -> None:
        self.tmp, self.fake = tmp_path, fake
        home, self.data, self.bundles = tmp_path / "home", tmp_path / "data", tmp_path / "bundles"
        (home / "tmp").mkdir(parents=True)
        self.data.mkdir()
        self.bundle_id, self.flow_id = _bundle(self.bundles)
        shutil.copy(REPO / "flows" / "bundles" / "basic-agent@0.0.5.flow", self.bundles / "basic-agent@0.0.5.flow")
        self.port = _free_port()
        gateway_src = str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)
        self.env = {
            "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # The caller's PYTHONPATH too: the AbstractRuntime that has EffectInterrupted.
            "PYTHONPATH": os.pathsep.join([gateway_src] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
            "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
            "OPENAI_BASE_URL": fake.base_url, "OPENAI_API_KEY": "fake-key",
            "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN, "ABSTRACTGATEWAY_FLOWS_DIR": str(self.bundles),
            "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_POLL_S": "0.05",
            "ABSTRACTGATEWAY_STORE_BACKEND": "file",
        }
        self.headers = {"Authorization": f"Bearer {TOKEN}"}
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=15.0, trust_env=False)
        self.proc: Any = None
        self.boots = 0
        self.log = tmp_path / "gateway-0.log"

    def boot(self) -> None:
        self.boots += 1
        self.log = self.tmp / f"gateway-{self.boots}.log"
        argv = [sys.executable, "-m", "abstractgateway", "serve", "--tailscale-https", "off", "--model-host", "process", "--data-dir",
                str(self.data), "--host", "127.0.0.1", "--port", str(self.port), "--no-tray"]
        self.proc = subprocess.Popen(argv, env=self.env, stdout=self.log.open("w"), stderr=subprocess.STDOUT,
                                     cwd=str(self.tmp))

        def _health():
            try:
                body = self.client.get("/api/health", timeout=2.0).json()
            except (httpx.HTTPError, ValueError):
                return None
            return body if body.get("status") != "starting" and (body.get("model_host") or {}).get("state") == "up" else None

        _wait(_health, timeout_s=120, what="the gateway's health", proc=self.proc, log=self.log)

    def sigterm_mid_call(self, asked_before: int) -> str:
        """Wait for the model call to be in flight, SIGTERM the gateway, wait for its exit; its log."""
        _wait(lambda: len(self.fake.requests) > asked_before, timeout_s=60, what="a request at the model",
              proc=self.proc, log=self.log)
        self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout=150)
        out = self.log.read_text()
        assert code == 0, out[-4000:]
        assert "shutdown: runner drain TIMED OUT" in out and "run(s) interrupted" in out, out[-4000:]
        assert "stopped while answering" not in out, out[-4000:]
        return out

    def get(self, path: str, **kw) -> Any:
        r = self.client.get(path, headers=self.headers, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        r = self.client.post(path, headers=self.headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def wait_terminal(self, run_id: str, timeout_s: float = 120) -> Dict[str, Any]:
        return _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed", "cancelled") else None)(
            self.get(f"/api/gateway/runs/{run_id}")), timeout_s=timeout_s, what=f"run {run_id}", proc=self.proc, log=self.log)

    # -- evidence read from the data dir
    def ledger_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for p in self.data.rglob("ledger_*.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def run_files(self) -> List[Dict[str, Any]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.data.rglob("run_*.json")]

    def incidents(self) -> List[Path]:
        return sorted(self.data.rglob("model-host-*.json"))

    def close(self) -> None:
        self.client.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture()
def gateway(tmp_path: Path):
    with FakeOpenAIServer(delay_s=0.0, chunks=4) as fake:
        gw = _Gateway(tmp_path, fake)
        try:
            yield gw
        finally:
            gw.close()


def _no_failure_recorded(gw: _Gateway) -> None:
    failed = [r for r in gw.ledger_rows() if r.get("status") == "failed"]
    assert failed == [], [(r.get("run_id"), (r.get("effect") or {}).get("type"), r.get("error")) for r in failed]
    assert not any(run.get("status") == "failed" for run in gw.run_files())
    assert gw.incidents() == [], "a requested stop wrote an incident"


def test_a_plain_run_is_running_after_the_stop_and_completes_after_the_next_start(gateway: _Gateway) -> None:
    gw = gateway
    gw.boot()
    gw.fake.delay_s = LONG_CALL_DELAY_S
    asked = len(gw.fake.requests)
    victim = gw.post("/api/gateway/runs/start", {"bundle_id": gw.bundle_id, "flow_id": gw.flow_id,
                                                 "input_data": {"prompt": "victim"}})["run_id"]
    gw.sigterm_mid_call(asked)
    [on_disk] = [r for r in gw.run_files() if r.get("run_id") == victim]
    assert on_disk["status"] == "running" and not on_disk.get("error"), on_disk
    _no_failure_recorded(gw)

    gw.fake.delay_s = 0.0
    asked = len(gw.fake.requests)
    gw.boot()
    assert gw.wait_terminal(victim)["status"] == "completed"
    assert len(gw.fake.requests) > asked, "the interrupted call was not sent again"
    ledger = gw.get(f"/api/gateway/runs/{victim}/ledger", params={"limit": 5000})["items"]
    llm = [rec for rec in ledger if (rec.get("effect") or {}).get("type") == "llm_call"]
    assert [rec["status"] for rec in llm] == ["started", "started", "completed"], [rec["status"] for rec in llm]
    assert {rec.get("attempt") for rec in llm} == {1}, "the interruption consumed a retry attempt"
    _no_failure_recorded(gw)


def test_a_chat_turn_resumes_and_answers_instead_of_completing_with_the_failure(gateway: _Gateway) -> None:
    gw = gateway
    gw.boot()
    gw.fake.delay_s = LONG_CALL_DELAY_S
    asked = len(gw.fake.requests)
    turn = gw.post("/api/gateway/runs/start", {
        "bundle_id": "basic-agent", "flow_id": "81795ea9", "kind": "conversation",
        "input_data": {"prompt": "tell me a story", "provider": "openai-compatible", "model": "fake-model", "tools": []},
    })["run_id"]
    gw.sigterm_mid_call(asked)
    _no_failure_recorded(gw)
    [root] = [r for r in gw.run_files() if r.get("run_id") == turn]
    assert root["status"] in ("running", "waiting"), root["status"]  # suspended, not finished

    gw.fake.delay_s = 0.0
    gw.boot()
    done = gw.wait_terminal(turn)
    text = json.dumps(done)
    assert done["status"] == "completed", done
    assert not any(w in text for w in FAILURE_WORDS), text[:3000]
    assert "reply:" in text, f"the turn did not answer: {text[:3000]}"
    _no_failure_recorded(gw)


def test_an_automation_occurrence_keeps_attempt_1_and_schedules_no_retry(gateway: _Gateway) -> None:
    gw = gateway
    gw.boot()
    auto = gw.post("/api/gateway/automations", {
        "request_id": str(uuid.uuid4()), "title": "Restart while running",
        "target": {"bundle_ref": "basic-agent@0.0.5", "flow_id": "81795ea9",
                   "input_data": {"prompt": "the digest", "provider": "openai-compatible", "model": "fake-model", "tools": []}},
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"start_at": "2099-01-01T00:00:00Z"}},
        "context": {"mode": "independent"}, "policy": {"tool_approval": "auto", "retry": {"max_attempts": 3}},
    })
    aid = auto["automation_id"]
    gw.fake.delay_s = LONG_CALL_DELAY_S
    asked = len(gw.fake.requests)
    gw.post(f"/api/gateway/automations/{aid}/commands",
            {"command_id": str(uuid.uuid4()), "type": "automation.run_now", "payload": {}})
    gw.sigterm_mid_call(asked)
    _no_failure_recorded(gw)

    gw.fake.delay_s = 0.0
    gw.boot()

    def _occurrence():
        items = gw.get(f"/api/gateway/automations/{aid}/occurrences").get("items") or []
        done = [o for o in items if o.get("status") in ("completed", "failed")]
        return done[0] if done else None

    occ = _wait(_occurrence, timeout_s=180, what="the occurrence", proc=gw.proc, log=gw.log)
    assert occ["status"] == "completed" and occ.get("attempts") == 1, occ
    kinds = [str(r.get("kind") or (r.get("effect") or {}).get("type") or "") for r in gw.ledger_rows()]
    blob = json.dumps(gw.ledger_rows())
    assert "automation.retry_scheduled" not in blob, "a restart consumed an attempt"
    assert not any("email.notification_sent" in p.read_text(encoding="utf-8", errors="replace")
                   for p in gw.data.rglob("audit_log*.jsonl")), "a restart sent a notification"
    assert kinds  # the ledgers were read
    _no_failure_recorded(gw)
