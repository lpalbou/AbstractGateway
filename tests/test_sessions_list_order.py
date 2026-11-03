"""ADR 0048, sessions: `GET /runs?root_only=true` serves turns most recently updated first, so a
client folding the rows by `session_id` (first row of a session = its place) lists the session
with the latest activity first, and a new turn in an older session moves that session to the top.
Archived sessions stay out of the listing (their own `archived_only=true` listing)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, gateway_env, save_runs


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def _turn(session_id: str, at: str):
    run = chat_run(session_id=session_id, created_at=at)
    run.updated_at = at
    return run


def _sessions(c: TestClient, query: str = "") -> list:
    r = c.get(f"/api/gateway/runs?root_only=true&limit=50{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    order: list = []
    for row in r.json()["items"]:
        if row["session_id"] not in order:
            order.append(row["session_id"])
    return order


def test_sessions_are_listed_by_their_latest_turn_and_a_new_turn_moves_a_session_up(client: TestClient) -> None:
    save_runs(_turn("s-old", "2026-11-01T08:00:00+00:00"))
    save_runs(_turn("s-mid", "2026-11-01T09:00:00+00:00"))
    save_runs(_turn("s-new", "2026-11-01T10:00:00+00:00"))
    assert _sessions(client) == ["s-new", "s-mid", "s-old"]

    # A new turn in the oldest session: that session now leads.
    save_runs(_turn("s-old", "2026-11-02T08:00:00+00:00"))
    assert _sessions(client) == ["s-old", "s-new", "s-mid"]

    # Archived: out of the default listing, alone in its own.
    r = client.post("/api/gateway/sessions/s-new/archive", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _sessions(client) == ["s-old", "s-mid"]
    assert _sessions(client, "&archived_only=true") == ["s-new"]
