"""Split mode, booted for real: `serve --no-runner` + `abstractgateway runner` on one data dir.

Hermetic: 127.0.0.1, scratch ports (never the configured one), scratch HOME / HF_HOME / data dir,
`--no-tray`, local model servers pointed at a closed port. The API process starts a trivial run
(start -> wait 0.5 s -> end); the runner process ticks it to completion. `/api/health` names the
split (`mode: split`, `process: api`, the runner process alive, its model host up), `/v1`
answers through the runner's model host, and only the runner process builds the default model.

"Builds a model" is observed provider-agnostically: a `sitecustomize` the test puts on both processes'
PYTHONPATH records every AbstractCore provider construction (`ProviderRegistry.create_provider_instance`)
with its pid. The bundle carries a second flow (never run) with an LLM node pinned to Ollama on a
closed loopback port, so both processes build an LLM runtime with a default text model on every
platform: AbstractCore's host recommendation where one applies (MLX on an Apple install with MLX,
LM Studio without), else that pin. Without an LLM flow neither process builds one and the check
would be vacuous; asserting on the MLX provider's construction warning only held on Apple silicon
with MLX installed and an LLM bundle present (AbstractNote).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

TOKEN = "split-smoke-token-0123456789abcdef"
ENDPOINT_TOKEN = "split-smoke-endpoint-0123456789"


# Recorded in each gateway process (the test's own harness, not a product knob): every AbstractCore
# provider construction, one JSON line {pid, provider, model}.
_PROVIDER_PROBE = """
import importlib.util, json, os, sys

_LOG = os.environ.get("SPLIT_SMOKE_PROVIDER_LOG")


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


def _provider_builds(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _bundle(bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": "root", "name": "split-smoke", "description": "", "interfaces": [], "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0.0, "y": 0.0},
             "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "wait", "type": "wait_until", "position": {"x": 220.0, "y": 0.0},
             "data": {"nodeType": "wait_until", "label": "Wait Until",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "duration", "label": "duration", "type": "number"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "result", "label": "result", "type": "string"}],
                      "pinDefaults": {"duration": 0.5}, "effectConfig": {"durationType": "seconds"}}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 440.0, "y": 0.0},
             "data": {"nodeType": "on_flow_end", "label": "On Flow End",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "wait", "targetHandle": "exec-in"},
            {"id": "e2", "source": "wait", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
    }
    # Never run: its LLM node makes the gateway build an LLM runtime whose default text model is this
    # pin (Ollama on the closed port OLLAMA_BASE_URL names) unless a configured default outranks it.
    ask = {
        "id": "ask", "name": "split-smoke-ask", "description": "", "interfaces": [], "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0.0, "y": 0.0},
             "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "llm", "type": "llm_call", "position": {"x": 220.0, "y": 0.0},
             "data": {"nodeType": "llm_call", "label": "LLM Call",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "prompt", "label": "prompt", "type": "string"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "response", "label": "response", "type": "string"}],
                      "pinDefaults": {"prompt": "hello"},
                      "effectConfig": {"provider": "ollama", "model": "split-smoke-model"}}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 440.0, "y": 0.0},
             "data": {"nodeType": "on_flow_end", "label": "On Flow End",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "llm", "targetHandle": "exec-in"},
            {"id": "e2", "source": "llm", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
    }
    manifest = {"bundle_format_version": "1", "bundle_id": "split-smoke", "bundle_version": "0.0.0",
                "created_at": "2026-10-26T00:00:00+00:00",
                "entrypoints": [{"flow_id": "root", "name": "smoke", "description": "", "interfaces": []}],
                "flows": {"root": "flows/root.json", "ask": "flows/ask.json"},
                "artifacts": {}, "assets": {}, "metadata": {}}
    with zipfile.ZipFile(bundles_dir / "split-smoke.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))
        zf.writestr("flows/ask.json", json.dumps(ask))
    return "split-smoke", "root"


def _wait(predicate, *, timeout_s: float, what: str, procs=()) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        for name, proc, log_path in procs:
            if proc.poll() is not None:
                raise AssertionError(f"{name} exited early ({proc.returncode}):\n{log_path.read_text()[-4000:]}")
        if predicate():
            return
        time.sleep(0.1)
    details = "\n".join(f"--- {n}\n{p.read_text()[-3000:]}" for n, _proc, p in procs)
    raise AssertionError(f"timeout waiting for {what}\n{details}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_split_mode_boots_runs_and_reports_itself(tmp_path: Path) -> None:
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
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(
            [str(probe_dir), str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]
            + [str(Path(x).resolve()) for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]
        ),
        "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        # The test harness's own knobs (the operator's two commands need none of these).
        "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN, "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_POLL_S": "0.05",
        "ABSTRACTGATEWAY_STORE_BACKEND": "file",
        "SPLIT_SMOKE_PROVIDER_LOG": str(provider_log),
    }
    runner_log, api_log = tmp_path / "runner.log", tmp_path / "api.log"
    # The two commands of split mode, exactly as docs/configuration.md gives them.
    runner = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "runner", "--data-dir", str(data), "--model-host", "in-process"],
        env=env, stdout=runner_log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))
    api = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--no-runner", "--data-dir", str(data),
         "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
        env=env, stdout=api_log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))
    procs = (("runner", runner, runner_log), ("api", api, api_log))
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0, trust_env=False)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        _wait(lambda: (data / "split" / "model_host.json").exists(), timeout_s=90, what="the runner's model host",
              procs=procs)
        # The runner built its runtime (and warmed its default text model) before starting the model host.
        runner_builds = [b for b in _provider_builds(provider_log) if b["pid"] == runner.pid]
        assert runner_builds, f"the runner process built no default model: {_provider_builds(provider_log)}"

        def _ready() -> bool:
            try:
                body = client.get("/api/health").json()
            except Exception:  # noqa: BLE001 - not listening yet
                return False
            rows = (body.get("runner") or {}).get("runners") or []
            return body.get("status") != "starting" and any(
                (r.get("runner_process") or {}).get("alive") for r in rows)

        _wait(_ready, timeout_s=120, what="the API process's health", procs=procs)
        health = client.get("/api/health").json()
        row = health["runner"]["runners"][0]
        assert row["mode"] == "split" and row["process"] == "api" and row["status"] == "disabled", row
        rp = row["runner_process"]
        assert rp["alive"] is True and rp["model_host"] == "up" and rp["pid"] == runner.pid, rp
        assert "secret" not in json.dumps(health) and "model_host.json" not in json.dumps(health)

        started = client.post("/api/gateway/runs/start", headers=headers,
                              json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}})
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        def _completed() -> bool:
            r = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("status") not in ("failed", "cancelled"), body
            return body.get("status") == "completed"

        _wait(_completed, timeout_s=60, what="the run to complete (ticked by the runner process)", procs=procs)

        # /v1 through the API process lands on the runner's model host.
        models = client.get("/v1/models", headers={"Authorization": f"Bearer {ENDPOINT_TOKEN}"}, timeout=60.0)
        assert models.status_code == 200, models.text
        assert models.json().get("object") == "list"
        assert "the OpenAI API's model host is on http://127.0.0.1:" in runner_log.read_text()
        # The API process built its runtime (it serves the run above) without its default model, and
        # /v1 built none there either: the model is the runner's alone.
        api_builds = [b for b in _provider_builds(provider_log) if b["pid"] == api.pid]
        assert api_builds == [], f"the API process built a model: {api_builds}"
    finally:
        client.close()
        _stop(api)
        _stop(runner)
    assert not (data / "split" / "model_host.json").exists(), "the runner removes its address when it stops"
