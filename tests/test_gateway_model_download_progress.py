"""Download progress through the Gateway: the aggregate parent, cancel, the SSE stream.

The Gateway renders AbstractCore's per-job progress contract (state, bytes,
speed, ETA, files, stall) unchanged, and adds what is Gateway-proper:

  - "Use recommended defaults" is ONE parent job (`grp_...`) over one child
    per model, whose numbers are computed from its children;
  - `POST /models/download/{job}/cancel` (admin), which stops a group too;
  - `GET /models/downloads/stream`, Server-Sent Events of the same dicts.

These tests drive AbstractCore's REAL job registry and replace only the
provider tool (`model_materializer.download`).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

TOKEN = "admin-token-for-download-progress-tests"


class _Outcome(dict):
    command: list = []

    def to_dict(self):
        return dict(self)


@pytest.fixture(autouse=True)
def core_jobs(monkeypatch):
    from abstractcore.config import host_jobs, model_materializer

    registry = host_jobs.HostJobRegistry(persist_dir=None, tick_s=0.05)
    host_jobs.set_default_registry(registry)

    def install(fake):
        def download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False, expected_bytes=None):
            return _Outcome(fake(provider, artifact, progress_cb=progress_cb, dry_run=dry_run) or {})

        monkeypatch.setattr(model_materializer, "download", download)

    yield SimpleNamespace(registry=registry, install=install)
    host_jobs.set_default_registry(None)


RECOMMENDED = [
    {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit"},
    {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3"},
    {"route": "output.image", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit"},
]


@pytest.fixture
def three_sources(monkeypatch, core_jobs):
    """Three fake sources that move bytes under test control."""

    from abstractcore.download import DownloadProgress, DownloadStatus
    from abstractgateway import core_config

    from abstractgateway import model_downloads

    monkeypatch.setattr(core_config, "recommended_core_model_downloads", lambda: [dict(r) for r in RECOMMENDED])
    # model_downloads binds the name at import: patch the name it calls, or an
    # earlier import in the same session serves the REAL recommended set (on a
    # Mac, the memory-tier MLX build) instead of these three fakes.
    monkeypatch.setattr(model_downloads, "recommended_core_model_downloads", lambda: [dict(r) for r in RECOMMENDED])
    step = {a["artifact"]: threading.Event() for a in RECOMMENDED}
    finish = {a["artifact"]: threading.Event() for a in RECOMMENDED}
    outcome = {a["artifact"]: {"ok": True, "status": "completed", "message": "fetched"} for a in RECOMMENDED}
    sizes = {"qwen/qwen3.5-9b@4bit": 6_000, "supertonic-3": 400, "AbstractFramework/flux.2-klein-4b-8bit": 8_000}

    def fake(provider, artifact, *, progress_cb=None, dry_run=False):
        total = sizes[artifact]
        progress_cb(DownloadProgress(status=DownloadStatus.DOWNLOADING, message="go", downloaded_bytes=total // 4, total_bytes=total))
        step[artifact].wait(10)
        progress_cb(DownloadProgress(status=DownloadStatus.DOWNLOADING, message="go", downloaded_bytes=total // 2, total_bytes=total))
        finish[artifact].wait(10)
        control_cancelled = _cancelled()
        if control_cancelled:
            return {"ok": False, "status": "cancelled", "message": "cancelled"}
        return outcome[artifact]

    def _cancelled():
        from abstractcore.config.host_jobs import current_job_control

        control = current_job_control()
        return bool(control and control.is_cancelled())

    core_jobs.install(fake)
    return SimpleNamespace(step=step, finish=finish, outcome=outcome, sizes=sizes)


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_recommended_is_one_parent_whose_numbers_come_from_its_children(three_sources):
    from abstractgateway import model_downloads

    started = model_downloads.start_recommended_group()
    group = started["group"]
    assert group["job_id"].startswith("grp_") and group["kind"] == "download_group"
    assert len(started["jobs"]) == 3 and len(group["children"]) == 3
    gid = group["job_id"]

    total = sum(three_sources.sizes.values())
    assert _wait(lambda: model_downloads.get_job(gid)["bytes_total"] == total)
    view = model_downloads.get_job(gid)
    assert view["state"] == "downloading" and view["status"] == "running"
    assert view["bytes_done"] == sum(s // 4 for s in three_sources.sizes.values())
    assert view["percent"] == pytest.approx(view["bytes_done"] / total * 100, abs=0.01)
    assert view["message"].startswith("Downloading 3 models · 0 of 3 ready · ")
    assert [f["name"] for f in view["files"]] == [f"{r['provider']} {r['artifact']}" for r in RECOMMENDED]

    # Children name their parent in the listing; the parent is listed too.
    listing = model_downloads.list_jobs()
    assert listing[0]["job_id"] == gid or any(j["job_id"] == gid for j in listing)
    kids = [j for j in listing if j.get("parent_job") == gid]
    assert len(kids) == 3

    for ev in three_sources.step.values():
        ev.set()
    assert _wait(lambda: model_downloads.get_job(gid)["bytes_done"] == sum(s // 2 for s in three_sources.sizes.values()))
    # One child fails: the parent keeps running until every child ends, then says which failed.
    three_sources.outcome["supertonic-3"] = {"ok": False, "status": "failed", "message": "HTTP 503 from the hub"}
    for ev in three_sources.finish.values():
        ev.set()
    assert _wait(lambda: model_downloads.get_job(gid)["status"] != "running")
    final = model_downloads.get_job(gid)
    assert final["state"] == "failed"
    assert final["message"].startswith("1 of 3 models failed")
    assert "supertonic supertonic-3: HTTP 503 from the hub" in final["error"]


def test_all_children_done_is_a_done_parent_at_100_percent(three_sources):
    from abstractgateway import model_downloads

    gid = model_downloads.start_recommended_group()["group"]["job_id"]
    for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
        ev.set()
    assert _wait(lambda: model_downloads.get_job(gid)["status"] != "running")
    final = model_downloads.get_job(gid)
    assert final["state"] == "done" and final["percent"] == 100.0
    assert final["bytes_done"] == final["bytes_total"] == sum(three_sources.sizes.values())
    assert final["message"] == "All 3 models ready"


def test_a_child_that_never_started_fails_the_parent_with_its_reason(monkeypatch, three_sources):
    from abstractgateway import core_config, model_downloads

    real = core_config.core_start_model_download

    def refuse_voice(provider, artifact, **kw):
        if provider == "supertonic":
            raise RuntimeError("abstractvoice is not importable")
        return real(provider, artifact, **kw)

    monkeypatch.setattr(model_downloads, "core_start_model_download", refuse_voice)
    gid = model_downloads.start_recommended_group()["group"]["job_id"]
    for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
        ev.set()
    assert _wait(lambda: model_downloads.get_job(gid)["status"] != "running")
    final = model_downloads.get_job(gid)
    assert final["state"] == "failed" and "abstractvoice is not importable" in final["error"]


def test_cancelling_the_parent_cancels_every_running_child(three_sources):
    from abstractgateway import model_downloads

    gid = model_downloads.start_recommended_group()["group"]["job_id"]
    assert _wait(lambda: model_downloads.get_job(gid)["bytes_done"] > 0)
    view = model_downloads.cancel_job(gid)
    assert all(c.get("cancel_requested") for c in view["children"])
    for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
        ev.set()
    assert _wait(lambda: model_downloads.get_job(gid)["status"] != "running")
    assert model_downloads.get_job(gid)["state"] == "cancelled"
    assert model_downloads.cancel_job("dl_unknown") is None


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {TOKEN}"}


def test_routes_post_recommended_poll_parent_cancel_and_stream(three_sources, tmp_path, monkeypatch):
    client, h = _client(tmp_path, monkeypatch)
    with client:
        got = client.post("/api/gateway/models/download", headers=h, json={"recommended": True})
        assert got.status_code == 200
        body = got.json()
        assert body["recommended"] is True and len(body["jobs"]) == 3
        gid = body["group"]["job_id"]
        assert _wait(lambda: client.get(f"/api/gateway/models/download/{gid}", headers=h).json()["job"]["bytes_done"] > 0)
        polled = client.get(f"/api/gateway/models/download/{gid}", headers=h).json()["job"]
        assert polled["kind"] == "download_group" and polled["state"] == "downloading"

        listing = client.get("/api/gateway/models/downloads", headers=h).json()["jobs"]
        assert any(j["job_id"] == gid for j in listing)

        assert client.post("/api/gateway/models/download/dl_nope/cancel", headers=h).status_code == 404
        cancelled = client.post(f"/api/gateway/models/download/{gid}/cancel", headers=h)
        assert cancelled.status_code == 200
        for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
            ev.set()

        with client.stream("GET", f"/api/gateway/models/downloads/stream?job_id={gid}&until_idle=1", headers=h) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            events = [json.loads(line[len("data: "):]) for line in stream.iter_lines() if line.startswith("data: ")]
        assert events, "the stream sent the job"
        assert events[-1]["job"]["state"] == "cancelled"

        with client.stream("GET", "/api/gateway/models/downloads/stream?until_idle=1", headers=h) as stream:
            names = [line for line in stream.iter_lines() if line.startswith("event: ")]
        assert names and names[0] == "event: downloads"


def test_cancel_route_is_admin_only():
    from abstractgateway.security.authorization import gateway_route_authorization_requirement

    req = gateway_route_authorization_requirement("/api/gateway/models/download/dl_abc/cancel", "POST")
    assert req is not None and req.admin_required
    assert gateway_route_authorization_requirement("/api/gateway/models/downloads/stream", "GET") is None


# ---------------------------------------------------------------------------
# Mission KK: who cancelled, and why a download ended (never a bare "Cancelled")
# ---------------------------------------------------------------------------

DROP = "httpx.RemoteProtocolError: peer closed connection without sending complete message body"


def test_a_console_cancel_records_who_and_a_drop_is_failed_not_cancelled(three_sources, tmp_path, monkeypatch):
    three_sources.outcome["supertonic-3"] = {"ok": False, "status": "failed", "message": DROP}
    client, h = _client(tmp_path, monkeypatch)
    with client:
        body = client.post("/api/gateway/models/download", headers=h, json={"recommended": True}).json()
        gid = body["group"]["job_id"]
        ids = {j["artifact"]: j["job_id"] for j in body["jobs"]}
        text_id = ids["qwen/qwen3.5-9b@4bit"]
        assert _wait(lambda: client.get(f"/api/gateway/models/download/{text_id}", headers=h).json()["job"]["bytes_done"])
        # A person clicked "Stop download" in the console: the body says so.
        got = client.post(f"/api/gateway/models/download/{text_id}/cancel", headers=h, json={"via": "console"}).json()["job"]
        assert got["cancel_requested"] is True and got["cancelled_by"] == "console" and got["cancelled_by_user"]
        # A script's cancel (no body) is an API cancel.
        image_id = ids["AbstractFramework/flux.2-klein-4b-8bit"]
        assert client.post(f"/api/gateway/models/download/{image_id}/cancel", headers=h).json()["job"]["cancelled_by"] == "api"
        for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
            ev.set()
        assert _wait(lambda: client.get(f"/api/gateway/models/download/{gid}", headers=h).json()["job"]["status"] != "running")
        text = client.get(f"/api/gateway/models/download/{text_id}", headers=h).json()["job"]
        voice = client.get(f"/api/gateway/models/download/{ids['supertonic-3']}", headers=h).json()["job"]
        group = client.get(f"/api/gateway/models/download/{gid}", headers=h).json()["job"]
    assert text["status"] == "cancelled" and text["ended_reason"].startswith("Cancelled in the console by ")
    # The dropped connection ended its job as FAILED, with the plain reason.
    assert voice["status"] == "failed" and voice["cancelled_by"] is None
    assert voice["ended_reason"].startswith("The connection to Hugging Face dropped")
    assert group["state"] == "failed"
    assert "Cancelled in the console by" in group["ended_reason"] and "The connection to Hugging Face dropped" in group["ended_reason"]


def test_a_cancel_body_other_than_console_is_an_api_cancel(three_sources):
    from abstractgateway import model_downloads

    gid = model_downloads.start_recommended_group()["group"]["job_id"]
    view = model_downloads.cancel_job(gid, via="tray-made-up", user=None)
    assert {c.get("cancelled_by") for c in view["children"]} == {"api"}
    for ev in list(three_sources.step.values()) + list(three_sources.finish.values()):
        ev.set()
