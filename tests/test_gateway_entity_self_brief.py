"""GET /api/gateway/entities/{name}/self-brief serves the runtime's self-knowledge brief (`<home>/self_brief.md`) as
{text, written_at, version, tokens}; 404 no_brief before the first sleep that wrote one; 500 for a file that is not a
brief. The Entity app shows it (Identity page); the Files view lists only the workspace, so this route is its only door."""
from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("abstractmemory")

_TOKEN = "test-token-self-brief"


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _born(client: TestClient, name: str = "Orion") -> Any:
    from abstractgateway.service import get_gateway_service

    assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201
    return get_gateway_service().entity_registry.get_home(name.lower())


def test_a_home_with_no_brief_answers_404_no_brief() -> None:
    with _client() as client:
        _born(client)
        r = client.get("/api/gateway/entities/Orion/self-brief")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "no_brief"


def test_the_route_serves_the_brief_the_runtime_wrote() -> None:
    from abstractruntime.identity.self_brief import SELF_BRIEF_FILENAME

    with _client() as client:
        home = _born(client)
        (home.home_dir / SELF_BRIEF_FILENAME).write_text(
            "---\nversion_seq: 107166\nwritten_at: '2026-11-10T14:00:48+00:00'\ntokens: 245\n---\n"
            "Tending the twelve continuity bridges: routines and checklists.\n", encoding="utf-8")
        r = client.get("/api/gateway/entities/Orion/self-brief")
        assert r.status_code == 200, r.text
        assert r.json() == {"text": "Tending the twelve continuity bridges: routines and checklists.",
                            "written_at": "2026-11-10T14:00:48+00:00", "version": 107166, "tokens": 245}


def test_a_file_that_is_not_a_brief_answers_500_with_the_runtime_s_reason() -> None:
    from abstractruntime.identity.self_brief import SELF_BRIEF_FILENAME

    with _client() as client:
        home = _born(client)
        (home.home_dir / SELF_BRIEF_FILENAME).write_text("just text, no header\n", encoding="utf-8")
        r = client.get("/api/gateway/entities/Orion/self-brief")
        assert r.status_code == 500, r.text
        assert "has no header" in r.json()["detail"]


def test_an_unknown_entity_answers_404() -> None:
    with _client() as client:
        assert client.get("/api/gateway/entities/nobody/self-brief").status_code == 404
