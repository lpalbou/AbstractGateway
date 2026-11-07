"""The identity voice check lists voices AS THE CALLER (0.20.3).

Operator report 2026-11-07: saving an entity's identity with a voice the picker showed failed with
"castor can't speak with 250b665e…: this gateway offers no qwen3-tts voice right now", while the
signed-in `GET /voice/voices?provider=qwen3-tts` listed 93 voices. The check called the voice
route function directly, and a FastAPI parameter it did not pass (`language`) arrived as its
`Query(None)` object, which filtered every voice out. A conversation's identity
(`POST /runs/start` with `identity`) ran the same check ON the event loop, where it could not run
at all and refused every voice.

Now the check asks the voice route as the caller (their principal, their plane: the account's own
voices in front of the shared library), with every parameter at its declared default, from a
worker thread. An entity speaks in its creator's plane, so the creator's own voices are its
voices; another account's own voice, or an unknown id, is refused with whose voices it may be.

The voice catalog is faked at the discovery facade (no engine): one shared library and one own
store per account, chosen by the principal of the call, filtering by `language` like the real one.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from email_fixtures import *  # noqa: F401,F403 - the `gateway` fixture (alice, bob, admin token)

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractavatar")
pytest.importorskip("abstractruntime.identity.digital_identity")

PROVIDER, MODEL = "qwen3-tts", "qwen3-tts-0.6b"
LIBRARY = ["aiden", "dylan", "sisko"]          # built-in voices + one an administrator made (shared)
OWN = {"alice": ["a1ce0000"], "bob": ["b0b00000"]}  # each account's own voices
DEFAULT_VOICE = {"provider": PROVIDER, "model": MODEL, "voice": "aiden"}


class _FakeDiscovery:
    """The voice catalog of the plane serving the call: the library + the caller's own voices."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def get_voice_catalog(self, **kw: Any) -> Dict[str, Any]:
        from abstractgateway.security.principal import current_gateway_principal

        principal = current_gateway_principal()
        user = str(getattr(principal, "user_id", "") or "")
        self.calls.append({"user": user, **kw})
        language = kw.get("language")

        def rec(vid: str, kind: str) -> Dict[str, Any]:
            return {"id": vid, "voice_id": vid, "provider": PROVIDER, "model": MODEL, "languages": ["en"], "voice_kind": kind}

        voices = [rec(v, "voice") for v in LIBRARY[:2]]
        clones = [rec(v, "clone") for v in LIBRARY[2:] + OWN.get(user, [])]
        if language is not None:  # the real catalog keeps the voices that speak `language`
            voices = [v for v in voices if language in v["languages"]]
            clones = [v for v in clones if language in v["languages"]]
        return {"available": True, "providers": [PROVIDER], "tts_providers": [PROVIDER], "voices": voices, "cloned_voices": clones,
                "tts_models_by_provider": {PROVIDER: [MODEL]}, "tts_models": [MODEL]}


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def world(gateway, monkeypatch: pytest.MonkeyPatch):
    """alice creates Castor; bob is another member; the voice catalog is per plane."""
    import abstractgateway.core_config as core_config
    import abstractgateway.entity_identity as ei
    import abstractgateway.routes.entities as entities_routes
    import abstractgateway.routes.gateway as gw
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router

    fake = _FakeDiscovery()
    monkeypatch.setattr(gw, "_gateway_abstractcore_discovery_facade", lambda: (fake, None))
    monkeypatch.setattr(core_config, "model_not_downloaded", lambda provider, model: None)
    monkeypatch.setattr(ei, "gateway_default_voice", lambda data_dir: dict(DEFAULT_VOICE))
    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Castor", "spark": _spark("Castor"), "skills": []})
    assert r.status_code == 201, r.text
    return {"c": c, "fake": fake, "alice": gateway["alice"], "bob": gateway["bob"]}


def _identity(voice: str) -> Dict[str, Any]:
    return {"avatar": {"style": "watch-lotus", "color": "#3366cc"}, "voice": {"provider": PROVIDER, "model": MODEL, "voice": voice}}


def _picker(c: TestClient, headers: Dict[str, str]) -> List[str]:
    r = c.get(f"/api/gateway/voice/voices?provider={PROVIDER}&compact=true", headers=headers)
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


