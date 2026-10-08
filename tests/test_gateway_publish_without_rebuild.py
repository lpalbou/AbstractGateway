"""Publishing a workflow must not rebuild every service (R16.2, backlog 0846).

Before: every publish/promote/upload called `reload_bundles_from_disk`, which built a
brand-new host — a new runtime, LLM client and provider (for an in-process model: a
second copy of its weights and an EMPTY prompt-cache store) — once per instantiated
service. Measured on the live gateway: publish up to 24 s, promote up to 99.7 s, the
session prompt caches gone after each one, /api/health unanswered meanwhile.

Now a reload compiles the changed bundle files into a new WorkflowRegistry and swaps it
onto the EXISTING runtime. These tests drive a FAKE engine — an LLM client whose
construction stands for "load the model" (it counts, and holds the GIL like a weight
load does) and which keeps a per-instance prompt-cache store keyed by the runtime's
`prompt_cache_key`, reporting `metadata.prompt_cache` exactly where the run ledger
records it. No model is ever loaded.

Pins:
* B1 a publish swaps the registry on the same runtime: no new runtime, no new client;
  only the changed bundle is compiled (spec identity of the others is kept); nothing
  changed = nothing swapped; the first LLM workflow on a plain runtime is the one
  case that rebuilds THIS service (`service_reload`); `full=True` rebuilds on request.
* B2 the next turn after a republish reads the session's prompt cache exactly as the
  same turn without a republish (ledger `metadata.prompt_cache` identical).
* B3 /api/health p99 < 500 ms while 20 publishes run; every publish response carries
  `reload {kind, services, duration_ms, sentence}` and the audit line records it.
* B4 a run in flight keeps the spec it resolved (new version AND overwritten version);
  a run started after the publish gets the new one.
* one service per data directory under user auth (no duplicate global service).
"""

from __future__ import annotations

import json
import statistics
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------- fake engine


class FakeEngine:
    """Process-wide record of what the fake model did (class-level: the factory builds clients)."""

    constructions = 0
    calls: List[Dict[str, Any]] = []
    # Seconds of GIL-holding work per construction ("loading the weights").
    load_cost_s = 0.0

    @classmethod
    def reset(cls, *, load_cost_s: float = 0.0) -> None:
        cls.constructions = 0
        cls.calls = []
        cls.load_cost_s = float(load_cost_s)


def _hold_the_gil_for(seconds: float) -> None:
    # One C-level call (`sum` over a range) never yields the GIL to other threads,
    # which is how a native weight load behaves; a Python loop or sleep would not.
    if seconds <= 0:
        return
    n = 2_000_000
    started = time.perf_counter()
    sum(range(n))
    per = max(time.perf_counter() - started, 1e-6)
    sum(range(int(n * seconds / per)))


class FakeCachingLLMClient:
    """Stands in for MultiLocalAbstractCoreLLMClient + an in-process provider.

    The prompt-cache store lives on the INSTANCE, as it does on an MLX provider: a
    rebuilt client starts empty, which is the defect R16.2 removes.
    """

    def __init__(self, provider: str = "fake", model: str = "fake-model", **kwargs: Any) -> None:
        type(self).__name__  # keep the signature permissive like the real pool
        FakeEngine.constructions += 1
        _hold_the_gil_for(FakeEngine.load_cost_s)
        self._provider = provider or "fake"
        self._model = model or "fake-model"
        self._default_provider = self._provider
        self._default_model = self._model
        self._cache: Dict[str, int] = {}
        self.instance_no = FakeEngine.constructions

    def generate(self, *, prompt: str = "", messages: Any = None, system_prompt: Any = None, tools: Any = None, media: Any = None, params: Optional[Dict[str, Any]] = None, **_: Any) -> Dict[str, Any]:
        params = dict(params or {})
        key = params.get("prompt_cache_key")
        fed = 100 + len(str(prompt or ""))
        if not key:
            pc = {"mode": "none", "key": None, "outcome": "disabled", "cached_tokens": 0, "fed_tokens": fed}
        elif key in self._cache:
            pc = {"mode": "kv", "key": key, "outcome": "hit", "cached_tokens": self._cache[key], "fed_tokens": fed}
        else:
            pc = {"mode": "kv", "key": key, "outcome": "miss_created", "cached_tokens": 0, "fed_tokens": fed}
        if key:
            self._cache[key] = fed
        # The runtime prefixes a <runtime_metadata> line (wall clock); keep the node's own text.
        text = str(prompt or "").rsplit("\n", 1)[-1]
        FakeEngine.calls.append({"prompt": text, "prompt_cache": dict(pc), "instance": self.instance_no})
        return {
            "content": f"echo:{prompt}",
            "tool_calls": None,
            "usage": {"prompt_tokens": fed, "completion_tokens": 2, "total_tokens": fed + 2},
            "model": self._model,
            "finish_reason": "stop",
            "metadata": {"prompt_cache": pc},
        }


