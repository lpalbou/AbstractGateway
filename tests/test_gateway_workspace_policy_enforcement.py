from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-19T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-policy", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_server_file_endpoints_ignore_client_scope_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r1 = client.get(
            "/api/gateway/files/search",
            params={"query": "secret", "limit": 20, "workspace_root": str(outside)},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        items = r1.json().get("items") or []
        paths = {it.get("path") for it in items if isinstance(it, dict)}
        assert "secret.txt" not in paths

        r2 = client.get(
            "/api/gateway/files/read",
            params={"path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r2.status_code == 403, r2.text

        r3 = client.post(
            "/api/gateway/attachments/ingest",
            json={"session_id": "s1", "path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r3.status_code == 403, r3.text


def test_workspace_policy_endpoint_exposes_mount_names_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_MOUNTS", f"notes={notes}\n")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r = client.get("/api/gateway/workspace/policy", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        policy = body.get("policy") or {}
        assert isinstance(policy, dict)
        assert policy.get("target") == "server"
        mounts = policy.get("mounts") or []
        assert {"name": "notes"} in mounts
        assert str(notes) not in json.dumps(body)


def test_stored_workspace_policy_overrides_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_ws = tmp_path / "env-workspace"
    env_ws.mkdir(parents=True, exist_ok=True)
    stored_ws = tmp_path / "stored-workspace"
    stored_ws.mkdir(parents=True, exist_ok=True)
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    stored_file = stored_ws / "inside.txt"
    stored_file.write_text("stored\n", encoding="utf-8")
    env_file = env_ws / "env-only.txt"
    env_file.write_text("env\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(env_ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    from abstractgateway.runtime_config import write_runtime_config

    runtime_dir = tmp_path / "runtime"
    write_runtime_config(
        runtime_dir,
        {
            "workspace_root": str(stored_ws),
            "workspace_mounts": f"notes={notes}",
        },
        actor="person:admin",
    )

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        policy = client.get("/api/gateway/workspace/policy", headers=headers)
        assert policy.status_code == 200, policy.text
        assert {"name": "notes"} in (policy.json().get("policy", {}).get("mounts") or [])
        assert str(notes) not in json.dumps(policy.json())

        stored_ok = client.get(
            "/api/gateway/files/read",
            params={"path": str(stored_file)},
            headers=headers,
        )
        assert stored_ok.status_code == 200, stored_ok.text

        env_denied = client.get(
            "/api/gateway/files/read",
            params={"path": str(env_file)},
            headers=headers,
        )
        assert env_denied.status_code == 403, env_denied.text


def test_sanitize_run_workspace_policy_rejects_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    # Launch-folder trust defaults ON since the 2026-08-19 ruling; this test
    # pins the LOCKED-DOWN posture, so switch it off through the settings
    # store (the one lane — deliberately no env for this knob).
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    write_runtime_config(runtime_dir, {"trust_client_launch_folder": False}, actor="person:admin")

    # backlog 0232 §1: an out-of-scope workspace_root REFUSES (400) naming the
    # rejected root and the allowed roots. It is never silently popped — the
    # bare `pop()` let the run start believing it had a workspace it did not
    # have, which is the root cause of every symptom in that item.
    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy(
            {
                "workspace_root": str(outside),
                "workspace_access_mode": "all_except_ignored",
                "workspace_allowed_paths": [str(outside)],
            }
        )
    assert excinfo.value.status_code == 400
    detail = str(excinfo.value.detail)
    assert str(outside) in detail, "the refusal must name the rejected root"
    assert str(ws) in detail, "the refusal must name the allowed roots"

    # An out-of-scope allowed-path entry refuses on the same grounds, even when
    # workspace_root itself is fine (it used to vanish from the list silently,
    # narrowing the grant the operator declared with nothing said about it).
    with pytest.raises(HTTPException) as excinfo2:
        _sanitize_run_workspace_policy({"workspace_allowed_paths": [str(outside)]})
    assert excinfo2.value.status_code == 400
    assert str(outside) in str(excinfo2.value.detail)

    # In-scope values still pass through, and the access-mode downgrade stands.
    sanitized = _sanitize_run_workspace_policy(
        {
            "workspace_root": str(ws),
            "workspace_access_mode": "all_except_ignored",
        }
    )
    assert sanitized.get("workspace_root") == str(ws)
    assert sanitized.get("workspace_access_mode") == "workspace_only"


def test_sanitize_run_workspace_policy_rejects_blocked_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    blocked = tmp_path / "blocked"
    blocked.mkdir(parents=True, exist_ok=True)
    blocked_child = blocked / "nested"
    blocked_child.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")

    from abstractgateway.runtime_config import write_runtime_config
    from fastapi import HTTPException
    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    write_runtime_config(
        runtime_dir,
        {"workspace_blocked_paths": str(blocked)},
        actor="person:admin",
    )

    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy({"workspace_root": str(blocked_child)})
    assert excinfo.value.status_code == 400
    detail = str(excinfo.value.detail)
    assert "blocked by the gateway workspace deny list" in detail
    assert str(blocked) in detail


def test_server_file_endpoints_honor_client_scope_overrides_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r1 = client.get(
            "/api/gateway/files/search",
            params={"query": "secret", "limit": 20, "workspace_root": str(outside)},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        items = r1.json().get("items") or []
        paths = {it.get("path") for it in items if isinstance(it, dict)}
        assert "secret.txt" in paths

        r2 = client.get(
            "/api/gateway/files/read",
            params={"path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert (r2.json() or {}).get("path") == "secret.txt"
        assert "secret" in str((r2.json() or {}).get("content") or "")

        r3 = client.post(
            "/api/gateway/attachments/ingest",
            json={"session_id": "s1", "path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r3.status_code == 200, r3.text
        attachment = (r3.json() or {}).get("attachment") or {}
        assert attachment.get("source_path") == "secret.txt"


def test_sanitize_run_workspace_policy_accepts_outside_root_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    sanitized = _sanitize_run_workspace_policy(
        {
            "workspace_root": str(outside),
            "workspace_access_mode": "all_except_ignored",
            "workspace_allowed_paths": [str(outside)],
        }
    )
    assert sanitized.get("workspace_root") == str(outside)
    assert sanitized.get("workspace_access_mode") == "all_except_ignored"
    assert str(outside) in str(sanitized.get("workspace_allowed_paths") or "")


def test_stored_client_scope_override_beats_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    from abstractgateway.runtime_config import write_runtime_config

    runtime_dir = tmp_path / "runtime"
    write_runtime_config(
        runtime_dir,
        {"client_workspace_scope_overrides": True},
        actor="person:admin",
    )

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r = client.get(
            "/api/gateway/files/read",
            params={"path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r.status_code == 200, r.text


def test_open_run_workspace_uses_stored_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)

    import abstractgateway.routes.gateway as gateway_routes

    opened: list[list[str]] = []

    def _fake_command(path: Path) -> list[str]:
        return ["open-test", str(path)]

    async def _fake_launch(command: list[str]) -> None:
        opened.append(list(command))

    monkeypatch.setattr(gateway_routes, "_workspace_open_command", _fake_command)
    monkeypatch.setattr(gateway_routes, "_launch_workspace_opener", _fake_launch)

    with client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "bundle-policy", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        resp = client.post(f"/api/gateway/runs/{run_id}/workspace/open", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        workspace_root = Path(body["workspace_root"])
        assert body["opened"] is True
        assert workspace_root.exists()
        assert workspace_root.is_dir()
        assert opened == [["open-test", str(workspace_root)]]
