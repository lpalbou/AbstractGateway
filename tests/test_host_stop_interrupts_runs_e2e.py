"""Backlog 1103, booted for real: SIGTERM on the gateway while a run's model call is in flight, then
a fresh start. The run is interrupted, never failed, and continues where it stood.

Opt-in (`ABSTRACT_E2E_MODEL_HOST=1`, like `test_model_host_kill_e2e.py`, whose bundle and helpers
it reuses). Hermetic: 127.0.0.1, scratch HOME / data dir, `--no-tray`, `--model-host process`, the
text model is `model_host_fakes.FakeOpenAIServer`.

Incident 2026-11-02: the same polite stop failed the call with 424 at stop + 30 s, marked the run
failed, used an automation retry and sent an `automation_failed` email. What it proves now:
1. after the stop the run is still RUNNING on disk, its ledger has no FAILED record, and no incident
   file was written;
2. the next start sends the model call again and the run completes: the call's records are
   started, started, completed, all attempt 1 (no retry consumed), and nothing failed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from model_host_fakes import FakeOpenAIServer
from test_model_host_kill_e2e import TOKEN, _bundle, _free_port, _wait

pytestmark = pytest.mark.skipif(os.environ.get("ABSTRACT_E2E_MODEL_HOST") != "1",
                                reason="opt-in: ABSTRACT_E2E_MODEL_HOST=1 boots a real gateway")


def test_sigterm_mid_call_then_a_new_start_continues_the_run_with_no_failure(tmp_path: Path) -> None:
    home, data, bundles = tmp_path / "home", tmp_path / "data", tmp_path / "bundles"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    bundle_id, flow_id = _bundle(bundles)
    port = _free_port()
    with FakeOpenAIServer(delay_s=0.0, chunks=4) as fake:
        gateway_src = str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)
        env = {
            "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # The caller's PYTHONPATH too: the AbstractRuntime that has EffectInterrupted.
            "PYTHONPATH": os.pathsep.join([gateway_src] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
            "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
            "OPENAI_BASE_URL": fake.base_url, "OPENAI_API_KEY": "fake-key",
            "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN, "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles),
            "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_POLL_S": "0.05",
            "ABSTRACTGATEWAY_STORE_BACKEND": "file",
        }
        argv = [sys.executable, "-m", "abstractgateway", "serve", "--model-host", "process", "--data-dir", str(data),
                "--host", "127.0.0.1", "--port", str(port), "--no-tray"]
        headers = {"Authorization": f"Bearer {TOKEN}"}
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0, trust_env=False)

        def _boot(log: Path) -> subprocess.Popen:
            gw = subprocess.Popen(argv, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))

            def _health():
                try:
                    body = client.get("/api/health", timeout=2.0).json()
                except (httpx.HTTPError, ValueError):
                    return None
                return body if body.get("status") != "starting" and (body.get("model_host") or {}).get("state") == "up" else None

            _wait(_health, timeout_s=120, what="the gateway's health", proc=gw, log=log)
            return gw

        def _run(run_id: str) -> dict:
            r = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert r.status_code == 200, r.text
            return r.json()

        log1, log2 = tmp_path / "gateway-1.log", tmp_path / "gateway-2.log"
        gw = _boot(log1)
        try:
            fake.delay_s = 10.0  # the fake answers after 40 s: longer than the 30 s runner drain
            asked = len(fake.requests)
            r = client.post("/api/gateway/runs/start", headers=headers,
                            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": "victim"}})
            assert r.status_code == 200, r.text
            victim = r.json()["run_id"]
            _wait(lambda: len(fake.requests) > asked, timeout_s=30, what="the victim's request at the model", proc=gw, log=log1)

            # 1. A polite stop while the model call is in flight.
            gw.send_signal(signal.SIGTERM)
            code = gw.wait(timeout=120)
            stop_log = log1.read_text()
            assert code == 0, stop_log[-4000:]
            [run_file] = [p for p in data.rglob(f"run_{victim}.json")]
            on_disk = json.loads(run_file.read_text())
            assert on_disk["status"] == "running" and not on_disk.get("error"), on_disk
            assert not list((data / "incidents").glob("model-host-*.json")) if (data / "incidents").exists() else True
            assert "shutdown: runner drain TIMED OUT" in stop_log and "1 run(s) interrupted" in stop_log, stop_log[-4000:]
            assert "stopped while answering" not in stop_log, stop_log[-4000:]

            # 2. A new start: the call is sent again and the run completes.
            fake.delay_s = 0.0
            asked = len(fake.requests)
            gw = _boot(log2)
            done = _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed") else None)(_run(victim)),
                         timeout_s=90, what="the victim after the restart", proc=gw, log=log2)
            assert done["status"] == "completed", done
            assert len(fake.requests) > asked, "the interrupted call was not sent again"
            ledger = client.get(f"/api/gateway/runs/{victim}/ledger", headers=headers, params={"limit": 5000}).json()["items"]
            assert not any(rec.get("status") == "failed" for rec in ledger), [r.get("status") for r in ledger]
            llm = [rec for rec in ledger if (rec.get("effect") or {}).get("type") == "llm_call"]
            assert [rec["status"] for rec in llm] == ["started", "started", "completed"], [rec["status"] for rec in llm]
            assert {rec.get("attempt") for rec in llm} == {1}, "the interruption consumed a retry attempt"
            assert not (data / "incidents").exists() or not list((data / "incidents").glob("model-host-*.json"))
            print(f"\nHOST STOP TEST: SIGTERM mid-call -> exit {code}; run {victim} running on disk; "
                  f"after the restart completed with llm records {[rec['status'] for rec in llm]}")
        finally:
            client.close()
            if gw.poll() is None:
                gw.terminate()
                try:
                    gw.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    gw.kill()
