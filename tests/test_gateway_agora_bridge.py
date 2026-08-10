"""Agora hub → gateway bridge (hooks plan P2, 2026-07-12).

The identity-carrying transport that wakes gateway-hosted residents on hub
traffic: per-resident alias (H8), durable per-(alias,channel) seq cursors
(sticky-inbox safe), durable emit_event delivery, idempotent resident
starter with parked-actor alias seeding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.integrations.agora_bridge import (
    AgoraBridge,
    AgoraBridgeConfig,
    AgoraResidentConfig,
    build_agora_bridge,
)


class _SpyRunner:
    def __init__(self) -> None:
        self.emits: List[Dict[str, Any]] = []
        self.run_store = _SpyRunStore()
        # Receiver counts per emit (F2 contract): default = one receiver.
        self.counts: Dict[str, int] = {"resumed": 0, "appended": 1}

    def emit_event(self, **kwargs: Any) -> Dict[str, int]:
        self.emits.append(dict(kwargs))
        return dict(self.counts)


class _Run:
    def __init__(self, run_id: str, mailbox: Optional[str] = None, status: str = "waiting") -> None:
        self.run_id = run_id
        self.vars: Dict[str, Any] = {"events_mailbox": mailbox} if mailbox else {}
        self.actor_id = "gateway:agora-pending"

        class _S:
            value = status

        self.status = _S()


class _SpyRunStore:
    def __init__(self) -> None:
        self.runs: Dict[str, _Run] = {}
        self.saved: List[str] = []

    def list_runs(self, status: Any = None, limit: int = 1000) -> List[Dict[str, Any]]:
        return [{"run_id": rid} for rid in self.runs]

    def load(self, run_id: str) -> Optional[_Run]:
        return self.runs.get(run_id)

    def save(self, run: _Run) -> None:
        self.runs[run.run_id] = run
        self.saved.append(run.run_id)


class _SpyHost:
    def __init__(self, run_store: _SpyRunStore) -> None:
        self.run_store = run_store
        self.started: List[Dict[str, Any]] = []
        self._n = 0

    def start_run(self, **kwargs: Any) -> str:
        self._n += 1
        rid = f"resident-run-{self._n}"
        self.started.append({**kwargs, "run_id": rid})
        run = _Run(rid, mailbox=str(kwargs.get("input_data", {}).get("mailbox") or ""))
        self.run_store.runs[rid] = run
        return rid


def _bridge(tmp_path: Path, residents: List[AgoraResidentConfig], **kw: Any) -> AgoraBridge:
    config = AgoraBridgeConfig(
        enabled=True,
        residents=tuple(residents),
        poll_wait_s=0.01,
        floor_sleep_s=0.01,
        state_path=tmp_path / "agora_bridge_state.json",
    )
    runner = _SpyRunner()
    host = _SpyHost(runner.run_store)
    return AgoraBridge(config=config, runner=runner, host=host)


def test_env_config_parses_residents_and_refuses_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AGORA_BRIDGE", "1")
    monkeypatch.setenv(
        "ABSTRACTGATEWAY_AGORA_RESIDENTS",
        json.dumps([{"alias": "resident-a", "mailbox": "a-inbox", "flow_id": "event-inbox-react-agent"}]),
    )
    cfg = AgoraBridgeConfig.from_env(tmp_path)
    assert cfg.enabled and len(cfg.residents) == 1
    assert cfg.residents[0].alias == "resident-a"
    assert cfg.residents[0].resolved_session_id() == "agora:resident-a"

    # Malformed fleet config is LOUD at boot, never a silent no-resident bridge.
    monkeypatch.setenv("ABSTRACTGATEWAY_AGORA_RESIDENTS", "[{broken")
    with pytest.raises(ValueError):
        AgoraBridgeConfig.from_env(tmp_path)
    monkeypatch.setenv("ABSTRACTGATEWAY_AGORA_RESIDENTS", json.dumps([{"alias": "x"}]))
    with pytest.raises(ValueError):
        AgoraBridgeConfig.from_env(tmp_path)


def test_new_envelopes_emit_durably_and_cursor_skips_sticky_reserves(tmp_path: Path) -> None:
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])
    cursors = bridge._cursors.setdefault("resident-a", {})

    env1 = {"channel": "commons", "seq": 7, "from": "chair", "status": "open", "title": "task", "id": "m1"}
    # First sighting: delivered.
    for env in (env1,):
        channel, seq = env["channel"], int(env["seq"])
        if seq > cursors.get(channel, 0):
            bridge._deliver(res, env)
            cursors[channel] = seq

    runner = bridge.runner
    assert len(runner.emits) == 1
    emitted = runner.emits[0]
    assert emitted["name"] == "a-inbox"
    assert emitted["scope"] == "global"
    assert emitted["durable"] is True, "resident delivery must ride the durable mailbox lane"
    assert emitted["payload"]["channel"] == "commons" and emitted["payload"]["seq"] == 7
    assert emitted["payload"]["kind"] == "agora_message"

    # The hub re-serves undischarged envelopes past acks (sticky inbox) —
    # the seq cursor makes the re-serve a no-op.
    channel, seq = env1["channel"], int(env1["seq"])
    assert seq <= cursors.get(channel, 0), "cursor must treat the re-serve as not-news"


def test_resident_loop_delivers_then_floor_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One loop pass end-to-end: fresh envelope delivered + cursor persisted;
    second pass (same envelopes re-served) delivers nothing and floor-sleeps."""
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])

    polls: List[int] = []

    def _fake_inbox(bridge_res: AgoraResidentConfig) -> List[Dict[str, Any]]:
        polls.append(1)
        if len(polls) >= 2:
            bridge._stop.set()  # end the loop after the second poll
        return [{"channel": "commons", "seq": 3, "from": "chair", "status": "open", "id": "m1"}]

    monkeypatch.setattr(bridge, "_check_inbox", _fake_inbox)
    bridge._resident_loop(res)

    assert len(bridge.runner.emits) == 1, "the re-served envelope must not double-deliver"
    saved = json.loads(Path(bridge.config.state_path).read_text(encoding="utf-8"))
    assert saved["resident-a"]["commons"] == 3, "cursor persists durably after delivery"