# ---------------------------------------------------------------- bundles


def _llm_flow(flow_id: str, *, prompt: str) -> Dict[str, Any]:
    return {
        "id": flow_id,
        "name": flow_id,
        "description": "",
        "interfaces": [],
        "nodes": [
            {"id": "node-1", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "node-2", "type": "llm_call", "position": {"x": 200, "y": 0},
             "data": {"nodeType": "llm_call", "label": "LLM Call",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "prompt", "label": "prompt", "type": "string"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "response", "label": "response", "type": "string"}],
                      "pinDefaults": {"prompt": prompt},
                      "effectConfig": {"provider": "fake", "model": "fake-model"}}},
            {"id": "node-3", "type": "on_flow_end", "position": {"x": 400, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "End",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }


def _plain_flow(flow_id: str) -> Dict[str, Any]:
    return {
        "id": flow_id,
        "name": flow_id,
        "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "end", "type": "on_flow_end", "data": {"inputs": [{"id": "exec-in", "label": "", "type": "execution"}]}},
        ],
        "edges": [{"id": "e", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
    }


def _bundle_bytes(*, bundle_id: str, version: str, flow: Dict[str, Any]) -> bytes:
    import io

    flow_id = str(flow["id"])
    manifest = {
        "bundle_format_version": "1", "bundle_id": bundle_id, "bundle_version": version,
        "created_at": "2026-10-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return buf.getvalue()


def _write_bundle(dir_: Path, *, bundle_id: str, version: str, flow: Dict[str, Any]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{bundle_id}@{version}.flow"
    path.write_bytes(_bundle_bytes(bundle_id=bundle_id, version=version, flow=flow))
    return path


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    monkeypatch.setattr(ac_factory, "MultiLocalAbstractCoreLLMClient", FakeCachingLLMClient)
    monkeypatch.delenv("ABSTRACTGATEWAY_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("ABSTRACTRUNTIME_PROMPT_CACHE", raising=False)
    FakeEngine.reset()
    return FakeEngine


def _host(bundles: Path, data: Path):
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles,
        data_dir=data,
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )


def _tick_to_end(host: Any, run_id: str, *, max_rounds: int = 20) -> Any:
    for _ in range(max_rounds):
        runtime, spec = host.runtime_and_workflow_for_run(run_id)
        state = runtime.tick(workflow=spec, run_id=run_id, max_steps=50)
        status = str(getattr(getattr(state, "status", None), "value", getattr(state, "status", "")))
        if status in {"completed", "failed", "cancelled"}:
            return state
    raise AssertionError(f"run {run_id} did not finish")


def _ledger_prompt_cache(host: Any, run_id: str) -> List[Dict[str, Any]]:
    """`result.metadata.prompt_cache` of every llm_call in the run ledger (the memory's method)."""
    out: List[Dict[str, Any]] = []
    for rec in host.ledger_store.list(run_id):
        rec = dict(rec) if isinstance(rec, dict) else rec.__dict__
        eff = rec.get("effect") or {}
        status = rec.get("status")
        if str((eff or {}).get("type") or "") != "llm_call" or str(getattr(status, "value", status) or "") != "completed":
            continue
        meta = ((rec.get("result") or {}).get("metadata") or {})
        out.append(dict(meta.get("prompt_cache") or {}))
    return out


def _start(host: Any, bundle_id: str, *, session_id: str, version: Optional[str] = None) -> str:
    ver = version or host.latest_bundle_versions[bundle_id]
    return host.start_run(flow_id="root", bundle_id=f"{bundle_id}@{ver}", input_data={}, session_id=session_id)


# ---------------------------------------------------------------- B1


def test_publish_swaps_the_registry_on_the_same_runtime(tmp_path: Path, fake_engine) -> None:
    bundles = tmp_path / "bundles"
    _write_bundle(bundles, bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="hi"))
    _write_bundle(bundles, bundle_id="other", version="1.0.0", flow=_llm_flow("root", prompt="other"))
    host = _host(bundles, tmp_path / "data")
    runtime0, client0 = host.runtime, host.runtime._abstractcore_llm_client
    assert fake_engine.constructions == 1
    other_spec = host.specs["other@1.0.0:root"]

    _write_bundle(bundles, bundle_id="chat", version="0.0.2", flow=_llm_flow("root", prompt="hi again"))
    out = host.reload_bundles_from_disk()

    assert out["reload"]["kind"] == "registry_swap" and out["reload"]["changed"] is True
    assert host.runtime is runtime0, "a publish built a new runtime"
    assert host.runtime._abstractcore_llm_client is client0, "a publish built a new LLM client"
    assert fake_engine.constructions == 1, "a publish re-initialised the provider (model reload)"
    assert host.runtime.workflow_registry is host.workflow_registry
    assert host.workflow_registry.get("chat@0.0.2:root") is not None
    assert host.latest_bundle_versions["chat"] == "0.0.2"
    # Only the new file compiled: every other spec is the SAME object.
    assert host.specs["other@1.0.0:root"] is other_spec

    again = host.reload_bundles_from_disk()
    assert again["reload"]["changed"] is False, "nothing moved on disk but the reload swapped"


def test_first_llm_workflow_on_a_plain_runtime_rebuilds_only_that_service(tmp_path: Path, fake_engine) -> None:
    bundles = tmp_path / "bundles"
    _write_bundle(bundles, bundle_id="plain", version="1.0.0", flow=_plain_flow("root"))
    host = _host(bundles, tmp_path / "data")
    plain_runtime = host.runtime
    assert fake_engine.constructions == 0

    _write_bundle(bundles, bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="hi"))
    out = host.reload_bundles_from_disk()
    assert out["reload"]["kind"] == "service_reload"
    assert "language model" in out["reload"]["reason"]
    assert host.runtime is not plain_runtime and fake_engine.constructions == 1

    llm_runtime = host.runtime
    _write_bundle(bundles, bundle_id="chat", version="0.0.2", flow=_llm_flow("root", prompt="v2"))
    assert host.reload_bundles_from_disk()["reload"]["kind"] == "registry_swap"
    assert host.runtime is llm_runtime and fake_engine.constructions == 1

    full = host.reload_bundles_from_disk(full=True)
    assert full["reload"]["kind"] == "full_rebuild" and host.runtime is not llm_runtime


# ---------------------------------------------------------------- B2


def _two_turns(tmp_path: Path, *, republish_between: bool, full: bool = False) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Any]:
    bundles = tmp_path / "bundles"
    _write_bundle(bundles, bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="turn"))
    host = _host(bundles, tmp_path / "data")
    r1 = _start(host, "chat", session_id="sess-1")
    _tick_to_end(host, r1)
    if republish_between:
        _write_bundle(bundles, bundle_id="chat", version="0.0.2", flow=_llm_flow("root", prompt="turn"))
        host.reload_bundles_from_disk(full=full)
        assert host.latest_bundle_versions["chat"] == "0.0.2"
    r2 = _start(host, "chat", session_id="sess-1")
    _tick_to_end(host, r2)
    return _ledger_prompt_cache(host, r1), _ledger_prompt_cache(host, r2), host


