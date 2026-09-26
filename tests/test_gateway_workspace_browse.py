"""Session workspace browse + preview (CONTRACTS §W, amendment A-1).

Pins:
- confinement: relative paths only; `..` and absolute paths refused; a
  symlink that leads outside is hidden in a listing (counted) and refused on
  read; the gateway's marker is never listed nor served;
- the deny lists hold on every list and read; a listing limit is reported as
  `truncated`, hidden entries are counted by reason;
- content streams the full bytes with the artifact headers and honours Range;
- the root must still be allowed by the caller's CURRENT policy, and the
  gateway data folder is never served except the caller's own gateway-made
  workspace; run start refuses a data-folder workspace_root the same way;
- another user's run id is a 404 on all three routes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.workspace_browse import WorkspacePathError, confine, list_entries, normalize_rel, open_slice
from test_gateway_runs_list_endpoint import _write_min_bundle

MARKER = ".abstractgateway-workspace.json"


# ------------------------------------------------------------------ helpers (pure)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / "src" / "deep" / "b.txt").write_text("bee\n")
    (root / "notes.md").write_text("# notes\n")
    (root / MARKER).write_text('{"owner": "abstractgateway", "kind": "session_workspace"}')
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key.txt").write_text("TOPSECRET")
    os.symlink(outside, root / "escape")
    os.symlink(outside / "key.txt", root / "escape.txt")
    os.symlink(root / "notes.md", root / "inside-link.md")
    return root


def test_normalize_rel_refuses_absolute_and_parent_paths() -> None:
    assert normalize_rel("") == "" and normalize_rel(".") == "" and normalize_rel("./a//b/") == "a/b"
    for bad in ("/etc/passwd", "../x", "a/../../x", "a/..", "C:\\\\x", "C:/x"):
        with pytest.raises(WorkspacePathError) as e:
            normalize_rel(bad)
        assert e.value.status == 400, bad


def test_confine_symlink_escape_and_marker(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert confine(root, "inside-link.md")[0] == (root / "notes.md").resolve()
    for rel in ("escape", "escape/key.txt", "escape.txt"):
        with pytest.raises(WorkspacePathError) as e:
            confine(root, rel)
        assert e.value.status == 403, rel
    with pytest.raises(WorkspacePathError) as e:
        confine(root, MARKER)
    assert e.value.status == 404
    with pytest.raises(WorkspacePathError) as e:
        confine(root, "missing.txt")
    assert e.value.status == 404


def test_listing_hides_and_counts_and_reports_truncation(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    out = list_entries(root, "")
    names = [e["name"] for e in out["entries"]]
    assert names == ["src", "inside-link.md", "notes.md"], "folders first, marker and escaping links hidden"
    assert out["hidden"] == {"outside_links": 2, "blocked": 0, "other": 0}
    assert out["truncated"] is False and out["limit"] is None
    notes = next(e for e in out["entries"] if e["name"] == "notes.md")
    assert notes["type"] == "file" and notes["size_bytes"] == 8 and notes["mtime"].endswith("Z")
    assert next(e for e in out["entries"] if e["name"] == "src")["size_bytes"] is None

    rec = list_entries(root, "", recursive=True)
    assert {e["path"] for e in rec["entries"]} >= {"src/a.py", "src/deep", "src/deep/b.txt"}

    cut = list_entries(root, "", recursive=True, limit=2)
    assert cut["truncated"] is True and len(cut["entries"]) == 2 and cut["limit"] == 2

    blocked = (root / "src").resolve()
    b = list_entries(root, "", is_blocked=lambda p: str(p).startswith(str(blocked)))
    assert "src" not in [e["name"] for e in b["entries"]] and b["hidden"]["blocked"] == 1
    with pytest.raises(WorkspacePathError) as e:
        list_entries(root, "src", is_blocked=lambda p: str(p).startswith(str(blocked)))
    assert e.value.status == 404, "a blocked folder is answered like a missing one"
    with pytest.raises(WorkspacePathError) as e:
        list_entries(root, "notes.md")
    assert e.value.status == 400


def test_slices_and_ranges(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    whole = open_slice(root, "src/a.py")
    assert whole.partial is False and b"".join(whole.iter_bytes()) == b"print('a')\n"
    h = whole.headers()
    assert h["Content-Security-Policy"] == "sandbox" and h["X-Content-Type-Options"] == "nosniff"
    assert h["Content-Disposition"] == 'inline; filename="a.py"' and h["Accept-Ranges"] == "bytes"
    part = open_slice(root, "src/a.py", range_header="bytes=2-4")
    assert part.partial and b"".join(part.iter_bytes()) == b"int" and part.headers()["Content-Range"] == "bytes 2-4/11"
    tail = open_slice(root, "src/a.py", range_header="bytes=-3")
    assert b"".join(tail.iter_bytes()) == b"a')\n"[-3:]
    with pytest.raises(WorkspacePathError) as e:
        open_slice(root, "src/a.py", range_header="bytes=50-60")
    assert e.value.status == 416
    with pytest.raises(WorkspacePathError) as e:
        open_slice(root, "src")
    assert e.value.status == 400
    with pytest.raises(WorkspacePathError) as e:
        open_slice(root, "src/a.py", is_blocked=lambda p: True)
    assert e.value.status == 404


# ------------------------------------------------------------------ routes


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, peer: str = "127.0.0.1") -> tuple[TestClient, dict]:
    bundles = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles, bundle_id="ws-bundle", flow_id="root")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)
    from abstractgateway.app import app

    return TestClient(app, client=(peer, 50123)), {"Authorization": "Bearer t"}


def _start(client: TestClient, h: dict, **input_data) -> str:
    res = client.post("/api/gateway/runs/start", headers=h,
                      json={"bundle_id": "ws-bundle", "flow_id": "root", "session_id": "chat-1", "input_data": input_data})
    assert res.status_code == 200, res.text
    return res.json()["run_id"]


def test_session_workspace_routes_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        rid = _start(client, h)
        info = client.get(f"/api/gateway/runs/{rid}/workspace", headers=h)
        assert info.status_code == 200, info.text
        body = info.json()
        root = Path(body["workspace_root"])
        assert root.is_absolute() and root.name.startswith("session-") and body["kind"] == "session"
        assert body["exists"] is True and body["session_id"] == "chat-1"
        assert body["host"]["hostname"] and body["host"]["caller_is_this_machine"] is True
        assert body["open_supported"] is True, "admin at this machine"

        (root / "report.md").write_text("# hello\n" * 3)
        (root / "sub").mkdir()
        (root / "sub" / "x.bin").write_bytes(bytes(range(256)))

        files = client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()
        assert [e["name"] for e in files["entries"]] == ["sub", "report.md"], "the marker is hidden"
        assert files["truncated"] is False
        sub = client.get(f"/api/gateway/runs/{rid}/workspace/files?path=sub", headers=h).json()
        assert sub["entries"][0]["path"] == "sub/x.bin" and sub["entries"][0]["size_bytes"] == 256
        cut = client.get(f"/api/gateway/runs/{rid}/workspace/files?recursive=true&limit=1", headers=h).json()
        assert cut["truncated"] is True and len(cut["entries"]) == 1

        full = client.get(f"/api/gateway/runs/{rid}/workspace/content?path=sub/x.bin", headers=h)
        assert full.status_code == 200 and full.content == bytes(range(256))
        assert full.headers["content-security-policy"] == "sandbox" and full.headers["x-content-type-options"] == "nosniff"
        assert full.headers["content-disposition"].startswith("inline")
        part = client.get(f"/api/gateway/runs/{rid}/workspace/content?path=sub/x.bin", headers={**h, "Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == bytes(range(10, 20))
        assert part.headers["content-range"] == "bytes 10-19/256"
        bad_range = client.get(f"/api/gateway/runs/{rid}/workspace/content?path=sub/x.bin", headers={**h, "Range": "bytes=999-"})
        assert bad_range.status_code == 416

        for rel, code in (("../x", 400), ("/etc/passwd", 400), (MARKER, 404), ("nope.txt", 404)):
            r = client.get(f"/api/gateway/runs/{rid}/workspace/content", params={"path": rel}, headers=h)
            assert r.status_code == code, (rel, r.status_code, r.text)
        os.symlink("/etc", root / "etc-link")
        r = client.get(f"/api/gateway/runs/{rid}/workspace/content?path=etc-link/hosts", headers=h)
        assert r.status_code == 403
        listed = client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()
        assert "etc-link" not in [e["name"] for e in listed["entries"]] and listed["hidden"]["outside_links"] == 1

        # The deny list binds at browse time too.
        ok = client.post("/api/gateway/admin/runtime-config", headers=h, json={"workspace_blocked_paths": [str(root / "sub")]})
        assert ok.status_code == 200, ok.text
        listed = client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()
        assert "sub" not in [e["name"] for e in listed["entries"]] and listed["hidden"]["blocked"] == 1
        assert client.get(f"/api/gateway/runs/{rid}/workspace/content?path=sub/x.bin", headers=h).status_code == 404

        assert client.get("/api/gateway/runs/nope/workspace", headers=h).status_code == 404


def test_caller_on_another_machine_gets_no_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch, peer="192.0.2.10")
    with client:
        rid = _start(client, h)
        body = client.get(f"/api/gateway/runs/{rid}/workspace", headers=h).json()
        assert body["host"]["caller_is_this_machine"] is False and body["open_supported"] is False
        assert Path(body["workspace_root"]).is_absolute(), "the path + host name are always shown"


def test_data_folder_is_never_a_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    data = (tmp_path / "runtime").resolve()
    with client:
        first = _start(client, h)
        session_ws = Path(client.get(f"/api/gateway/runs/{first}/workspace", headers=h).json()["workspace_root"])
        # Refused at start: the data folder itself, a folder in it, an unmarked folder under workspaces.
        (data / "workspaces" / "not-made-by-gateway").mkdir(parents=True, exist_ok=True)
        for bad in (data, data / "auth", data / "workspaces", data / "workspaces" / "not-made-by-gateway"):
            r = client.post("/api/gateway/runs/start", headers=h,
                            json={"bundle_id": "ws-bundle", "flow_id": "root", "input_data": {"workspace_root": str(bad)}})
            assert r.status_code == 400 and "data folder" in r.json()["detail"], (bad, r.text)
        # Echoing the caller's own gateway-made folder back is accepted.
        again = client.post("/api/gateway/runs/start", headers=h,
                            json={"bundle_id": "ws-bundle", "flow_id": "root", "session_id": "chat-1",
                                  "input_data": {"workspace_root": str(session_ws)}})
        assert again.status_code == 200, again.text
        # ...but not into another conversation (the folder is derived from the session, not a marker).
        other = client.post("/api/gateway/runs/start", headers=h,
                            json={"bundle_id": "ws-bundle", "flow_id": "root", "session_id": "chat-2",
                                  "input_data": {"workspace_root": str(session_ws)}})
        assert other.status_code == 400 and "data folder" in other.json()["detail"]

        # A run whose stored root points into the data folder is not served.
        from abstractgateway.service import get_gateway_service

        rs = get_gateway_service().host.run_store
        run = rs.load(first)
        run.vars["workspace_root"] = str(data)
        rs.save(run)
        r = client.get(f"/api/gateway/runs/{first}/workspace/files", headers=h)
        assert r.status_code == 403 and "data folder" in r.json()["detail"]


def test_a_launch_folder_that_contains_the_data_folder_never_serves_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review B1: root = the PARENT of the gateway data folder. The data
    folder is hidden from listings and 404 on read; the run's tools are
    denied it too."""
    client, h = _client(tmp_path, monkeypatch)
    with client:
        rid = _start(client, h, workspace_root=str(tmp_path))
        listing = client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()
        names = [e["name"] for e in listing["entries"]]
        assert "runtime" not in names and "bundles" in names and listing["hidden"]["blocked"] >= 1
        deep = client.get(f"/api/gateway/runs/{rid}/workspace/files?recursive=true", headers=h).json()
        assert not any(e["path"].startswith("runtime") for e in deep["entries"])
        for rel in ("runtime/config/runtime_config.json", "runtime/.workflow_policy_secret", "runtime"):
            r = client.get(f"/api/gateway/runs/{rid}/workspace/content", params={"path": rel}, headers=h)
            assert r.status_code in (400, 404), (rel, r.status_code)
        assert client.get(f"/api/gateway/runs/{rid}/workspace/files?path=runtime", headers=h).status_code == 404
        from abstractgateway.service import get_gateway_service

        vars_ = get_gateway_service().host.run_store.load(rid).vars
        denied = vars_.get("workspace_builtin_deny_prefixes") or []
        assert str((tmp_path / "runtime").resolve()) in denied, denied