def test_delivery_failure_does_not_advance_the_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("emit down")

    monkeypatch.setattr(bridge.runner, "emit_event", _boom)
    polls: List[int] = []

    def _fake_inbox(bridge_res: AgoraResidentConfig) -> List[Dict[str, Any]]:
        polls.append(1)
        bridge._stop.set()
        return [{"channel": "commons", "seq": 5, "id": "m1"}]

    monkeypatch.setattr(bridge, "_check_inbox", _fake_inbox)
    bridge._resident_loop(res)

    # At-least-once: the failed delivery leaves the cursor untouched so the
    # next poll retries the same envelope.
    assert bridge._cursors.get("resident-a", {}).get("commons", 0) == 0


def test_starter_is_idempotent_and_seeds_the_alias_parked(tmp_path: Path) -> None:
    res = AgoraResidentConfig(
        alias="resident-a", mailbox="a-inbox", flow_id="event-inbox-react-agent", task="be helpful"
    )
    bridge = _bridge(tmp_path, [res])
    host = bridge.host

    started = bridge.ensure_resident_runs()
    assert len(started) == 1
    call = host.started[0]
    assert call["input_data"]["mailbox"] == "a-inbox"
    assert call["input_data"]["task"] == "be helpful"
    # Parked-actor pattern: started invisible to the tick loop, alias seeded,
    # THEN flipped to gateway.
    assert call["actor_id"] == "gateway:agora-pending"
    run = host.run_store.load(started[0])
    assert run.vars["_runtime"]["agora_agent"] == "resident-a", "H8 alias must be seeded before first tick"
    assert run.actor_id == "gateway"

    # Second call: the mailbox already has a live run — nothing double-starts.
    assert bridge.ensure_resident_runs() == []
    assert len(host.started) == 1


