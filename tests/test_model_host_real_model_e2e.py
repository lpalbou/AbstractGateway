"""ADR 0002 with a REAL local model: ONE weights instance in the model host child, before and after a
`kill -9` restart and after the next chats, and N concurrent requests completing on it.

Opt-in: `ABSTRACT_E2E_MODEL_HOST_REAL_MODEL=<provider>:<model>` (a model already in the local cache;
the test is offline), for example `mlx:mlx-community/Qwen1.5-0.5B-Chat-4bit` on a Mac or
`huggingface:<gguf repo>` on a GPU server. Counts every provider instance the child builds for that
model (the construction probe) and reads `GET /api/gateway/models/loaded`; prints the numbers.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from test_model_host_restart_chat_e2e import TOKEN, _PROVIDER_PROBE, _bundle, _free_port, _wait

SPEC = os.environ.get("ABSTRACT_E2E_MODEL_HOST_REAL_MODEL", "")
pytestmark = pytest.mark.skipif(":" not in SPEC or os.name != "posix",
                                reason="opt-in: ABSTRACT_E2E_MODEL_HOST_REAL_MODEL=<provider>:<model>")
CONCURRENT = int(os.environ.get("ABSTRACT_E2E_MODEL_HOST_CONCURRENT", "4"))


def _rss(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) * 1024 if out else 0


def test_one_weights_instance_across_a_restart_and_concurrent_requests(tmp_path: Path) -> None:
    provider, model = SPEC.split(":", 1)
    home, data, bundles = tmp_path / "home", tmp_path / "data", tmp_path / "bundles"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    bundle_id, flow_id = _bundle(bundles)
    port = _free_port()
    probe_dir, provider_log = tmp_path / "probe", tmp_path / "provider_builds.jsonl"
    probe_dir.mkdir()
    (probe_dir / "sitecustomize.py").write_text(_PROVIDER_PROBE, encoding="utf-8")
    # The passwd entry, not ~: the suite's conftest points HOME at a scratch dir.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    # The weights from the real cache (read-only use; offline), everything else in the scratch home:
    # the capability-default write below must never touch the operator's own Core store.
    (home / ".cache").mkdir(parents=True)
    (home / ".cache" / "huggingface").symlink_to(real_home / ".cache" / "huggingface")
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(probe_dir), str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]),
        "LANG": "en_US.UTF-8",
        "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN, "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_POLL_S": "0.05",
        "ABSTRACTGATEWAY_STORE_BACKEND": "file", "ABSTRACTGATEWAY_EMBEDDING_PROVIDER": "disabled",
        "MODEL_HOST_E2E_PROVIDER_LOG": str(provider_log),
    }
    log = tmp_path / "gateway.log"
    gw = subprocess.Popen([sys.executable, "-m", "abstractgateway", "serve", "--model-host", "process", "--data-dir", str(data),
                           "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
                          env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=600.0, trust_env=False)
    h = {"Authorization": f"Bearer {TOKEN}"}
    report = []
    try:
        def _healthy():
            try:
                body = c.get("/api/health", timeout=2).json()
            except httpx.HTTPError:
                return None
            return body if body.get("status") != "starting" else None

        _wait(_healthy, timeout_s=180, what="health", proc=gw, log=log)
        r = c.put("/api/gateway/config/capability-defaults/output/text", headers=h, json={"provider": provider, "model": model})
        assert r.status_code == 200, r.text

        def run_chat(prompt: str) -> dict:
            r = c.post("/api/gateway/runs/start", headers=h, json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": prompt}})
            assert r.status_code == 200, r.text
            rid = r.json()["run_id"]
            return _wait(lambda: (lambda b: b if b.get("status") in ("completed", "failed") else None)(
                c.get(f"/api/gateway/runs/{rid}", headers=h).json()), timeout_s=600, what=prompt, proc=gw, log=log)

        def sandbox(prompt: str) -> httpx.Response:
            return c.post("/api/gateway/sandbox/generate", headers=h, json={"provider": provider, "model": model, "prompt": prompt, "max_tokens": 16})

        def builds(pid: int) -> list:
            rows = [json.loads(x) for x in provider_log.read_text().splitlines() if x.strip()] if provider_log.exists() else []
            return [b for b in rows if b["pid"] == pid and b["model"] == model]

        def loaded() -> list:
            body = c.get("/api/gateway/models/loaded", headers=h).json()
            return [row for row in (body.get("rows") or []) if model in json.dumps(row)]

        def phase(name: str) -> None:
            mh = c.get("/api/health").json()["model_host"]
            rows = loaded()
            report.append(f"{name}: child {mh['pid']} builds of the model {len(builds(mh['pid']))}, "
                          f"loaded rows {len(rows)} {[{k: (row.get('details') or {}).get(k) for k in ('process_holders', 'est_weights_bytes', 'held_bytes')} for row in rows]}, "
                          f"child RSS {_rss(mh['pid']) / 1e9:.2f} GB")
            assert len(builds(mh["pid"])) == 1, f"{name}: {len(builds(mh['pid']))} instances of {model} in the child: {builds(mh['pid'])}"
            assert len(rows) <= 1, f"{name}: {rows}"
            holders = [(row.get("details") or {}).get("process_holders") for row in rows]
            assert all(n in (None, 1) for n in holders), f"{name}: weights holders in the child {holders}"

        t = time.monotonic()
        warm = sandbox("warm")
        assert warm.status_code == 200, warm.text[:600]
        first = run_chat("before")
        child_log = data / "logs" / "model-host.log"
        assert first["status"] == "completed", (json.dumps(first)[:800] + "\n--- model host log:\n"
                                                + (child_log.read_text()[-4000:] if child_log.exists() else ""))
        assert sandbox("sandbox").status_code == 200
        report.append(f"first run + sandbox: {time.monotonic() - t:.2f} s")
        t = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(CONCURRENT) as ex:
            codes = list(ex.map(lambda i: sandbox(f"concurrent {i}").status_code, range(CONCURRENT)))
        report.append(f"{CONCURRENT} concurrent requests: {codes} in {time.monotonic() - t:.2f} s")
        assert codes == [200] * CONCURRENT
        phase("before the restart")

        child = c.get("/api/health").json()["model_host"]["pid"]
        os.kill(child, signal.SIGKILL)
        _wait(lambda: (lambda m: m if m.get("state") == "up" and m.get("pid") not in (None, child) else None)(
            c.get("/api/health").json()["model_host"]), timeout_s=60, what="restart", proc=gw, log=log)
        time.sleep(2.0)
        phase("after the restart")
        t = time.monotonic()
        assert run_chat("after")["status"] == "completed"
        assert sandbox("sandbox again").status_code == 200
        with concurrent.futures.ThreadPoolExecutor(CONCURRENT) as ex:
            codes = list(ex.map(lambda i: sandbox(f"concurrent again {i}").status_code, range(CONCURRENT)))
        assert codes == [200] * CONCURRENT
        report.append(f"after the restart, run + sandbox + {CONCURRENT} concurrent: {time.monotonic() - t:.2f} s")
        phase("after the next chats")
    finally:
        print("\nREAL MODEL E2E (" + SPEC + "):\n  " + "\n  ".join(report))
        c.close()
        if gw.poll() is None:
            gw.terminate()
            try:
                gw.wait(timeout=30)
            except subprocess.TimeoutExpired:
                gw.kill()
