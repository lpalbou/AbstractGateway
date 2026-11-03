"""ADR 0002, booted for real: `serve --model-host process`, a chat, and `kill -9` on the model host.

Opt-in (`ABSTRACT_E2E_MODEL_HOST=1`; the CI matrix runs the unit tests of the same code). Hermetic:
127.0.0.1, scratch HOME / data dir, `--no-tray`, the text model is a fake OpenAI-compatible server in
this test (`model_host_fakes.FakeOpenAIServer`), so the test needs no weights and runs anywhere.

What it proves (the operator's acceptance, numbers printed for the release report):
1. the run's LLM call is answered by the child (the gateway process builds no provider);
2. `kill -9` on the child while the call runs: `/api/health` keeps answering 200 throughout, the run
   fails within 5 s with the sentence naming the kill, and an incident file exists;
3. the child restarts by itself and the next run completes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from model_host_fakes import FakeOpenAIServer

pytestmark = pytest.mark.skipif(os.environ.get("ABSTRACT_E2E_MODEL_HOST") != "1",
                                reason="opt-in: ABSTRACT_E2E_MODEL_HOST=1 boots a real gateway")

TOKEN = "model-host-e2e-token-0123456789abcdef"
ENDPOINT_TOKEN = "model-host-e2e-endpoint-0123456789"

_PROVIDER_PROBE = """
import importlib.util, json, os, sys

_LOG = os.environ.get("MODEL_HOST_E2E_PROVIDER_LOG")


class _RecordProviderBuilds:
    def find_spec(self, name, path=None, target=None):
        if name != "abstractcore.providers.registry":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        exec_module = spec.loader.exec_module

        def _exec(module):
            exec_module(module)
            original = module.ProviderRegistry.create_provider_instance

            def create_provider_instance(self, provider_name, model=None, **kwargs):
                with open(_LOG, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "provider": provider_name, "model": model}) + "\\n")
                return original(self, provider_name, model, **kwargs)

            module.ProviderRegistry.create_provider_instance = create_provider_instance

        spec.loader.exec_module = _exec
        return spec


if _LOG:
    sys.meta_path.insert(0, _RecordProviderBuilds())