def test_externally_managed_residents_are_never_started(tmp_path: Path) -> None:
    res = AgoraResidentConfig(alias="resident-b", mailbox="b-inbox")  # no flow/bundle
    bridge = _bridge(tmp_path, [res])
    assert bridge.ensure_resident_runs() == []
    assert bridge.host.started == []


def test_build_bridge_disabled_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTGATEWAY_AGORA_BRIDGE", raising=False)
    assert build_agora_bridge(base_dir=tmp_path, runner=_SpyRunner(), host=None) is None


def test_corrupt_cursor_state_degrades_to_redelivery(tmp_path: Path) -> None:
    state = tmp_path / "agora_bridge_state.json"
    state.write_text("{not json", encoding="utf-8")
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])
    assert bridge._cursors == {}, "corrupt state resets cursors (at-least-once), never crashes the bridge"


# ---------------------------------------------------------------------------
# Bridge adversary fold (2026-07-12): F1/F2/F4/F5/F10/F3 pins
# ---------------------------------------------------------------------------


def test_f1_failed_delivery_freezes_the_channel_for_the_rest_of_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversary F1 (HIGH, repro'd): seq 5 fails, seq 6 succeeds in the same
    batch — the old code advanced the cursor to 6 and the hub's re-serve of 5
    was filtered forever. The latch must hold the channel at the failed seq;
    other channels stay independent."""
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])

    calls: List[int] = []

    def _flaky_emit(**kwargs: Any) -> Dict[str, int]:
        seq = int(kwargs["payload"]["seq"])
        calls.append(seq)
        if kwargs["payload"]["channel"] == "commons" and seq == 5:
            raise RuntimeError("store hiccup")
        return {"resumed": 1, "appended": 0}

    monkeypatch.setattr(bridge.runner, "emit_event", _flaky_emit)

    polls: List[int] = []

    def _fake_inbox(bridge_res: AgoraResidentConfig) -> List[Dict[str, Any]]:
        polls.append(1)
        if len(polls) >= 2:
            bridge._stop.set()  # end AFTER the first full batch (F8 checks stop mid-batch)
            return []
        return [
            {"channel": "commons", "seq": 5, "id": "m5"},
            {"channel": "commons", "seq": 6, "id": "m6"},
            {"channel": "side", "seq": 2, "id": "s2"},
        ]

    monkeypatch.setattr(bridge, "_check_inbox", _fake_inbox)
    bridge._resident_loop(res)

    cursors = bridge._cursors["resident-a"]
    assert cursors.get("commons", 0) == 0, "cursor must never advance past a lost envelope"
    assert cursors.get("side") == 2, "an independent channel still advances"
    assert 6 not in calls, "the latch must not even attempt later seqs on a failed channel"


def test_f2_zero_receivers_holds_the_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversary F2 (HIGH): an emit that resumed nothing and appended nothing
    means the resident is dead/missing — advancing the cursor would consume
    the message into the void. Zero receivers = delivery failure = retry."""
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])
    bridge.runner.counts = {"resumed": 0, "appended": 0}

    def _fake_inbox(bridge_res: AgoraResidentConfig) -> List[Dict[str, Any]]:
        bridge._stop.set()
        return [{"channel": "commons", "seq": 9, "id": "m9"}]

    monkeypatch.setattr(bridge, "_check_inbox", _fake_inbox)
    bridge._resident_loop(res)

    assert bridge._cursors["resident-a"].get("commons", 0) == 0, "void delivery must hold the cursor"


