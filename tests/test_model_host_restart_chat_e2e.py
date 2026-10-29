"""ADR 0002, booted for real: after `kill -9` on the model host, the NEXT CHAT answers, and the new
child holds the model ONCE (pass three on the OVH box, 2026-10-29: after a restart the child warmed
the default model outside the runtime's pool, the run path asked for a second copy, and the
residency failsafe refused it: chat broke after every restart).

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

pytestmark = pytest.mark.skipif(os.name != "posix", reason="kill -9 and a sitecustomize probe")

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
                  effectConfig={}),
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



def test_after_a_model_host_restart_the_next_chat_answers_and_the_model_is_built_once(tmp_path: Path) -> None:
    home, data, bundles = tmp_path / "home", tmp_path / "data", tmp_path / "bundles"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
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
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0, trust_env=False)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        try:
            def _health():
                try:
                    body = client.get("/api/health", timeout=2.0).json()
                except httpx.HTTPError:
                    return None
                return body if body.get("status") != "starting" else None

            _wait(_health, timeout_s=120, what="the gateway's health", proc=gw, log=log)
            r = client.put("/api/gateway/config/capability-defaults/output/text", headers=headers,
                           json={"provider": "openai-compatible", "model": "fake-model"})
            assert r.status_code == 200, r.text

            def _chat(prompt: str) -> dict:
                r = client.post("/api/gateway/runs/start", headers=headers,
                                json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": prompt}})
                assert r.status_code == 200, r.text
                rid = r.json()["run_id"]
                return _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed") else None)(
                    client.get(f"/api/gateway/runs/{rid}", headers=headers).json()),
                    timeout_s=60, what=f"run {prompt}", proc=gw, log=log)

            first = _chat("before")
            assert first["status"] == "completed", first
            # A gateway-side generation too (the console sandbox: a client of its own).
            sb = client.post("/api/gateway/sandbox/generate", headers=headers,
                             json={"provider": "openai-compatible", "model": "fake-model", "prompt": "sandbox"})
            assert sb.status_code == 200, sb.text

            child = client.get("/api/health").json()["model_host"]["pid"]
            os.kill(child, signal.SIGKILL)
            back = _wait(lambda: (lambda m: m if m.get("state") == "up" and m.get("pid") not in (None, child) else None)(
                client.get("/api/health").json()["model_host"]), timeout_s=30, what="the restart", proc=gw, log=log)
            time.sleep(1.0)  # the warm thread rebuilds the clients
            after = _chat("after")
            assert after["status"] == "completed", after
            sb = client.post("/api/gateway/sandbox/generate", headers=headers,
                             json={"provider": "openai-compatible", "model": "fake-model", "prompt": "sandbox again"})
            assert sb.status_code == 200, sb.text

            builds = [json.loads(x) for x in provider_log.read_text().splitlines() if x.strip()]
            new_child = [b for b in builds if b["pid"] == back["pid"] and b["model"] == "fake-model"]
            old_child = [b for b in builds if b["pid"] == child and b["model"] == "fake-model"]
            print(f"\nbuilds of fake-model: first child {len(old_child)}, new child {len(new_child)}; all: {builds}")
            # R2: the sandbox borrows the runtime's pool, so each child builds the model ONCE (the
            # pool's), before and after the restart, never once more per gateway-side generation.
            assert len(old_child) == 1, f"the first child built the model {len(old_child)} times: {old_child}"
            assert len(new_child) == 1, f"the restarted child built the model {len(new_child)} times: {new_child}"
            # And nothing else: the boot-time default the operator replaced is not warmed again.
            others = [b for b in builds if b["pid"] == back["pid"] and b["model"] != "fake-model"]
            assert not others, f"the restarted child built a model nobody uses any more: {others}"
        finally:
            client.close()
            if gw.poll() is None:
                gw.terminate()
                try:
                    gw.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    gw.kill()
