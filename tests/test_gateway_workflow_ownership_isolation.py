"""The shared workflow registry belongs to the operator.

THE HOLE: `/bundles/upload`, `/bundles/{id}` (DELETE), `/bundles/reload`, the
deprecation routes and `/visualflows/{id}/publish` each build a
`WorkflowBundleRegistry` over `host.bundles_dir` themselves. They are
deliberately user-level writes, justified by each principal owning its own
bundles dir — but with hosted user auth OFF there is only ONE dir, the
operator's, and `/session/login` authenticates registry users regardless of
auth mode. A non-admin could therefore delete or overwrite the workflows every
user shares, and `/visualflows/publish` could overwrite them by bundle id.

Every test here drives the REAL app over HTTP with a REAL non-admin principal,
because the previous version of this file asserted helper return values and
kept passing with the fix reverted.

TWO LINES OF DEFENCE (mission BB, 2026-09-24). The FIRST line now closes the
lane at the door: with user accounts off a non-admin registry identity can no
longer hold a session at all (security/sessions.py
`principal_barred_from_shared_runtime`; pinned in
test_gateway_session_user_accounts_off.py and by
`test_the_first_line_refuses_the_non_admin_at_sign_in` below). The registry
gate this file exists for is the SECOND, independent line; to keep proving it
on its own, `shared_gateway` knocks the first line out — exactly the
regression the second line is there to survive.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_workflow_registry_honesty import _min_flow, _write_bundle  # noqa: F401

ADMIN_TOKEN = "admin-token"


@pytest.fixture()
def shared_gateway(tmp_path, monkeypatch):
    """A single-user gateway (hosted user auth OFF) with a user registry.

    This is the configuration the hole lived in: registry identities exist and
    can sign in, while every request resolves to the ONE shared service.
    """
    import abstractgateway.config as cfg

    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "basic-agent.flow").write_bytes((Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes())
    _write_bundle(flows / "shared-wf@1.0.0.flow", bundle_id="shared-wf", bundle_version="1.0.0")

    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MULTI_USER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE", raising=False)
    _knock_out_the_first_line(monkeypatch)
    return flows


def _knock_out_the_first_line(monkeypatch) -> None:
    """Simulate a regression of the sign-in rule so the registry gate is
    tested ALONE (both import sites: the session store and the login route)."""
    import abstractgateway.routes.gateway as g
    import abstractgateway.security.sessions as sessions

    monkeypatch.setattr(sessions, "principal_barred_from_shared_runtime", lambda *_a, **_k: False)
    monkeypatch.setattr(g, "principal_barred_from_shared_runtime", lambda *_a, **_k: False)


def _mint_user(tmp_path, monkeypatch, *, user_id: str, roles: list[str]) -> str:
    """A real registry user + token, exactly as the console's create-user does."""
    from abstractgateway.users import GatewayUserRegistry

    registry_path = tmp_path / "runtime" / "auth" / "users.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(registry_path))

    reg = GatewayUserRegistry()
    _record, token = reg.create_user(user_id=user_id, roles=roles, scopes=["*"], runtime_id=user_id)
    return str(token)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client, *, user_id: str, token: str) -> dict[str, str]:
    """Sign in the way the console does — a browser SESSION, not a bearer.

    This WAS the reachable lane: with hosted user auth off the bearer path only
    accepts the static gateway token, while `/session/login` authenticated
    against the user registry regardless of auth mode. It is reachable here
    only because `shared_gateway` knocked the first line out. Writes
    additionally carry the CSRF header the session issues.
    """
    from abstractgateway.security.sessions import gateway_csrf_cookie_name, gateway_csrf_header_name

    res = client.post("/api/gateway/session/login", json={"user_id": user_id, "token": token})
    assert res.status_code == 200, res.text
    csrf = client.cookies.get(gateway_csrf_cookie_name()) or ""
    assert csrf, "login issued no CSRF token"
    return {gateway_csrf_header_name(): csrf}


