from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class _Stores:
    base_dir: Path


@dataclass
class _Service:
    stores: _Stores


def _make_app(*, monkeypatch: pytest.MonkeyPatch, gateway_base_dir: Path) -> FastAPI:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gateway_base_dir)))
    monkeypatch.setattr(gateway_routes, "backlog_exec_runner_status", lambda: {"alive": True, "error": None})

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


@pytest.mark.basic
def test_backlog_exec_config_and_requests_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    (gateway_dir / "backlog_exec_queue").mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    # Enable runner config for endpoint.
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXECUTOR", "codex_cli")
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_CODEX_BIN", sys.executable)
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_CODEX_MODEL", "gpt-5.2-xhigh")

    # Seed a few request JSON files.
    qdir = gateway_dir / "backlog_exec_queue"
    (qdir / "aaaaaaaaaaaaaaaa.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T00:00:00Z",',
                '  "request_id": "aaaaaaaaaaaaaaaa",',
                '  "status": "queued",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (qdir / "bbbbbbbbbbbbbbbb.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T01:00:00Z",',
                '  "request_id": "bbbbbbbbbbbbbbbb",',
                '  "status": "running",',
                '  "started_at": "2026-01-31T01:00:10Z",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (qdir / "cccccccccccccccc.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T02:00:00Z",',
                '  "request_id": "cccccccccccccccc",',
                '  "status": "failed",',
                '  "finished_at": "2026-01-31T02:02:00Z",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"},',
                '  "result": {"ok": false, "exit_code": 1, "error": "boom"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (qdir / "dddddddddddddddd.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T03:00:00Z",',
                '  "request_id": "dddddddddddddddd",',
                '  "status": "queued",',
                '  "backlog": {"kind": "planned", "filename": "batch(2)", "relpath": ""},',
                '  "backlog_queue": {',
                '    "mode": "sequential",',
                '    "items": [',
                '      {"kind": "planned", "filename": "701-framework-a.md", "relpath": "docs/backlog/planned/701-framework-a.md"},',
                '      {"kind": "planned", "filename": "702-framework-b.md", "relpath": "docs/backlog/planned/702-framework-b.md"}',
                "    ]",
                "  }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        cfg = client.get("/api/gateway/backlog/exec/config")
        assert cfg.status_code == 200
        body = cfg.json()
        assert body["runner_enabled"] is True
        assert body["runner_alive"] is True
        assert body["executor"] == "codex_cli"
        assert body["can_execute"] is True
        assert body.get("codex_model") == "gpt-5.2"
        assert body.get("codex_reasoning_effort") == "xhigh"

        lst = client.get("/api/gateway/backlog/exec/requests?status=queued,running&limit=10")
        assert lst.status_code == 200
        items = lst.json()["requests"]
        assert len(items) == 3
        statuses = {it["status"] for it in items}
        assert statuses == {"queued", "running"}

        failed = client.get("/api/gateway/backlog/exec/requests?status=failed")
        assert failed.status_code == 200
        items = failed.json()["requests"]
        assert len(items) == 1
        assert items[0]["request_id"] == "cccccccccccccccc"
        assert items[0]["error"] == "boom"

        active = client.get("/api/gateway/backlog/exec/active_items")
        assert active.status_code == 200
        active_items = active.json()["items"]
        rels = {it["relpath"] for it in active_items}
        assert "docs/backlog/planned/650-framework-test.md" in rels
        assert "docs/backlog/planned/701-framework-a.md" in rels
        assert "docs/backlog/planned/702-framework-b.md" in rels

        detail = client.get("/api/gateway/backlog/exec/requests/cccccccccccccccc")
        assert detail.status_code == 200
        payload = detail.json()["payload"]
        assert payload.get("request_id") == "cccccccccccccccc"
        assert "prompt" not in payload

        # Log tail endpoint (best-effort).
        run_dir = gateway_dir / "backlog_exec_runs" / "cccccccccccccccc"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "codex_events.jsonl").write_text("line1\nline2\n", encoding="utf-8")
        (run_dir / "codex_stderr.log").write_text("stderr\n", encoding="utf-8")
        tail = client.get("/api/gateway/backlog/exec/requests/cccccccccccccccc/logs/tail?name=events&max_bytes=2048")
        assert tail.status_code == 200
        t = tail.json()
        assert t["name"] == "events"
        assert "line2" in t["content"]


