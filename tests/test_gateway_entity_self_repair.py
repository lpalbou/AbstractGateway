"""Entity loop self-repair (laurent, DM 2026-07-21: "you should self-repair
the entity then?" — the notify-only sweeper redirected to automatic repair).

The design IS the guards, so the pins are mostly refusals:
- a failure-CULLED loop (stopped_by=failures) respawns with the recorded
  spawn parameters; a CRASHED loop (status says day/between, pid dead)
  respawns too;
- deliberate stops (operator/schedule words) NEVER repair;
- paused (kill switch) and a lapsed personal grant NEVER repair;
- one repair per death (signature dedup), and a second death within the
  breaker window stays DOWN with the operator notified;
- every repair lands a personal_started marker (channel=self-repair).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")

from abstractgateway import entity_repair


class _Registry:
    """Just enough registry for the sweep: one home, marker capture."""

    def __init__(self, entities_dir: Path, slug: str = "castor") -> None:
        self.entities_dir = entities_dir
        self._slug = slug
        self.markers: List[Dict[str, Any]] = []

    def list_entities(self) -> List[Dict[str, Any]]:
        return [{"slug": self._slug, "entity_id": f"entity:{self._slug}"}]

    def get_home(self, slug: str) -> Any:
        class _Mem:
            @staticmethod
            def current_seq() -> int:
                return 7

        class _Home:
            memory = _Mem()

        return _Home()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "entities" / "castor"
    home_dir.mkdir(parents=True)
    # Standing until_revoked grant (the loop legitimately runs) — the
    # runtime's phases.yaml shape, personal bucket.
    (home_dir / "phases.yaml").write_text(
        "schema_version: 1\n"
        "personal:\n"
        "  mode: until_revoked\n"
        "  granted_by: person:admin\n"
        "  granted_at: '2026-07-21T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    return home_dir


def _write_status(home_dir: Path, **fields: Any) -> None:
    (home_dir / "loop_status").write_text(json.dumps(fields), encoding="utf-8")


def _sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, respawns: List[Dict[str, Any]],
           notifications: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    registry = _Registry(tmp_path / "entities")

    def _fake_start_loop(home_dir: Path, **kwargs: Any) -> Dict[str, Any]:
        respawns.append(dict(kwargs))
        return {"pid": 4242}

    monkeypatch.setattr("abstractgateway.entity_loop.start_loop", _fake_start_loop)
    # The idle -> sleep step rides the same sweep (contract v3 D2); this file tests the repair half.
    monkeypatch.setattr("abstractgateway.entity_idle_sleep.idle_sweep_one", lambda registry, slug: None)
    monkeypatch.setattr(
        "abstractgateway.entity_chat.resolve_substrate",
        # Contract-faithful triple: (provider, model, thinking). Request
        # values (the sidecar's, under the one-precedence-rule fix) win;
        # the home file fills what the request leaves blank.
        lambda p, m, home_dir, thinking=None: (
            p or "lmstudio",
            m or "test-model",
            thinking
            or __import__("abstractgateway.entity_chat", fromlist=["read_entity_substrate"]).read_entity_substrate(home_dir).get("thinking")
            or None,
        ),
    )
    if notifications is not None:
        monkeypatch.setattr(
            entity_repair, "_notify_operator",
            lambda subject, body: notifications.append(subject) or None,
        )
    captured: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        entity_repair, "_marker",
        lambda registry, slug, entity_id, home_dir, details: captured.append(details),
    )
    actions = entity_repair.sweep_entity_repairs(registry)
    for a in actions:
        a.setdefault("_markers", captured)
    return actions


def test_a_culled_loop_is_left_down_for_the_operators_retry(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review 2026-11-09 #11 (operator 2026-11-09: own time is the operator's to give): the
    sweep no longer respawns a culled or crashed loop. It records the death once, tells the
    operator, marks the biography, and leaves the loop down for the phase bar's Retry."""
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:03:00+00:00")
    entity_repair.record_spawn_params(home, {"provider": "lmstudio", "model": "ornith-1.0-35b", "tick_seconds": 20.0})
    respawns: List[Dict[str, Any]] = []
    notes: List[str] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns, notifications=notes)
    assert [a["action"] for a in actions] == ["left_down"], actions
    assert respawns == [], "the sweep respawned a loop without the operator"
    assert notes, "the operator is told the loop is down"
    markers = actions[0]["_markers"]
    assert markers and markers[0]["repair"] is False and markers[0]["prior_stopped_by"] == "failures"


def test_a_crashed_loop_is_left_down_too(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_status(home, phase="day", pid=99999999, pid_started_at="Tue Jul 21 04:00:00 2026")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a["action"] for a in actions] == ["left_down"], actions
    assert actions[0]["kind"] == "crashed" and respawns == []