def test_builtin_deny_list_hides_credential_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling 2026-09-26: ~/.ssh and friends are never served, and runs'
    tools are denied them by default (an admin may turn the runs part off)."""
    client, h = _client(tmp_path, monkeypatch)
    home = Path.home()
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE")
    (home / "notes.txt").write_text("ok")
    with client:
        rid = _start(client, h, workspace_root=str(home))
        names = [e["name"] for e in client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()["entries"]]
        assert ".ssh" not in names and "notes.txt" in names
        r = client.get(f"/api/gateway/runs/{rid}/workspace/content?path=.ssh/id_ed25519", headers=h)
        assert r.status_code == 404
        from abstractgateway.service import get_gateway_service

        denied = get_gateway_service().host.run_store.load(rid).vars.get("workspace_builtin_deny_prefixes") or []
        assert str((home / ".ssh").resolve()) in denied
        cfg = client.get("/api/gateway/admin/runtime-config", headers=h).json()["builtin_deny"]
        assert cfg["enabled"] is True and str((home / ".ssh").resolve()) in cfg["value"]
        assert client.post("/api/gateway/admin/runtime-config", headers=h, json={"workspace_builtin_deny": False}).status_code == 200
        rid2 = _start(client, h, workspace_root=str(home))
        vars2 = get_gateway_service().host.run_store.load(rid2).vars
        assert "workspace_builtin_deny_prefixes" not in vars2, "the admin turned the runs part off"
        # Browse still refuses (for every principal).
        assert client.get(f"/api/gateway/runs/{rid2}/workspace/content?path=.ssh/id_ed25519", headers=h).status_code == 404


def test_a_forged_marker_proves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review S4/S5: kind comes from the gateway's knowledge; the marker is
    hidden by identity (a link to it under another name is hidden too)."""
    client, h = _client(tmp_path, monkeypatch)
    launch = tmp_path / "project"
    launch.mkdir()
    (launch / MARKER).write_text('{"owner": "abstractgateway", "kind": "session_workspace", "first_run_id": "x"}')
    os.symlink(launch / MARKER, launch / "alias.json")
    (launch / "a.txt").write_text("a")
    with client:
        rid = _start(client, h, workspace_root=str(launch))
        body = client.get(f"/api/gateway/runs/{rid}/workspace", headers=h).json()
        assert body["kind"] == "launch_folder"
        names = [e["name"] for e in client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h).json()["entries"]]
        assert names == ["a.txt"]
        assert client.get(f"/api/gateway/runs/{rid}/workspace/content?path=alias.json", headers=h).status_code == 404


