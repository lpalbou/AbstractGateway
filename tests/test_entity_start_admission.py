"""Starts and remembered admissions on the runtime's fence (Codex review 4 #7 / #9; runtime d47b7a2, 3cd872b).

- A start is admitted BEFORE its checks and the spawn runs under that admission (`spawn_loop_process(fence=)`,
  REQUIRED since d47b7a2): a Stop landing after the decision spawns nothing.
- The runtime reads a missing lifecycle signal as moved (3cd872b, fail closed) and only an admission writes it: a
  fence the gateway builds from a REMEMBERED generation (`fenced()`, `admission_fence(generation)`) admits a home
  with no signal once first, else every write of a home never admitted since 3cd872b would be refused."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-start-admission-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _registry():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().entity_registry


def _born(client: TestClient) -> None:
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    assert client.put("/api/gateway/entities/Castor/personal-grant", json={"mode": "until_revoked"}).status_code == 200


def test_the_operator_s_start_hands_the_spawn_its_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """loop/start passes the spawn the fence it admitted the start at: the home's generation, standing."""
    import abstractruntime.identity.life as life_mod
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    seen = {}

    def _fake_spawn(home_dir, **kwargs):
        seen.update(kwargs)
        return {"pid": 4242, "log": str(home_dir / "own_time.log")}

    monkeypatch.setattr(life_mod, "spawn_loop_process", _fake_spawn)
    with _client() as client:
        _born(client)
        r = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert r.status_code == 200, r.text
        fence = seen.get("fence")
        assert isinstance(fence, LifecycleFence), seen
        home_dir = _registry().entities_dir / "castor"
        assert fence.generation == life_mod.read_lifecycle_generation(home_dir) and not fence.moved()


def test_a_stop_after_the_start_was_decided_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admission is taken before the start's checks: a Stop landing after them (here, during the archived
    check) makes the runtime's spawn refuse - 409 stopped, no loop."""
    import abstractgateway.entity_access as access
    from abstractruntime.identity.life import bump_lifecycle_generation, loop_process_status

    real_check = access.refuse_if_entity_archived
    with _client() as client:
        _born(client)
        home_dir = _registry().entities_dir / "castor"

        def _stop_meanwhile(*args, **kwargs):
            bump_lifecycle_generation(home_dir, written_by="test")  # the operator's Stop, between decision and spawn
            return real_check(*args, **kwargs)

        monkeypatch.setattr(access, "refuse_if_entity_archived", _stop_meanwhile)
        r = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["reason_code"] == "stopped_use_resume", r.json()
        assert loop_process_status(home_dir).get("running") is not True


def test_a_home_without_a_lifecycle_signal_is_admitted_once_by_a_remembered_fence() -> None:
    """A home whose signal was never written (every home that predates runtime 3cd872b and was never stopped):
    a fence built from its remembered generation reads standing - the home is admitted once, the signal written -
    and the gateway's own state write through fenced() lands."""
    from abstractruntime.identity.life import read_entity_state, read_lifecycle_generation
    from abstractruntime.identity.lifecycle_fence import signal_path

    from abstractgateway.entity_phase import admission_fence, fenced

    with _client() as client:
        _born(client)
        registry = _registry()
        home_dir = registry.entities_dir / "castor"
        generation = read_lifecycle_generation(home_dir)
        signal_path(home_dir).unlink()
        assert not admission_fence(registry.entities_dir, "castor", generation).moved()
        assert signal_path(home_dir).exists()
        signal_path(home_dir).unlink()
        with fenced(registry.entities_dir, "castor", generation) as rec:
            assert not rec.fence.moved()
        signal_path(home_dir).unlink()
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "asleep"})
        assert r.status_code == 200, r.text
        assert read_entity_state(home_dir)["state"] == "asleep"
