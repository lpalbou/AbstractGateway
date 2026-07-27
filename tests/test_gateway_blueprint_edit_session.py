"""Blueprint-edit P0 reproduction (framework c4779: operator CANNOT edit the
blueprint through the app, only curl-with-admin-token works).

Reproduces the operator's path — a BROWSER SESSION (cookie), not a bearer
token — against PUT /api/gateway/entities/spec/phases, and localizes the
block: session-authenticated WRITES require the CSRF header (bearer tokens
bypass it). A UI that omits the header gets 403 csrf_required and no overlay
ever lands — exactly the "no edit has ever landed" symptom. The fix half is
the editor client SENDING the header; this pins the gateway contract so the
diagnosis is proven, not asserted, and the write path is shown to work.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from abstractgateway.security.sessions import (
    gateway_csrf_cookie_name,
    gateway_csrf_header_name,
    gateway_session_cookie_name,
)

_ADMIN_TOKEN = "blueprint-admin-token"


@pytest.fixture(autouse=True)
def _multiuser(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    # User-auth posture (the operator's live shape: browser sessions, CSRF).
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    # Register the admin user whose token the browser session logs in with.
    from abstractgateway.users import GatewayUserRegistry

    reg = GatewayUserRegistry()
    _rec, token = reg.create_user(user_id="admin", roles=["admin", "user"])
    globals()["_ADMIN_USER_TOKEN"] = token


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app)


def _login(client: TestClient) -> tuple[str, str]:
    """Log in as the operator would through the app; returns (session_cookie,
    csrf_token) — the two secrets the browser then holds."""
    r = client.post(
        "/api/gateway/session/login",
        json={"user_id": "admin", "token": globals()["_ADMIN_USER_TOKEN"]},
    )
    assert r.status_code == 200, r.text
    session_cookie = r.cookies.get(gateway_session_cookie_name())
    csrf = r.cookies.get(gateway_csrf_cookie_name())
    assert session_cookie and csrf
    return session_cookie, csrf


def _valid_tunables(client: TestClient, session_cookie: str) -> dict:
    """A minimal in-bounds tunables patch derived from the live spec."""
    r = client.get(
        "/api/gateway/entities/spec/phases",
        cookies={gateway_session_cookie_name(): session_cookie},
    )
    assert r.status_code == 200, r.text
    # Pick one known numeric tunable from the served meta; fall back to a
    # commonly-present one. The spec exposes tunables + tunables_meta.
    spec = r.json()
    # Served shape: effective_tunables at top level; spec.tunables is the raw.
    tun = spec.get("effective_tunables") or (spec.get("spec") or {}).get("tunables") or {}
    # Real flat numeric dials (from the packaged spec), sent at an in-bounds
    # value so the route's known-keys+bounds validation accepts the patch.
    for key, val in (("sleep_bound_h", 1.0), ("unattended_wake_cadence_h", 6.0),
                     ("grant_unused_floor_h", 2.0)):
        if key in tun and isinstance(tun[key], (int, float)):
            return {key: val}
    pytest.skip("no numeric tunable available to exercise the edit path")


def test_operator_session_write_without_csrf_is_the_block() -> None:
    """THE REPRODUCTION: a session PUT with NO CSRF header is refused 403
    csrf_required — the operator's browser edit silently fails here and no
    overlay lands (bearer-token curl bypasses CSRF, which is why THAT works)."""
    with _client() as client:
        session_cookie, _csrf = _login(client)
        patch = _valid_tunables(client, session_cookie)
        r = client.put(
            "/api/gateway/entities/spec/phases",
            cookies={gateway_session_cookie_name(): session_cookie},
            json={"tunables": patch},
        )
        assert r.status_code == 403, r.text
        assert r.json().get("reason_code") == "csrf_required"


def test_operator_session_write_with_csrf_lands_the_overlay() -> None:
    """THE FIX CONTRACT: the SAME session write WITH the CSRF header succeeds
    and the overlay file lands — proving the write path works from the
    operator's chair once the editor sends the header (the editor's half)."""
    import json
    import os

    with _client() as client:
        session_cookie, csrf = _login(client)
        patch = _valid_tunables(client, session_cookie)
        r = client.put(
            "/api/gateway/entities/spec/phases",
            cookies={
                gateway_session_cookie_name(): session_cookie,
                gateway_csrf_cookie_name(): csrf,
            },
            headers={gateway_csrf_header_name(): csrf},
            json={"tunables": patch},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert int(body.get("edit_seq") or 0) >= 1
        # GET-symmetric overlay block (entity c4828): the save note renders
        # "edit N" from a PRESENT field, never an inferred dangling "?"
        # (the lying-save-note class cured at the source).
        assert body.get("operator_edited") is True
        ov = body.get("overlay") or {}
        assert int(ov.get("edit_seq") or 0) == int(body["edit_seq"])
        assert ov.get("edited_by") == "person:admin"
        assert "edited_at" in ov
        # The overlay file must exist now — the "no edit has ever landed"
        # symptom is cured once the header rides.
        data_dir = os.environ["ABSTRACTGATEWAY_DATA_DIR"]
        overlay = os.path.join(data_dir, "config", "entity_phases_overlay.json")
        assert os.path.exists(overlay), "overlay must land after a CSRF'd session edit"
        saved = json.load(open(overlay))
        assert int(saved.get("edit_seq") or 0) >= 1
        assert saved.get("edited_by") == "person:admin"


def test_bearer_token_write_bypasses_csrf_as_expected() -> None:
    """Why curl-with-admin-token 'works': a bearer principal is not a browser
    session, so the CSRF gate does not apply — the asymmetry framework
    observed, pinned so the fix never accidentally breaks the token lane."""
    import os

    from abstractgateway.users import GatewayUserRegistry  # noqa: F401

    # Static operator token principal (bearer) — set the env token.
    os.environ["ABSTRACTGATEWAY_AUTH_TOKEN"] = _ADMIN_TOKEN
    try:
        with _client() as client:
            # A session login only to read a valid tunable for the patch.
            session_cookie, _csrf = _login(client)
            patch = _valid_tunables(client, session_cookie)
            r = client.put(
                "/api/gateway/entities/spec/phases",
                headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                json={"tunables": patch},
            )
            assert r.status_code == 200, r.text
    finally:
        os.environ.pop("ABSTRACTGATEWAY_AUTH_TOKEN", None)