def test_the_opened_file_is_verified(tmp_path: Path) -> None:
    """Review S6: open without following a final link, and check the OPENED
    file is inside the root."""
    from abstractgateway.workspace_browse import _open_verified

    root = _tree(tmp_path)
    fd = _open_verified((root / "notes.md").resolve(), root)
    os.close(fd)
    with pytest.raises(WorkspacePathError) as e:
        _open_verified(root / "escape.txt", root)  # a link as the final component
    assert e.value.status == 403
    with pytest.raises(WorkspacePathError):
        _open_verified((tmp_path / "secret" / "key.txt").resolve(), root)  # a real file outside


def test_launch_folder_needs_the_current_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    launch = tmp_path / "project"
    launch.mkdir()
    (launch / "main.py").write_text("x = 1\n")
    with client:
        rid = _start(client, h, workspace_root=str(launch))
        body = client.get(f"/api/gateway/runs/{rid}/workspace", headers=h).json()
        assert body["kind"] == "launch_folder" and body["workspace_root"] == str(launch.resolve())
        assert client.get(f"/api/gateway/runs/{rid}/workspace/content?path=main.py", headers=h).content == b"x = 1\n"
        # The admin withdraws launch-folder trust: the folder is no longer served.
        assert client.post("/api/gateway/admin/runtime-config", headers=h, json={"trust_client_launch_folder": False}).status_code == 200
        r = client.get(f"/api/gateway/runs/{rid}/workspace/files", headers=h)
        assert r.status_code == 403 and "policy" in r.json()["detail"]


