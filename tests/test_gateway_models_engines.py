"""Models & engines through the Gateway: AbstractCore's payloads, the Gateway's rules.

The Gateway inherits AbstractCore's host profile, engines, catalog, installed
models, deletes and host jobs through the one seam (`core_config` -> Runtime
`config_facade`). These tests replace the facade functions with recorders and
pin what the Gateway adds on top:

  - the route table and its arguments (`fits=1` is `fits_only`, `tag` repeats);
  - payloads pass through unchanged, except job `cli_equivalent` strings,
    which name `abstractgateway` (the twin CLI takes the same verbs);
  - every POST is admin-only; reads are user-level; anonymous is refused;
  - real engine installs need `allow_engine_install` (default on only for a
    loopback bind); dry runs never do;
  - facade refusals keep their HTTP status and structured body; an older
    AbstractCore answers 501, a missing one 503 -- never a 500;
  - the legacy `POST /models/download` lane keeps its `{ok, job}` envelope;
  - the CLI verbs drive the same routes (and `--local` the same seam);
  - `claim-url --base-url` and `service install` defaults (bootstrap seam).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

TOKEN = "admin-token-for-models-engines-tests"


def _job(job_id: str = "dl_1", kind: str = "download", status: str = "queued", **extra: Any) -> Dict[str, Any]:
    cli = {
        "download": "abstractcore models download ollama qwen3:8b",
        "delete": "abstractcore models delete ollama qwen3:8b --yes",
        "engine_install": "abstractcore engines install ollama --yes",
    }[kind]
    job = {
        "schema": "host_job_v1",
        "job_id": job_id,
        "kind": kind,
        "status": status,
        "provider": "ollama" if kind != "engine_install" else None,
        "artifact": "qwen3:8b" if kind != "engine_install" else None,
        "engine": "ollama" if kind == "engine_install" else None,
        "percent": None,
        "message": status,
        "log_tail": [],
        "command": [],
        "dry_run": False,
        "started_at": "2026-09-23T10:00:00Z",
        "joined": 0,
        "cli_equivalent": cli,
        "job": job_id,
        "events": [],
        "elapsed_s": 0.0,
        "result": None,
    }
    job.update(extra)
    return job


@pytest.fixture()
def facade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Replace every models & engines facade function with a recorder.

    Engine installs run in the Gateway's own job registry
    (`abstractgateway.engines_install`); it is swapped for one whose installer
    records the call instead of touching the host (the real installer paths
    are tested in test_gateway_engines_install.py)."""
    import threading

    from abstractgateway import core_config
    from abstractgateway import engines_install as ei

    real = core_config.config_facade
    calls: Dict[str, List[Any]] = {}
    state = SimpleNamespace(calls=calls, raise_on={}, jobs={}, installs=[], install_gate=None)

    class _NoHost(ei.System):
        def which(self, name):
            return None

        def run(self, argv, **kw):
            calls.setdefault("host_run", []).append(list(argv))
            return 0, ""

        def writable_dir(self, path):
            return True

        def http_json(self, url, timeout=2.0):
            return None

    class _RecordingInstaller(ei.EngineInstaller):
        def install(self, eid, ctx):
            state.installs.append({"engine": eid, "force": ctx.job.force, "location": ctx.job.location})
            if state.install_gate is not None:
                state.install_gate.wait(10)
            return {"installed": True, "version": "9.9"}

    host = ei.HostFacts(os_id="darwin", arch="arm64", accelerator="metal", macos_version=(15, 0))

    def make(**_kw):
        return _RecordingInstaller(system=_NoHost(), host=host, python="/nonexistent/python", cache_dir=tmp_path / "engines",
                                   home=tmp_path / "home", system_apps_dir=tmp_path / "Applications")

    state.registry = ei.EngineJobRegistry(make, log_dir=tmp_path / "engines" / "jobs")
    state.gate = threading.Event
    monkeypatch.setattr(ei, "default_installer", make)
    ei.reset_default_registry_for_tests(state.registry)

    def rec(name: str, value: Any):
        def fn(*args: Any, **kwargs: Any) -> Any:
            calls.setdefault(name, []).append((args, kwargs))
            err = state.raise_on.get(name)
            if err is not None:
                raise err
            return value(*args, **kwargs) if callable(value) else value

        monkeypatch.setattr(real, name, fn)

    rec("host_profile", {"schema": "host_profile_v1", "os": "darwin", "accelerator": "metal"})
    rec("engine_inventory", lambda probe=False: {"schema": "engines_status_v1", "engines": [{"id": "ollama", "install": {"argv": ["brew", "install", "ollama"], "url": "https://ollama.com/download"}}]})
    rec("engine_status", lambda engine_id, probe=False: {"id": engine_id, "install": {"argv": ["brew", "install", engine_id], "url": f"https://{engine_id}.example/download"}})
    rec("engine_install_plan", {"available": True, "argv": ["brew", "install", "ollama"]})
    rec("engine_download_url", lambda engine_id: f"https://{engine_id}.example/download")
    rec("engine_install", lambda engine_id, **kw: _job("eng_1", "engine_install", "completed" if kw.get("dry_run") else "queued", dry_run=bool(kw.get("dry_run"))))
    rec("model_catalog", {"schema": "model_catalog_v1", "rows": [], "host_profile": {}})
    rec("list_installed_models", {"schema": "models_installed_v1", "rows": [{"provider": "ollama", "artifact": "qwen3:8b"}]})
    rec("delete_model_artifact", lambda provider, artifact, **kw: _job("rm_1", "delete", "completed" if kw.get("dry_run") else "queued"))
    rec("start_model_download_job", lambda provider, artifact, **kw: _job("dl_1", "download", "queued", joined=1))
    rec("host_jobs_list", lambda kind=None, status=None: {"schema": "host_jobs_v1", "jobs": [_job("dl_1"), _job("rm_1", "delete", "completed")], "generated_at": "2026-09-23T10:00:00Z"})
    rec("host_job", lambda job_id: state.jobs.get(job_id))
    rec("host_job_cancel", lambda job_id, by="api", user=None: (dict(state.jobs[job_id], status="cancelled", cancelled_by=by, cancelled_by_user=user) if job_id in state.jobs else None))
    rec("models_engines_support", {"available": True, "abstractcore_version": "2.14.0", "required": "2.14.0", "missing": []})
    state.real = real
    yield state
    ei.reset_default_registry_for_tests(None)


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_auth: bool = False) -> tuple[TestClient, dict]:
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    if user_auth:
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_reads_serve_the_core_payloads_with_their_arguments(facade, tmp_path, monkeypatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        got = client.get("/api/gateway/host/profile?refresh=1", headers=h)
        assert got.status_code == 200 and got.json()["schema"] == "host_profile_v1"
        assert facade.calls["host_profile"][-1][1] == {"refresh": True}

        got = client.get("/api/gateway/engines?probe=1", headers=h)
        assert got.status_code == 200
        body = got.json()
        assert body["schema"] == "gateway_engines_v2" and body["core_schema"] == "engines_status_v1"
        assert [e["id"] for e in body["engines"]] == ["ollama", "lmstudio", "mlx", "llamacpp", "vllm", "huggingface"]
        assert body["install_allowed"] is False  # unknown bind (a test client): not loopback
        assert body["install_policy"]["source"] == "default"
        assert facade.calls["engine_inventory"][-1][1] == {"probe": True}

        got = client.get("/api/gateway/engines/lmstudio", headers=h)
        assert got.status_code == 200 and got.json()["id"] == "lmstudio"

        got = client.get("/api/gateway/models/catalog?q=qwen&engine=ollama&fits=1&hub=1&tag=chat&tag=coding", headers=h)
        assert got.status_code == 200 and got.json()["schema"] == "model_catalog_v1"
        args, kwargs = facade.calls["model_catalog"][-1]
        assert args == ("qwen",)
        assert kwargs == {"engine": "ollama", "fits_only": True, "hub": True, "tags": ["chat", "coding"]}

        client.get("/api/gateway/models/catalog", headers=h)
        assert facade.calls["model_catalog"][-1] == ((None,), {"engine": None, "fits_only": False, "hub": False, "tags": None})

        got = client.get("/api/gateway/models/installed?provider=ollama", headers=h)
        assert got.status_code == 200 and got.json()["rows"][0]["artifact"] == "qwen3:8b"
        assert facade.calls["list_installed_models"][-1][0] == ("ollama",)


def test_jobs_routes_rewrite_the_cli_and_404_unknown_ids(facade, tmp_path, monkeypatch) -> None:
    facade.jobs["dl_1"] = _job("dl_1")
    client, h = _client(tmp_path, monkeypatch)
    with client:
        listing = client.get("/api/gateway/jobs?kind=download&status=queued", headers=h)
        assert listing.status_code == 200
        body = listing.json()
        assert body["schema"] == "host_jobs_v1"
        assert [j["cli_equivalent"] for j in body["jobs"]] == [
            "abstractgateway models download ollama qwen3:8b",
            "abstractgateway models delete ollama qwen3:8b --yes",
        ]
        assert facade.calls["host_jobs_list"][-1][1] == {"kind": "download", "status": "queued"}

        one = client.get("/api/gateway/jobs/dl_1", headers=h)
        assert one.status_code == 200
        assert one.json()["schema"] == "host_job_v1" and one.json()["status"] == "queued"  # bare Core shape
        assert one.json()["cli_equivalent"].startswith("abstractgateway models download")

        missing = client.get("/api/gateway/jobs/nope", headers=h)
        assert missing.status_code == 404
        assert missing.json()["status"] == "not_found" and missing.json()["error"]["message"] == "no job nope"

        cancelled = client.post("/api/gateway/jobs/dl_1/cancel", headers=h)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post("/api/gateway/jobs/nope/cancel", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# Engine installs: admin + allow_engine_install
# ---------------------------------------------------------------------------


def test_engine_install_needs_the_knob_except_for_dry_runs(facade, tmp_path, monkeypatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        # Unknown bind -> default off -> a real install is refused before Core is asked.
        refused = client.post("/api/gateway/engines/ollama/install", headers=h, json={"dry_run": False})
        assert refused.status_code == 403
        body = refused.json()
        assert body["reason"] == "not_allowed" and body["status"] == "refused"
        assert "allow_engine_install" in body["message"]
        assert "engine_install" not in facade.calls

        # A dry run is always allowed and runs nothing.
        dry = client.post("/api/gateway/engines/ollama/install", headers=h, json={"dry_run": True})
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True and dry.json()["plan"]["method"] == "app"
        assert dry.json()["cli_equivalent"] == "abstractgateway engines install ollama --yes"
        assert facade.installs == [] and "engine_install" not in facade.calls

        # No body at all is a real install with defaults.
        assert client.post("/api/gateway/engines/ollama/install", headers=h).status_code == 403

        # An admin turns the knob on (runtime config) -> the install starts.
        on = client.post("/api/gateway/admin/runtime-config", headers=h, json={"allow_engine_install": True})
        assert on.status_code == 200, on.text
        assert on.json()["allow_engine_install"]["value"] is True
        assert on.json()["allow_engine_install"]["source"] == "stored"
        started = client.post("/api/gateway/engines/ollama/install", headers=h, json={"dry_run": False, "force": True})
        assert started.status_code == 200, started.text
        assert started.json()["schema"] == "engine_install_job_v1" and started.json()["job_id"].startswith("eng_")
        facade.registry.get(started.json()["job_id"]).thread.join(5)
        assert facade.installs[-1] == {"engine": "ollama", "force": True, "location": "auto"}
        done = client.get(f"/api/gateway/engines/jobs/{started.json()['job_id']}", headers=h).json()
        assert done["state"] == "done" and done["percent"] == 100.0
        assert client.get("/api/gateway/engines", headers=h).json()["install_allowed"] is True


def test_loopback_bind_allows_installs_by_default_and_a_stored_off_wins(facade, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", "127.0.0.1")
    client, h = _client(tmp_path, monkeypatch)
    with client:
        engines = client.get("/api/gateway/engines", headers=h).json()
        assert engines["install_allowed"] is True
        assert engines["install_policy"] == {"value": True, "source": "default", "bind_host": "127.0.0.1", "loopback_bind": True, "caller_on_this_machine": False}
        assert client.post("/api/gateway/engines/ollama/install", headers=h, json={}).status_code == 200

        assert client.post("/api/gateway/admin/runtime-config", headers=h, json={"allow_engine_install": False}).status_code == 200
        assert client.post("/api/gateway/engines/ollama/install", headers=h, json={}).status_code == 403
        # Clearing the stored value restores the bind-derived default.
        cleared = client.post("/api/gateway/admin/runtime-config", headers=h, json={"allow_engine_install": None})
        assert cleared.json()["allow_engine_install"]["source"] == "default"
        assert client.post("/api/gateway/engines/ollama/install", headers=h, json={}).status_code == 200


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.20", "gateway.example.org"])
def test_a_non_loopback_bind_defaults_installs_off(facade, tmp_path, monkeypatch, bind) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", bind)
    client, h = _client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/gateway/engines", headers=h).json()["install_allowed"] is False
        assert client.post("/api/gateway/engines/ollama/install", headers=h, json={}).status_code == 403


@pytest.mark.parametrize(
    "peer,headers,allowed",
    [
        ("192.168.1.175", {}, True),  # the person at the gateway machine, through its LAN address
        ("127.0.0.1", {}, True),  # the same person through loopback
        ("192.168.1.50", {}, False),  # another computer on the LAN
        ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, False),  # a proxy on this host, for someone else
    ],
)
def test_a_lan_bound_gateway_lets_the_person_at_the_machine_install_engines(facade, tmp_path, monkeypatch, peer, headers, allowed) -> None:
    """Mission HH (2026-09-24): same rule as the apps (security/same_machine.py)."""
    from abstractgateway.security import same_machine

    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", "0.0.0.0")
    monkeypatch.setattr(same_machine, "own_addresses", lambda **kw: frozenset({"127.0.0.1", "::1", "192.168.1.175"}))
    base, _h = _client(tmp_path, monkeypatch)
    client = TestClient(base.app, client=(peer, 50321))
    h = {"Authorization": f"Bearer {TOKEN}", **headers}
    with client:
        got = client.get("/api/gateway/engines", headers=h)
        engines = got.json()
        assert got.status_code == 200 and engines["install_allowed"] is allowed, got.text
        assert engines["install_policy"]["caller_on_this_machine"] is allowed
        assert engines["install_policy"]["source"] == ("default_same_machine" if allowed else "default")
        r = client.post("/api/gateway/engines/ollama/install", headers=h, json={})
        assert r.status_code == (200 if allowed else 403), r.text


def test_install_refusals_keep_their_status_and_body(facade, tmp_path, monkeypatch) -> None:
    import threading

    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", "localhost")
    facade.install_gate = threading.Event()
    client, h = _client(tmp_path, monkeypatch)
    with client:
        first = client.post("/api/gateway/engines/lmstudio/install", headers=h, json={})
        assert first.status_code == 200, first.text
        # The same engine joins its running job; another engine is refused while it runs.
        again = client.post("/api/gateway/engines/lmstudio/install", headers=h, json={})
        assert again.json()["job_id"] == first.json()["job_id"] and again.json()["joined"] is True
        got = client.post("/api/gateway/engines/ollama/install", headers=h, json={})
        assert got.status_code == 409
        body = got.json()
        assert body["ok"] is False and body["reason"] == "busy" and first.json()["job_id"] in body["message"]
        assert body["error"]["type"] == "host_action_refused"
        facade.install_gate.set()

        assert client.post("/api/gateway/engines/x/install", headers=h, json={"dry_run": True}).status_code == 404
        assert client.post("/api/gateway/engines/x/install", headers=h, json={}).status_code == 404
        # vLLM on a Mac: a refusal with the reason, never a job.
        vllm = client.post("/api/gateway/engines/vllm/install", headers=h, json={})
        assert vllm.status_code == 409 and vllm.json()["reason"] == "unsupported_on_this_machine"
        assert "macOS" in vllm.json()["message"]


# ---------------------------------------------------------------------------
# Deletes and downloads
# ---------------------------------------------------------------------------


def test_delete_refusals_and_success(facade, tmp_path, monkeypatch) -> None:
    from abstractgateway import core_config

    client, h = _client(tmp_path, monkeypatch)
    with client:
        facade.raise_on["delete_model_artifact"] = core_config.HostActionRefused(
            "refusing to delete: loaded (send force=true to override)", status_code=409, status="refused", extra={"delete_blockers": ["loaded"]}
        )
        got = client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b"})
        assert got.status_code == 409
        assert got.json()["delete_blockers"] == ["loaded"]
        assert "force=true" in got.json()["message"]

        facade.raise_on["delete_model_artifact"] = core_config.HostActionRefused(
            "gone:1b is not installed for ollama", status_code=404, status="not_found", extra={"delete_blockers": []}
        )
        assert client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama", "artifact": "gone:1b"}).status_code == 404

        facade.raise_on.pop("delete_model_artifact")
        ok = client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b", "dry_run": True, "force": True})
        assert ok.status_code == 200, ok.text
        assert ok.json()["kind"] == "delete"
        assert ok.json()["cli_equivalent"] == "abstractgateway models delete ollama qwen3:8b --yes"
        assert facade.calls["delete_model_artifact"][-1] == (("ollama", "qwen3:8b"), {"dry_run": True, "force": True, "run_inline": None})

        assert client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama"}).status_code == 422


def test_legacy_download_lane_keeps_its_envelope_over_the_core_registry(facade, tmp_path, monkeypatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        got = client.post("/api/gateway/models/download", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b", "expected_bytes": 5200000000})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["ok"] is True
        job = body["job"]
        # The lane's vocabulary: `job`, `running` for a queued job, joined counting the first request.
        assert job["job"] == "dl_1" and job["job_id"] == "dl_1"
        assert job["status"] == "running" and job["host_status"] == "queued"
        assert job["joined"] == 2
        assert job["schema"] == "host_job_v1"  # the embedded screens read the envelope too
        assert job["cli_equivalent"] == "abstractgateway models download ollama qwen3:8b"
        assert facade.calls["start_model_download_job"][-1] == (
            ("ollama", "qwen3:8b"), {"dry_run": False, "expected_bytes": 5200000000, "run_inline": None}
        )

        facade.jobs["dl_1"] = _job("dl_1", status="completed", percent=100.0)
        polled = client.get("/api/gateway/models/download/dl_1", headers=h)
        assert polled.status_code == 200 and polled.json()["job"]["status"] == "completed"
        assert client.get("/api/gateway/models/download/unknown", headers=h).status_code == 404

        listing = client.get("/api/gateway/models/downloads", headers=h).json()
        assert listing["ok"] is True and listing["jobs"][0]["job"] == "dl_1"
        assert facade.calls["host_jobs_list"][-1][1] == {"kind": "download", "status": None}

        # Contract unchanged: `recommended` and a named artifact are exclusive; both parts required.
        assert client.post("/api/gateway/models/download", headers=h, json={"provider": "ollama"}).status_code == 400


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------


def test_reads_are_user_level_and_every_post_is_admin_only(facade, tmp_path, monkeypatch) -> None:
    facade.jobs["dl_1"] = _job("dl_1")
    client, admin = _client(tmp_path, monkeypatch, user_auth=True)
    with client:
        created = client.post("/api/gateway/admin/users", headers=admin, json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
        assert created.status_code == 200, created.text
        user = {"Authorization": f"Bearer {created.json()['token']}"}

        for path in (
            "/api/gateway/host/profile",
            "/api/gateway/engines",
            "/api/gateway/engines/ollama",
            "/api/gateway/models/catalog?fits=1",
            "/api/gateway/models/installed",
            "/api/gateway/jobs",
            "/api/gateway/jobs/dl_1",
        ):
            assert client.get(path, headers=user).status_code == 200, path
            assert client.get(path).status_code == 401, path

        for path, payload in (
            ("/api/gateway/engines/ollama/install", {"dry_run": True}),
            ("/api/gateway/models/delete", {"provider": "ollama", "artifact": "qwen3:8b", "dry_run": True}),
            ("/api/gateway/jobs/dl_1/cancel", None),
            ("/api/gateway/models/download", {"provider": "ollama", "artifact": "qwen3:8b", "dry_run": True}),
        ):
            denied = client.post(path, headers=user, json=payload)
            assert denied.status_code == 403, (path, denied.text)
            assert denied.json()["required_role"] == "admin", path
            assert client.post(path, headers=admin, json=payload).status_code == 200, path
        # The policy row refused before the facade was reached.
        assert len(facade.calls["host_job_cancel"]) == 1


def test_policy_rows_name_every_new_post() -> None:
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    def gated(path: str, method: str = "POST") -> bool:
        return any(p.matches(path, method) and p.requirement(method).admin_required for p in GATEWAY_ROUTE_POLICIES) if hasattr(
            GATEWAY_ROUTE_POLICIES[0], "matches"
        ) else None

    if gated("/api/gateway/models/delete") is None:
        pytest.skip("policy rows expose no matcher here; covered end to end above")
    for path in (
        "/api/gateway/models/delete", "/api/gateway/engines/ollama/install", "/api/gateway/jobs/dl_1/cancel",
        "/api/gateway/engines/ollama/start", "/api/gateway/engines/lmstudio/stop",
        "/api/gateway/engines/jobs/eng_1/continue", "/api/gateway/engines/jobs/eng_1/cancel",
    ):
        assert gated(path), path
    for path in ("/api/gateway/engines", "/api/gateway/models/catalog", "/api/gateway/models/installed", "/api/gateway/jobs/dl_1",
                 "/api/gateway/host/profile", "/api/gateway/engines/jobs/eng_1", "/api/gateway/engines/jobs"):
        assert not gated(path, "GET"), path


def test_admin_posts_land_in_the_audit_log(facade, tmp_path, monkeypatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        assert client.post("/api/gateway/engines/ollama/install", headers=h, json={"dry_run": True}).status_code == 200
        assert client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b", "dry_run": True}).status_code == 200
    log = tmp_path / "runtime" / "audit_log.jsonl"
    assert log.exists(), "the audit middleware writes every request"
    text = log.read_text(encoding="utf-8")
    assert "/api/gateway/engines/ollama/install" in text
    assert "/api/gateway/models/delete" in text


# ---------------------------------------------------------------------------
# Older or missing AbstractCore
# ---------------------------------------------------------------------------


def test_an_older_abstractcore_is_501_and_a_missing_one_503(facade, tmp_path, monkeypatch) -> None:
    from abstractgateway import core_config

    too_old = core_config.CoreTooOld("Engine detection", "2.13.42", missing="abstractcore.config.engines")
    for name in ("host_profile", "engine_inventory", "engine_status", "engine_install", "model_catalog", "list_installed_models",
                 "delete_model_artifact", "start_model_download_job", "host_jobs_list", "host_job", "host_job_cancel"):
        facade.raise_on[name] = too_old
    client, h = _client(tmp_path, monkeypatch)
    with client:
        for method, path, payload in (
            ("GET", "/api/gateway/host/profile", None),
            ("GET", "/api/gateway/engines?probe=1", None),
            ("GET", "/api/gateway/engines/ollama", None),
            ("GET", "/api/gateway/models/catalog?fits=1", None),
            ("GET", "/api/gateway/models/installed", None),
            ("POST", "/api/gateway/models/delete", {"provider": "ollama", "artifact": "x", "dry_run": True}),
            ("POST", "/api/gateway/models/download", {"provider": "ollama", "artifact": "x"}),
            ("GET", "/api/gateway/models/download/dl_1", None),
            ("GET", "/api/gateway/models/downloads", None),
            ("GET", "/api/gateway/jobs", None),
            ("GET", "/api/gateway/jobs/dl_1", None),
            ("POST", "/api/gateway/jobs/dl_1/cancel", None),
        ):
            got = client.request(method, path, headers=h, json=payload)
            assert got.status_code == 501, (path, got.status_code, got.text)
            body = got.json()
            assert body["reason"] == "abstractcore_too_old", path
            assert body["required"] == "2.14.0" and body["installed"] == "2.13.42", path
            assert "abstractcore>=2.14.0" in body["message"], path

        facade.raise_on["engine_inventory"] = RuntimeError("Engine detection needs AbstractCore, which is not installed.")
        got = client.get("/api/gateway/engines", headers=h)
        assert got.status_code == 503
        assert got.json()["reason"] == "abstractcore_unavailable"


def test_the_real_facade_reports_too_old_as_501_end_to_end(tmp_path, monkeypatch) -> None:
    """No recorder: the REAL Runtime facade over an AbstractCore without the engines module."""
    import sys

    monkeypatch.setitem(sys.modules, "abstractcore.config.engines", None)
    client, h = _client(tmp_path, monkeypatch)
    with client:
        got = client.get("/api/gateway/engines", headers=h)
        assert got.status_code == 501, got.text
        assert got.json()["reason"] == "abstractcore_too_old"
        assert got.json()["missing"] == "abstractcore.config.engines"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str], capsys) -> tuple[int, str, str]:
    from abstractgateway.cli import main

    with pytest.raises(SystemExit) as info:
        main(argv)
    out = capsys.readouterr()
    return int(info.value.code or 0), out.out, out.err


@pytest.fixture()
def cli_over_testclient(facade, tmp_path, monkeypatch):
    """Point the CLI's HTTP transport at an in-process gateway (no sockets)."""
    from abstractgateway import models_engines_cli as mec

    client, h = _client(tmp_path, monkeypatch)
    client.__enter__()
    seen: List[tuple] = []

    class _Via:
        url = "http://testserver"

        def __init__(self, args):
            self.args = args

        def call(self, method, path, body=None, *, timeout=None):
            seen.append((method, path, body))
            res = client.request(method, "/api/gateway" + path, headers=h, json=body)
            try:
                data = res.json()
            except Exception:
                data = None
            return mec._Answer(res.status_code, data)

    monkeypatch.setattr(mec, "_Http", _Via)
    monkeypatch.setattr(mec, "_POLL_S", 0.0)
    try:
        yield SimpleNamespace(seen=seen, facade=facade)
    finally:
        client.__exit__(None, None, None)


def test_cli_verbs_drive_the_gateway_routes(cli_over_testclient, capsys) -> None:
    f = cli_over_testclient.facade
    rc, out, _ = _run_cli(["models", "list", "--provider", "ollama", "--json"], capsys)
    assert rc == 0 and json.loads(out)["schema"] == "models_installed_v1"

    rc, out, _ = _run_cli(["models", "search", "qwen 8b", "--fits", "--engine", "ollama", "--tag", "chat", "--json"], capsys)
    assert rc == 0 and json.loads(out)["schema"] == "model_catalog_v1"
    assert f.calls["model_catalog"][-1] == (("qwen 8b",), {"engine": "ollama", "fits_only": True, "hub": False, "tags": ["chat"]})

    rc, out, _ = _run_cli(["engines", "status", "--probe", "--json"], capsys)
    assert rc == 0 and json.loads(out)["engines"][0]["id"] == "ollama"

    # Download waits by polling /jobs/{id} until the job ends.
    f.jobs["dl_1"] = _job("dl_1", status="completed", percent=100.0)
    rc, out, _ = _run_cli(["models", "download", "ollama", "qwen3:8b", "--json"], capsys)
    assert rc == 0
    assert json.loads(out)["status"] == "completed"
    assert ("GET", "/jobs/dl_1", None) in cli_over_testclient.seen

    rc, out, _ = _run_cli(["models", "download", "ollama", "qwen3:8b", "--no-wait", "--json"], capsys)
    assert rc == 0 and json.loads(out)["host_status"] == "queued"

    rc, out, _ = _run_cli(["models", "jobs", "--kind", "download", "--json"], capsys)
    assert rc == 0 and json.loads(out)["jobs"][0]["cli_equivalent"].startswith("abstractgateway ")

    rc, out, _ = _run_cli(["models", "cancel", "dl_1", "--json"], capsys)
    assert rc == 0 and json.loads(out)["status"] == "cancelled"
    rc, _, err = _run_cli(["models", "cancel", "nope"], capsys)
    assert rc == 1 and "no job nope" in err


def test_cli_destructive_verbs_need_yes_and_refusals_exit_2(cli_over_testclient, capsys) -> None:
    f = cli_over_testclient.facade
    rc, out, _ = _run_cli(["models", "delete", "ollama", "qwen3:8b", "--json"], capsys)
    assert rc == 2 and json.loads(out)["reason"] == "not_confirmed"
    assert "delete_model_artifact" not in f.calls

    rc, out, _ = _run_cli(["models", "delete", "ollama", "qwen3:8b", "--dry-run", "--json"], capsys)
    assert rc == 0 and json.loads(out)["kind"] == "delete"

    rc, out, _ = _run_cli(["engines", "install", "ollama", "--json"], capsys)
    assert rc == 2 and json.loads(out)["install"]["method"] == "app"
    assert f.installs == []

    # The gateway's own refusal (knob off on an unknown bind) is exit 2 as well.
    rc, out, err = _run_cli(["engines", "install", "ollama", "--yes"], capsys)
    assert rc == 2 and "allow_engine_install" in err

    rc, out, _ = _run_cli(["engines", "install", "ollama", "--dry-run", "--json"], capsys)
    assert rc == 0 and json.loads(out)["dry_run"] is True

    rc, out, _ = _run_cli(["engines", "open", "lmstudio", "--no-browser"], capsys)
    assert rc == 0 and out.strip() == "https://lmstudio.ai/download"


def test_cli_local_mode_uses_the_seam_without_a_gateway(facade, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    rc, out, _ = _run_cli(["engines", "install", "ollama", "--dry-run", "--local", "--json"], capsys)
    assert rc == 0
    job = json.loads(out)
    assert job["cli_equivalent"] == "abstractgateway engines install ollama --yes"
    assert job["dry_run"] is True and facade.installs == []
    # The person at the terminal is on the host: allowed, foreground.
    rc, out, _ = _run_cli(["engines", "install", "mlx", "--yes", "--local", "--json"], capsys)
    assert rc == 0, out
    assert json.loads(out)["state"] == "done" and facade.installs[-1]["engine"] == "mlx"

    rc, out, _ = _run_cli(["models", "catalog", "--fits", "--local", "--json"], capsys)
    assert rc == 0 and facade.calls["model_catalog"][-1][1]["fits_only"] is True

    rc, out, _ = _run_cli(["engines", "open", "ollama", "--no-browser", "--local"], capsys)
    assert rc == 0 and out.strip() == "https://ollama.example/download"

    facade.raise_on["list_installed_models"] = __import__("abstractgateway.core_config", fromlist=["x"]).CoreTooOld("Listing installed models", "2.13.42")
    rc, _, err = _run_cli(["models", "list", "--local"], capsys)
    assert rc == 1 and "abstractcore>=2.14.0" in err


def test_cli_http_default_finds_the_recorded_gateway_and_its_token(tmp_path, monkeypatch) -> None:
    from abstractgateway import models_engines_cli as mec
    from abstractgateway.first_run import write_serve_record

    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.delenv("ABSTRACTGATEWAY_URL", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    write_serve_record(data_dir=data, host="127.0.0.1", port=18123, auth={"mode": "users"}, data_dir_source="env")
    (data / "auth").mkdir(parents=True)
    (data / "auth" / "bootstrap-admin-token").write_text("tok-from-file\n", encoding="utf-8")
    args = argparse.Namespace(url=None, token=None, data_dir=None)
    url, token, source = mec._resolve_connection(args)
    assert (url, token, source) == ("http://127.0.0.1:18123", "tok-from-file", "serve_record")

    # A non-loopback URL never gets the local token file.
    args = argparse.Namespace(url="https://gw.example.org", token=None, data_dir=None)
    assert mec._resolve_connection(args)[1] == ""


# ---------------------------------------------------------------------------
# Bootstrap seam (CONTRACTS §I)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv0", [["claim"], ["config", "claim-url"]])
def test_claim_accepts_base_url_as_an_alias_of_url(tmp_path, monkeypatch, capsys, argv0) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    if argv0 == ["claim"]:
        from abstractgateway.cli import main

        with pytest.raises(SystemExit) as info:
            main(["claim", "--base-url", "http://127.0.0.1:18444", "--json"])
    else:
        from abstractgateway.config_cli import main as config_main

        with pytest.raises(SystemExit) as info:
            config_main(["claim-url", "--base-url", "http://127.0.0.1:18444", "--json"])
    assert int(info.value.code or 0) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_url"] == "http://127.0.0.1:18444" and payload["base_url_source"] == "flag"
    assert payload["url"].startswith("http://127.0.0.1:18444/console#claim=")


def test_service_install_with_host_and_port_starts_and_waits_by_default(tmp_path, monkeypatch, capsys) -> None:
    from abstractgateway import os_service
    from abstractgateway.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    executed: List[Any] = []
    waited: List[tuple] = []
    monkeypatch.setattr(os_service, "execute_plan", lambda plan, **kw: executed.append(plan) or [])
    monkeypatch.setattr(os_service, "write_service_record", lambda plan: None)
    monkeypatch.setattr(os_service, "wait_for_health", lambda url, timeout_s=60.0: waited.append((url, timeout_s)) or True)
    monkeypatch.setattr(os_service, "choose_port", lambda host, requested, persisted: {"port": int(requested), "source": "flag"})

    with pytest.raises(SystemExit) as info:
        main(["service", "install", "--host", "127.0.0.1", "--port", "18999", "--no-claim"])
    assert int(info.value.code or 0) == 0
    assert len(executed) == 1
    plan = executed[0]
    assert plan.commands, "install must register AND start the service by default"
    assert os_service.without_start(plan) != plan.commands, "the default plan includes the start commands"
    assert waited and waited[0][0].endswith(":18999"), "install waits for /api/health by default"


def test_the_console_crate_request_shapes_are_accepted(facade, tmp_path, monkeypatch) -> None:
    """What the terminal console (`abstractgateway-console`, HttpTransport) sends."""
    facade.jobs["eng_1"] = _job("eng_1", "engine_install", "running", error=None)
    facade.jobs["dl_1"] = _job("dl_1", "download", "failed", error="pull failed")
    client, h = _client(tmp_path, monkeypatch)
    with client:
        # `q` always sent, possibly empty (= no filter); flags as 0/1.
        assert client.get("/api/gateway/models/catalog?q=&fits=0&hub=0", headers=h).status_code == 200
        assert facade.calls["model_catalog"][-1] == ((None,), {"engine": None, "fits_only": False, "hub": False, "tags": None})
        client.get("/api/gateway/models/catalog?q=&fits=1", headers=h)
        assert facade.calls["model_catalog"][-1][1]["fits_only"] is True
        assert client.get("/api/gateway/engines?probe=0", headers=h).status_code == 200
        assert facade.calls["engine_inventory"][-1][1] == {"probe": False}
        # Cancel with an empty JSON object.
        got = client.post("/api/gateway/jobs/eng_1/cancel", headers=h, json={})
        assert got.status_code == 200 and got.json()["status"] == "cancelled"
        # Every job kind, legacy downloads included, is served under /jobs/{id}; `error` is a string when set.
        for jid in ("eng_1", "dl_1"):
            one = client.get(f"/api/gateway/jobs/{jid}", headers=h)
            assert one.status_code == 200 and one.json()["job_id"] == jid
        assert client.get("/api/gateway/jobs/dl_1", headers=h).json()["error"] == "pull failed"
        # The three POSTs return a host_job_v1, bare or as {"job": ...}.
        dl = client.post("/api/gateway/models/download", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b", "dry_run": False}).json()
        assert dl["job"]["schema"] == "host_job_v1" and dl["job"]["job_id"]
        rm = client.post("/api/gateway/models/delete", headers=h, json={"provider": "ollama", "artifact": "qwen3:8b", "dry_run": False, "force": False}).json()
        assert rm["schema"] == "host_job_v1"
        monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", "127.0.0.1")
        ins = client.post("/api/gateway/engines/ollama/install", headers=h, json={"dry_run": False}).json()
        # The engine job (contract engine_install_job_v1) still carries the host_job_v1
        # fields the console crate reads, and the older /jobs/{id} lane serves it.
        assert ins["schema"] == "engine_install_job_v1" and ins["job_id"] and ins["status"] in {"queued", "running", "completed"}
        legacy = client.get(f"/api/gateway/jobs/{ins['job_id']}", headers=h).json()
        assert legacy["schema"] == "host_job_v1" and legacy["kind"] == "engine_install" and legacy["engine"] == "ollama"