def test_session_prompt_cache_survives_a_republish(tmp_path: Path, fake_engine) -> None:
    c1, c2, _ = _two_turns(tmp_path / "control", republish_between=False)
    FakeEngine.reset()
    p1, p2, host = _two_turns(tmp_path / "publish", republish_between=True)

    assert c1 and c2 and p1 and p2, "every turn must record metadata.prompt_cache in its ledger"
    assert c1[0]["outcome"] == "miss_created" and c2[0]["outcome"] == "hit" and c2[0]["cached_tokens"] > 0
    # THE pin: the turn after a republish reads the cache exactly like the control.
    assert p2 == c2, f"republish changed the next turn's prompt cache: {p2} != control {c2}"
    assert FakeEngine.constructions == 1, "the republish loaded the model again"


def test_a_rebuild_is_what_loses_the_cache(tmp_path: Path, fake_engine) -> None:
    """The control for the pin above: the old behaviour (rebuild) misses — the test can see it."""
    _c1, c2, _ = _two_turns(tmp_path / "control", republish_between=False)
    FakeEngine.reset()
    _p1, p2, _ = _two_turns(tmp_path / "rebuild", republish_between=True, full=True)
    assert p2 != c2 and p2[0]["outcome"] == "miss_created"


# ---------------------------------------------------------------- B4


