"""The entity's cognition profile through the gateway (runtime identity.cognition_profile).

`GET/PUT /api/gateway/entities/{name}/cognition-profile`: the GET shape on a home without
`cognition.yaml`; a PUT recorded marker-first (`cognition_profile_changed` lands before the
file moves, a failed marker writes nothing); refusals in sentences; null resets; the creator
bound; and the recall width (`recall.shelf_size`) applied where a visit, a durable visit leg or
the own-time loop starts without an explicit width.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from email_fixtures import *  # noqa: F401,F403 - the `gateway` fixture
from test_entity_session_tools_api import client as session_client  # noqa: F401 - visit fixture

pytestmark = pytest.mark.integration

pytest.importorskip("abstractmemory")
yaml = pytest.importorskip("yaml")

THEFORGE = {"provider": "endpoint:compat-theforge-nvidia", "model": "qwen/qwen3.8-27b"}
OFFERED_MODELS = {"lmstudio": ["qwen3-4b", "qwen3-30b"], "endpoint:compat-theforge-nvidia": ["qwen/qwen3.8-27b"]}
ROUTE_MIND = {"provider": "lmstudio", "model": "route-model", "base_url": None, "reasoning": "low"}
SHELF_SENTENCE = "Memories per turn must be a whole number from 1 to 400."


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _known_endpoints(monkeypatch: pytest.MonkeyPatch, known=("compat-theforge-nvidia",)) -> None:
    """The provider-connection store answers for theforge only (no real store in these tests)."""
    import abstractgateway.provider_endpoint_profiles as pep

    def resolve(provider, *, base_dir, root_base_dir=None):
        pid = pep.profile_id_from_virtual_provider(provider)
        return SimpleNamespace(id=pid, enabled=True) if pid in known else None

    monkeypatch.setattr(pep, "resolve_effective_endpoint_profile", resolve)


@pytest.fixture
def world(gateway, monkeypatch):
    """alice (member) creates Aster; bob creates nothing; an admin account. Real middleware and
    entity routers over one registry; the offered models and the gateway's text route stubbed."""
    from fastapi.testclient import TestClient

    import abstractgateway.entity_settings_access as esa
    import abstractgateway.routes.entities as entities_routes
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.users import GatewayUserRegistry

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    monkeypatch.setattr(esa, "offered_text_models", lambda p: (list(OFFERED_MODELS.get(p, [])), None))
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: dict(ROUTE_MIND))
    _known_endpoints(monkeypatch)
    c = TestClient(app)
    _rec, admin_token = GatewayUserRegistry().create_user(user_id="root", roles=["admin", "user"], runtime_id="default")
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Aster", "spark": _spark("Aster"), "skills": []})
    assert r.status_code == 201, r.text
    return {
        "c": c,
        "registry": registry,
        "home": registry.entities_dir / "aster",
        "admin": {"Authorization": f"Bearer {admin_token}"},
        "alice": gateway["alice"],
        "bob": gateway["bob"],
    }


URL = "/api/gateway/entities/aster/cognition-profile"


def _markers(world) -> list:
    from abstractgateway.entity_replay import read_host_markers

    return [
        m["payload"]
        for m in read_host_markers(world["registry"].entities_dir, "aster", since_seq=-1.0)
        if (m.get("payload") or {}).get("kind") == "cognition_profile_changed"
    ]


def test_get_without_a_file_shows_the_mind_everywhere_and_the_defaults(world) -> None:
    r = world["c"].get(URL, headers=world["admin"])
    assert r.status_code == 200, r.text
    mind = {"provider": "lmstudio", "model": "route-model", "thinking": "low"}
    assert r.json() == {
        "schema_version": 1,
        "capabilities": {
            cap: {"choice": {"inherit": "mind"}, "effective": mind, "source": "mind"}
            for cap in ("summaries", "understanding", "extraction")
        },
        "recall": {"shelf_size": 50, "source": "default", "applies_to_running_loop": "on restart"},
        "rebuild": {
            "sensitivity": 0.5, "cap_per_turn": 3, "source": "default",
            "sources": {"sensitivity": "default", "cap_per_turn": "default"},
        },
        "mind": {**mind, "source": "gateway"},
    }
    assert not (world["home"] / "cognition.yaml").exists()
    # The entity's own mind is what an inherited capability shows.
    assert world["c"].put(
        "/api/gateway/entities/aster/substrate", headers=world["admin"], json={"provider": "lmstudio", "model": "qwen3-30b"}
    ).status_code == 200
    body = world["c"].get(URL, headers=world["admin"]).json()
    assert body["mind"] == {"provider": "lmstudio", "model": "qwen3-30b", "thinking": None, "source": "entity"}
    assert body["capabilities"]["understanding"]["effective"] == {"provider": "lmstudio", "model": "qwen3-30b", "thinking": None}