def test_another_users_run_is_404_on_all_three_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundles = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles, bundle_id="ws-bundle", flow_id="root")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway.app import app

    def user(client: TestClient, uid: str) -> dict:
        r = client.post("/api/gateway/admin/users", headers={"Authorization": "Bearer admin-token"},
                        json={"user_id": uid, "tenant_id": "default", "roles": ["user"], "runtime_id": uid})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    client = TestClient(app, client=("127.0.0.1", 50123))
    with client:
        alice, bob = user(client, "alice"), user(client, "bob")
        _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / "alice" / "flows", bundle_id="ws-bundle", flow_id="root")
        start = client.post("/api/gateway/runs/start", headers=alice,
                            json={"bundle_id": "ws-bundle", "flow_id": "root", "session_id": "a", "input_data": {}})
        assert start.status_code == 200, start.text
        rid = start.json()["run_id"]
        mine = client.get(f"/api/gateway/runs/{rid}/workspace", headers=alice)
        assert mine.status_code == 200, mine.text
        assert mine.json()["open_supported"] is False, "not an admin"
        root = Path(mine.json()["workspace_root"])
        (root / "private.txt").write_text("alice only")
        assert client.get(f"/api/gateway/runs/{rid}/workspace/content?path=private.txt", headers=alice).content == b"alice only"
        for route in ("workspace", "workspace/files", "workspace/content?path=private.txt"):
            r = client.get(f"/api/gateway/runs/{rid}/{route}", headers=bob)
            assert r.status_code == 404, (route, r.status_code, r.text)