def test_in_flight_run_keeps_its_version_new_runs_get_the_new_one(tmp_path: Path, fake_engine) -> None:
    bundles = tmp_path / "bundles"
    _write_bundle(bundles, bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="old"))
    host = _host(bundles, tmp_path / "data")
    in_flight = _start(host, "chat", session_id="s-old")  # created, not yet ticked

    _write_bundle(bundles, bundle_id="chat", version="0.0.2", flow=_llm_flow("root", prompt="new"))
    host.reload_bundles_from_disk()
    fresh = _start(host, "chat", session_id="s-new")

    _tick_to_end(host, in_flight)
    _tick_to_end(host, fresh)
    prompts = [c["prompt"] for c in FakeEngine.calls]
    assert prompts == ["old", "new"], prompts
    assert host.run_store.load(in_flight).workflow_id == "chat@0.0.1:root"
    assert host.run_store.load(fresh).workflow_id == "chat@0.0.2:root"


def test_overwritten_version_does_not_swap_under_a_run_in_flight(tmp_path: Path, fake_engine) -> None:
    """Drafts and `overwrite: true` replace a version IN PLACE: the same workflow id."""
    bundles = tmp_path / "bundles"
    path = _write_bundle(bundles, bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="before"))
    host = _host(bundles, tmp_path / "data")
    in_flight = _start(host, "chat", session_id="s1")

    time.sleep(0.01)  # a distinct mtime_ns even on coarse filesystems
    path.write_bytes(_bundle_bytes(bundle_id="chat", version="0.0.1", flow=_llm_flow("root", prompt="after")))
    out = host.reload_bundles_from_disk()
    assert out.get("pinned_runs") == 1, out
    fresh = _start(host, "chat", session_id="s2")

    _tick_to_end(host, in_flight)
    _tick_to_end(host, fresh)
    assert [c["prompt"] for c in FakeEngine.calls] == ["before", "after"]
    host.reload_bundles_from_disk()
    assert not host._run_spec_pins, "a finished run's pin must be released"


# ---------------------------------------------------------------- B3 (HTTP)


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_engine):
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_bundle(bundles_dir, bundle_id="chat", version="0.0.0", flow=_llm_flow("root", prompt="hi"))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from fastapi.testclient import TestClient

    from abstractgateway.app import app

    with TestClient(app) as client:
        assert client.get("/api/gateway/bundles", headers=HEADERS).status_code == 200  # service built
        yield client, runtime_dir


def _upload(client: Any, version: str, prompt: str = "hi") -> Any:
    return client.post(
        "/api/gateway/bundles/upload",
        headers=HEADERS,
        files={"file": (f"chat@{version}.flow", _bundle_bytes(bundle_id="chat", version=version, flow=_llm_flow("root", prompt=prompt)), "application/octet-stream")},
        data={"overwrite": "false", "reload": "true"},
    )


def test_publish_response_and_audit_line_carry_the_reload(gw) -> None:
    client, runtime_dir = gw
    res = _upload(client, "0.0.1")
    assert res.status_code == 200, res.text
    reload = res.json()["reload"]
    assert reload["kind"] == "registry_swap"
    assert [s["service"] for s in reload["services"]] == ["default:default"]
    assert isinstance(reload["duration_ms"], int)
    assert reload["sentence"].startswith("Workflows updated in place on 1 service in ")
    assert "Nothing was restarted" in reload["sentence"]

    lines = [json.loads(x) for x in (runtime_dir / "audit_log.jsonl").read_text().splitlines() if x.strip()]
    upload_lines = [x for x in lines if x.get("path") == "/api/gateway/bundles/upload"]
    assert upload_lines and isinstance(upload_lines[-1].get("duration_ms"), int)
    assert upload_lines[-1]["reload"] == {"kind": "registry_swap", "duration_ms": reload["duration_ms"], "services": ["default:default"]}