@pytest.mark.basic
def test_backlog_exec_feedback_and_promote_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    qdir = gateway_dir / "backlog_exec_queue"
    qdir.mkdir(parents=True, exist_ok=True)
    runs_dir = gateway_dir / "backlog_exec_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "demo").mkdir(parents=True, exist_ok=True)
    (repo_root / "demo" / "demo.txt").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    # Seed an awaiting_qa request (eligible for feedback/promote).
    rid = "eeeeeeeeeeeeeeee"
    run_dir = runs_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    # Candidate workspace with one repo folder ("demo") that changes demo.txt.
    candidate_rel = f"untracked/backlog_exec_uat/workspaces/{rid}"
    candidate_root = repo_root / candidate_rel
    (candidate_root / "demo").mkdir(parents=True, exist_ok=True)
    (candidate_root / "demo" / "demo.txt").write_text("new\n", encoding="utf-8")

    manifest = {
        "version": 1,
        "created_at": "2026-02-03T10:00:00Z",
        "request_id": rid,
        "candidate_relpath": candidate_rel,
        "repos": [{"repo": "demo", "repo_relpath": "demo", "copy": ["demo.txt"], "delete": [], "skipped": []}],
    }
    (run_dir / "candidate_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload = {
        "created_at": "2026-02-03T10:00:00Z",
        "request_id": rid,
        "status": "awaiting_qa",
        "execution_mode": "uat",
        "attempt": 1,
        "run_dir_relpath": f"backlog_exec_runs/{rid}",
        "candidate_relpath": candidate_rel,
        "candidate_manifest_relpath": f"backlog_exec_runs/{rid}/candidate_manifest.json",
        "backlog": {"kind": "planned", "filename": "715-framework-uat.md", "relpath": "docs/backlog/planned/715-framework-uat.md"},
        "result": {"ok": True, "exit_code": 0, "last_message": "done"},
    }
    (qdir / f"{rid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        fb = client.post(f"/api/gateway/backlog/exec/requests/{rid}/feedback", json={"feedback": "please fix X"})
        assert fb.status_code == 200
        fb_payload = fb.json()["payload"]
        assert fb_payload["status"] == "queued"
        assert fb_payload.get("attempt") == 2
        assert fb_payload.get("result") == {}
        assert isinstance(fb_payload.get("feedback"), list) and fb_payload["feedback"]
        assert isinstance(fb_payload.get("result_history"), list) and fb_payload["result_history"]

        # Put it back into awaiting_qa so promote can run.
        payload_on_disk = json.loads((qdir / f"{rid}.json").read_text(encoding="utf-8"))
        payload_on_disk["status"] = "awaiting_qa"
        payload_on_disk["result"] = {"ok": True, "exit_code": 0}
        payload_on_disk["candidate_relpath"] = candidate_rel
        payload_on_disk["candidate_manifest_relpath"] = f"backlog_exec_runs/{rid}/candidate_manifest.json"
        (qdir / f"{rid}.json").write_text(json.dumps(payload_on_disk, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        pr = client.post(f"/api/gateway/backlog/exec/requests/{rid}/promote", json={"redeploy": False})
        assert pr.status_code == 200
        pr_payload = pr.json()["payload"]
        assert pr_payload["status"] == "promoted"
        assert pr_payload.get("promoted_at")
        assert pr_payload.get("promotion", {}).get("copied") == 1

    assert (repo_root / "demo" / "demo.txt").read_text(encoding="utf-8") == "new\n"


@pytest.mark.basic
def test_backlog_exec_promote_releases_uat_lock_no_autodeploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    qdir = gateway_dir / "backlog_exec_queue"
    qdir.mkdir(parents=True, exist_ok=True)
    runs_dir = gateway_dir / "backlog_exec_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    # Seed prod repo content to be updated on promotion.
    (repo_root / "demo").mkdir(parents=True, exist_ok=True)
    (repo_root / "demo" / "demo.txt").write_text("old\n", encoding="utf-8")

    rid1 = "aaaaaaaaaaaaaaaa"
    rid2 = "bbbbbbbbbbbbbbbb"
    cand1_rel = f"untracked/backlog_exec_uat/workspaces/{rid1}"
    cand2_rel = f"untracked/backlog_exec_uat/workspaces/{rid2}"
    cand1_root = repo_root / cand1_rel
    cand2_root = repo_root / cand2_rel
    (cand1_root / "demo").mkdir(parents=True, exist_ok=True)
    (cand2_root / "demo").mkdir(parents=True, exist_ok=True)
    (cand1_root / "demo" / "demo.txt").write_text("new1\n", encoding="utf-8")
    (cand2_root / "demo" / "demo.txt").write_text("new2\n", encoding="utf-8")

    # Candidate manifests.
    for rid, cand_rel in [(rid1, cand1_rel), (rid2, cand2_rel)]:
        run_dir = runs_dir / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": 1,
            "created_at": "2026-02-03T10:00:00Z",
            "request_id": rid,
            "candidate_relpath": cand_rel,
            "repos": [{"repo": "demo", "repo_relpath": "demo", "copy": ["demo.txt"], "delete": [], "skipped": []}],
        }
        (run_dir / "candidate_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # Two awaiting_qa requests; rid1 is the current UAT lock owner.
    (qdir / f"{rid1}.json").write_text(
        json.dumps(
            {
                "created_at": "2026-02-03T10:00:00Z",
                "request_id": rid1,
                "status": "awaiting_qa",
                "execution_mode": "uat",
                "run_dir_relpath": f"backlog_exec_runs/{rid1}",
                "candidate_relpath": cand1_rel,
                "candidate_manifest_relpath": f"backlog_exec_runs/{rid1}/candidate_manifest.json",
                "backlog": {"kind": "planned", "filename": "700-a.md", "relpath": "docs/backlog/planned/700-a.md"},
                "uat_lock_acquired": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (qdir / f"{rid2}.json").write_text(
        json.dumps(
            {
                "created_at": "2026-02-03T10:01:00Z",
                "request_id": rid2,
                "status": "awaiting_qa",
                "execution_mode": "uat",
                "run_dir_relpath": f"backlog_exec_runs/{rid2}",
                "candidate_relpath": cand2_rel,
                "candidate_manifest_relpath": f"backlog_exec_runs/{rid2}/candidate_manifest.json",
                "backlog": {"kind": "planned", "filename": "701-b.md", "relpath": "docs/backlog/planned/701-b.md"},
                "uat_lock_acquired": False,
                "uat_pending": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    # Durable lock file owned by rid1.
    (gateway_dir / "uat_deploy_lock.json").write_text(
        json.dumps({"version": 1, "owner_request_id": rid1, "candidate_relpath": cand1_rel}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        pr = client.post(f"/api/gateway/backlog/exec/requests/{rid1}/promote", json={"redeploy": False})
        assert pr.status_code == 200
        payload = pr.json()["payload"]
        assert payload["status"] == "promoted"
        rep = payload.get("promotion_report") or {}
        assert rep.get("uat_lock_owner_request_id") == rid1
        assert rep.get("uat_lock_released") is True
        assert rep.get("next_uat_autodeployed_request_id") in {None, ""}