def test_builtin_deny_is_prefixes_and_the_prompt_is_stable_while_the_data_folder_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW/16 (2026-09-26): the built-in rule is the data folder + the
    credential folders as PREFIXES and one allow entry (the run's own folder),
    never an enumeration of what the data folder holds. Two turns of one
    session with files added to the data folder between them get the same
    entries and a byte-identical workspace section of the system prompt; the
    tool sandbox still refuses the data folder and serves the run's folder."""
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import (
        WorkspaceScope,
        describe_workspace_scope,
        rewrite_tool_arguments,
    )

    client, h = _client(tmp_path, monkeypatch)
    data = (tmp_path / "runtime").resolve()
    with client:
        from abstractgateway.service import get_gateway_service

        rs = get_gateway_service().host.run_store
        first = _start(client, h, workspace_access_mode="all_except_ignored",
                       workspace_ignored_paths=str(tmp_path / "operator-said-no"))
        # The data folder grows between the turns (runs, ledgers, other sessions).
        for i in range(40):
            (data / f"run_extra_{i}.json").write_text("{}")
        other_ws = data / "workspaces" / "session-someone-else"
        other_ws.mkdir(parents=True, exist_ok=True)
        (other_ws / "secret.txt").write_text("theirs")
        second = _start(client, h, workspace_access_mode="all_except_ignored",
                        workspace_ignored_paths=str(tmp_path / "operator-said-no"))

        v1, v2 = rs.load(first).vars, rs.load(second).vars
        own = Path(v1["workspace_root"])
        assert own.parent.parent == data and v2["workspace_root"] == v1["workspace_root"], "one session, one folder"
        assert v1["workspace_builtin_deny_prefixes"] == v2["workspace_builtin_deny_prefixes"]
        assert v1["workspace_builtin_deny_prefixes"][0] == str(data)
        assert all(not p.startswith(str(data) + os.sep) for p in v1["workspace_builtin_deny_prefixes"]), (
            "nothing INSIDE the data folder is listed: no enumeration"
        )
        assert v1["workspace_builtin_allow"] == v2["workspace_builtin_allow"] == [str(own)]
        # The operator's own entry is kept as it was, and is the only ignored path.
        assert str(v1["workspace_ignored_paths"]).splitlines() == [str(tmp_path / "operator-said-no")]

        p1 = describe_workspace_scope(WorkspaceScope.from_input_data(v1))
        p2 = describe_workspace_scope(WorkspaceScope.from_input_data(v2))
        assert p1 == p2, "the workspace section of the system prompt is byte-identical across turns"
        assert str(data) not in p1.replace(str(own), ""), "no data-folder path other than the run's own is shown"
        assert "session-someone-else" not in p1

        # Enforced: the session run reads its own folder (inside the denied data folder)...
        scope = WorkspaceScope.from_input_data(v2)
        (own / "notes.txt").write_text("mine")
        ok = rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(own / "notes.txt")}, scope=scope)
        assert Path(ok["file_path"]).resolve() == (own / "notes.txt").resolve()
        # ...and a run whose launch folder CONTAINS the data folder is refused
        # everything in it by the host rule (not merely by the workspace bound).
        wide = _start(client, h, workspace_root=str(tmp_path))
        vw = rs.load(wide).vars
        assert "workspace_builtin_allow" not in vw
        wide_scope = WorkspaceScope.from_input_data(vw)
        for denied in (data / "run_extra_3.json", other_ws / "secret.txt", data / "auth"):
            with pytest.raises(ValueError, match="protected by the host"):
                rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(denied)}, scope=wide_scope)
        (tmp_path / "bundles" / "readme.txt").write_text("fine")
        ok = rewrite_tool_arguments(tool_name="read_file", args={"file_path": "bundles/readme.txt"}, scope=wide_scope)
        assert Path(ok["file_path"]).resolve() == (tmp_path / "bundles" / "readme.txt").resolve()
        assert describe_workspace_scope(wide_scope).count(str(data)) == 0, "the rule is enforced, not described"


