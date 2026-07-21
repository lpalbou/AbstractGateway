"""Per-entity voice (laurent dm#10, 2026-07-17; adversarial design review).

The choice is a FULL TRIPLE in the home (`<home>/voice.yaml` — a bare voice
id recreates the M1 cross-provider leak); PUT is marker-first
(`voice_changed`); resolution is late-bound, server-side, and anti-mixing
(the home triple applies only when the request names NO voice fields);
missing voice DEGRADES down the chain — never a refusal (voice is
presentation, not the mind).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def test_voice_file_round_trip_full_triple_required(tmp_path: Path) -> None:
    from abstractgateway.entity_voice import clear_entity_voice, read_entity_voice, write_entity_voice

    home = tmp_path / "home"
    home.mkdir()
    assert read_entity_voice(home) == {}

    with pytest.raises(ValueError):
        write_entity_voice(home, provider="supertonic", model="", voice="M2")
    with pytest.raises(ValueError):
        write_entity_voice(home, provider="", model="x", voice="M2")

    write_entity_voice(home, provider="supertonic", model="supertonic-3", voice="M2", speed=1.1)
    stored = read_entity_voice(home)
    assert stored == {"provider": "supertonic", "model": "supertonic-3", "voice": "M2", "speed": 1.1}

    # A partial file on disk (hand-edited) reads as UNSET — degrade, never
    # a half-applied voice.
    (home / "voice.yaml").write_text("voice: M2\n", encoding="utf-8")
    assert read_entity_voice(home) == {}

    assert clear_entity_voice(home) is True
    assert clear_entity_voice(home) is False
    assert read_entity_voice(home) == {}


def test_anti_mixing_resolution(tmp_path: Path) -> None:
    """The home triple applies ONLY to voice-field-free requests — filling
    gaps in a partial request would mix provider/voice identities (the
    'Unknown voice_id: M1' class)."""
    from abstractgateway.entity_voice import resolve_entity_voice_fields, write_entity_voice

    home = tmp_path / "home"
    home.mkdir()

    # No home choice, bare request: unset — downstream defaults resolve.
    fields, source = resolve_entity_voice_fields(home)
    assert fields == {} and source == "unset"

    write_entity_voice(home, provider="supertonic", model="supertonic-3", voice="M2")

    # Bare request: the entity's triple applies.
    fields, source = resolve_entity_voice_fields(home)
    assert source == "entity"
    assert fields["provider"] == "supertonic" and fields["voice"] == "M2"

    # ANY request voice field passes through untouched — never merged.
    for kwargs in (
        {"request_voice": "alloy"},
        {"request_provider": "piper"},
        {"request_model": "x"},
        {"request_profile": "p1"},
    ):
        fields, source = resolve_entity_voice_fields(home, **kwargs)
        assert fields == {} and source == "request", kwargs


def test_voice_put_is_marker_first_and_admin_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "voice-marker-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer voice-marker-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        # Unset reads honestly.
        r0 = client.get("/api/gateway/entities/Castor/voice")
        assert r0.status_code == 200 and r0.json()["source"] == "unset"

        # Partial choice refuses BEFORE any marker (full-triple rule).
        r1 = client.put("/api/gateway/entities/Castor/voice", json={"voice": "M2"})
        assert r1.status_code == 400

        # Full triple lands marker-first.
        r2 = client.put(
            "/api/gateway/entities/Castor/voice",
            json={"provider": "supertonic", "model": "supertonic-3", "voice": "M2"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["source"] == "entity" and r2.json()["voice"] == "M2"

        # Clear removes the choice; the transition is a second marker.
        r3 = client.put("/api/gateway/entities/Castor/voice", json={"clear": True})
        assert r3.status_code == 200 and r3.json()["source"] == "unset"

        from abstractgateway.service import get_gateway_service

        markers_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "castor.jsonl"
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "voice_changed"]
        assert len(changed) == 2
        assert changed[0]["old"] is None
        assert changed[0]["new"] == {"provider": "supertonic", "model": "supertonic-3", "voice": "M2"}
        assert changed[1]["old"]["voice"] == "M2" and changed[1]["new"] is None
        assert changed[0]["by"] == "person:admin"


def test_console_carries_the_voice_picker() -> None:
    """The entity-personal-voice room's console half (laurent: 'i do not see
    it at the level of the gateway'): the Substrate panel carries the voice
    picker with cascading provider/model/voice selects, audition-before-save
    (the fabricated-selection lesson), save + clear, and admin gating."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    for el in ("entity-voice-provider", "entity-voice-model", "entity-voice-voice",
               "entity-voice-audition", "entity-voice-save", "entity-voice-clear"):
        assert f'id="{el}"' in html, f"missing {el}"
    # The audition speaks through the ENTITY'S OWN lane (anti-mixing:
    # explicit fields win) — never the generic run route.
    assert "/voice/tts" in html and "entityVoiceAudition" in html
    # Mutations are admin-gated in the UI as at the server.
    assert '"entity-voice-save", "entity-voice-clear"' in html


def test_unset_entity_serves_the_resolved_effective_default(monkeypatch) -> None:
    """Inheritance semantics (laurent dm#68): an unset entity serves the
    fully-resolved triple it would speak with (source=gateway-default);
    no configured default = honest absence + label, never a fabricated
    triple; a set entity's effective IS its own choice."""
    import pytest as _pytest
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "voice-eff-secret")
    from abstractgateway.app import app
    from abstractgateway.routes import entities as entities_routes

    def _defaults_with_voice(**kwargs):
        return {"ok": True, "routes": [
            {"kind": "output", "modality": "voice", "task": "text_to_speech",
             "provider": "supertonic", "model": "supertonic-3", "options": {"voice": "M2"}},
        ]}

    with TestClient(app, headers={"Authorization": "Bearer voice-eff-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        import abstractgateway.capability_defaults as capdef

        monkeypatch.setattr(capdef, "gateway_capability_defaults_payload", _defaults_with_voice)
        body = client.get("/api/gateway/entities/Castor/voice").json()
        assert body["source"] == "unset"
        assert body["effective"] == {"provider": "supertonic", "model": "supertonic-3",
                                     "voice": "M2", "source": "gateway-default"}

        # No default configured: honest absence, labeled.
        monkeypatch.setattr(capdef, "gateway_capability_defaults_payload",
                            lambda **kw: {"ok": True, "routes": []})
        body = client.get("/api/gateway/entities/Castor/voice").json()
        assert "effective" not in body
        assert "engine decides" in body.get("note", "")

        # A set entity's effective is its own choice.
        put = client.put("/api/gateway/entities/Castor/voice",
                         json={"provider": "piper", "model": "piper-1", "voice": "fr_FR-upmc"})
        assert put.status_code == 200, put.text
        body = client.get("/api/gateway/entities/Castor/voice").json()
        assert body["source"] == "entity"
        assert body["effective"]["source"] == "entity"
        assert body["effective"]["voice"] == "fr_FR-upmc"
