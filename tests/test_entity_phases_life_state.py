"""`GET /entities/{name}/life_state` — the phase bar's one read (operator ruling 2026-11-08; root
untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2 §1, §5). One derivation shared with
/cognition and the tile; six served words (`idle` and `stopped` are new); `rev` versions the whole
snapshot and survives restarts; each new rev is announced once as a `phase_changed` host marker,
recoverably. Hermetic gateway; the own-time loop is a real child process."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-life-state-secret"
NEW_KEYS = {"name", "rev", "phase", "stopped", "toggles", "personal_expires_at", "since", "sentence", "next",
            "blocked", "can", "visit", "work", "sleep"}
OLD_KEYS = {"entity_id", "posture", "chat_open", "chat_id", "state", "state_mode", "state_reason",
            "own_time_running", "own_time_phase", "visit_run_id"}


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(
        "abstractgateway.entity_chat.gateway_text_mind",
        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None},
    )


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        return self._Reply(self._replies.pop(0) if self._replies else "(unused)")


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _home_dir():
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / "castor"


@pytest.fixture
def loops():
    procs = []

    def _start(home_dir, phase="between"):
        from entity_fake_loop import spawn

        proc = spawn(home_dir, phase)
        procs.append(proc)
        return proc

    yield _start
    for p in procs:
        p.kill()


def _ls(client):
    r = client.get("/api/gateway/entities/Castor/life_state")
    assert r.status_code == 200, r.text
    return r.json()


def _markers(home_dir):
    path = home_dir.parent / ".host_stream" / "castor.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r["payload"] for r in rows if (r.get("payload") or {}).get("kind") == "phase_changed"]


def test_every_field_is_present_and_a_newborn_is_asleep_until_woken():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        body = _ls(client)
        assert NEW_KEYS | OLD_KEYS <= set(body)
        assert body["phase"] == "sleep" and body["stopped"] is False
        assert body["toggles"] == {"work": True, "personal": False}
        assert body["sleep"]["kind"] == "operator" and body["sleep"]["ends"] is None
        assert body["sentence"] == "Asleep until you wake him."
        assert body["visit"] is None and body["work"] is None and body["blocked"] is None
        assert body["can"]["sleep_now"] == {"allowed": False, "sentence": "He is already resting; a sleep cannot follow a sleep."}
        assert body["can"]["visit"] == {"allowed": True, "sentence": None}
        assert body["rev"] >= 1 and body["since"]


def test_awake_with_nothing_running_is_idle_never_sleep():
    """The old fold painted an awake, visitable entity with no loop as `sleep` (IDLE-IS-SLEEP);
    the aletheia report's wake that "did nothing" was this lie."""
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home_dir(), "awake", reason="woken")
        body = _ls(client)
        assert body["phase"] == "idle" and body["posture"] == "idle"
        assert body["sentence"] == "Awake and reachable; nothing of his own is running."
        cog = client.get("/api/gateway/entities/Castor/cognition").json()
        assert cog["phase"] == "idle"


def test_a_gate_sleep_with_no_loop_is_idle_root_1178():
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home_dir(), "asleep", reason="personal ended (grant_revoked) - the ruled landing is sleep",
                           written_by="grant-gate", wake_at="2099-01-01T00:00:00+00:00")
        body = _ls(client)
        assert body["phase"] == "idle" and body["sleep"] is None
        assert body["can"]["visit"]["allowed"] is True


