"""`POST /api/gateway/models/delete-download`: the Models page's "Delete download".

End to end through the REAL seam (core_config -> Runtime config_facade ->
AbstractCore `delete_artifact`) on FAKE artifacts only: a Hugging Face cache
laid out in tmp_path exactly as huggingface_hub writes it, and an HTTP Ollama
on a free port. HOME, every model store, the AbstractCore config dir and PATH
point into tmp_path, so the real caches, the real `ollama` and `lms` are never
touched, and nothing is ever loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

TOKEN = "admin-token-for-delete-download-tests"
URL = "/api/gateway/models/delete-download"


def make_hf_repo(cache: Path, repo_id: str, files: Dict[str, bytes], revision: str = "a" * 40) -> Path:
    folder = cache / ("models--" + repo_id.replace("/", "--"))
    blobs = folder / "blobs"
    snap = folder / "snapshots" / revision
    blobs.mkdir(parents=True, exist_ok=True)
    snap.mkdir(parents=True, exist_ok=True)
    (folder / "refs").mkdir(exist_ok=True)
    (folder / "refs" / "main").write_text(revision)
    for name, data in files.items():
        blob = blobs / hashlib.sha256(data).hexdigest()
        blob.write_bytes(data)
        target = snap / name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(blob, target.parent), target)
    return folder


class FakeOllama:
    """`/api/tags`, `/api/ps`, `DELETE /api/delete` with Ollama's field names."""

    def __init__(self, models: List[Dict[str, Any]], loaded: List[str]):
        self.models = models
        self.loaded = loaded
        self.deletes: List[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, code: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/api/tags":
                    return self._json(200, {"models": fake.models})
                if self.path == "/api/ps":
                    return self._json(200, {"models": [m for m in fake.models if m["name"] in fake.loaded]})
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.20.2"})
                return self._json(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                name = body.get("model") or body.get("name")
                fake.deletes.append(name)
                before = len(fake.models)
                fake.models = [m for m in fake.models if m["name"] != name]
                if len(fake.models) == before:
                    return self._json(404, {"error": f"model '{name}' not found"})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def ollama_tag(name: str, size: int) -> Dict[str, Any]:
    return {
        "name": name, "model": name, "modified_at": "2026-09-01T10:00:00+02:00", "size": size,
        "digest": hashlib.sha256(name.encode()).hexdigest(),
        "details": {"format": "gguf", "family": "qwen3", "families": ["qwen3"], "parameter_size": "8B", "quantization_level": "Q4_K_M"},
    }


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    hf = home / ".cache" / "huggingface" / "hub"
    hf.mkdir(parents=True)
    (home / ".lmstudio" / "models").mkdir(parents=True)
    (home / ".ollama" / "models").mkdir(parents=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "DIFFUSERS_CACHE", "HF_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HF_HUB_CACHE", str(hf))
    monkeypatch.setenv("LMSTUDIO_MODELS_DIR", str(home / ".lmstudio" / "models"))
    monkeypatch.setenv("OLLAMA_MODELS", str(home / ".ollama" / "models"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("ABSTRACTCORE_LMS_CLI", str(fakebin / "lms-not-installed"))
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_DIR", str(tmp_path / "coreconfig"))
    monkeypatch.setenv("ABSTRACTCORE_JOBS_DIR", str(tmp_path / "coreconfig" / "jobs"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(fakebin), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    flows = tmp_path / "flows"
    flows.mkdir()
    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractcore.config import engines, host_jobs

    engines._reset_caches_for_tests()
    host_jobs.set_default_registry(host_jobs.HostJobRegistry(persist_dir=tmp_path / "coreconfig" / "jobs"))

    from abstractgateway.routes import model_download_delete as route

    resident: List[Dict[str, Any]] = []
    monkeypatch.setattr(route, "_resident_rows", lambda: list(resident))
    from abstractgateway.app import app

    client = TestClient(app)
    with client:
        yield {"client": client, "h": {"Authorization": f"Bearer {TOKEN}"}, "hf": hf, "data": data, "resident": resident}


def _audit(data: Path) -> List[Dict[str, Any]]:
    path = data / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events(data: Path, name: str) -> List[Dict[str, Any]]:
    return [e for e in _audit(data) if e.get("event") == name]


def test_mlx_download_is_measured_then_deleted_from_the_cache_and_audited(gw) -> None:
    folder = make_hf_repo(gw["hf"], "mlx-community/Qwen3-8B-4bit", {"model.safetensors": b"m" * 351, "config.json": b"{}"})
    c, h = gw["client"], gw["h"]

    plan = c.post(URL, headers=h, json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit", "dry_run": True})
    assert plan.status_code == 200, plan.text
    assert plan.json()["schema"] == "model_download_delete_v1"
    assert plan.json()["status"] == "planned" and plan.json()["freed_bytes"] == 353
    assert folder.exists()
    assert not _events(gw["data"], "model.download_deleted")

    done = c.post(URL, headers=h, json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "deleted" and body["presence"] == "absent" and body["freed_bytes"] == 353
    assert not folder.exists()
    events = _events(gw["data"], "model.download_deleted")
    assert len(events) == 1
    assert events[0]["provider"] == "mlx" and events[0]["artifact"] == "mlx-community/Qwen3-8B-4bit" and events[0]["freed_bytes"] == 353

    again = c.post(URL, headers=h, json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit"})
    assert again.status_code == 404 and again.json()["reason"] == "not_downloaded"


def test_a_resident_model_is_refused_with_unload_it_first_and_nothing_is_deleted(gw) -> None:
    folder = make_hf_repo(gw["hf"], "mlx-community/Qwen3-8B-4bit", {"model.safetensors": b"m" * 50})
    gw["resident"].append({"provider": "mlx", "model": "mlx-community/Qwen3-8B-4bit", "resident": True, "locked": False})
    got = gw["client"].post(URL, headers=gw["h"], json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit"})
    assert got.status_code == 409, got.text
    assert got.json()["reason"] == "resident"
    assert "Unload it first" in got.json()["fix"]
    assert folder.exists()
    refused = _events(gw["data"], "model.download_delete_refused")
    assert refused and refused[-1]["reason"] == "resident"

    gw["resident"][0]["locked"] = True
    locked = gw["client"].post(URL, headers=gw["h"], json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit", "dry_run": True})
    assert locked.status_code == 409 and locked.json()["reason"] == "locked"
    assert "Unlock and unload it first" in locked.json()["fix"]
    assert folder.exists()


def test_a_running_download_is_refused(gw, monkeypatch) -> None:
    folder = make_hf_repo(gw["hf"], "mlx-community/Qwen3-8B-4bit", {"model.safetensors": b"m" * 50})
    from abstractgateway import model_downloads

    monkeypatch.setattr(model_downloads, "active_job_for", lambda p, a: {"job": "dl_1", "status": "running"})
    got = gw["client"].post(URL, headers=gw["h"], json={"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit"})
    assert got.status_code == 409 and got.json()["reason"] == "downloading"
    assert folder.exists()


def test_one_gguf_quant_goes_and_its_sibling_quant_stays(gw) -> None:
    folder = make_hf_repo(gw["hf"], "unsloth/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": b"q" * 300, "Qwen3-8B-Q8_0.gguf": b"e" * 500})
    got = gw["client"].post(URL, headers=gw["h"], json={"provider": "huggingface", "artifact": "unsloth/Qwen3-8B-GGUF:Q4_K_M"})
    assert got.status_code == 200, got.text
    assert got.json()["freed_bytes"] == 300
    snap = folder / "snapshots" / ("a" * 40)
    assert not (snap / "Qwen3-8B-Q4_K_M.gguf").exists()
    assert (snap / "Qwen3-8B-Q8_0.gguf").read_bytes() == b"e" * 500


def test_ollama_uses_its_own_delete_and_a_loaded_model_is_refused(gw, monkeypatch) -> None:
    fake = FakeOllama([ollama_tag("qwen3:8b", 1000), ollama_tag("gemma3:1b", 10)], loaded=["qwen3:8b"])
    try:
        monkeypatch.setenv("OLLAMA_BASE_URL", fake.url)
        c, h = gw["client"], gw["h"]
        refused = c.post(URL, headers=h, json={"provider": "ollama", "artifact": "qwen3:8b"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["reason"] == "resident" and "Unload it first" in refused.json()["fix"]
        assert fake.deletes == []

        done = c.post(URL, headers=h, json={"provider": "ollama", "artifact": "gemma3:1b"})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "deleted" and done.json()["freed_bytes"] == 10
        assert fake.deletes == ["gemma3:1b"]
    finally:
        fake.close()


def test_lmstudio_is_managed_in_lm_studio(gw) -> None:
    got = gw["client"].post(URL, headers=gw["h"], json={"provider": "lmstudio", "artifact": "qwen/qwen3-8b"})
    assert got.status_code == 409 and got.json()["reason"] == "managed_elsewhere"
    assert "LM Studio" in got.json()["fix"]


def test_delete_download_is_admin_only() -> None:
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    rows = [p for p in GATEWAY_ROUTE_POLICIES if URL in (getattr(p, "exact", None) or ())]
    assert rows and rows[0].reason_code == "admin_required"
