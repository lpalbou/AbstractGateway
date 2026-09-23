"""One gateway-owned workspace per SESSION, not per run.

Why this file exists (2026-09-22): when a client starts a run without a
`workspace_root`, the gateway mints the folder — and that folder's path is
rendered into the SYSTEM prompt as the run's default working directory. A
conversation is a new RUN per turn, so a per-run `workspaces/<uuid4>` moved
the HEAD of the prompt on every turn and no prefix/KV cache could restore
anything: 7 turns of one session measured `outcome: cold, cached_tokens: 0`
through a hermetic gateway. Clients that read the first run's workspace back
and echo it (abstractcode web, abstractassistant) hid the defect; the
Telegram bridge and thin HTTP callers paid full prefill every turn.

Pins:
- same session_id + no workspace_root -> the SAME directory, every run;
- different sessions -> different directories, and different PRINCIPALS
  with the same session id never collide;
- no session_id -> the historical per-run folder (unchanged);
- the resolved folder lives under `<data_dir>/workspaces` and is therefore
  accepted when a client echoes it back at the policy clamp;
- a session workspace SURVIVES the ephemeral-draft purge (it belongs to the
  conversation, not to the one run that happened to create it), while a
  per-run workspace is still deleted.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_runs_list_endpoint import _wait_until, _write_min_bundle


def _configure_gateway(
    monkeypatch: pytest.MonkeyPatch, *, runtime_dir: Path, bundles_dir: Path, token: str
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-ws", flow_id="root")
    token = "t"
    _configure_gateway(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _start(
    client: TestClient,
    headers: dict[str, str],
    *,
    session_id: str | None = None,
    input_data: dict | None = None,
) -> str:
    payload: dict = {"bundle_id": "bundle-ws", "flow_id": "root", "input_data": dict(input_data or {})}
    if session_id is not None:
        payload["session_id"] = session_id
    res = client.post("/api/gateway/runs/start", headers=headers, json=payload)
    assert res.status_code == 200, res.text
    return str(res.json()["run_id"])


def _workspace_of(client: TestClient, headers: dict[str, str], run_id: str) -> Path:
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(run_id)
    assert run is not None, f"run {run_id} not found"
    raw = (run.vars or {}).get("workspace_root")
    assert isinstance(raw, str) and raw.strip(), f"run {run_id} has no workspace_root: {run.vars}"
    return Path(raw)


# ------------------------------------------------------------------ naming


def test_session_workspace_name_is_derived_stable_and_principal_scoped() -> None:
    from abstractgateway.run_retention import SESSION_WORKSPACE_PREFIX, session_workspace_dirname

    a = session_workspace_dirname("sess-1", tenant_id="default", user_id="alice")
    again = session_workspace_dirname("sess-1", tenant_id="default", user_id="alice")
    other_session = session_workspace_dirname("sess-2", tenant_id="default", user_id="alice")
    other_user = session_workspace_dirname("sess-1", tenant_id="default", user_id="bob")
    other_tenant = session_workspace_dirname("sess-1", tenant_id="acme", user_id="alice")

    assert a and a == again, "the name must be derived, not minted"
    assert a.startswith(SESSION_WORKSPACE_PREFIX)
    assert len({a, other_session, other_user, other_tenant}) == 4

    # No session -> caller falls back to the per-run folder.
    assert session_workspace_dirname("") is None
    assert session_workspace_dirname(None) is None

    # A hostile session id can never climb out of `workspaces/`.
    hostile = session_workspace_dirname("../../etc/passwd")
    assert hostile and "/" not in hostile and not hostile.startswith(".")


# ------------------------------------------------------------- the route


def test_two_runs_of_one_session_share_one_gateway_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        first = _workspace_of(client, headers, _start(client, headers, session_id="chat-a"))
        second = _workspace_of(client, headers, _start(client, headers, session_id="chat-a"))

        assert first == second
        assert first.is_dir()
        # Under the gateway data dir, so the policy clamp accepts it when a
        # client echoes it back on a later turn.
        assert first.parent == (tmp_path / "runtime" / "workspaces").resolve() or first.parent == (
            tmp_path / "runtime" / "workspaces"
        )
        assert first.name.startswith("session-")

        # Files written on turn 1 are still there on turn 2 (the same property
        # the prompt cache needs, seen from the agent's side).
        (first / "turn1.txt").write_text("hello\n", encoding="utf-8")
        assert (second / "turn1.txt").read_text(encoding="utf-8") == "hello\n"


def test_different_sessions_get_different_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        a = _workspace_of(client, headers, _start(client, headers, session_id="chat-a"))
        b = _workspace_of(client, headers, _start(client, headers, session_id="chat-b"))
        assert a != b


def test_runs_without_a_session_keep_the_per_run_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        a = _workspace_of(client, headers, _start(client, headers))
        b = _workspace_of(client, headers, _start(client, headers))
        assert a != b
        assert not a.name.startswith("session-")
        assert len(a.name) == 32  # uuid4().hex, as before


def test_client_named_workspace_root_still_wins_and_is_still_clamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "operator-workspace"
    ws.mkdir()
    chosen = ws / "project"
    chosen.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.runtime_config import write_runtime_config

        write_runtime_config(
            tmp_path / "runtime", {"trust_client_launch_folder": False}, actor="person:test"
        )

        # An explicit, in-scope root beats the session folder.
        run_id = _start(
            client, headers, session_id="chat-a", input_data={"workspace_root": str(chosen)}
        )
        assert _workspace_of(client, headers, run_id) == chosen.resolve()

        # Out-of-scope values are still refused, not silently relocated.
        res = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={
                "bundle_id": "bundle-ws",
                "flow_id": "root",
                "session_id": "chat-a",
                "input_data": {"workspace_root": str(outside)},
            },
        )
        assert res.status_code == 400, res.text
        assert str(outside.resolve()) in res.text


def test_echoed_session_workspace_passes_the_policy_clamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that echoes the gateway's own session folder must not be refused."""
    ws = tmp_path / "operator-workspace"
    ws.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.runtime_config import write_runtime_config

        write_runtime_config(
            tmp_path / "runtime", {"trust_client_launch_folder": False}, actor="person:test"
        )
        first = _workspace_of(client, headers, _start(client, headers, session_id="chat-a"))
        echoed = _start(
            client, headers, session_id="chat-a", input_data={"workspace_root": str(first)}
        )
        assert _workspace_of(client, headers, echoed) == first.resolve()