def test_a_client_cannot_send_the_hosts_builtin_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        rid = _start(client, h, workspace_builtin_allow=["/"], workspace_builtin_deny_prefixes=[])
        from abstractgateway.service import get_gateway_service

        v = get_gateway_service().host.run_store.load(rid).vars
        assert v["workspace_builtin_allow"] == [v["workspace_root"]], "the client's allow entry is dropped"
        assert str((tmp_path / "runtime").resolve()) in v["workspace_builtin_deny_prefixes"]
        # A run outside the data folder gets NO allow entry: the client's is dropped, not kept.
        wide = _start(client, h, workspace_root=str(tmp_path), workspace_builtin_allow=["/"])
        assert "workspace_builtin_allow" not in get_gateway_service().host.run_store.load(wide).vars
        # With the rule turned off by an admin, a client still cannot set it.
        assert client.post("/api/gateway/admin/runtime-config", headers=h, json={"workspace_builtin_deny": False}).status_code == 200
        off = _start(client, h, workspace_builtin_allow=["/"], workspace_builtin_deny_prefixes=["/nothing"])
        v_off = get_gateway_service().host.run_store.load(off).vars
        assert "workspace_builtin_allow" not in v_off and "workspace_builtin_deny_prefixes" not in v_off


def test_the_data_folder_enumeration_helper_is_gone() -> None:
    """REVIEW/19 G3: `data_dir_tool_deny` listed the data folder's contents
    into every run's deny list (REVIEW/16's prompt growth). It must not come
    back: the built-in rule is whole-folder prefixes only."""
    import abstractgateway.routes.gateway as gw
    import abstractgateway.workspace_browse as wb

    assert not hasattr(wb, "data_dir_tool_deny")
    assert "data_dir_tool_deny" not in Path(gw.__file__).read_text()
