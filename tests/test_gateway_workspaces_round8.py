"""Round 8 (DESIGN.md R8.2) server contract, always on (the console markup of round 9 is
test_r9w2_console_workspace_modals.py; the browser proofs are opt-in):

- POST /workspace/path-check: folder rows are checked with the SAME rules the policy writes
  enforce (absolute, existing directory) and say why in plain words.
- GET /admin/runtimes?account=<id>: the Accounts Runtime link's filter is the server's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "workspaces-round8-admin-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _user(user_id: str) -> dict:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id=user_id, roles=["user"])
    return {"Authorization": f"Bearer {token}"}


def test_path_check_says_why_and_matches_the_write_rules(tmp_path: Path) -> None:
    folder = tmp_path / "projects"
    folder.mkdir()
    (tmp_path / "notes.txt").write_text("x")
    with _client() as c:
        def check(path, headers=None):
            r = c.post("/api/gateway/workspace/path-check", json={"path": path}, headers=headers or {})
            assert r.status_code == 200, r.text
            return r.json()

        ok = check(str(folder) + "/")
        assert ok["valid"] is True and ok["sentence"] == "" and ok["normalized"] == str(folder.resolve())
        assert ok["absolute"] and ok["exists"] and ok["is_dir"]
        for path, sentence in (
            ("", "Type a directory path."),
            ("relative/folder", "Use a full path that starts with / (or ~ for the gateway's home directory)."),
            (str(tmp_path / "missing"), "No directory at this path on the gateway's computer."),
            (str(tmp_path / "notes.txt"), "This is a file, not a directory."),
        ):
            out = check(path)
            assert out["valid"] is False and out["sentence"] == sentence, (path, out)
            # The write refuses exactly what the check refuses (one rule, two doors).
            w = c.put("/api/gateway/workspace/policy", json={"folders": [{"path": path if path else "  ", "mode": "rw"}]})
            assert w.status_code == 400, (path, w.text)
            if path:
                assert sentence in w.json()["detail"]["message"], (path, w.text)
        assert c.put("/api/gateway/workspace/policy", json={"folders": [{"path": str(folder), "mode": "rw"}]}).status_code == 200
        # Any signed-in principal may check (a user's own policy write names a missing folder anyway).
        alice = _user("alice")
        assert check(str(folder), alice)["valid"] is True
        anon = TestClient(c.app).post("/api/gateway/workspace/path-check", json={"path": str(folder)}, headers={"Authorization": "Bearer nope"})
        assert anon.status_code == 401


def test_runtimes_account_filter_is_server_side() -> None:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    with _client() as c:
        alice = _user("alice")
        _user("bob")
        assert c.post("/api/gateway/entities", json={"name": "Vesta", "spark": {**DEFAULT_SPARK_TEMPLATE, "name": "Vesta"}}).status_code == 201
        everything = c.get("/api/gateway/admin/runtimes?include_sizes=false").json()
        assert "filter" not in everything
        assert {r["runtime_id"] for r in everything["runtimes"]} >= {"default", "alice", "bob", "runtime_vesta"}

        mine = c.get("/api/gateway/admin/runtimes?account=alice&include_sizes=false").json()
        assert mine["filter"] == {"account": "alice", "tenant_id": None}
        assert [(r["kind"], r["runtime_id"]) for r in mine["runtimes"]] == [("user", "alice")]
        ent = c.get("/api/gateway/admin/runtimes?account=vesta&include_sizes=false").json()
        assert [(r["kind"], r["runtime_id"]) for r in ent["runtimes"]] == [("entity", "runtime_vesta")]
        assert c.get("/api/gateway/admin/runtimes?account=alice&tenant_id=other&include_sizes=false").json()["runtimes"] == []
        assert c.get("/api/gateway/admin/runtimes?account=nobody&include_sizes=false").json()["runtimes"] == []
        # Still admin-only.
        assert c.get("/api/gateway/admin/runtimes?account=alice", headers=alice).status_code == 403
