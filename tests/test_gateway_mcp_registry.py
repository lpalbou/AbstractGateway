"""MCP servers tab routes (DESIGN-v3 §6.2 + §13.6): register / edit / archive (no delete),
v1 registry still read, header values sealed in the secret store (never in the JSON, never
returned), real handshake tests against fake HTTP and stdio servers, admin only."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from email_fixtures import memory_keyring  # noqa: F401 - autouse: the OS keychain stays untouched
from mcp_fakes import FakeHttpMcp, write_stdio_fake

pytestmark = pytest.mark.basic
ADMIN = {"Authorization": "Bearer admin-token"}
SECRET = "sk-test-VERY-SECRET-1234"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.routes import entities_router, gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(entities_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    with TestClient(app) as c:
        yield c, data


def _servers(client: TestClient) -> dict:
    r = client.get("/api/gateway/mcp/servers", headers=ADMIN)
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["servers"]}


def test_add_edit_archive_and_headers_never_in_clear(env) -> None:
    client, data = env
    with FakeHttpMcp(require_bearer=SECRET) as fake:
        r = client.post("/api/gateway/admin/mcp/servers", headers=ADMIN, json={
            "name": "docs", "transport": "http", "url": fake.url,
            "headers": {"Authorization": f"Bearer {SECRET}"}, "description": "Team docs"})
        assert r.status_code == 200, r.text
        row = r.json()
        assert row["headers"] == {"Authorization": {"fingerprint": row["headers"]["Authorization"]["fingerprint"]}}
        assert len(row["headers"]["Authorization"]["fingerprint"]) == 12

        listing = client.get("/api/gateway/mcp/servers", headers=ADMIN)
        assert SECRET not in listing.text
        assert listing.json()["agents_can_call"] is False
        assert listing.json()["agents_note"].startswith("No server is offered to agents yet")
        registry_text = (data / "config" / "mcp_servers.json").read_text()
        assert SECRET not in registry_text and json.loads(registry_text)["version"] == 2
        # The value is sealed in the secret store, not readable in any file in clear.
        for path in (data / "config").rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes(), path

        # Saved test uses the sealed header value: the fake requires it.
        t = client.post("/api/gateway/admin/mcp/servers/docs/test", headers=ADMIN)
        assert t.status_code == 200, t.text
        assert t.json()["ok"] is True, t.json()
        assert [x["name"] for x in t.json()["tools"]] == ["search_docs", "read_page", "list_spaces"]
        assert t.json()["server_info"]["name"] == "fake-docs"
        assert SECRET not in t.text
        assert _servers(client)["docs"]["last_test"]["ok"] is True

        # Edit keeping the masked header (null), change the description.
        e = client.put("/api/gateway/admin/mcp/servers/docs", headers=ADMIN, json={
            "transport": "http", "url": fake.url, "headers": {"Authorization": None}, "description": "Docs v2"})
        assert e.status_code == 200, e.text
        assert e.json()["description"] == "Docs v2"
        assert client.post("/api/gateway/admin/mcp/servers/docs/test", headers=ADMIN).json()["ok"] is True

        # Wrong header => a sentence that says why.
        bad = client.post("/api/gateway/admin/mcp/test", headers=ADMIN, json={
            "transport": "http", "url": fake.url, "headers": {"Authorization": "Bearer nope"}})
        assert bad.json()["ok"] is False and "check the headers" in bad.json()["message"]

    a = client.post("/api/gateway/admin/mcp/servers/docs/archive", headers=ADMIN)
    assert a.status_code == 200 and a.json()["archived"] is True
    assert _servers(client)["docs"]["archived"] is True  # kept, marked
    u = client.post("/api/gateway/admin/mcp/servers/docs/unarchive", headers=ADMIN)
    assert u.json()["archived"] is False
    assert client.delete("/api/gateway/admin/mcp/servers/docs", headers=ADMIN).status_code in (404, 405)
    dup = client.post("/api/gateway/admin/mcp/servers", headers=ADMIN, json={"name": "docs", "transport": "http", "url": "http://x"})
    assert dup.status_code == 409 and "already registered" in dup.json()["detail"]["message"]


def test_unsaved_test_against_fake_http_lists_tools(env) -> None:
    client, _data = env
    with FakeHttpMcp() as fake:
        r = client.post("/api/gateway/admin/mcp/test", headers=ADMIN, json={"transport": "http", "url": fake.url})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    assert body["message"] == "Connected to fake-docs 2.1.0 · 3 tools."
    assert body["server_info"]["protocol_version"] == "2025-06-18"
    assert [m["method"] for m in fake.log] == ["initialize", "notifications/initialized", "tools/list", "tools/list"]


def test_test_against_fake_stdio_lists_tools_and_kills_the_process(env, tmp_path: Path) -> None:
    client, data = env
    script = write_stdio_fake(tmp_path)
    pidfile = tmp_path / "pid"
    r = client.post("/api/gateway/admin/mcp/servers", headers=ADMIN, json={
        "name": "calc", "transport": "stdio", "command": sys.executable, "args": ["-u", str(script), str(pidfile)]})
    assert r.status_code == 200, r.text
    t = client.post("/api/gateway/admin/mcp/servers/calc/test", headers=ADMIN).json()
    assert t["ok"] is True, t
    assert [x["name"] for x in t["tools"]] == ["add", "echo"]
    assert t["server_info"] == {"name": "fake-stdio", "version": "0.3", "protocol_version": "2025-11-25"}
    pid = int(pidfile.read_text())
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the stdio server {pid} is still running after the test")
    # Scratch working folder removed.
    assert not any((data / "tmp" / "mcp-tests").glob("mcp-test-*"))


def test_stdio_failures_are_sentences(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _data = env
    script = write_stdio_fake(tmp_path)
    r = client.post("/api/gateway/admin/mcp/test", headers=ADMIN, json={"transport": "stdio", "command": "definitely-not-a-command-xyz", "args": ["--stdio"]})
    assert r.json() == {**r.json(), "ok": False}
    assert r.json()["message"] == "Couldn't start `definitely-not-a-command-xyz --stdio`: command not found."
    r = client.post("/api/gateway/admin/mcp/test", headers=ADMIN, json={"transport": "stdio", "command": sys.executable, "args": ["-u", str(script), str(tmp_path / "refuse.pid"), "refuse"]})
    assert r.json()["message"] == "The server answered but refused initialize: Unsupported protocol version"
    import abstractgateway.mcp_registry as reg

    monkeypatch.setattr(reg, "TEST_TIMEOUT_S", 1.0)
    real = reg.run_connection_test
    monkeypatch.setattr(reg, "run_connection_test", lambda c, h, **kw: real(c, h, **{**kw, "timeout_s": 1.0}))
    pidfile = tmp_path / "hang.pid"
    r = client.post("/api/gateway/admin/mcp/test", headers=ADMIN, json={"transport": "stdio", "command": sys.executable, "args": ["-u", str(script), str(pidfile), "hang"]})
    assert r.json()["ok"] is False and "did not answer initialize within 1 seconds" in r.json()["message"], r.json()
    pid = int(pidfile.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_v1_registry_is_still_read_and_upgraded_on_first_edit(env) -> None:
    client, data = env
    (data / "config").mkdir(parents=True, exist_ok=True)
    (data / "config" / "mcp_servers.json").write_text(json.dumps({"version": 1, "servers": [
        {"name": "legacy", "url": "http://127.0.0.1:9/mcp", "description": "Old", "auth_required": True, "tags": ["docs"]}]}))
    row = _servers(client)["legacy"]
    assert row["transport"] == "http" and row["url"] == "http://127.0.0.1:9/mcp" and row["tags"] == ["docs"]
    assert json.loads((data / "config" / "mcp_servers.json").read_text())["version"] == 1  # reading never rewrites
    client.post("/api/gateway/admin/mcp/servers/legacy/archive", headers=ADMIN)
    doc = json.loads((data / "config" / "mcp_servers.json").read_text())
    assert doc["version"] == 2 and doc["servers"][0]["archived"] is True and doc["servers"][0]["tags"] == ["docs"]


def test_non_admin_is_refused_on_every_mcp_write(env) -> None:
    client, _data = env
    created = client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
    user = {"Authorization": f"Bearer {created.json()['token']}"}
    body = {"name": "x", "transport": "http", "url": "http://127.0.0.1:9"}
    assert client.post("/api/gateway/admin/mcp/servers", headers=user, json=body).status_code == 403
    assert client.put("/api/gateway/admin/mcp/servers/x", headers=user, json=body).status_code == 403
    assert client.post("/api/gateway/admin/mcp/servers/x/archive", headers=user).status_code == 403
    assert client.post("/api/gateway/admin/mcp/servers/x/test", headers=user).status_code == 403
    assert client.post("/api/gateway/admin/mcp/test", headers=user, json=body).status_code == 403
    # The list is admin configuration too (commands, URLs): 403 with a sentence.
    listing = client.get("/api/gateway/mcp/servers", headers=user)
    assert listing.status_code == 403
    assert listing.json()["detail"]["message"] == "Only an admin can see the MCP servers of this gateway."
    assert client.get("/api/gateway/mcp/servers", headers=ADMIN).status_code == 200