def test_stopped_personal_work_and_a_self_sleep(loops):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        write_personal_grant(home, mode="until_revoked", granted_by="person:admin")
        write_entity_state(home, "awake", reason="woken")
        loops(home, "between")
        body = _ls(client)
        assert body["phase"] == "personal" and body["sentence"] == "On his own time, resting."
        assert body["toggles"]["personal"] is True and body["posture"] == "resting"

        (home / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        loops(home, "day")
        body = _ls(client)
        assert body["phase"] == "work" and body["work"] == {"kind": "work_order", "run_id": None, "since": None, "finishing": False}

        (home / "work_order.md").unlink()
        loops(home, "between")
        write_entity_state(home, "asleep", reason="self-elected sleep: memory saturated", written_by="self",
                           wake_at="2099-01-01T00:30:00+00:00")
        body = _ls(client)
        assert body["phase"] == "sleep" and body["sleep"]["kind"] == "self"
        assert body["sleep"]["ends"] == "2099-01-01T00:30:00+00:00"
        assert body["sleep"]["reason"] == "self-elected sleep: memory saturated"

        write_entity_state(home, "paused", reason="kill switch")
        body = _ls(client)
        assert body["phase"] == "stopped" and body["stopped"] is True and body["posture"] == "paused"
        assert body["can"]["visit"] == {"allowed": False, "sentence": "Stopped. Resume to visit."}
        assert body["can"]["toggle_work"]["allowed"] is False
        assert client.get("/api/gateway/entities/Castor/cognition").json()["phase"] == "stopped"


def test_a_crashed_loop_is_idle_blocked_with_a_retry(loops):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        write_personal_grant(home, mode="until_revoked", granted_by="person:admin")
        write_entity_state(home, "awake", reason="woken")
        proc = loops(home, "day")
        proc.kill()
        proc.wait(5)
        body = _ls(client)
        assert body["phase"] == "idle"
        assert body["blocked"] == {"code": "loop_crashed", "sentence": "His own time stopped unexpectedly. Retry to start it again.",
                                   "action": "retry"}


def test_an_open_visit_is_the_visit_phase(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["(unused)"]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        body = _ls(client)
        assert body["phase"] == "visit" and body["visit_run_id"] == opened.json()["run_id"]
        v = body["visit"]
        assert v["lane"] == "visit" and v["run_id"] == opened.json()["run_id"] and v["chat_id"] is None
        assert v["participants"] == ["person:admin"] and v["since"] and v["waiting_for"] is None
        assert body["can"]["sleep_now"] == {"allowed": False, "sentence": "He is in a visit; end it to let him sleep."}


def test_rev_versions_the_snapshot_survives_a_restart_and_is_announced_once_per_rev():
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway.entity_phase import read_record, write_record

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        a = _ls(client)
        assert _ls(client) == a  # nothing changed: same rev, same content
        write_entity_state(home, "awake", reason="woken")
        b = _ls(client)
        assert b["rev"] == a["rev"] + 1 and b["phase"] == "idle"
        marks = _markers(home)
        assert [m["rev"] for m in marks] == sorted({m["rev"] for m in marks})  # one per rev, in order
        last = marks[-1]
        assert last["rev"] == b["rev"] and last["from"] == "sleep" and last["to"] == "idle"
        assert last["toggles"] == b["toggles"] and last["sentence"] == b["sentence"] and last["since"] == b["since"]

        # A crash between the rev write and the marker: the next read announces it.
        rec = read_record(home.parent, "castor")
        write_entity_state(home, "paused", reason="stop")
        _ls(client)
        rec = read_record(home.parent, "castor")
        rec["published_rev"] = rec["rev"] - 1
        write_record(home.parent, "castor", rec)
        before = len(_markers(home))
        _ls(client)
        after = _markers(home)
        assert len(after) == before  # the rev was already announced (dedup by rev): no second marker
        assert read_record(home.parent, "castor")["published_rev"] == rec["rev"]
        rev_before_restart = _ls(client)["rev"]

    with _client() as client:  # a new app lifespan over the same data dir
        assert _ls(client)["rev"] == rev_before_restart


def test_unknown_entity_is_404_with_a_code():
    with _client() as client:
        r = client.get("/api/gateway/entities/Nobody/life_state")
        assert r.status_code == 404
        assert r.json()["code"] == "entity_not_found" and r.json()["detail"]["life_state"] is None
