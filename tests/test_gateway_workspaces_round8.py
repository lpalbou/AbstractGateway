"""Round 8 (DESIGN.md R8.2) server and markup contract, always on (the browser proofs are
test_gateway_console_browser_{workspaces,accounts}.py, opt-in):

- POST /workspace/path-check: the Workspaces page's folder rows are checked with the SAME rules the
  policy writes enforce (absolute, existing directory) and say why in plain words.
- GET /admin/runtimes?account=<id>: the Accounts Runtime link's filter is the server's.
- The console: "Workspaces" right after Accounts; Accounts = Name · Email · Runtime · Active ·
  Actions with icon actions (no "⋯" menu); the old workspace disclosure and modal are gone.
"""

from __future__ import annotations

import re
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
            ("", "Type a folder path."),
            ("relative/folder", "Use a full path that starts with / (or ~ for the gateway's home folder)."),
            (str(tmp_path / "missing"), "No folder at this path on the gateway's computer."),
            (str(tmp_path / "notes.txt"), "This is a file, not a folder."),
        ):
            out = check(path)
            assert out["valid"] is False and out["sentence"] == sentence, (path, out)
            # The write refuses exactly what the check refuses (one rule, two doors).
            w = c.post("/api/gateway/admin/runtime-config", json={"workspace_allowed_paths": [path] if path else ["  "]})
            if path:
                assert w.status_code == 400, (path, w.text)
        assert c.post("/api/gateway/admin/runtime-config", json={"workspace_allowed_paths": [str(folder)]}).status_code == 200
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


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def test_console_markup_round8_accounts_and_workspaces() -> None:
    html = _html()
    nav = html[html.index('<nav class="shell_nav"') : html.index("</nav>")]
    order = re.findall(r'id="tab-button-([a-z]+)"', nav)
    assert order[:2] == ["users", "workspaces"], order
    assert '<span class="shell_nav_label">Workspaces</span>' in nav
    assert 'id="tab-workspaces"' in html and 'id="workspaces-root"' in html
    # Accounts: ONE Email column; the policy left the page (no disclosure, no modal).
    assert "<th>Name</th><th>Email</th><th>Runtime</th><th>Active</th><th>Actions</th>" in html
    for gone in ('id="my-workspace-policy-section"', 'id="workspace-policy-modal-backdrop"', 'id="wsp-mode-cards"', "openWorkspacePolicyModal", "openGatewayPolicyModal"):
        assert gone not in html, gone
    render = html[html.index("function renderAccounts(rows)") : html.index("function askRotateAccount(")]
    # Icon actions only: no kit menu, no "⋯", every action through accountIconButton with a tooltip.
    assert "af-menu" not in render and "⋯" not in render and "accountMenu(" not in html
    for action in ('"email", "mail", "Email"', '"openai_api", "openai"', '"logs", "logs", "Logs"', '"workspace", "folder", "Workspace"',
                   '"manage", "manage", "Manage"', '"rotate", "rotate", "Rotate token"', '"archive", "archive", "Archive"', '"unarchive", "unarchive", "Unarchive"'):
        assert action in render, action
    # The Runtime cell links to the filtered Runtimes page; the Workspace icon to the Workspaces page.
    assert "runtimesHref({ account: a.id" in html and "openWorkspacesFor({ account: a.id" in html
    assert 'id="runtimes-filter"' in html and "/api/gateway/admin/runtimes${filter ?" in html
    # Rows apply on blur; the path check runs first; no Save button on the page.
    assert "/api/gateway/workspace/path-check" in html and "input.onblur = async" in html
    assert re.search(r"\.icon-btn\[data-tip\]::after\s*\{[^}]*content: attr\(data-tip\)", html)
