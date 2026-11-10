"""Birth records are authored (AbstractMemory 82178c9 engram(author=)): a spark item without a name gets its title from
the entity's summaries capability before it forms, never the "trait-N" stand-in (castor carried two "trait-0"); a
failing capability forms it pending with its authoring-failure line."""

from __future__ import annotations

import copy
import re
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-birth-authoring-secret"
_STAND_IN = re.compile(r"^(value|purpose|trait|limit)-\d+$")


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _self_records(slug: str) -> List[Any]:
    from abstractgateway.service import get_gateway_service

    registry = get_gateway_service().entity_registry
    home = registry.get_home(slug)
    return list(home.memory.self_records(scope="self", owner_id=home.entity_id))


def _unnamed_count() -> int:
    spark = _spark("x")
    return sum(1 for sec in ("values", "purposes", "traits", "honesty") for it in spark.get(sec) or () if not it.get("name"))


class _Titles:
    model_info = {"provider": "lmstudio", "model": "test-summaries"}

    def __init__(self) -> None:
        self.n = 0

    def generate(self, *args: Any, response_model: Any = None, **kwargs: Any) -> Any:
        self.n += 1
        return response_model(title=f"Birth title {self.n}", summary=f"What birth record {self.n} says.")


@pytest.mark.capability_models
def test_unnamed_spark_items_are_authored_at_birth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    model = _Titles()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: model)
    assert _unnamed_count() >= 1  # the default spark names some items, not all
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        rows = _self_records("castor")
        titles = [str((r.attributes or {}).get("title") or "") for r in rows]
        assert not [t for t in titles if _STAND_IN.match(t)], titles
        authored = [t for t in titles if t.startswith("Birth title ")]
        assert len(authored) == _unnamed_count() and model.n == _unnamed_count(), titles
        assert not [r for r in rows if ((r.attributes or {}).get("pending") or {}).get("title")]


@pytest.mark.capability_models
def test_a_failing_capability_forms_them_pending_with_a_failure_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat
    from abstractgateway.service import get_gateway_service

    class _Down:
        model_info = {"provider": "lmstudio", "model": "test-summaries"}

        def generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the summaries model is unreachable")

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _Down())
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        rows = _self_records("castor")
        pending = [r for r in rows if ((r.attributes or {}).get("pending") or {}).get("title")]
        assert len(pending) == _unnamed_count(), [(r.attributes or {}).get("title") for r in rows]
        assert not [r for r in rows if _STAND_IN.match(str((r.attributes or {}).get("title") or ""))]
        home_dir = get_gateway_service().entity_registry.entities_dir / "castor"
        log = (home_dir / "own_time.log").read_text(encoding="utf-8")
        assert log.count("#AUTHORING_FAILURE") >= _unnamed_count() and "engram" in log, log[-1500:]