# ------------------------------------------------------------- retention


def test_draft_purge_deletes_a_per_run_workspace_but_never_a_session_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    lifecycle = {
        "source": "abstractflow.editor",
        "purpose": "draft_test",
        "visibility": "private",
        "retention": {"mode": "ephemeral", "expires_at": "2000-01-01T00:00:00+00:00"},
    }
    with client:
        payload_session = {
            "bundle_id": "bundle-ws",
            "flow_id": "root",
            "session_id": "chat-a",
            "input_data": {},
            "run_lifecycle": lifecycle,
        }
        res = client.post("/api/gateway/runs/start", headers=headers, json=payload_session)
        assert res.status_code == 200, res.text
        session_run = str(res.json()["run_id"])

        payload_lone = dict(payload_session)
        payload_lone.pop("session_id")
        res = client.post("/api/gateway/runs/start", headers=headers, json=payload_lone)
        assert res.status_code == 200, res.text
        lone_run = str(res.json()["run_id"])

        def _done() -> bool:
            for rid in (session_run, lone_run):
                rr = client.get(f"/api/gateway/runs/{rid}", headers=headers)
                assert rr.status_code == 200, rr.text
                if rr.json().get("status") != "completed":
                    return False
            return True

        _wait_until(_done, timeout_s=10.0, poll_s=0.05)

        session_ws = _workspace_of(client, headers, session_run)
        lone_ws = _workspace_of(client, headers, lone_run)
        # A later turn of the same conversation already lives in this folder.
        (session_ws / "turn1.txt").write_text("keep me\n", encoding="utf-8")

        res = client.post(
            "/api/gateway/runs/purge_drafts",
            headers=headers,
            json={"dry_run": False, "force": True, "delete_workspaces": True},
        )
        assert res.status_code == 200, res.text

        assert not lone_ws.exists(), "a per-run draft workspace is still purged"
        assert session_ws.exists(), "a session workspace outlives any single run of it"
        assert (session_ws / "turn1.txt").read_text(encoding="utf-8") == "keep me\n"