def test_the_picker_lists_each_callers_own_voices_in_front_of_the_library(world) -> None:
    assert _picker(world["c"], world["alice"]) == LIBRARY[:2] + LIBRARY[2:] + OWN["alice"]
    assert "b0b00000" not in _picker(world["c"], world["alice"])
    assert "b0b00000" in _picker(world["c"], world["bob"]) and "a1ce0000" not in _picker(world["c"], world["bob"])


@pytest.mark.parametrize("voice", ["aiden", "sisko", "a1ce0000"], ids=["built-in", "shared", "the-callers-own-clone"])
def test_a_voice_the_callers_picker_lists_is_saved(world, voice) -> None:
    c = world["c"]
    assert voice in _picker(c, world["alice"])
    r = c.put("/api/gateway/entities/castor/identity", headers=world["alice"], json=_identity(voice))
    assert r.status_code == 200, r.text
    assert r.json()["identity"]["voice"]["voice"] == voice
    # The check asked the catalog as alice, with no stray FastAPI default (language=None).
    check = world["fake"].calls[-1]
    assert check["user"] == "alice" and check["language"] is None and check["provider"] == PROVIDER


@pytest.mark.parametrize("voice", ["b0b00000", "zz_nobody"], ids=["another-users-clone", "unknown-voice"])
def test_a_voice_the_caller_cannot_give_the_entity_is_refused_plainly(world, voice) -> None:
    r = world["c"].put("/api/gateway/entities/castor/identity", headers=world["alice"], json=_identity(voice))
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail.startswith(f"Castor can't speak with {voice}: it isn't a {PROVIDER} voice Castor can use. ")
    assert "its creator's own voices; another person's own voice stays theirs until an administrator shares it with everyone." in detail
    assert "Choose one of: aiden, dylan, sisko, a1ce0000." in detail


def test_the_creators_legacy_voice_door_takes_the_same_list(world) -> None:
    """`voice_bound_problem` (PUT /voice on a home born before the identity) asks as the caller too."""
    from abstractgateway.entity_settings_access import offered_voices, voice_bound_problem
    from abstractgateway.security.principal import GatewayPrincipal, reset_current_gateway_principal, set_current_gateway_principal

    alice = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",), scopes=(), runtime_id="alice", source="test")
    token = set_current_gateway_principal(alice)
    try:
        voices, models, err = offered_voices(PROVIDER, MODEL)
        assert voices == LIBRARY + OWN["alice"] and models == [MODEL] and err is None
        assert voice_bound_problem(PROVIDER, MODEL, "a1ce0000") is None
        assert "another person's own voice stays theirs" in (voice_bound_problem(PROVIDER, MODEL, "b0b00000") or "")
    finally:
        reset_current_gateway_principal(token)


def test_a_conversation_identity_checks_its_voice_off_the_loop_as_the_caller(world) -> None:
    """POST /runs/start with `identity`: the check ran on the event loop and refused every voice."""
    c = world["c"]
    base = {"bundle_id": "no-such-bundle", "flow_id": "root", "session_id": "s-voice", "input_data": {"prompt": "hi"}}
    r = c.post("/api/gateway/runs/start", headers=world["alice"], json={**base, "identity": _identity("a1ce0000")})
    assert "can't speak with" not in r.text, r.text  # past the identity ladder (the bundle is the next refusal)
    r = c.post("/api/gateway/runs/start", headers=world["alice"], json={**base, "identity": _identity("b0b00000")})
    assert r.status_code == 400, r.text
    assert "This conversation can't speak with b0b00000" in r.text and "your own voices" in r.text


def test_called_on_the_event_loop_the_check_says_so_instead_of_refusing_the_voice() -> None:
    import asyncio

    from abstractgateway.entity_settings_access import ChecksOnTheEventLoop, offered_voices

    async def on_the_loop() -> None:
        offered_voices(PROVIDER, MODEL)

    with pytest.raises(ChecksOnTheEventLoop):
        asyncio.run(on_the_loop())


def test_route_defaults_never_pass_a_fastapi_parameter_object() -> None:
    from pydantic.fields import FieldInfo

    from abstractgateway.entity_settings_access import _route_defaults
    from abstractgateway.routes.gateway import discovery_provider_models, voice_voices_catalog

    for fn, given in ((voice_voices_catalog, {"request": None, "provider": PROVIDER}), (discovery_provider_models, {"request": None, "provider_name": "x"})):
        kwargs = _route_defaults(fn, given)
        assert not [k for k, v in kwargs.items() if isinstance(v, FieldInfo)], kwargs
    assert _route_defaults(voice_voices_catalog, {"request": None})["language"] is None
