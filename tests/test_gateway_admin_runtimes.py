"""The runtimes-first admin surface (operator order 2026-07-14 12:24:
"runtime tab = I SEE THE RUNTIMES FIRST ... when i click on a runtime, i do
see the associated sessions").

GET /admin/runtimes = the inventory (default + per-user + per-entity planes,
owners joined, entity liveness riding); GET .../runs = the lazy drill-in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "admin-runtimes-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    return {**DEFAULT_SPARK_TEMPLATE, "name": name}


def test_inventory_lists_default_and_entity_planes_with_liveness() -> None:
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Vesta", "spark": _spark("Vesta")}).status_code == 201

        r = client.get("/api/gateway/admin/runtimes")
        assert r.status_code == 200, r.text
        rows = r.json()["runtimes"]
        kinds = {row["kind"] for row in rows}
        assert "default" in kinds and "entity" in kinds

        ent = next(row for row in rows if row["kind"] == "entity")
        assert ent["runtime_id"] == "runtime_vesta"
        assert ent["entity"] == "vesta"
        # Newborn = sleep (c1503); sleep is ALIVE on the liveness axis (c1559).
        assert ent["state"] == "asleep" and ent["liveness"] == "alive"
        assert isinstance(ent.get("size_bytes"), int)

        default = next(row for row in rows if row["kind"] == "default")
        assert default["runtime_id"] == "default"


def test_drill_in_serves_entity_runs_and_404s_unknown_planes() -> None:
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Norns", "spark": _spark("Norns")}).status_code == 201

        # Entity plane: newborn = no runs yet, but the plane answers.
        r = client.get("/api/gateway/admin/runtimes/entity/default/runtime_norns/runs")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entity"] == "norns"
        assert body["items"] == []

        # Default plane answers too.
        d = client.get("/api/gateway/admin/runtimes/default/default/default/runs")
        assert d.status_code == 200 and isinstance(d.json()["items"], list)

        # Unknown planes are honest 404s, never empty-list fakes.
        assert client.get("/api/gateway/admin/runtimes/entity/default/runtime_ghost/runs").status_code == 404
        assert client.get("/api/gateway/admin/runtimes/user/default/ghost/runs").status_code == 404
        assert client.get("/api/gateway/admin/runtimes/warp/default/x/runs").status_code == 404


def test_inventory_requires_authentication() -> None:
    from abstractgateway.app import app

    with TestClient(app) as anon:
        assert anon.get("/api/gateway/admin/runtimes").status_code in (401, 403)


def test_console_runtimes_tab_order_and_creation_modals() -> None:
    """Markup pins for the operator's 12:24 order: runtimes FIRST on the
    Runtimes tab (retained demoted to a disclosure at the bottom); creation
    is progressive-disclosure modals; entities never render inside the users
    table (separate roster + honest count note)."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()

    # Runtimes tab order (console-TUI mirror, laurent dm#35): runtimes list
    # -> inspect pane (teaching line until chosen; then Sessions | Data &
    # cache tabs) -> retained (last). No separate runs section: the chosen
    # runtime's runs ARE the Sessions tab, and the data homes ride inside the
    # runtimes list (the standalone data-homes section was folded in).
    order = [
        'id="runtimes-section"',
        'id="runtime-detail-section"',
        'id="runtime-reservations-section"',
    ]
    positions = [html.index(n) for n in order]
    assert positions == sorted(positions), "runtimes tab section order broken"
    # Demoted, not deleted: retained runtimes are a session-only section.
    assert '<section id="runtime-reservations-section" class="session-only hidden">' in html

    # Creation modals exist; the old always-visible inline forms are gone.
    for needle in (
        'id="user-create-backdrop"',
        'id="entity-create-backdrop"',
        'id="templates-backdrop"',
        'id="open-create-user"',
        'id="open-create-entity"',
        'id="open-templates"',
    ):
        assert needle in html, needle

    # Roles is a dropdown of the accepted vocabulary (entity is door-assigned,
    # deliberately not offered).
    assert '<select id="new-roles"' in html
    assert 'value="user" selected' in html and 'value="admin"' in html and 'value="readonly"' in html
    assert 'value="entity"' not in html

    # The users table has no Tenant column (demoted to Advanced in the modal)
    # and carries the entity-principal count note hook.
    assert 'id="users-entity-note"' in html

    # Shared abstractuic dialogue classes ride the console conversations.
    assert "pc-chat-item" in html and "pc-chat-thread" in html
