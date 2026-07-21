"""The B3 spend wire: GET /entities/{name}/cognition (dispatch c1340 2c).

Laurent's 04:58 finding — "unclear when it's working and consuming
credits" — needs ONE read the observer board tile and the entity app's
meter can both poll: working-now + tick phase + BILLED token spend.
These tests pin the composition:

- spend folds the per-home run ledger's completed llm_call records
  (result.usage.total_tokens — recorded by the entity LLM handler),
  lifetime across the home store plus the live visit run tree;
- working is store-read state, never fabricated (parked visit = not
  working; loop phase=day = working);
- the loop's home-direct cognition gap is a labeled #FALLBACK, never a
  silent zero presented as truth.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-cognition-shared-secret"


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


class _UsageReply:
    """A reply that carries billed usage — the shape the real provider
    returns and the entity LLM handler records onto the ledger."""

    def __init__(self, content: str, total_tokens: int) -> None:
        self.content = content
        self.usage = {"total_tokens": total_tokens, "input_tokens": total_tokens - 5, "output_tokens": 5}


class _ScriptedUsageLLM:
    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        if not self._replies:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        return self._replies.pop(0)


def _install_llm(monkeypatch: pytest.MonkeyPatch, replies) -> None:
    from abstractgateway import entity_chat

    llm = _ScriptedUsageLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)


def test_cognition_folds_billed_spend_from_the_home_run_ledger(monkeypatch: pytest.MonkeyPatch):
    _install_llm(monkeypatch, [
        _UsageReply("Hello Laurent — I am here.", total_tokens=120),
        _UsageReply("Looking back: a good visit.", total_tokens=80),  # reflection
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # Newborn = sleep (c1503); the cold read says so, then wake.
        newborn = client.get("/api/gateway/entities/Castor/cognition").json()
        assert newborn["phase"] == "sleep" and newborn["state"]["state"] == "asleep"
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

        # Before any cognition: zeros, honest, no crash on an empty home store.
        cold = client.get("/api/gateway/entities/Castor/cognition")
        assert cold.status_code == 200, cold.text
        body = cold.json()
        assert body["working"] is False
        assert body["visit"]["open"] is False
        assert body["spend"]["lifetime"]["tokens_total"] == 0
        assert body["spend"]["source"] == "home-run-ledger+loop-spend"
        # The composite axes ride the same read (console adversary P1-1):
        # state + the ONE mutually-exclusive phase + the grant axis (fail-
        # closed: no phases.yaml = disabled, never a fabricated armed).
        # PHASE IS TOTAL WHILE ALIVE (laurent c203, 2026-07-20: "awake is
        # NOT a state" — the old None here rendered the awake-idle hanging
        # state he rejects). Idle folds to sleep, the resting default;
        # sleep_detail says it honestly runs no consolidation.
        assert body["phase"] == "sleep"
        assert body["sleep_detail"] == "resting"
        assert body["phase_source"] == "actual"
        assert body["liveness"] == "alive"
        assert "frozen" not in body  # retired from the serve (c1559)
        assert body["state"]["state"] == "awake"
        assert body["state"]["liveness"] == "alive"
        assert body["personal"]["mode"] == "disabled"
        assert body["personal"]["armed"] is False
        assert body["personal"]["source"] == "phases.yaml"
        # Loop spend folds in (runtime read_loop_spend; zeros for a home
        # that never ticked — honest, not estimated).
        assert body["spend"]["loop"]["tokens_total"] == 0
        assert body["spend"]["source"] == "home-run-ledger+loop-spend"

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "Hello — are you there?"},
        )
        assert turned.status_code == 200, turned.text

        warm = client.get("/api/gateway/entities/Castor/cognition").json()
        # Parked visit = present but not consuming; billed spend is REAL.
        assert warm["working"] is False
        assert warm["phase"] == "visit"  # a live durable visit IS the phase (ruled key, not "visiting")
        assert warm["visit"]["open"] is True and warm["visit"]["run_id"] == run_id
        assert warm["spend"]["lifetime"]["llm_calls"] >= 1
        assert warm["spend"]["lifetime"]["tokens_total"] == 120
        assert warm["spend"]["live_visit"]["run_id"] == run_id
        assert warm["spend"]["live_visit"]["tokens_total"] == 120

        closed = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "operator", "reason": "test done"},
        )
        assert closed.status_code == 200, closed.text

        # Lifetime keeps the closed visit's spend (120 + 80 reflection); live slot empties.
        done = client.get("/api/gateway/entities/Castor/cognition").json()
        assert done["visit"]["open"] is False
        assert done["spend"]["lifetime"]["tokens_total"] == 200
        assert done["spend"]["live_visit"] is None


def test_cognition_working_flag_and_loop_spend_fold(monkeypatch: pytest.MonkeyPatch):
    """A running day-phase loop flips working=True; loop usage folds from
    <home>/loop_spend.json (runtime 1154194) so the old #FALLBACK gap label
    is GONE — billed loop spend is real now, never estimated."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Pollux", "spark": _spark("Pollux")}).status_code == 201
        # Newborn = sleep (c1503): wake so the day-phase fold reads personal.
        assert client.post("/api/gateway/entities/Pollux/state", json={"state": "awake"}).status_code == 200

        from abstractgateway import entity_loop
        from abstractgateway.service import get_gateway_service

        monkeypatch.setattr(
            entity_loop,
            "loop_status",
            lambda home_dir: {"running": True, "phase": "day", "pid": 4242},
        )
        home_dir = (
            __import__("pathlib").Path(get_gateway_service().config.data_dir) / "entities" / "pollux"
        )
        import json as _json

        (home_dir / "loop_spend.json").write_text(
            _json.dumps({"llm_calls": 7, "tool_calls": 2, "tokens_total": 4321, "ticks": 7,
                         "source": "loop-home-direct", "updated_at": "2026-07-13T10:00:00+00:00"}),
            encoding="utf-8",
        )
        body = client.get("/api/gateway/entities/Pollux/cognition").json()
        assert body["working"] is True
        assert body["loop"]["phase"] == "day"
        assert body["phase"] == "personal" and body["resting"] is False  # mid-day loop = the personal phase
        assert body["spend"]["loop"]["tokens_total"] == 4321
        assert body["spend"]["lifetime"]["tokens_total"] == 4321  # folded in
        assert not any("loop cognition spend not included" in w for w in body.get("warnings", []))