def test_f4_event_id_is_stable_per_alias_channel_seq(tmp_path: Path) -> None:
    """The runner dedups durable appends by event_id — the bridge must stamp
    a STABLE id so crash-replayed re-sends land once."""
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])
    env = {"channel": "commons", "seq": 7, "id": "m7", "body": "hello"}
    bridge._deliver(res, env)
    bridge._deliver(res, env)  # replay
    ids = [e["event_id"] for e in bridge.runner.emits]
    assert ids == ["agora:resident-a:commons:7"] * 2, f"event_id must be stable: {ids}"


def test_f7_huge_bodies_are_clamped_with_a_truncation_label(tmp_path: Path) -> None:
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox")
    bridge = _bridge(tmp_path, [res])
    bridge._deliver(res, {"channel": "commons", "seq": 1, "id": "m", "body": "x" * 100_000})
    sent = bridge.runner.emits[0]["payload"]["body"]
    assert len(sent.encode("utf-8")) < 40_000
    assert "#TRUNCATION" in sent


def test_f5_f10_alias_validation_and_duplicates_refuse_at_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversary F5/F10: a malformed alias must refuse at config parse (not
    warn-loop at poll time); duplicate aliases race one cursor — refused."""
    monkeypatch.setenv("ABSTRACTGATEWAY_AGORA_BRIDGE", "1")
    monkeypatch.setenv(
        "ABSTRACTGATEWAY_AGORA_RESIDENTS",
        json.dumps([{"alias": "Resident-A", "mailbox": "a-inbox"}]),
    )
    with pytest.raises(ValueError):
        AgoraBridgeConfig.from_env(tmp_path)

    monkeypatch.setenv(
        "ABSTRACTGATEWAY_AGORA_RESIDENTS",
        json.dumps([
            {"alias": "resident-a", "mailbox": "a-inbox"},
            {"alias": "resident-a", "mailbox": "b-inbox"},
        ]),
    )
    with pytest.raises(ValueError):
        AgoraBridgeConfig.from_env(tmp_path)


def test_f3_starter_declares_the_mailbox_at_start_not_first_tick(tmp_path: Path) -> None:
    """Adversary F3: the liveness scan matches vars.events_mailbox, which the
    flow only sets at its first tick — a crash before that tick made the run
    invisible to the scan (duplicate residents) AND to durable emits. The
    starter now declares it in the same save that seeds the alias."""
    res = AgoraResidentConfig(alias="resident-a", mailbox="a-inbox", flow_id="f")
    bridge = _bridge(tmp_path, [res])
    started = bridge.ensure_resident_runs()
    run = bridge.host.run_store.load(started[0])
    assert run.vars.get("events_mailbox") == "a-inbox"
    # And therefore the liveness scan sees it immediately: no double-start.
    assert bridge.ensure_resident_runs() == []


def test_resident_config_has_no_thinking_knob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator ruling 2026-08-04: agora is a standalone library — LLM
    parameters are NOT resident config. The reasoning default lives in the
    gateway capability-defaults store (`output.text` route `reasoning`) and
    reaches resident LLM calls through the runtime's capability cascade. A
    `thinking` key in the residents env must not silently become config."""
    import dataclasses

    assert "thinking" not in {f.name for f in dataclasses.fields(AgoraResidentConfig)}

    monkeypatch.setenv("ABSTRACTGATEWAY_AGORA_BRIDGE", "1")
    monkeypatch.setenv(
        "ABSTRACTGATEWAY_AGORA_RESIDENTS",
        json.dumps([{"alias": "resident-a", "mailbox": "a-inbox", "flow_id": "f", "thinking": "medium"}]),
    )
    cfg = AgoraBridgeConfig.from_env(tmp_path)
    assert not hasattr(cfg.residents[0], "thinking")

    # And the starter sends no `_runtime` of its own.
    bridge = _bridge(tmp_path, [AgoraResidentConfig(alias="resident-a", mailbox="a-inbox", flow_id="f")])
    bridge.ensure_resident_runs()
    assert "_runtime" not in bridge.host.started[0]["input_data"]