"""


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _node(nid, ntype, x, inputs, outputs, **data):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 0.0},
            "data": {"nodeType": ntype, "label": ntype, "inputs": inputs, "outputs": outputs, **data}}


def _bundle(bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    exec_in = {"id": "exec-in", "label": "", "type": "execution"}
    exec_out = {"id": "exec-out", "label": "", "type": "execution"}
    flow = {
        "id": "chat", "name": "model-host-chat", "description": "", "interfaces": [], "entryNode": "start",
        "nodes": [
            _node("start", "on_flow_start", 0.0, [], [exec_out, {"id": "prompt", "label": "prompt", "type": "string"}]),
            _node("llm", "llm_call", 220.0,
                  [exec_in, {"id": "prompt", "label": "prompt", "type": "string"}],
                  [exec_out, {"id": "response", "label": "response", "type": "string"}],
                  pinDefaults={"prompt": "hello"},
                  effectConfig={"provider": "openai-compatible", "model": "fake-model"}),
            _node("end", "on_flow_end", 440.0, [exec_in, {"id": "response", "label": "response", "type": "string"}], []),
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "llm", "targetHandle": "exec-in"},
            {"id": "e2", "source": "start", "sourceHandle": "prompt", "target": "llm", "targetHandle": "prompt"},
            {"id": "e3", "source": "llm", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"id": "e4", "source": "llm", "sourceHandle": "response", "target": "end", "targetHandle": "response"},
        ],
    }
    manifest = {"bundle_format_version": "1", "bundle_id": "model-host-e2e", "bundle_version": "0.0.0",
                "created_at": "2026-10-29T00:00:00+00:00",
                "entrypoints": [{"flow_id": "chat", "name": "chat", "description": "", "interfaces": []}],
                "flows": {"chat": "flows/chat.json"}, "artifacts": {}, "assets": {}, "metadata": {}}
    with zipfile.ZipFile(bundles_dir / "model-host-e2e.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/chat.json", json.dumps(flow))
    return "model-host-e2e", "chat"


def _wait(predicate, *, timeout_s: float, what: str, proc=None, log: Path | None = None):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"the gateway exited early ({proc.returncode}):\n{log.read_text()[-4000:] if log else ''}")
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}\n{log.read_text()[-4000:] if log else ''}")


def test_kill_dash_nine_on_the_model_host_costs_the_request_not_the_gateway(tmp_path: Path) -> None:
    home, data, bundles = tmp_path / "home", tmp_path / "data", tmp_path / "bundles"
    (home / "tmp").mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    (data / "config" / "core_endpoint.json").write_text(json.dumps(
        {"enabled": True, "access": "token", "reach": "machine", "token": ENDPOINT_TOKEN}), encoding="utf-8")
    bundle_id, flow_id = _bundle(bundles)
    port = _free_port()
    probe_dir, provider_log = tmp_path / "probe", tmp_path / "provider_builds.jsonl"
    probe_dir.mkdir()
    (probe_dir / "sitecustomize.py").write_text(_PROVIDER_PROBE, encoding="utf-8")
    with FakeOpenAIServer(delay_s=0.0, chunks=4) as fake:
        env = {
            "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.pathsep.join(
                [str(probe_dir), str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]),
            "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
            "OPENAI_BASE_URL": fake.base_url, "OPENAI_API_KEY": "fake-key",
            "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN, "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles),
            "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_POLL_S": "0.05",
            "ABSTRACTGATEWAY_STORE_BACKEND": "file", "MODEL_HOST_E2E_PROVIDER_LOG": str(provider_log),
        }
        log = tmp_path / "gateway.log"
        gw = subprocess.Popen(
            [sys.executable, "-m", "abstractgateway", "serve", "--model-host", "process", "--data-dir", str(data),
             "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
            env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0, trust_env=False)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        try:
            def _health():
                try:
                    r = client.get("/api/health", timeout=2.0)
                except httpx.HTTPError:
                    return None
                body = r.json()
                return body if body.get("status") != "starting" else None

            health = _wait(_health, timeout_s=120, what="the gateway's health", proc=gw, log=log)
            mh = health["model_host"]
            assert mh["mode"] == "process" and mh["state"] == "up", mh
            child_pid = mh["pid"]
            assert child_pid and child_pid != gw.pid

            def _start(prompt: str) -> str:
                r = client.post("/api/gateway/runs/start", headers=headers,
                                json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": prompt}})
                assert r.status_code == 200, r.text
                return r.json()["run_id"]

            def _run(run_id: str) -> dict:
                r = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
                assert r.status_code == 200, r.text
                return r.json()

            # 1. A chat answered by the child.
            first = _start("first")
            done = _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed") else None)(_run(first)),
                         timeout_s=60, what="the first run", proc=gw, log=log)
            assert done["status"] == "completed", done
            builds = [json.loads(x) for x in provider_log.read_text().splitlines() if x.strip()]
            assert any(b["pid"] == child_pid for b in builds), builds
            assert not any(b["pid"] == gw.pid for b in builds), f"the gateway process built a provider: {builds}"

            # /v1 is Core's app in the child, behind the gateway's admission.
            v1 = client.get("/v1/models", headers={"Authorization": f"Bearer {ENDPOINT_TOKEN}"}, timeout=60.0)
            assert v1.status_code == 200 and v1.json().get("object") == "list", v1.text
            assert client.get("/v1/models", timeout=10.0).status_code == 401

            # 2. kill -9 during a chat.
            fake.delay_s = 2.0  # the fake answers after 8 s
            asked = len(fake.requests)
            victim = _start("victim")
            _wait(lambda: len(fake.requests) > asked, timeout_s=30, what="the victim's request at the model", proc=gw, log=log)
            os.kill(child_pid, signal.SIGKILL)
            killed_at = time.monotonic()
            health_codes = []
            status = None
            while time.monotonic() - killed_at < 5.0:
                health_codes.append(client.get("/api/health", timeout=2.0).status_code)
                status = _run(victim)
                if status.get("status") == "failed":
                    break
                time.sleep(0.05)
            failed_after = time.monotonic() - killed_at
            assert status and status.get("status") == "failed", f"not failed {failed_after:.2f} s after the kill: {status}"
            error_text = json.dumps(status)
            assert "model host" in error_text and "signal 9" in error_text, error_text[:2000]
            assert health_codes and set(health_codes) == {200}, health_codes
            incidents = sorted((data / "incidents").glob("model-host-*.json"))
            assert incidents, "no incident file"
            incident = json.loads(incidents[-1].read_text())
            assert incident["schema"] == "abstractgateway.incident.v1" and incident["signal"] == "SIGKILL", incident

            # 3. The child is back, and the next chat completes.
            fake.delay_s = 0.0
            back = _wait(lambda: (lambda m: m if m.get("state") == "up" and m.get("pid") not in (None, child_pid) else None)(
                client.get("/api/health").json()["model_host"]), timeout_s=30, what="the model host's restart", proc=gw, log=log)
            restarted_after = time.monotonic() - killed_at
            nxt = _start("after")
            done = _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed") else None)(_run(nxt)),
                         timeout_s=60, what="the run after the restart", proc=gw, log=log)
            assert done["status"] == "completed", done
            v1 = client.get("/v1/models", headers={"Authorization": f"Bearer {ENDPOINT_TOKEN}"}, timeout=60.0)
            assert v1.status_code == 200, v1.text
            print(f"\nMODEL HOST KILL TEST: run failed {failed_after:.2f} s after kill -9; health 200 x{len(health_codes)}; "
                  f"child {child_pid} -> {back['pid']} back after {restarted_after:.2f} s; incident {incidents[-1].name}")
        finally:
            client.close()
            if gw.poll() is None:
                gw.terminate()
                try:
                    gw.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    gw.kill()