def test_cognition_unknown_entity_is_404():
    with _client() as client:
        assert client.get("/api/gateway/entities/Nobody/cognition").status_code == 404


def test_phase_enum_is_the_ruled_closed_set_with_the_liveness_axis():
    """The closed set IS pinnable now (totality ruled 13:46; semantics lifted
    the contingency at c1472): phase ∈ {visit, work, personal, sleep};
    TOTAL WHILE ALIVE since laurent c203 (2026-07-20: "awake is NOT a
    state") — None survives only under the kill switch (liveness=stopped).
    asleep folds to sleep; idle folds to sleep (resting default). LIVENESS
    AXIS (c1559): `frozen` is RETIRED; the one derived field is liveness
    alive|stopped (paused => stopped — the kill switch, never a phase)."""
    RULED = {"visit", "work", "personal", "sleep"}
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Norns", "spark": _spark("Norns")}).status_code == 201

        # Newborn = sleep (the artifact's initial_phase, folded from the
        # birth state create() writes — c1503). Sleep is ALIVE (reachable).
        born = client.get("/api/gateway/entities/Norns/cognition").json()
        assert born["phase"] == "sleep" and born["phase"] in RULED
        assert born["liveness"] == "alive"

        # state=awake with nothing running: the resting default — sleep,
        # honestly detailed (never the awake-idle dwelling c203 rejects).
        assert client.post("/api/gateway/entities/Norns/state", json={"state": "awake"}).status_code == 200
        idle = client.get("/api/gateway/entities/Norns/cognition").json()
        assert idle["phase"] == "sleep" and idle["phase"] in RULED
        assert idle["sleep_detail"] == "resting"
        assert idle["liveness"] == "alive"

        assert client.post("/api/gateway/entities/Norns/state", json={"state": "asleep"}).status_code == 200
        asleep = client.get("/api/gateway/entities/Norns/cognition").json()
        assert asleep["phase"] == "sleep" and asleep["liveness"] == "alive"
        assert asleep["phase"] in RULED

        assert client.post("/api/gateway/entities/Norns/state", json={"state": "paused"}).status_code == 200
        paused = client.get("/api/gateway/entities/Norns/cognition").json()
        assert paused["liveness"] == "stopped"
        assert "frozen" not in paused  # retired — one axis, one spelling
        assert paused["phase"] is None  # the kill switch is not a phase — nothing active
        assert paused["state"]["state"] == "paused"  # at-rest key unchanged (engraved)
        assert paused["state"]["liveness"] == "stopped"


def test_state_verb_survives_a_broken_marker_lane(monkeypatch: pytest.MonkeyPatch):
    """Live find (2026-07-14): a marker-lane flood exhausted the 999-per-base
    fan-out budget and every state verb 500'd while the state file HAD
    changed — the kill switch looked broken exactly when bookkeeping broke.
    The act's success must be served with a labeled record gap, never an
    error that hides an applied act."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Janus", "spark": _spark("Janus")}).status_code == 201

        from abstractgateway import entity_replay

        def _boom(**_kwargs):
            raise RuntimeError("host marker fan-out exhausted at journal seq 42 for 'janus' (test)")

        monkeypatch.setattr(entity_replay, "record_host_marker", _boom)
        r = client.post("/api/gateway/entities/Janus/state", json={"state": "paused", "reason": "stop under flood"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"]["state"] == "paused"
        assert body["marker_seq"] is None
        assert "#FALLBACK state applied but the host marker failed" in str(body.get("warning") or "")
        # The stop ACTUALLY landed: the serve says stopped.
        st = client.get("/api/gateway/entities/Janus/state").json()
        assert st["state"] == "paused" and st["liveness"] == "stopped"


def test_liveness_rides_every_operator_facing_state_serve():
    """c1559 spelling point 2: ONE derived field on EVERY operator-facing
    serve of state — /state, the board feed (list rows), and the card's
    state overlay all carry liveness from the single derivation rule."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Vesta", "spark": _spark("Vesta")}).status_code == 201

        # Newborn asleep => alive on all three serves.
        st = client.get("/api/gateway/entities/Vesta/state").json()
        assert st["state"] == "asleep" and st["liveness"] == "alive"
        rows = client.get("/api/gateway/entities").json()["entities"]
        mine = next(r for r in rows if r.get("slug") == "vesta")
        assert mine["state"]["liveness"] == "alive"
        card = client.get("/api/gateway/entities/Vesta/card").json()
        assert card["state"]["liveness"] == "alive"

        # Stopped (paused) => stopped on all three serves, same derivation.
        assert client.post("/api/gateway/entities/Vesta/state", json={"state": "paused"}).status_code == 200
        st2 = client.get("/api/gateway/entities/Vesta/state").json()
        assert st2["state"] == "paused" and st2["liveness"] == "stopped"
        rows2 = client.get("/api/gateway/entities").json()["entities"]
        mine2 = next(r for r in rows2 if r.get("slug") == "vesta")
        assert mine2["state"]["liveness"] == "stopped"
        card2 = client.get("/api/gateway/entities/Vesta/card").json()
        assert card2["state"]["liveness"] == "stopped"