def test_health_stays_fast_during_twenty_publishes(gw) -> None:
    """B3: p99 < 500 ms. A model load holds the GIL; a rebuilding publish shows up here."""
    client, _ = gw
    FakeEngine.load_cost_s = 1.0  # what a rebuild would cost per publish (GIL held)
    built = FakeEngine.constructions
    latencies: List[float] = []
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            r = client.get("/api/health")
            latencies.append(time.perf_counter() - t0)
            assert r.status_code == 200
            time.sleep(0.002)

    th = threading.Thread(target=hammer, daemon=True)
    th.start()
    while not latencies:
        time.sleep(0.001)
    kinds = []
    try:
        for i in range(1, 21):
            res = _upload(client, f"0.1.{i}", prompt=f"v{i}")
            assert res.status_code == 200, res.text
            kinds.append(res.json()["reload"]["kind"])
    finally:
        stop.set()
        th.join(5)

    # Latency first: a rebuilding publish must fail HERE (the B3 measurement), not
    # only on the kind check below.
    assert len(latencies) >= 10, f"too few health probes ({len(latencies)})"
    p99 = statistics.quantiles(latencies, n=100)[98] if len(latencies) >= 100 else max(latencies)
    assert p99 < 0.5, f"/api/health p99 {p99 * 1000:.0f} ms during 20 publishes (max {max(latencies) * 1000:.0f} ms)"
    assert FakeEngine.constructions == built, "a publish constructed a model client"
    assert kinds == ["registry_swap"] * 20, kinds


# ---------------------------------------------------------------- one service per data dir


def test_user_auth_has_one_service_for_the_default_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_engine) -> None:
    from abstractgateway import service as service_mod
    from abstractgateway.security.principal import GatewayPrincipal

    bundles = tmp_path / "bundles"
    _write_bundle(bundles, bundle_id="chat", version="0.0.0", flow=_llm_flow("root", prompt="hi"))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setattr(service_mod, "gateway_multi_user_enabled", lambda: True)
    service_mod.stop_gateway_runner()
    try:
        admin = GatewayPrincipal(user_id="admin", tenant_id="default", roles=("admin",), runtime_id="default")
        via_admin = service_mod.get_gateway_service_for_principal(admin)
        principal_less = service_mod.get_gateway_service()
        assert principal_less is via_admin, "a principal-less caller built a second service on the admin's data dir"
        assert service_mod._service is None
        assert FakeEngine.constructions == 1, "two services = two model clients on one data dir"

        out = service_mod.reload_gateway_workflow_bundles()
        assert out["unchanged_services"] == 1 and out["services"] == [], out
    finally:
        service_mod.stop_gateway_runner()


def test_reload_object_says_what_really_happened(gw) -> None:
    """`kind` and the sentence follow the hosts' results, never a constant."""
    client, _ = gw
    res = client.post("/api/gateway/bundles/reload?full=true", headers=HEADERS)
    assert res.status_code == 200, res.text
    reload = res.json()["reload"]
    assert reload["kind"] == "full_rebuild"
    assert reload["services"][0]["kind"] == "full_rebuild"
    assert reload["sentence"].startswith("Full rebuild of 1 service in ")

    res = client.post("/api/gateway/bundles/reload", headers=HEADERS)
    reload = res.json()["reload"]
    assert reload["kind"] == "registry_swap" and reload["services"] == [] and reload["unchanged_services"] == 1
    assert reload["sentence"] == "Nothing changed on disk; no service was touched."


def test_describe_workflow_reload_aggregates_per_service_results() -> None:
    from abstractgateway.workflow_reload import describe_workflow_reload

    swapped = {"ok": True, "service": "default:alice", "count": 3, "reload": {"kind": "registry_swap", "duration_ms": 4, "reason": "workflows changed on disk", "changed": True}}
    rebuilt = {"ok": True, "service": "default:bob", "count": 2, "reload": {"kind": "service_reload", "duration_ms": 900, "reason": "the workflows now need a language model, which this service's runtime was started without", "changed": True}}
    idle = {"ok": True, "service": "default:carol", "count": 1, "reload": {"kind": "registry_swap", "duration_ms": 1, "reason": "nothing changed on disk", "changed": False}}
    broken = {"ok": False, "service": "default:dave", "error": "disk full"}

    out = describe_workflow_reload([swapped, rebuilt, idle], duration_ms=905)
    assert out["kind"] == "service_reload" and out["ok"] is True
    assert [s["service"] for s in out["services"]] == ["default:alice", "default:bob"] and out["unchanged_services"] == 1
    assert out["sentence"] == (
        "Rebuilt default:bob in 905 ms because the workflows now need a language model, which this service's runtime "
        "was started without; its loaded models and prompt caches start empty. 1 other service updated in place."
    )
    out = describe_workflow_reload([swapped, broken], duration_ms=5)
    assert out["ok"] is False and out["kind"] == "registry_swap"
    assert out["sentence"].endswith("1 service could not reload: default:dave: disk full.")


