"""The digital identity at the door (framework ADR 0052, backlog 1119; the gateway half).

AbstractRuntime owns the record (`<home>/identity.json`, its bounds, rev and biography row); the
gateway validates at SET time, records the default once per home, serves the record, routes the
legacy voice/appearance doors through it, and names the identity by reference where an entity
speaks. Every test here goes red without the rule it names. Fakes only: the offered voices and the
download probe are stubbed, no model loads, nothing reaches :8080.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from email_fixtures import *  # noqa: F401,F403 - the `gateway` fixture (alice, bob, admin token)

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractavatar")
pytest.importorskip("abstractruntime.identity.digital_identity")

_TOKEN = "entity-identity-secret"
DEFAULT_VOICE = {"provider": "kokoro", "model": "kokoro-82m", "voice": "af_heart"}
OFFERED_VOICES = {"kokoro": (["af_heart", "am_adam"], ["kokoro-82m", "kokoro-big"])}
NOT_DOWNLOADED = {"kokoro-big"}
CHOSEN = {"avatar": {"style": "watch-lotus", "color": "#3366cc"}, "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam"}}


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _missing(provider: str, model: str):
    if model in NOT_DOWNLOADED:
        # The typed answer of core_config.model_not_downloaded (its fields, never its sentence).
        return {"reason_code": "model_not_downloaded", "message": "(the default-voice sentence)", "provider": provider, "model": model,
                "artifact": model, "size_mb": 330, "command": f"abstractgateway models download {provider} {model}",
                "download": {"provider": provider, "model": model}}
    return None


@pytest.fixture(autouse=True)
def _gateway_knows(monkeypatch: pytest.MonkeyPatch) -> None:
    """What only the gateway knows, stubbed: the offered voices, the download probe, the default voice,
    and the mind for visits (no model is reached)."""
    import abstractgateway.core_config as core_config
    import abstractgateway.entity_identity as ei
    import abstractgateway.entity_settings_access as esa

    monkeypatch.setattr(esa, "offered_voices", lambda p, m: (*OFFERED_VOICES.get(p, ([], [])), None))
    monkeypatch.setattr(core_config, "model_not_downloaded", _missing)
    monkeypatch.setattr(ei, "gateway_default_voice", lambda data_dir: dict(DEFAULT_VOICE))
    monkeypatch.setattr(
        "abstractgateway.entity_chat.gateway_text_mind",
        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None},
    )


# --------------------------------------------------------------------------- the single-user door


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as client:
        yield {"c": client, "entities": Path(get_gateway_service().config.data_dir) / "entities"}


def _born(c: TestClient, name: str, **extra: Any) -> Dict[str, Any]:
    r = c.post("/api/gateway/entities", json={"name": name, "spark": _spark(name), "skills": [], **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _markers(entities: Path, slug: str, kind: str) -> List[Dict[str, Any]]:
    path = entities / ".host_stream" / f"{slug}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r["payload"] for r in rows if (r.get("payload") or {}).get("kind") == kind]


def _identity_file(entities: Path, slug: str) -> Dict[str, Any]:
    return json.loads((entities / slug / "identity.json").read_text())


def _as_home_born_before_the_identity(home_dir: Path) -> None:
    """A home born before ADR 0052: no identity record (set aside), its legacy files apply."""
    import os

    os.replace(home_dir / "identity.json", home_dir / "identity.json.born-before")


# --------------------------------------------------------------------------- birth and the default


def test_birth_records_the_default_once_and_a_bad_birth_identity_creates_nothing(door) -> None:
    from abstractruntime.identity.digital_identity import default_identity

    c, entities = door["c"], door["entities"]
    out = _born(c, "Castor")
    record = _identity_file(entities, "castor")
    avatar, voice = default_identity("entity:castor", DEFAULT_VOICE)
    assert record["rev"] == 1 and record["set_by"] == "default"
    assert record["avatar"] == avatar.to_dict() and record["voice"] == voice.to_dict()
    assert out["identity"]["rev"] == 1
    # A bad identity at birth is refused before anything exists: no home, no record.
    bad = {**CHOSEN, "avatar": {"style": "iphone-weave", "color": "#3366cc"}}
    r = c.post("/api/gateway/entities", json={"name": "Lyra", "spark": _spark("Lyra"), "skills": [], "identity": bad})
    assert r.status_code == 400 and "can't be drawn on the Watch" in r.json()["detail"], r.text
    assert not (entities / "lyra").exists()
    # A chosen identity at birth is rev 1, chosen.
    _born(c, "Vega", identity=CHOSEN)
    vega = _identity_file(entities, "vega")
    assert vega["rev"] == 1 and vega["avatar"]["source"] == "chosen" and vega["voice"]["voice"] == "am_adam"


def test_a_birth_look_is_the_identity_look(door) -> None:
    c, entities = door["c"], door["entities"]
    _born(c, "Vega", appearance={"color": "#f59e0b", "shape": "face"})
    record = _identity_file(entities, "vega")
    assert record["rev"] == 1 and record["avatar"] == {"style": "watch-shape-face", "color": "#f59e0b", "source": "chosen"}
    assert record["voice"]["voice"] == DEFAULT_VOICE["voice"]
    assert c.get("/api/gateway/entities/vega/appearance").json() == {"color": "#f59e0b", "shape": "face", "source": "identity"}


def test_get_identity_serves_the_record_fingerprint_age_and_earlier_revs(door) -> None:
    c = door["c"]
    _born(c, "Castor")
    body = c.get("/api/gateway/entities/castor/identity").json()
    assert body["entity_id"] == "entity:castor"
    assert body["identity"]["rev"] == 1 and body["identity"]["avatar"]["watch_code"]
    assert body["identity_ref"] == {"of": "entity:castor", "rev": 1}
    assert body["fingerprint"]["actor_id"].startswith("ar_") and body["fingerprint"]["kind"] == "entity"
    assert body["age"]["born"] and body["age"]["days"] == 0
    assert c.put("/api/gateway/entities/castor/identity", json=CHOSEN).status_code == 200
    assert c.get("/api/gateway/entities/castor/identity").json()["identity"]["rev"] == 2
    first = c.get("/api/gateway/entities/castor/identity?rev=1").json()["identity"]
    assert first["rev"] == 1 and first["set_by"] == "default"
    assert c.get("/api/gateway/entities/castor/identity?rev=9").status_code == 404


def test_a_valid_put_writes_through_the_runtime_with_the_host_marker(door) -> None:
    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    r = c.put("/api/gateway/entities/castor/identity", json=CHOSEN)
    assert r.status_code == 200, r.text
    record = _identity_file(entities, "castor")
    assert record["rev"] == 2 and record["set_by"] == "person:admin"
    assert record["avatar"]["source"] == "chosen" and record["voice"]["source"] == "chosen"
    marker = _markers(entities, "castor", "identity_changed")[-1]
    assert marker["rev"] == 2 and marker["from"]["rev"] == 1 and marker["to"]["rev"] == 2 and marker["by"] == "person:admin"
    rows = [json.loads(line) for line in (entities / "castor" / "state_history.jsonl").read_text().splitlines()]
    assert [row["rev"] for row in rows if row.get("marker") == "identity_changed"] == [1, 2]
    # Never cleared.
    assert c.put("/api/gateway/entities/castor/identity", json={**CHOSEN, "clear": True}).status_code == 400
    assert _identity_file(entities, "castor")["rev"] == 2


# --------------------------------------------------------------------------- the ladder, every role


@pytest.fixture
def world(gateway, monkeypatch: pytest.MonkeyPatch):
    """alice (member) creates Aster; an admin account; real middleware and the entity routers."""
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.users import GatewayUserRegistry

    import abstractgateway.routes.entities as entities_routes

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    _rec, admin_token = GatewayUserRegistry().create_user(user_id="root", roles=["admin", "user"], runtime_id="default")
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Aster", "spark": _spark("Aster"), "skills": []})
    assert r.status_code == 201, r.text
    return {"c": c, "registry": registry, "entities": registry.entities_dir,
            "roles": {"admin": {"Authorization": f"Bearer {admin_token}"}, "creator": gateway["alice"]}, "bob": gateway["bob"]}


def _unchanged(world, rev: int) -> None:
    assert _identity_file(world["entities"], "aster")["rev"] == rev
    assert len(_markers(world["entities"], "aster", "identity_changed")) == rev - 1


@pytest.mark.parametrize("role", ["admin", "creator"])
def test_the_refusal_ladder_in_order_for_every_role(world, role) -> None:
    c, h, registry = world["c"], world["roles"][role], world["registry"]
    url = "/api/gateway/entities/aster/identity"
    not_offered = {**CHOSEN, "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "zz_nobody"}}
    not_downloaded = {**CHOSEN, "voice": {"provider": "kokoro", "model": "kokoro-big", "voice": "af_heart"}}
    unknown_style = {**CHOSEN, "avatar": {"style": "watch-nosuch", "color": "#3366cc"}}

    # 1. the hold, before anything else (even a bad design).
    registry.open_maintenance_window("aster", reason="doctoring", actor="person:root")
    r = c.put(url, headers=h, json=unknown_style)
    assert r.status_code == 409 and "closed for maintenance" in r.json()["detail"], r.text
    registry.close_maintenance_window("aster", reason="done", actor="person:root")
    _unchanged(world, 1)
    # 2. the design (before a voice that is not offered).
    r = c.put(url, headers=h, json={**not_offered, "avatar": unknown_style["avatar"]})
    assert r.status_code == 400 and r.json()["detail"].startswith("watch-nosuch isn't an avatar design this gateway has. Designs: ")
    r = c.put(url, headers=h, json={**CHOSEN, "avatar": {"style": "iphone-weave", "color": "#3366cc"}})
    assert r.status_code == 400 and "can't be drawn on the Watch" in r.json()["detail"]
    _unchanged(world, 1)
    # 3. the voice offered (before its download), for an admin too.
    r = c.put(url, headers=h, json=not_offered)
    assert r.status_code == 400 and r.json()["detail"] == "Aster can't speak with zz_nobody: it isn't a kokoro voice this gateway offers. Choose one of: af_heart, am_adam."
    # 4. the model downloaded.
    r = c.put(url, headers=h, json=not_downloaded)
    assert r.status_code == 409 and r.json()["error"]["download"] == {"provider": "kokoro", "model": "kokoro-big"}, r.text
    assert r.json()["detail"] == (
        "Aster's voice needs the kokoro model kokoro-big (330 MB), which is not downloaded on this computer yet. "
        "Download it (the Models page, or: abstractgateway models download kokoro kokoro-big), then save the identity again."
    )
    _unchanged(world, 1)
    # A valid one lands, set by the caller.
    assert c.put(url, headers=h, json=CHOSEN).status_code == 200
    assert _identity_file(world["entities"], "aster")["set_by"] == ("person:root" if role == "admin" else "person:alice")


def test_only_an_admin_or_the_creator_changes_the_identity(world) -> None:
    r = world["c"].put("/api/gateway/entities/aster/identity", headers=world["bob"], json=CHOSEN)
    assert r.status_code in (403, 404), r.text
    _unchanged(world, 1)


# --------------------------------------------------------------------------- the boot pass


def _registry_home(c: TestClient, entities: Path, name: str, *, legacy: bool = True) -> Path:
    _born(c, name)
    if legacy:
        _as_home_born_before_the_identity(entities / name.lower())
    return entities / name.lower()


def test_the_boot_pass_writes_once_skips_held_homes_and_get_records_a_skipped_one(door, caplog) -> None:
    from abstractgateway.entity_identity import boot_pass
    from abstractgateway.service import get_gateway_service

    c, entities = door["c"], door["entities"]
    _registry_home(c, entities, "Castor")
    _registry_home(c, entities, "Pollux")
    get_gateway_service().entity_registry.open_maintenance_window("pollux", reason="doctoring", actor="person:admin")
    with caplog.at_level(logging.INFO, logger="abstractgateway.entity_identity"):
        assert boot_pass([entities]) == {"written": 1, "held": 1}
    # (The set-aside record left its biography row: the rev never reuses one, so it is the next.)
    assert _identity_file(entities, "castor")["set_by"] == "default"
    assert not (entities / "pollux" / "identity.json").exists()
    assert any("pollux" in r.getMessage() and "closed for maintenance" in r.getMessage() for r in caplog.records)
    # Idempotent: a second boot writes nothing and adds no biography row.
    history = (entities / "castor" / "state_history.jsonl").read_text()
    assert boot_pass([entities]) == {"kept": 1, "held": 1}
    assert (entities / "castor" / "state_history.jsonl").read_text() == history
    # The skipped home, once unheld, is recorded by its first GET /identity, once.
    get_gateway_service().entity_registry.close_maintenance_window("pollux", reason="done", actor="person:admin")
    first = c.get("/api/gateway/entities/pollux/identity").json()["identity"]
    assert first["set_by"] == "default"
    assert c.get("/api/gateway/entities/pollux/identity").json()["identity"] == first


def test_a_changed_default_voice_never_touches_a_recorded_identity(door, monkeypatch) -> None:
    import abstractgateway.entity_identity as ei
    from abstractgateway.entity_identity import boot_pass

    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    before = (entities / "castor" / "identity.json").read_text()
    monkeypatch.setattr(ei, "gateway_default_voice", lambda data_dir: {"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam"})
    boot_pass([entities])
    c.get("/api/gateway/entities/castor/identity")
    assert (entities / "castor" / "identity.json").read_text() == before


def test_no_avatar_catalog_refuses_the_set_and_writes_no_default(door, monkeypatch, caplog) -> None:
    import builtins

    import abstractgateway.entity_identity as ei
    from abstractgateway.entity_identity import boot_pass

    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    _registry_home(c, entities, "Pollux")
    real_import = builtins.__import__

    def no_avatar(name, *args, **kwargs):
        if name == "abstractavatar":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(ei, "_CATALOG", None)
    monkeypatch.setattr(builtins, "__import__", no_avatar)
    r = c.put("/api/gateway/entities/castor/identity", json=CHOSEN)
    assert r.status_code == 503 and "avatar catalog is not available" in r.json()["detail"], r.text
    with caplog.at_level(logging.ERROR, logger="abstractgateway.entity_identity"):
        assert boot_pass([entities]) == {"kept": 1, "no_catalog": 1}
    assert not (entities / "pollux" / "identity.json").exists()
    assert sum("pollux" in r.getMessage() and "avatar catalog" in r.getMessage() for r in caplog.records) == 1


# --------------------------------------------------------------------------- the legacy doors, side by side


def test_legacy_and_identity_homes_side_by_side(door) -> None:
    """The same calls on a home born before the identity (legacy files) and one born with it."""
    c, entities = door["c"], door["entities"]
    _registry_home(c, entities, "Old")
    _born(c, "New")
    legacy = {path: c.get(f"/api/gateway/entities/old/{path}").json() for path in ("voice", "appearance")}
    record = {path: c.get(f"/api/gateway/entities/new/{path}").json() for path in ("voice", "appearance")}
    assert legacy["appearance"] == {"color": None, "shape": None, "source": "default"}
    assert legacy["voice"]["source"] == "unset"  # the old degrade chain: the gateway default is only "effective"
    new_record = _identity_file(entities, "new")
    assert record["appearance"] == {"color": new_record["avatar"]["color"], "shape": "planet", "source": "identity"}
    assert record["voice"] == {**DEFAULT_VOICE, "source": "identity", "effective": {**DEFAULT_VOICE, "source": "identity"}}
    # Legacy writes on the legacy home: files, as before; a clear works there.
    assert c.put("/api/gateway/entities/old/voice", json={"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam"}).json()["source"] == "entity"
    assert (entities / "old" / "voice.yaml").exists() and not (entities / "old" / "identity.json").exists()
    assert c.put("/api/gateway/entities/old/voice", json={"clear": True}).json()["source"] == "unset"
    # Legacy writes on the identity home: through the record, rev + 1; a clear is refused.
    r = c.put("/api/gateway/entities/new/voice", json={"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam"})
    assert r.status_code == 200 and r.json()["voice"] == "am_adam" and r.json()["source"] == "identity", r.text
    assert _identity_file(entities, "new")["rev"] == 2 and not (entities / "new" / "voice.yaml").exists()
    assert c.put("/api/gateway/entities/new/voice", json={"clear": True}).status_code == 409
    r = c.put("/api/gateway/entities/new/appearance", json={"shape": "kitty"})
    assert r.json() == {"color": new_record["avatar"]["color"], "shape": "kitty", "source": "identity"}
    record_now = _identity_file(entities, "new")
    assert record_now["rev"] == 3 and record_now["avatar"]["style"] == "watch-shape-kitty" and record_now["voice"]["voice"] == "am_adam"
    assert not (entities / "new" / "appearance.json").exists()
    assert c.put("/api/gateway/entities/new/appearance", json={"clear": True}).status_code == 409
    # The legacy voice door applies the ladder for every role on an identity home.
    assert c.put("/api/gateway/entities/new/voice", json={"provider": "kokoro", "model": "kokoro-big", "voice": "af_heart"}).status_code == 409
    # The list row draws the record's look under `appearance` too.
    row = next(e for e in c.get("/api/gateway/entities").json()["entities"] if e["slug"] == "new")
    assert row["appearance"] == {"color": new_record["avatar"]["color"], "shape": "kitty"} and row["identity"]["rev"] == 3
    old_row = next(e for e in c.get("/api/gateway/entities").json()["entities"] if e["slug"] == "old")
    assert old_row["identity"] is None and "appearance" not in old_row


def test_a_voiceless_default_refuses_a_legacy_look_change_and_the_tts_lane(door, monkeypatch) -> None:
    import abstractgateway.entity_identity as ei

    monkeypatch.setattr(ei, "gateway_default_voice", lambda data_dir: None)
    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    assert _identity_file(entities, "castor")["voice"] is None
    sentence = "Castor has no voice yet: choose one in its Identity settings or set the Gateway default voice"
    r = c.put("/api/gateway/entities/castor/appearance", json={"color": "#112233"})
    assert r.status_code == 400 and r.json()["detail"].startswith(sentence), r.text  # the ladder's voice rung
    assert c.get("/api/gateway/entities/castor/voice").json()["note"] == sentence
    r = c.post("/api/gateway/entities/castor/voice/tts", json={"text": "hello"})
    assert r.status_code == 409 and r.json()["detail"] == sentence


# --------------------------------------------------------------------------- the TTS lanes


def test_the_tts_lanes_speak_the_record_voice_with_its_ref_and_never_another(door, monkeypatch) -> None:
    from fastapi import HTTPException

    import abstractgateway.routes.gateway as gw

    c = door["c"]
    _born(c, "Castor")
    assert c.put("/api/gateway/entities/castor/identity", json=CHOSEN).status_code == 200
    asked: List[Any] = []

    async def fake_tts(scope, req):
        asked.append((req.provider, req.model, req.voice))
        if req.voice == "am_adam":
            raise HTTPException(status_code=424, detail="the engine has no am_adam model")
        return {"ok": True}

    monkeypatch.setattr(gw, "voice_tts", fake_tts)
    r = c.post("/api/gateway/entities/castor/voice/tts", json={"text": "hello"})
    # The record's voice was asked, its failure is the answer: never the default voice in its place.
    assert r.status_code == 424 and asked == [("kokoro", "kokoro-82m", "am_adam")], r.text

    async def ok_tts(scope, req):
        return {"ok": True}

    monkeypatch.setattr(gw, "voice_tts", ok_tts)
    r = c.post("/api/gateway/entities/castor/voice/tts", json={"text": "hello"})
    assert r.json()["voice_source"] == "identity" and r.json()["identity_ref"] == {"of": "entity:castor", "rev": 2}
    assert r.headers["X-Identity-Ref"] == "entity:castor#2"
    # A request naming its own voice passes untouched, with no ref (it is not the identity's voice).
    r = c.post("/api/gateway/entities/castor/voice/tts", json={"text": "hello", "voice": "af_heart"})
    assert r.json()["voice_source"] == "request" and "identity_ref" not in r.json() and "X-Identity-Ref" not in r.headers


# --------------------------------------------------------------------------- visits, meets, public rows


class _Router:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def generate(self, **kwargs):
        system = str(kwargs.get("system_prompt") or "")
        tag = "castor" if "Castor" in system else ("pollux" if "Pollux" in system else "entity")
        return self._Reply(f"[{tag} replies]")


def _llm(monkeypatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _Router())


def _visit_run(slug: str, run_id: str) -> Any:
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().entity_registry.get_entity_runtime(slug).run_store.load(run_id)


def test_a_visit_open_stamps_the_ref_beside_the_signed_stamp_and_a_person_origin(door, monkeypatch) -> None:
    from abstractgateway.entity_gate import verify_summon_stamp
    from abstractgateway.service import get_gateway_service

    _llm(monkeypatch)
    c = door["c"]
    _born(c, "Castor")
    opened = c.post("/api/gateway/entities/Castor/visit/open", json={})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["identity_ref"] == {"of": "entity:castor", "rev": 1}
    run = _visit_run("castor", body["run_id"])
    rt = run.vars["_runtime"]
    assert rt["identity_ref"] == {"of": "entity:castor", "rev": 1} and "identity_ref" not in rt["entity"]
    ok, why = verify_summon_stamp(rt["entity"], data_dir=Path(get_gateway_service().config.data_dir), run=run)
    assert ok, why
    # One person, one spelling: the origin's actor is the participant the door stamped.
    origin = run.vars["_meta"]["origin"]
    assert origin["kind"] == "person" and origin["actor"] == "person:admin" and "person:admin" in body["participants"]
    assert "identity_since" not in run.vars["_visit"]  # no previous summon: the runtime tells the latest change
    c.post(f"/api/gateway/entities/Castor/visit/{body['run_id']}/close", json={"closed_by": "operator", "reason": "t"})
    # The next visit is told changes since this one.
    from abstractgateway.entity_visits import previous_summon_at

    since = previous_summon_at(door["entities"], "castor")
    assert since
    second = c.post("/api/gateway/entities/Castor/visit/open", json={}).json()
    assert _visit_run("castor", second["run_id"]).vars["_visit"]["identity_since"] == since
    c.post(f"/api/gateway/entities/Castor/visit/{second['run_id']}/close", json={"closed_by": "operator", "reason": "t"})


def test_a_legacy_home_visit_carries_no_ref(door, monkeypatch) -> None:
    _llm(monkeypatch)
    c, entities = door["c"], door["entities"]
    _registry_home(c, entities, "Castor")
    body = c.post("/api/gateway/entities/Castor/visit/open", json={}).json()
    assert "identity_ref" not in body
    assert "identity_ref" not in _visit_run("castor", body["run_id"]).vars["_runtime"]
    c.post(f"/api/gateway/entities/Castor/visit/{body['run_id']}/close", json={"closed_by": "operator", "reason": "t"})


def test_meet_legs_relays_and_status_name_each_speaker(door, monkeypatch) -> None:
    _llm(monkeypatch)
    c = door["c"]
    _born(c, "Castor")
    _born(c, "Pollux")
    c.put("/api/gateway/entities/pollux/identity", json=CHOSEN)
    meet = c.post("/api/gateway/entities/meets/open", json={"entity_a": "Castor", "entity_b": "Pollux"}).json()
    assert meet["a"]["identity_ref"] == {"of": "entity:castor", "rev": 1}
    assert meet["b"]["identity_ref"] == {"of": "entity:pollux", "rev": 2}
    ex = c.post(f"/api/gateway/entities/meets/{meet['meet_id']}/relay", json={"opener": "a", "text": "Hello."}).json()
    assert ex["spoke"]["identity_ref"] == meet["a"]["identity_ref"] and ex["heard"]["identity_ref"] == meet["b"]["identity_ref"]
    st = c.get(f"/api/gateway/entities/meets/{meet['meet_id']}").json()
    assert st["a"]["identity_ref"] != st["b"]["identity_ref"]
    c.post(f"/api/gateway/entities/meets/{meet['meet_id']}/close", json={})


def test_public_update_rows_carry_the_visit_ref_and_none_without_one(door, monkeypatch) -> None:
    from types import SimpleNamespace

    import abstractgateway.entity_session_tools as est

    def rows_for(vars_: Dict[str, Any]) -> List[Dict[str, Any]]:
        record = {"status": "completed", "effect": {"type": "emit_event", "payload": {"name": "public_update", "payload": {
            "run_id": "r1", "id": "u1", "text": "Found it.", "kind": "finding"}}}, "ended_at": "2026-11-04T10:00:00+00:00", "seq": 1}
        run = SimpleNamespace(run_id="r1", session_id="s1", vars=vars_, waiting=None)
        er = SimpleNamespace(run_store=SimpleNamespace(), ledger_store=SimpleNamespace(list=lambda rid: [record]))
        host = SimpleNamespace(_load_visit=lambda name, rid, need_spec=False: (er, run, None, None))
        monkeypatch.setattr(est, "_participant", lambda run: "person:admin")
        return est.public_conversation(host, "castor", "r1")["public_updates"]

    with_ref = rows_for({"_runtime": {"identity_ref": {"of": "entity:castor", "rev": 3}}})
    assert with_ref[0]["identity_ref"] == {"of": "entity:castor", "rev": 3}
    assert "identity_ref" not in rows_for({"_runtime": {}})[0]


def test_a_summon_stamps_the_person_origin_in_the_participants_form(monkeypatch) -> None:
    from abstractgateway.entity_visits import person_origin, person_participant

    origin = person_origin(actor=person_participant(["entity:pollux", "person:laurent"]), client="summon")
    assert origin == {"kind": "person", "actor": "person:laurent", "client": "summon", "ref": {}}
    assert person_participant(["entity:pollux"]) == "person:operator"
    import inspect

    import abstractgateway.routes.entities as routes

    # The summon door stamps the same form as its participants (`person:<caller_id>`).
    src = inspect.getsource(routes._summon_core)
    assert 'person_origin(actor=f"person:{caller_id}", client="summon")' in src
    assert 'participants: List[str] = [f"person:{caller_id}", home.entity_id]' in src


# --------------------------------------------------------------------------- the identity card


def test_the_identity_card_shows_biography_marker_rows_with_their_time(door) -> None:
    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    assert c.put("/api/gateway/entities/castor/identity", json=CHOSEN).status_code == 200
    with (entities / "castor" / "state_history.jsonl").open("a") as fh:
        fh.write(json.dumps({"marker": "phase_changed", "from": "visit", "to": "personal", "cause": "loop",
                             "written_by": "runtime", "at": "2026-11-04T09:00:00+00:00"}) + "\n")
    moments = c.get("/api/gateway/entities/castor/card", params={"moments_limit": 50}).json()["moments"]
    assert not [m for m in moments if m["kind"] == "state" and not m["at"]], moments
    phase = [m for m in moments if m["kind"] == "phase_changed"]
    assert phase and phase[0]["at"] == "2026-11-04T09:00:00+00:00" and phase[0]["details"]["to"] == "personal"
    identity = [m for m in moments if m["kind"] == "identity_changed"]
    # rev 1 (the birth default: biography row only) and rev 2 (the door's host marker wins its row).
    assert sorted(m["details"].get("rev") for m in identity) == [1, 2] and all(m["at"] for m in identity)


def test_an_unreadable_record_is_named_without_its_absolute_path(door) -> None:
    """The answer names the entity's file (`castor/identity.json`) and the reason; the gateway log
    keeps the absolute path."""
    c, entities = door["c"], door["entities"]
    _born(c, "Castor")
    (entities / "castor" / "identity.json").write_text("{not json", encoding="utf-8")
    for r in (c.get("/api/gateway/entities/castor/identity"), c.get("/api/gateway/entities/castor/voice"),
              c.post("/api/gateway/entities/castor/voice/tts", json={"text": "hi"})):
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "castor/identity.json" in detail and str(entities) not in detail, detail


def test_the_sentences_say_what_is_missing_and_what_to_do_never_the_default_voice(door, monkeypatch) -> None:
    import abstractgateway.entity_settings_access as esa

    c = door["c"]
    _born(c, "Castor")
    monkeypatch.setattr(esa, "offered_voices", lambda p, m: ([], [], "the engine is not installed"))
    r = c.put("/api/gateway/entities/castor/identity", json=CHOSEN)
    assert r.json()["detail"] == "Castor can't speak with am_adam: this gateway offers no kokoro voice right now (the engine is not installed). Choose a voice it offers."
    monkeypatch.setattr(esa, "offered_voices", lambda p, m: (["am_adam"], ["kokoro-82m"], None))
    r = c.put("/api/gateway/entities/castor/identity", json={**CHOSEN, "voice": {**CHOSEN["voice"], "model": "kokoro-x"}})
    assert r.json()["detail"] == "Castor can't speak with kokoro-x: it isn't a kokoro speech model this gateway offers. Choose one of: kokoro-82m."
    for refused in (r,):
        assert "default voice" not in refused.json()["detail"]


def test_a_revision_serves_the_real_reason_it_was_written(door) -> None:
    c = door["c"]
    _born(c, "Castor")
    assert c.put("/api/gateway/entities/castor/identity", json=CHOSEN).status_code == 200
    first = c.get("/api/gateway/entities/castor/identity?rev=1").json()["identity"]["change"]
    assert first["cause"] == "recorded_default" and first["written_by"] == "default" and first["at"]
    now = c.get("/api/gateway/entities/castor/identity").json()["identity"]["change"]
    assert now["cause"] == "chosen" and now["written_by"] == "person:admin" and now["at"]


def test_the_visit_state_carries_the_identity_ref_before_any_update(door, monkeypatch) -> None:
    _llm(monkeypatch)
    c = door["c"]
    _born(c, "Castor")
    opened = c.post("/api/gateway/entities/Castor/visit/open", json={"conversation": True}).json()
    state = c.get(f"/api/gateway/entities/Castor/visit/{opened['run_id']}/state")
    assert state.status_code == 200, state.text
    assert state.json()["identity_ref"] == {"of": "entity:castor", "rev": 1}
    c.post(f"/api/gateway/entities/Castor/visit/{opened['run_id']}/close", json={"closed_by": "operator", "reason": "t"})