def test_a_non_admin_cannot_delete_a_shared_workflow(shared_gateway, tmp_path, monkeypatch):
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])
    target = shared_gateway / "shared-wf@1.0.0.flow"
    assert target.is_file()

    from abstractgateway.app import app

    with TestClient(app) as client:
        csrf = _login(client, user_id="mallory", token=token)
        me = client.get("/api/gateway/me")
        assert me.status_code == 200, me.text
        assert "admin" not in (me.json().get("roles") or []), "precondition: a NON-admin principal"

        res = client.delete("/api/gateway/bundles/shared-wf?bundle_version=1.0.0", headers=csrf)
        assert res.status_code == 403, res.text

    assert target.is_file(), "a non-admin deleted a workflow shared by every user"


def test_a_non_admin_cannot_overwrite_a_shared_workflow_by_upload(shared_gateway, tmp_path, monkeypatch):
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])
    target = shared_gateway / "shared-wf@1.0.0.flow"
    original = target.read_bytes()

    staged = tmp_path / "evil.flow"
    _write_bundle(staged, bundle_id="shared-wf", bundle_version="1.0.0", flow_id="pwned")

    from abstractgateway.app import app

    with TestClient(app) as client:
        csrf = _login(client, user_id="mallory", token=token)
        res = client.post(
            "/api/gateway/bundles/upload",
            headers=csrf,
            files={"file": ("shared-wf@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "true"},
        )
        assert res.status_code == 403, res.text

    assert target.read_bytes() == original, "a non-admin substituted a shared workflow"


def test_the_first_line_refuses_the_non_admin_at_sign_in(shared_gateway, tmp_path, monkeypatch):
    """With the first line UP (undo the fixture's knock-out), the non-admin
    never gets a session on a gateway with user accounts off."""
    monkeypatch.undo()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(shared_gateway))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MULTI_USER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE", raising=False)
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])

    from abstractgateway.app import app

    with TestClient(app) as client:
        res = client.post("/api/gateway/session/login", json={"user_id": "mallory", "token": token})
        assert res.status_code == 401, res.text
        assert res.json()["detail"]["reason_code"] == "user_accounts_off_admin_only"


@pytest.mark.parametrize("action", ["deprecate", "undeprecate"])
def test_a_non_admin_cannot_change_deprecations_in_the_shared_registry(shared_gateway, tmp_path, monkeypatch, action):
    """The deprecation doors went through the SAME registry without the gate
    (a missing bundle answered 404 = authorization passed). Both must refuse
    before touching anything, and on a bundle that exists."""
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])

    from abstractgateway.app import app

    with TestClient(app) as client:
        csrf = _login(client, user_id="mallory", token=token)
        res = client.post(f"/api/gateway/bundles/shared-wf/{action}", headers=csrf, json={"reason": "mine now"})
        assert res.status_code == 403, res.text
        missing = client.post(f"/api/gateway/bundles/no-such-bundle/{action}", headers=csrf, json={})
        assert missing.status_code == 403, missing.text
        listing = client.get("/api/gateway/bundles?include_deprecated=true", headers=_headers(ADMIN_TOKEN))
        row = [i for i in listing.json()["items"] if i["bundle_id"] == "shared-wf"]
        assert row, listing.text
        entrypoints = row[0]["entrypoints"]
        assert entrypoints, listing.text
        assert [ep["deprecated"] for ep in entrypoints] == [False] * len(entrypoints), (
            "a non-admin deprecated a workflow shared by every user"
        )


def test_the_admin_can_still_deprecate_in_the_shared_registry(shared_gateway, tmp_path):
    from abstractgateway.app import app

    admin = _headers(ADMIN_TOKEN)
    with TestClient(app) as client:
        dep = client.post("/api/gateway/bundles/shared-wf/deprecate", headers=admin, json={"reason": "old"})
        assert dep.status_code == 200, dep.text
        listing = client.get("/api/gateway/bundles?include_deprecated=true", headers=admin)
        row = [i for i in listing.json()["items"] if i["bundle_id"] == "shared-wf"]
        assert row and all(ep["deprecated"] for ep in row[0]["entrypoints"]), listing.text
        undep = client.post("/api/gateway/bundles/shared-wf/undeprecate", headers=admin, json={})
        assert undep.status_code == 200, undep.text
        assert undep.json()["removed"] is True


def test_a_non_admin_cannot_reload_the_shared_registry(shared_gateway, tmp_path, monkeypatch):
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])

    from abstractgateway.app import app

    with TestClient(app) as client:
        csrf = _login(client, user_id="mallory", token=token)
        assert client.post("/api/gateway/bundles/reload", headers=csrf).status_code == 403