def _nested_bundle_bytes(*, version: str, child_prompt: str) -> bytes:
    """`root` starts the sub-workflow `child` (a subflow node); `child` calls the model."""
    import io

    x_in = {"id": "exec-in", "label": "", "type": "execution"}
    x_out = {"id": "exec-out", "label": "", "type": "execution"}
    root = {
        "id": "root", "name": "root", "entryNode": "s",
        "nodes": [
            {"id": "s", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [x_out]}},
            {"id": "sub", "type": "subflow", "data": {"nodeType": "subflow", "subflowId": "child", "inputs": [x_in], "outputs": [x_out]}},
            {"id": "e", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [x_in]}},
        ],
        "edges": [
            {"id": "a", "source": "s", "sourceHandle": "exec-out", "target": "sub", "targetHandle": "exec-in"},
            {"id": "b", "source": "sub", "sourceHandle": "exec-out", "target": "e", "targetHandle": "exec-in"},
        ],
    }
    child = _llm_flow("child", prompt=child_prompt)
    manifest = {
        "bundle_format_version": "1", "bundle_id": "nest", "bundle_version": version,
        "created_at": "2026-10-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {"root": "flows/root.json", "child": "flows/child.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(root))
        zf.writestr("flows/child.json", json.dumps(child))
    return buf.getvalue()


def _drive_tree_to_end(host: Any, root_id: str, *, max_rounds: int = 60) -> None:
    """Tick the root and every child the way the runner does (through the host's lookup)."""
    from abstractruntime.core.models import RunStatus

    for _ in range(max_rounds):
        root = host.run_store.load(root_id)
        if _run_is_done(root):
            return
        for status in (RunStatus.RUNNING, RunStatus.WAITING):
            for run in list(host.run_store.list_runs(status=status, limit=1000) or []):
                if status == RunStatus.WAITING:
                    continue  # the runtime resumes a parent when its child completes
                runtime, spec = host.runtime_and_workflow_for_run(run.run_id)
                runtime.tick(workflow=spec, run_id=run.run_id, max_steps=50)
        # resume parents whose child finished (what the runner's subworkflow pass does)
        for run in list(host.run_store.list_runs(status=RunStatus.WAITING, limit=1000) or []):
            children = [c for c in host.run_store.list_runs(limit=1000) if getattr(c, "parent_run_id", None) == run.run_id]
            if children and all(_run_is_done(c) for c in children):
                runtime, spec = host.runtime_and_workflow_for_run(run.run_id)
                child = children[-1]
                runtime.resume(workflow=spec, run_id=run.run_id, wait_key=getattr(run.waiting, "wait_key", None), payload={"sub_run_id": child.run_id, "output": getattr(child, "output", None) or {}})
    raise AssertionError("the run tree did not finish")


def _run_is_done(run: Any) -> bool:
    st = getattr(getattr(run, "status", None), "value", getattr(run, "status", None))
    return str(st) in {"completed", "failed", "cancelled"}


def test_sub_workflow_of_an_in_flight_run_keeps_the_overwritten_version(tmp_path: Path, fake_engine) -> None:
    """The draft/overwrite case desktop clients use at launch: the parent is in flight, the
    version is replaced IN PLACE, then the parent starts its sub-workflow — the child runs the
    version the parent started on; a run started after the overwrite runs the new one."""
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    path = bundles / "nest@0.0.1.flow"
    path.write_bytes(_nested_bundle_bytes(version="0.0.1", child_prompt="child-before"))
    host = _host(bundles, tmp_path / "data")
    in_flight = host.start_run(flow_id="root", bundle_id="nest@0.0.1", input_data={}, session_id="s1")

    time.sleep(0.01)
    path.write_bytes(_nested_bundle_bytes(version="0.0.1", child_prompt="child-after"))
    out = host.reload_bundles_from_disk()
    assert out.get("pinned_runs") == 1, out
    _drive_tree_to_end(host, in_flight)

    fresh = host.start_run(flow_id="root", bundle_id="nest@0.0.1", input_data={}, session_id="s2")
    _drive_tree_to_end(host, fresh)

    assert [c["prompt"] for c in FakeEngine.calls] == ["child-before", "child-after"], FakeEngine.calls