@pytest.mark.parametrize("word", ["stop_command", "stop_file", "operator-interrupt", "rest", "max_ticks"])
def test_deliberate_stops_never_repair(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    _write_status(home, phase="stopped", stopped_by=word, updated_at="2026-07-21T05:00:00+00:00")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert actions == [] and respawns == []


def test_paused_kill_switch_blocks_repair(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:03:00+00:00")
    (home / "state").write_text(json.dumps({"state": "paused", "reason": "operator stop"}), encoding="utf-8")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a["action"] for a in actions] == ["skipped"]
    assert "kill switch" in actions[0]["reason"] and respawns == []


def test_lapsed_grant_blocks_repair(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "phases.yaml").unlink()
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:03:00+00:00")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a["action"] for a in actions] == ["skipped"]
    assert "grant" in actions[0]["reason"] and respawns == []


def test_one_notice_per_death(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:03:00+00:00")
    respawns: List[Dict[str, Any]] = []
    notes: List[str] = []
    assert [a["action"] for a in _sweep(tmp_path, monkeypatch, respawns=respawns, notifications=notes)] == ["left_down"]
    # Same death again: nothing new. A new death: one more notice, still no respawn.
    assert _sweep(tmp_path, monkeypatch, respawns=respawns, notifications=notes) == []
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:20:00+00:00")
    assert [a["action"] for a in _sweep(tmp_path, monkeypatch, respawns=respawns, notifications=notes)] == ["left_down"]
    assert respawns == [] and len(notes) == 2


def test_visiting_posture_defers_repair(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_status(home, phase="stopped", stopped_by="failures", updated_at="2026-07-21T03:03:00+00:00")
    (home / "state").write_text(
        json.dumps({"state": "asleep", "mode": "visiting", "reason": "in conversation [visit chat-abc]"}),
        encoding="utf-8",
    )
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a["action"] for a in actions] == ["deferred"] and respawns == []


def test_sweeper_env_disable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR", "0")
    assert entity_repair.start_repair_sweeper(_Registry(tmp_path)) is None


# ---------------------------------------------------------------------------
# The unattended need-check (spec v13 wake_conditions; entity c356: my
# sweeper hosts the loop-less half). Zero-token by law.
# ---------------------------------------------------------------------------


def _asleep(home_dir: Path, **extra: Any) -> None:
    (home_dir / "state").write_text(
        json.dumps({"state": "asleep", "reason": "resting", **extra}), encoding="utf-8"
    )


def test_need_check_wakes_for_a_standing_work_order(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # UNARMED (no grant), taskless sleep + a standing work order.
    (home / "phases.yaml").unlink()
    _asleep(home)
    (home / "work_order.md").write_text("Investigate the drift.", encoding="utf-8")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    checks = [a for a in actions if a["action"] == "need_check"]
    assert checks and checks[0]["outcome"].startswith("woken"), actions
    assert len(respawns) == 1, "the wake starts the loop (its gate opens the work day)"
    markers = checks[0]["_markers"]
    assert any(m.get("cause") == "cadence_need_check" for m in markers)


def test_need_check_resleeps_silently_when_nothing_sanctioned(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "phases.yaml").unlink()
    _asleep(home)
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    checks = [a for a in actions if a["action"] == "need_check"]
    assert checks and "re-sleep" in checks[0]["outcome"]
    assert respawns == []
    # NO marker churn: the same sleep continues (v13's law).
    assert not checks[0]["_markers"]


def test_need_check_skips_armed_grants_and_stamped_sleeps(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ARMED grant (fixture default) => the cycle owns the sleep; not ours.
    _asleep(home)
    (home / "work_order.md").write_text("Standing.", encoding="utf-8")
    respawns: List[Dict[str, Any]] = []
    actions = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a for a in actions if a["action"] == "need_check"] == []
    # UNARMED but STAMPED wake_at => the stamped_wake_at source owns it.
    (home / "phases.yaml").unlink()
    _asleep(home, wake_at="2027-01-01T00:00:00+00:00")
    actions2 = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a for a in actions2 if a["action"] == "need_check"] == []
    assert respawns == []


def test_need_check_respects_the_cadence(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "phases.yaml").unlink()
    _asleep(home)
    respawns: List[Dict[str, Any]] = []
    first = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a for a in first if a["action"] == "need_check"], "first check runs"
    # Immediately again: inside the cadence window — silent skip.
    second = _sweep(tmp_path, monkeypatch, respawns=respawns)
    assert [a for a in second if a["action"] == "need_check"] == []
