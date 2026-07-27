"""The conversation seat (entity_seat.py) — slice 2 unit pins.

The sealed rule: machinery yields to humans; humans wait for each other.
These tests pin the pure mechanics route-free: TTL liveness (the incident
fix — the seat no longer frees between a conversation's turns), the
same-session slide, the priority matrix, the preempt cancel walk, and the
probe contract the visit/chat doors consume.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.entity_seat import (
    LIVE_RUN_RETRY_HINT_S,
    SEAT_IDLE_TTL_S,
    build_summon_seat_probe,
    cancel_run_tree,
    door_decision,
    normalize_caller_kind,
    read_seat,
    record_seat,
    retry_after_s,
    seat_occupancy,
    seat_path,
)


class _FakeRunStore:
    def __init__(self, runs: dict[str, str] | None = None, children: dict[str, list[str]] | None = None):
        self._runs = dict(runs or {})  # run_id -> status string
        self._children = dict(children or {})  # parent -> [child ids]

    def load(self, run_id: str):
        status = self._runs.get(run_id)
        if status is None:
            return None
        return SimpleNamespace(run_id=run_id, status=status)

    def list_children(self, parent_run_id: str):
        return [SimpleNamespace(run_id=cid) for cid in self._children.get(parent_run_id, [])]


def _take(entities_dir: Path, slug: str = "castor", **kw) -> None:
    defaults = dict(run_id="run-1", session_id="s-1", holder="laurent", holder_kind="human")
    defaults.update(kw)
    record_seat(entities_dir, slug, **defaults)


def _age_seat(entities_dir: Path, slug: str, *, seconds: int) -> None:
    """Rewind renewed_at so the TTL window can be crossed deterministically."""
    path = seat_path(entities_dir, slug)
    rec = json.loads(path.read_text(encoding="utf-8"))
    past = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    rec["renewed_at"] = past
    path.write_text(json.dumps(rec), encoding="utf-8")


# ------------------------------------------------------------- occupancy/TTL


def test_occupancy_free_when_no_record(tmp_path: Path):
    assert seat_occupancy(_FakeRunStore(), tmp_path, "castor") is None


def test_occupancy_live_run_holds(tmp_path: Path):
    _take(tmp_path)
    seat = seat_occupancy(_FakeRunStore({"run-1": "running"}), tmp_path, "castor")
    assert seat is not None and seat["run_live"] is True
    assert seat["status"] == "running"
    assert seat["lane"] == "summon"


def test_occupancy_ttl_holds_after_terminal_run(tmp_path: Path):
    """THE INCIDENT FIX: the seat stays held between a conversation's turns
    (the holding run is terminal, the TTL is fresh)."""
    _take(tmp_path)
    seat = seat_occupancy(_FakeRunStore({"run-1": "completed"}), tmp_path, "castor")
    assert seat is not None
    assert seat["run_live"] is False
    assert 0 < seat["ttl_remaining_s"] <= SEAT_IDLE_TTL_S


def test_occupancy_expires_after_ttl(tmp_path: Path):
    _take(tmp_path)
    _age_seat(tmp_path, "castor", seconds=SEAT_IDLE_TTL_S + 5)
    assert seat_occupancy(_FakeRunStore({"run-1": "completed"}), tmp_path, "castor") is None
    # ... but a LIVE run holds regardless of TTL age.
    assert seat_occupancy(_FakeRunStore({"run-1": "running"}), tmp_path, "castor") is not None


def test_legacy_record_keeps_day_one_semantics_when_stale(tmp_path: Path):
    """A pre-slice-2 record ({run_id, session_id, recorded_at} only) anchors
    its TTL on recorded_at — an OLD one holds only while its run is live
    (exactly the semantics it was written under)."""
    path = seat_path(tmp_path, "castor")
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(seconds=SEAT_IDLE_TTL_S + 60)).isoformat()
    path.write_text(json.dumps({"run_id": "run-1", "session_id": "s-1", "recorded_at": old}), encoding="utf-8")
    assert seat_occupancy(_FakeRunStore({"run-1": "completed"}), tmp_path, "castor") is None
    assert seat_occupancy(_FakeRunStore({"run-1": "waiting"}), tmp_path, "castor") is not None


def test_slide_preserves_held_since(tmp_path: Path):
    _take(tmp_path)
    first = read_seat(tmp_path, "castor")
    record_seat(
        tmp_path,
        "castor",
        run_id="run-2",
        session_id="s-1",
        holder="laurent",
        holder_kind="human",
        held_since=first["held_since"],
    )
    second = read_seat(tmp_path, "castor")
    assert second["held_since"] == first["held_since"]
    assert second["run_id"] == "run-2"
    assert second["renewed_at"] >= first["renewed_at"]


# ---------------------------------------------------------- decision matrix


def _seat(**kw):
    base = dict(
        lane="summon",
        run_id="run-1",
        session_id="s-1",
        status="completed",
        run_live=False,
        holder="flow",
        holder_kind="unknown",
        held_since="",
        renewed_at="",
        idle_ttl_s=SEAT_IDLE_TTL_S,
        ttl_remaining_s=120,
    )
    base.update(kw)
    return base


def test_free_seat_is_taken():
    assert door_decision(None, caller="anyone", caller_kind="agent", session_id="x")["action"] == "take"


def test_same_session_same_holder_slides():
    d = door_decision(_seat(holder="flow"), caller="flow", caller_kind="agent", session_id="s-1")
    assert d["action"] == "slide"


def test_same_session_foreign_holder_never_inherits():
    """The hijack guard: a foreign caller reusing a session id does not get
    the slide — it falls through the matrix (agent -> refuse)."""
    d = door_decision(_seat(holder="flow"), caller="intruder", caller_kind="agent", session_id="s-1")
    assert d["action"] == "refuse"


def test_agent_never_displaces_live_or_idle():
    assert door_decision(_seat(run_live=True, status="running"), caller="x", caller_kind="agent", session_id="s9")["action"] == "refuse"
    assert door_decision(_seat(), caller="x", caller_kind="agent", session_id="s9")["action"] == "refuse"


def test_human_preempts_agent_and_unknown_holders():
    for kind in ("agent", "unknown"):
        d = door_decision(_seat(holder_kind=kind, run_live=True), caller="laurent", caller_kind="human", session_id="s9")
        assert d["action"] == "preempt", kind


def test_human_waits_for_another_human():
    d = door_decision(_seat(holder="mira-mom", holder_kind="human"), caller="laurent", caller_kind="human", session_id="s9")
    assert d["action"] == "refuse"
    assert d["retry_after_s"] == 120  # exact TTL remainder on an idle seat


def test_human_reclaims_their_own_idle_seat_but_not_their_live_turn():
    own_idle = _seat(holder="laurent", holder_kind="human", run_live=False)
    assert door_decision(own_idle, caller="laurent", caller_kind="human", session_id="s9")["action"] == "take"
    own_live = _seat(holder="laurent", holder_kind="human", run_live=True, status="running")
    assert door_decision(own_live, caller="laurent", caller_kind="human", session_id="s9")["action"] == "refuse"


def test_undeclared_caller_reads_agent():
    assert normalize_caller_kind(None) == "agent"
    assert normalize_caller_kind("") == "agent"
    assert normalize_caller_kind("HUMAN") == "human"
    assert normalize_caller_kind("robot") == "agent"


def test_retry_after_live_is_a_hint_idle_is_exact():
    assert retry_after_s(_seat(run_live=True)) == LIVE_RUN_RETRY_HINT_S
    assert retry_after_s(_seat(ttl_remaining_s=42)) == 42
    assert retry_after_s(_seat(ttl_remaining_s=0)) == 1  # floor: never 0/negative


# ------------------------------------------------------------ preempt walk


def test_cancel_run_tree_walks_children_and_tolerates_terminal_and_missing():
    calls: list[str] = []

    class _Rt:
        def cancel_run(self, rid, *, reason=None):
            if rid == "gone":
                raise KeyError(rid)
            calls.append(rid)

    store = _FakeRunStore(
        runs={"root": "running", "kid-a": "waiting", "kid-b": "completed"},
        children={"root": ["kid-a", "kid-b", "gone"]},
    )
    flipped = cancel_run_tree(_Rt(), store, "root", reason="preempted")
    assert calls == ["root", "kid-a", "kid-b"]  # gone raised KeyError, walk survived
    # Only runs that were actually non-terminal count as flipped.
    assert flipped == ["root", "kid-a"]


# ------------------------------------------------------------ probe contract


def test_probe_answers_live_runs_only(tmp_path: Path):
    """The visit/chat doors' contract: a turn MID-FLIGHT blocks them; a
    TTL-idle seat never does (the operator switching lanes is a deliberate
    human act — summon-vs-summon keeps the full TTL)."""
    _take(tmp_path)
    live = build_summon_seat_probe(_FakeRunStore({"run-1": "running"}), tmp_path)
    assert live("castor") is not None
    idle = build_summon_seat_probe(_FakeRunStore({"run-1": "completed"}), tmp_path)
    assert idle("castor") is None


def test_seat_preempted_is_a_registered_marker_kind():
    from abstractgateway.entity_replay import HOST_MARKER_KINDS

    assert "seat_preempted" in HOST_MARKER_KINDS
    assert "summon_refused" in HOST_MARKER_KINDS