def test_get_names_the_missing_text_model_without_inventing_one(world, monkeypatch) -> None:
    monkeypatch.setattr(
        "abstractgateway.entity_chat.gateway_text_mind",
        lambda: {"provider": None, "model": None, "base_url": None, "reasoning": None},
    )
    body = world["c"].get(URL, headers=world["admin"]).json()
    assert body["mind"]["source"] == "unset" and body["mind"]["note"]
    assert body["capabilities"]["summaries"] == {"choice": {"inherit": "mind"}, "effective": None, "source": "mind"}


def test_put_theforge_summaries_records_the_marker_before_the_file(world, monkeypatch) -> None:
    import abstractgateway.entity_replay as replay

    home = world["home"]
    seen_at_marker: list = []
    real = replay.record_host_marker

    def spy(**kwargs):
        if kwargs.get("kind") == "cognition_profile_changed":
            path = home / "cognition.yaml"
            seen_at_marker.append(path.read_text(encoding="utf-8") if path.exists() else None)
        return real(**kwargs)

    monkeypatch.setattr(replay, "record_host_marker", spy)
    r = world["c"].put(URL, headers=world["admin"], json={"capabilities": {"summaries": {**THEFORGE, "thinking": "low"}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capabilities"]["summaries"] == {
        "choice": {**THEFORGE, "thinking": "low", "max_input_tokens": 140000},
        "effective": {**THEFORGE, "thinking": "low"},
        "source": "profile",
    }
    assert body["capabilities"]["understanding"]["source"] == "mind"
    # The marker landed while the file did not exist yet; then the file moved.
    assert seen_at_marker == [None]
    stored = {"schema_version": 1, "capabilities": {"summaries": {**THEFORGE, "thinking": "low"}}}
    assert yaml.safe_load((home / "cognition.yaml").read_text(encoding="utf-8")) == stored
    marker = _markers(world)[-1]
    assert marker["old"] == {"schema_version": 1} and marker["new"] == stored
    assert marker["by"] == "person:root" and marker["as"] == "admin"
    # The mind did not move.
    assert not (home / "substrate.yaml").exists()
    # A PUT that changes nothing records nothing.
    assert world["c"].put(URL, headers=world["admin"], json={"capabilities": {"summaries": {**THEFORGE, "thinking": "low"}}}).status_code == 200
    assert len(_markers(world)) == 1


def test_a_marker_that_cannot_be_recorded_writes_nothing(world, monkeypatch) -> None:
    import abstractgateway.entity_replay as replay

    def broken(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(replay, "record_host_marker", broken)
    r = world["c"].put(URL, headers=world["admin"], json={"recall": {"shelf_size": 7}})
    assert r.status_code == 503, r.text
    assert "could not be recorded" in r.json()["detail"]
    assert not (world["home"] / "cognition.yaml").exists()


@pytest.mark.parametrize(
    "body, words",
    [
        ({"recall": {"shelf_size": 0}}, SHELF_SENTENCE),
        ({"recall": {"shelf_size": 401}}, SHELF_SENTENCE),
        ({"recall": {"shelf_size": "7"}}, SHELF_SENTENCE),
        ({"recall": {"shelf_size": True}}, SHELF_SENTENCE),
        ({"rebuild": {"cap_per_turn": 11}}, "Rebuilds per turn must be a whole number from 0 to 10."),
        ({"rebuild": {"cap_per_turn": True}}, "Rebuilds per turn must be a whole number from 0 to 10."),
        ({"rebuild": {"sensitivity": 2}}, "Rebuild sensitivity must be a number from 0 to 1."),
        ({"rebuild": {"sensitivity": "0.5"}}, "Rebuild sensitivity must be a number from 0 to 1."),
        ({"capabilities": {"dialogue": dict(THEFORGE)}}, "There is no part of thinking called 'dialogue'."),
        ({"capabilities": {"summaries": {**THEFORGE, "thinking": "turbo"}}}, "Unknown reasoning effort 'turbo'"),
        ({"capabilities": {"summaries": {**THEFORGE, "max_input_tokens": 200000}}}, "must be a whole number from 4096 to 140000"),
        ({"capabilities": {"summaries": {"provider": "lmstudio"}}}, "needs both a provider and a model"),
        ({"capabilities": {"summaries": {"provider": "endpoint:nowhere", "model": "m"}}}, "isn't an enabled provider connection"),
        ({"capabilities": "mind"}, "capabilities must be an object"),
        ({"recall": {"width": 3}}, "recall has an unknown field 'width'"),
        ({"memories_rebuilt_per_turn": 1}, "Unknown field 'memories_rebuilt_per_turn'"),
    ],
)
def test_values_outside_the_schema_are_refused_with_a_sentence(world, body, words) -> None:
    r = world["c"].put(URL, headers=world["admin"], json=body)
    assert r.status_code == 400, r.text
    assert words in r.json()["detail"]
    assert not (world["home"] / "cognition.yaml").exists()
    assert _markers(world) == []


def test_absent_keeps_and_null_resets(world) -> None:
    c, admin = world["c"], world["admin"]
    r = c.put(URL, headers=admin, json={
        "capabilities": {"summaries": dict(THEFORGE), "extraction": dict(THEFORGE)},
        "recall": {"shelf_size": 7},
        "rebuild": {"sensitivity": 0.9, "cap_per_turn": 5},
    })
    assert r.status_code == 200, r.text
    # Absent fields keep their values.
    body = c.put(URL, headers=admin, json={"rebuild": {"cap_per_turn": 1}}).json()
    assert body["rebuild"]["sensitivity"] == 0.9 and body["rebuild"]["cap_per_turn"] == 1
    assert body["recall"] == {"shelf_size": 7, "source": "profile", "applies_to_running_loop": "on restart"}
    assert body["capabilities"]["extraction"]["source"] == "profile"
    # null (and inherit) return each to its default.
    body = c.put(URL, headers=admin, json={
        "capabilities": {"summaries": None, "extraction": {"inherit": "mind"}},
        "recall": {"shelf_size": None},
        "rebuild": {"sensitivity": None, "cap_per_turn": None},
    }).json()
    assert all(row["choice"] == {"inherit": "mind"} for row in body["capabilities"].values())
    assert body["recall"]["shelf_size"] == 50 and body["recall"]["source"] == "default"
    assert body["rebuild"]["source"] == "default" and (body["rebuild"]["sensitivity"], body["rebuild"]["cap_per_turn"]) == (0.5, 3)
    assert yaml.safe_load((world["home"] / "cognition.yaml").read_text(encoding="utf-8")) == {"schema_version": 1}


def test_the_creator_is_bound_to_the_offered_models_and_others_are_refused(world, monkeypatch) -> None:
    c = world["c"]
    r = c.put(URL, headers=world["alice"], json={"capabilities": {"summaries": {"provider": "lmstudio", "model": "llama-unlisted"}}})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "reason_code": "not_offered",
        "message": "Summaries and memory details: llama-unlisted isn't a lmstudio model this gateway offers. "
        "Offered: qwen3-4b, qwen3-30b.",
    }
    assert not (world["home"] / "cognition.yaml").exists()
    # Within the bound: allowed, and recorded as the creator.
    r = c.put(URL, headers=world["alice"], json={"capabilities": {"summaries": dict(THEFORGE)}, "recall": {"shelf_size": 20}})
    assert r.status_code == 200, r.text
    assert _markers(world)[-1]["as"] == "creator" and _markers(world)[-1]["by"] == "person:alice"
    # An admin may name any model; the creator may keep it when saving again.
    unlisted = {"provider": "lmstudio", "model": "llama-unlisted"}
    assert c.put(URL, headers=world["admin"], json={"capabilities": {"understanding": unlisted}}).status_code == 200
    r = c.put(URL, headers=world["alice"], json={"capabilities": {"understanding": unlisted}, "recall": {"shelf_size": 21}})
    assert r.status_code == 200, r.text
    # Another member never changes it: hidden from bob, it answers like a missing entity; were
    # he to see it, the settings guard says no with its sentence.
    r = c.put(URL, headers=world["bob"], json={"recall": {"shelf_size": 9}})
    assert r.status_code == 404, r.text
    import abstractgateway.entity_access as entity_access

    monkeypatch.setattr(entity_access, "sees_every_entity", lambda principal: True)
    r = c.put(URL, headers=world["bob"], json={"recall": {"shelf_size": 9}})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["message"] == "Only an admin or aster's creator can change how it thinks."
    assert c.get(URL, headers=world["admin"]).json()["recall"]["shelf_size"] == 21


# ------------------------------------------------------------- recall width applied

_TOKEN = "cognition-profile-secret"


def _token_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: dict(ROUTE_MIND))
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class _ScriptedLLM:
    def generate(self, **kwargs):
        return SimpleNamespace(content="Hello.")


def test_a_visit_open_without_a_width_uses_the_stored_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM())
    with _token_client(monkeypatch) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        base = "/api/gateway/entities/Castor"
        assert client.put(base + "/cognition-profile", json={"recall": {"shelf_size": 7}}).status_code == 200
        opened = client.post(base + "/chat/open", json={})
        assert opened.status_code == 200, opened.text
        assert opened.json()["budget_profile"]["shelf_size"] == 7
        assert client.post(f"{base}/chat/{opened.json()['chat_id']}/close", json={"reflect": False}).status_code == 200
        # An explicit width in the request still wins.
        opened = client.post(base + "/chat/open", json={"shelf_size": 9})
        assert opened.status_code == 200, opened.text
        assert opened.json()["budget_profile"]["shelf_size"] == 9
        assert client.post(f"{base}/chat/{opened.json()['chat_id']}/close", json={"reflect": False}).status_code == 200


