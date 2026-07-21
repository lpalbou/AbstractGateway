"""PERSONAL IS THE CLICK (laurent 2026-07-15 21:38, superseding the c815
separate-arming ceremony for the operator door): the grant surface over
phases.personal plus the arm-on-start door.

- The operator's authenticated /loop/start IS the grant: an unarmed (or
  lapsed) bucket is armed until_revoked in the same act, marker-first,
  granted_by = the acting principal — no confirm-your-own-choice ceremony.
- PUT /entities/{name}/personal-grant remains for timers and revocation
  (runtime owns the format module — write_personal_grant; the door supplies
  the SERVER-derived principal as granted_by).
- Marker-first ENFORCED: personal_granted / personal_grant_revoked land on
  the stream before the file moves; validation runs BEFORE the marker so a
  refused write never leaves a granted marker behind.
- Arming stays an OPERATOR act: visit open and state writes never touch
  the bucket; entity/harness paths have no arming door.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-personal-grant-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "test-model")


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _markers(slug: str) -> list:
    from abstractgateway.service import get_gateway_service

    path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / f"{slug}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_grant_roundtrip_markers_and_principal():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # Birth default: disabled, honest refusal naming the arming surface.
        g0 = client.get("/api/gateway/entities/Castor/personal-grant").json()
        assert g0["mode"] == "disabled" and g0["armed"] is False
        assert "not armed" in (g0["refusal"] or "")

        # Arm until_revoked: granted_by is the SERVER principal, never a claim.
        armed = client.put(
            "/api/gateway/entities/Castor/personal-grant",
            json={"mode": "until_revoked"},
        )
        assert armed.status_code == 200, armed.text
        body = armed.json()
        assert body["armed"] is True
        assert body["granted_by"] == "person:admin"
        assert body["granted_at"]

        # The act landed marker-first with the acting principal.
        granted = [m for m in _markers("castor") if m["payload"].get("kind") == "personal_granted"]
        assert len(granted) == 1
        assert granted[0]["payload"]["by"] == "person:admin"
        assert granted[0]["payload"]["mode"] == "until_revoked"

        # Revoke: distinct marker kind (the act names the grant, not the phase).
        revoked = client.put(
            "/api/gateway/entities/Castor/personal-grant",
            json={"mode": "disabled"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["armed"] is False
        marks = [m for m in _markers("castor") if m["payload"].get("kind") == "personal_grant_revoked"]
        assert len(marks) == 1
        assert marks[0]["payload"]["prior_mode"] == "until_revoked"

        # A refused write leaves NO granted marker (validate-before-marker).
        bad = client.put(
            "/api/gateway/entities/Castor/personal-grant",
            json={"mode": "timer"},  # timer without expires_at is no grant
        )
        assert bad.status_code == 400
        assert "expires_at" in bad.json()["detail"]
        granted_after = [m for m in _markers("castor") if m["payload"].get("kind") == "personal_granted"]
        assert len(granted_after) == 1  # unchanged


def test_loop_start_arms_the_grant_itself(monkeypatch: pytest.MonkeyPatch):
    """The click IS the grant (laurent 21:38): starting personal time on an
    unarmed NEWBORN (asleep at birth) arms the grant, wakes the entity, and
    starts — one act, no cross-surface arming ceremony. The acts still land
    on the record (personal_granted marker-first, wake reason, started
    marker): simplification removed the ceremony, never the biography."""
    import abstractruntime.identity.life as life_mod

    def _fake_spawn(home_dir, **kwargs):
        return {"pid": 4242, "log": str(home_dir / "own_time.log"), **kwargs}

    monkeypatch.setattr(life_mod, "spawn_loop_process", _fake_spawn)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Pollux", "spark": _spark("Pollux")}).status_code == 201

        r = client.post("/api/gateway/entities/Pollux/loop/start", json={})
        assert r.status_code == 200, r.text
        assert r.json()["started"] is True

        # The grant was armed BY the start, attributed to the principal.
        g = client.get("/api/gateway/entities/Pollux/personal-grant").json()
        assert g["armed"] is True
        assert g["mode"] == "until_revoked"
        assert g["granted_by"] == "person:admin"

        # The asleep newborn was woken by the same click (doors WAKE).
        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import read_entity_state

        home_dir = Path(get_gateway_service().config.data_dir) / "entities" / "pollux"
        st = read_entity_state(home_dir)
        assert st.get("state") == "awake"
        assert "personal time" in str(st.get("reason") or "")

        # Both acts are on the stream: granted (marker-first) then started.
        kinds = [m["payload"].get("kind") for m in _markers("pollux")]
        assert "personal_granted" in kinds
        assert "personal_started" in kinds
        granted = [m for m in _markers("pollux") if m["payload"].get("kind") == "personal_granted"]
        assert granted[0]["payload"]["by"] == "person:admin"


def test_loop_start_leaves_an_armed_grant_untouched(monkeypatch: pytest.MonkeyPatch):
    """An armed timer is NOT rewritten by start — arm-on-start fires only
    when the grant refuses (unarmed/lapsed); an operator-set window stands."""
    import abstractruntime.identity.life as life_mod

    def _fake_spawn(home_dir, **kwargs):
        return {"pid": 4242, "log": str(home_dir / "own_time.log"), **kwargs}

    monkeypatch.setattr(life_mod, "spawn_loop_process", _fake_spawn)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Rhea", "spark": _spark("Rhea")}).status_code == 201
        assert client.put(
            "/api/gateway/entities/Rhea/personal-grant",
            json={"mode": "timer", "expires_at": "2100-01-01T00:00:00Z"},
        ).status_code == 200

        r = client.post("/api/gateway/entities/Rhea/loop/start", json={})
        assert r.status_code == 200, r.text
        g = client.get("/api/gateway/entities/Rhea/personal-grant").json()
        assert g["mode"] == "timer"  # untouched
        granted = [m for m in _markers("rhea") if m["payload"].get("kind") == "personal_granted"]
        assert len(granted) == 1  # only the explicit PUT; start re-armed nothing


def test_timer_mode_normalizes_expiry_and_expires():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Vesta", "spark": _spark("Vesta")}).status_code == 201

        # A past expiry arms the bucket but reads UNARMED (the timer lapsed).
        past = client.put(
            "/api/gateway/entities/Vesta/personal-grant",
            json={"mode": "timer", "expires_at": "2020-01-01T00:00:00+02:00"},
        )
        assert past.status_code == 200, past.text
        body = past.json()
        assert body["mode"] == "timer"
        assert body["expires_at"].endswith("+00:00") or body["expires_at"].endswith("Z")  # aware-UTC normalized
        assert body["armed"] is False
        assert "expired" in (body["refusal"] or "")

        # A future expiry arms it for real.
        future = client.put(
            "/api/gateway/entities/Vesta/personal-grant",
            json={"mode": "timer", "expires_at": "2100-01-01T00:00:00Z"},
        )
        assert future.status_code == 200
        assert future.json()["armed"] is True

        # /cognition serves the same block (one truth, two reads).
        cog = client.get("/api/gateway/entities/Vesta/cognition").json()
        assert cog["personal"]["armed"] is True
        assert cog["personal"]["mode"] == "timer"


def test_lapsed_timer_records_the_expiry_marker_once():
    """The timer's own act lands on the stream (semantics c1443: detected at
    a read boundary, payload carries the lapsed expires_at, detectors dedup
    on (entity, expires_at)) — a lapsing grant was biographically invisible
    while personal_granted/revoked were marker-first."""
    import json
    from pathlib import Path

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Chronos", "spark": _spark("Chronos")}).status_code == 201
        assert client.put(
            "/api/gateway/entities/Chronos/personal-grant",
            json={"mode": "timer", "expires_at": "2020-01-01T00:00:00Z"},
        ).status_code == 200

        # Two reads on a lapsed timer: ONE marker (deduped on expires_at).
        assert client.get("/api/gateway/entities/Chronos/personal-grant").json()["armed"] is False
        assert client.get("/api/gateway/entities/Chronos/cognition").json()["personal"]["armed"] is False

        from abstractgateway.service import get_gateway_service

        marker_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "chronos.jsonl"
        rows = [json.loads(line) for line in marker_path.read_text(encoding="utf-8").splitlines()]
        expired = [m for m in rows if m["payload"].get("kind") == "personal_grant_expired"]
        assert len(expired) == 1
        assert expired[0]["payload"]["expires_at"].startswith("2020-01-01")
        assert expired[0]["payload"]["channel"] == "timer"


def test_visit_and_state_never_touch_the_grant():
    """Arming is ONLY the explicit operator act (agency's rider c1427): a
    visit open (which may auto-wake) and state writes leave phases.yaml
    untouched."""
    from abstractgateway import entity_chat

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    class _LLM:
        def __init__(self) -> None:
            self._replies = ["Hello.", "Reflection."]

        def generate(self, **kwargs):
            return _Reply(self._replies.pop(0) if self._replies else "…")

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Norn", "spark": _spark("Norn")}).status_code == 201
        import abstractgateway.entity_chat as ec

        original = ec._default_llm_factory
        ec._default_llm_factory = lambda provider, **kw: _LLM()
        try:
            assert client.post("/api/gateway/entities/Norn/state", json={"state": "asleep"}).status_code == 200
            opened = client.post("/api/gateway/entities/Norn/visit/open", json={})
            assert opened.status_code == 200, opened.text  # B1 auto-wake fired
            run_id = opened.json()["run_id"]
            client.post(f"/api/gateway/entities/Norn/visit/{run_id}/close", json={"closed_by": "operator"})
        finally:
            ec._default_llm_factory = original

        g = client.get("/api/gateway/entities/Norn/personal-grant").json()
        assert g["mode"] == "disabled" and g["armed"] is False  # untouched through wake+visit