def test_inline_sub_workflow_start_uses_the_parents_pinned_registry(tmp_path: Path, fake_engine) -> None:
    """The other door: a synchronous START_SUBWORKFLOW resolves and runs the child inside
    the parent's tick (`registry.get` with no run context) — the gateway's guard answers it
    from the parent's pinned registry."""
    from abstractruntime.core.models import Effect, EffectType

    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    path = bundles / "nest@0.0.1.flow"
    path.write_bytes(_nested_bundle_bytes(version="0.0.1", child_prompt="child-before"))
    host = _host(bundles, tmp_path / "data")
    parent_id = host.start_run(flow_id="root", bundle_id="nest@0.0.1", input_data={}, session_id="s1")

    time.sleep(0.01)
    path.write_bytes(_nested_bundle_bytes(version="0.0.1", child_prompt="child-after"))
    assert host.reload_bundles_from_disk().get("pinned_runs") == 1

    handler = host.runtime._handlers[EffectType.START_SUBWORKFLOW]
    parent = host.run_store.load(parent_id)
    outcome = handler(parent, Effect(type=EffectType.START_SUBWORKFLOW, payload={"workflow_id": "nest@0.0.1:child", "async": False}), None)
    assert str(getattr(outcome, "status", "")).endswith("completed"), outcome
    assert [c["prompt"] for c in FakeEngine.calls] == ["child-before"], FakeEngine.calls


def _ask_then_llm_bundle_bytes(*, prompt: str) -> bytes:
    import io

    x_in = {"id": "exec-in", "label": "", "type": "execution"}
    x_out = {"id": "exec-out", "label": "", "type": "execution"}
    llm = _llm_flow("root", prompt=prompt)["nodes"][1]
    flow = {
        "id": "root", "name": "root", "entryNode": "s",
        "nodes": [
            {"id": "s", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [x_out]}},
            {"id": "ask", "type": "ask_user", "data": {"nodeType": "ask_user", "inputs": [x_in, {"id": "prompt", "label": "prompt", "type": "string"}],
                                                      "outputs": [x_out, {"id": "response", "label": "response", "type": "string"}],
                                                      "pinDefaults": {"prompt": "Go?"}}},
            llm,
            {"id": "e", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [x_in]}},
        ],
        "edges": [
            {"id": "a", "source": "s", "sourceHandle": "exec-out", "target": "ask", "targetHandle": "exec-in"},
            {"id": "b", "source": "ask", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "c", "source": "node-2", "sourceHandle": "exec-out", "target": "e", "targetHandle": "exec-in"},
        ],
    }
    manifest = {
        "bundle_format_version": "1", "bundle_id": "ask", "bundle_version": "0.0.1",
        "created_at": "2026-10-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root", "flows": {"root": "flows/root.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))
    return buf.getvalue()


def test_runtime_initiated_resume_of_a_pinned_run_uses_its_pinned_spec(tmp_path: Path, fake_engine) -> None:
    """An EMIT_EVENT effect resumes its target with `registry.get(target.workflow_id)` — the
    overwrite. The gateway's runtime resumes a pinned run on its pinned spec instead."""
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    path = bundles / "ask@0.0.1.flow"
    path.write_bytes(_ask_then_llm_bundle_bytes(prompt="answer-before"))
    host = _host(bundles, tmp_path / "data")
    rid = host.start_run(flow_id="root", bundle_id="ask@0.0.1", input_data={}, session_id="s1")
    runtime, spec = host.runtime_and_workflow_for_run(rid)
    waiting = runtime.tick(workflow=spec, run_id=rid, max_steps=20)
    assert getattr(getattr(waiting, "waiting", None), "wait_key", None), "the run must park on its question"

    time.sleep(0.01)
    path.write_bytes(_ask_then_llm_bundle_bytes(prompt="answer-after"))
    assert host.reload_bundles_from_disk().get("pinned_runs") == 1

    new_spec = host.workflow_registry.get("ask@0.0.1:root")  # what the runtime's own lookup returns
    host.runtime.resume(workflow=new_spec, run_id=rid, wait_key=waiting.waiting.wait_key, payload={"response": "yes"}, max_steps=50)
    assert [c["prompt"] for c in FakeEngine.calls] == ["answer-before"], FakeEngine.calls