def test_a_durable_visit_leg_stamps_the_stored_width(session_client) -> None:  # noqa: F811
    from abstractgateway.routes.entities import _visit_host

    base = "/api/gateway/entities/session-test"
    assert session_client.put(base + "/cognition-profile", json={"recall": {"shelf_size": 11}}).status_code == 200
    opened = session_client.post(base + "/visit/open", json={"session_id": "width-visit", "tool_policy": {"modes": {}}})
    assert opened.status_code == 200, opened.text
    _er, run, _wf, _manifest = _visit_host()._load_visit("session-test", opened.json()["run_id"], need_spec=False)
    assert run.vars["_runtime"]["entity"]["budget_profile"]["shelf_size"] == 11
    assert session_client.post(base + "/visit/" + opened.json()["run_id"] + "/end").status_code == 200


def test_a_loop_spawn_without_a_width_passes_the_stored_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractruntime.identity.life as life_mod

    calls: list = []

    def fake_spawn(home_dir, **kwargs):
        calls.append(kwargs)
        return {"pid": 4242, "provider": kwargs.get("provider"), "model": kwargs.get("model"),
                "tick_seconds": kwargs.get("tick_seconds"), "ticks_per_day": kwargs.get("ticks_per_day"),
                "rest_minutes": kwargs.get("rest_minutes"), "shelf_size": kwargs.get("shelf_size")}

    monkeypatch.setattr(life_mod, "spawn_loop_process", fake_spawn)
    with _token_client(monkeypatch) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        base = "/api/gateway/entities/Castor"
        assert client.put(base + "/personal-grant", json={"mode": "until_revoked"}).status_code == 200
        assert client.post(base + "/state", json={"state": "awake"}).status_code == 200
        assert client.put(base + "/cognition-profile", json={"recall": {"shelf_size": 7}}).status_code == 200

        r = client.post(base + "/loop/start", json={})
        assert r.status_code == 200, r.text
        assert calls[-1]["shelf_size"] == 7
        from abstractgateway.service import get_gateway_service

        home = get_gateway_service().entity_registry.entities_dir / "castor"
        # Only an explicit width is recorded: a respawn reads the profile again.
        assert json.loads((home / "loop_spawn.json").read_text(encoding="utf-8"))["shelf_size"] is None

        # The respawn path (the repair sweeper calls start_loop without a width) follows a new value.
        assert client.put(base + "/cognition-profile", json={"recall": {"shelf_size": 30}}).status_code == 200
        from abstractgateway.entity_loop import start_loop

        start_loop(home, provider="lmstudio", model="route-model")
        assert calls[-1]["shelf_size"] == 30

        # An explicit width wins and is recorded.
        r = client.post(base + "/loop/start", json={"shelf_size": 12})
        assert r.status_code == 200, r.text
        assert calls[-1]["shelf_size"] == 12
        assert json.loads((home / "loop_spawn.json").read_text(encoding="utf-8"))["shelf_size"] == 12