def test_the_visualflow_publish_door_is_gated_too(shared_gateway, tmp_path, monkeypatch):
    """The door that made gating only `/bundles` pointless.

    `/visualflows/{id}/publish` takes an attacker-chosen bundle_id + version +
    overwrite, writes through the SAME registry, and reloads in-process.
    """
    token = _mint_user(tmp_path, monkeypatch, user_id="mallory", roles=["user"])

    from abstractgateway.app import app

    with TestClient(app) as client:
        csrf = _login(client, user_id="mallory", token=token)
        created = client.post(
            "/api/gateway/visualflows",
            headers=csrf,
            json={
                "name": "mine",
                "nodes": _min_flow("root")["nodes"],
                "edges": _min_flow("root")["edges"],
                "entryNode": "start",
            },
        )
        if created.status_code != 200:
            pytest.skip(f"visualflow authoring unavailable in this build: {created.status_code}")
        flow_id = str(created.json().get("id") or "")

        res = client.post(
            f"/api/gateway/visualflows/{flow_id}/publish",
            headers=csrf,
            json={"bundle_id": "basic-agent", "bundle_version": "9.9.9", "overwrite": True, "reload_gateway": True},
        )
        assert res.status_code == 403, res.text

    assert not (shared_gateway / "basic-agent@9.9.9.flow").exists(), (
        "a non-admin shadowed the default framework agent through the publish door"
    )


def test_the_admin_keeps_full_control_of_the_shared_registry(shared_gateway, tmp_path):
    """The gate must not cost the operator their own gateway."""
    from abstractgateway.app import app

    admin = _headers(ADMIN_TOKEN)
    staged = tmp_path / "ok.flow"
    _write_bundle(staged, bundle_id="admin-wf", bundle_version="1.0.0")

    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=admin,
            files={"file": ("admin-wf@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "true"},
        )
        assert up.status_code == 200, up.text
        assert up.json()["ok"] is True

        assert client.post("/api/gateway/bundles/reload", headers=admin).status_code == 200

        rm = client.delete("/api/gateway/bundles/admin-wf?bundle_version=1.0.0", headers=admin)
        assert rm.status_code == 200, rm.text
        assert rm.json()["removed"] == 1


def test_reads_stay_open_to_non_admins(shared_gateway, tmp_path, monkeypatch):
    """Ownership gates WRITES. Seeing the shared set is every user's business —
    they have to run those workflows."""
    token = _mint_user(tmp_path, monkeypatch, user_id="reader", roles=["user"])

    from abstractgateway.app import app

    with TestClient(app) as client:
        _login(client, user_id="reader", token=token)
        listing = client.get("/api/gateway/bundles")
        assert listing.status_code == 200, listing.text
        assert [i for i in listing.json()["items"] if i["bundle_id"] == "shared-wf"]


def test_a_per_user_registry_is_writable_by_its_owner(tmp_path, monkeypatch):
    """The gate is OWNERSHIP, not role: it must not lock users out of their own
    registry under hosted user auth, or 'users add their own workflows' dies."""
    from abstractgateway.routes.gateway import _is_shared_workflow_registry

    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "shared"))
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    import abstractgateway.config as cfg

    (tmp_path / "shared" / "basic-agent.flow").write_bytes(
        (Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes()
    )

    class _Host:
        def __init__(self, d):
            self.bundles_dir = d

    assert _is_shared_workflow_registry(_Host(tmp_path / "shared")) is True
    assert _is_shared_workflow_registry(_Host(tmp_path / "users" / "default" / "alice" / "flows")) is False


def test_an_unresolvable_registry_fails_closed(monkeypatch):
    """A configuration we cannot resolve must require admin, never hand out
    write access by defaulting to 'not shared'."""
    import abstractgateway.config as cfg
    from abstractgateway.routes import gateway as g

    class _Boom:
        @property
        def bundles_dir(self):
            return Path("/nonexistent/whatever")

    def _explode():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(cfg.GatewayHostConfig, "from_env", staticmethod(_explode))
    assert g._is_shared_workflow_registry(_Boom()) is True
