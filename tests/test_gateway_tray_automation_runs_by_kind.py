"""An automation's run is never listed as a conversation in the tray.

Operator report 2026-11-03: Conversations ▸ Running (2) listed two automation
runs ("You are an expert journalist…", "you are an expert at identifying…")
while the same automations sat under Automations. The tag that tells them apart
is the runtime's `session_kind` (abstractruntime `run_attribution.py`:
automation | occurrence = an automation's run; chat | discussion = a
conversation). `GET /host/runs` now serves it on each row and the tray sorts
its rows by it — never by title, workflow or id.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from abstractgateway.admin_runtimes import _runs_from_store
from abstractgateway.tray import menu_model as mm
from abstractgateway.tray import menu_v2 as v2
from abstractgateway.tray.sampler import AutomationRow, RunRow, Snapshot, run_rows_from_listing

pytestmark = pytest.mark.basic


def _snap(**over: Any) -> Snapshot:
    base: Dict[str, Any] = dict(
        ts=0.0, gateway_state="running", reachable=True, paused=False, inflight_ticks=0, paused_by=None, pause_reason=None,
        gpu_supported=True, gpu_reason=None, gpu_pct=20.0, gpu_name="Apple M5 Max", gpu_history=(20.0,), mem_supported=True, mem_reason=None,
        mem_used=1, mem_total=2, mem_pct=50.0, mem_history=(50.0,), process_rss=1, process_history=(1.0,),
        device_backend="metal", models=(), models_total_bytes=None, models_error=None, can_restart=True, can_shutdown=True, restart_block_reason=None,
        update_job_running=False, step_gate_supported=True, version="0.18.0", last_error=None, consecutive_failures=0, unauthorized=False,
    )
    base.update(over)
    return Snapshot(**base)


# The operator's case: an automation occurrence running for two hours, a person's conversation running too.
JOURNALIST = RunRow(
    "occ1", "basic-agent@0.0.5:81795ea9", "running", 5, 7380.0, 1.0, observer_path="/apps/observer/#run/occ1",
    title="You are an expert journalist reporting on the evolution of", title_source="automation",
    session_kind="automation", automation_id="auto1",
)
INDEPENDENT = RunRow("occ2", "basic-agent@0.0.5:81795ea9", "completed", 3, 60.0, 1.0, title="Weekly brief", title_source="automation", session_kind="occurrence", automation_id="auto2")
CHAT = RunRow("c1", "coding-agent:coder", "running", 4, 130.0, 1.0, title="Refactor the sampler", title_source="prompt", session_kind="chat")
# A wake runs in its caller's conversation: an occurrence whose session_kind is "chat" IS a conversation turn.
WAKE = RunRow("w1", "coding-agent:coder", "running", 2, 10.0, 1.0, title="Remind me at noon", title_source="prompt", session_kind="chat", automation_id="auto3")
DAILY = AutomationRow("auto1", "Journalist digest", "active", schedule_text="Every day at 09:00", capabilities=("run_now", "pause"), trigger_text="Every day at 09:00")


def _labels(nodes) -> List[str]:
    return [n.label for n in nodes]


def _child(nodes, label: str):
    hit = [n for n in nodes if n.label == label]
    assert hit, f"{label!r} not in {_labels(nodes)}"
    return hit[0].children


def test_host_listing_rows_carry_the_runtimes_session_kind() -> None:
    class Store:
        def list_run_index(self, *, status, root_only, limit):
            rows = [
                {"run_id": "occ1", "workflow_id": "basic-agent@0.0.5:81795ea9", "status": "running", "parent_run_id": "auto1",
                 "role": "occurrence", "session_kind": "automation", "automation_id": "auto1", "created_at": None, "updated_at": None},
                {"run_id": "c1", "workflow_id": "coding-agent:coder", "status": "running", "parent_run_id": None,
                 "role": None, "session_kind": "chat", "automation_id": None, "created_at": None, "updated_at": None},
            ]
            return [r for r in rows if status is None or r["status"] == status]

    rows = {r["run_id"]: r for r in _runs_from_store(Store(), limit=10, since_epoch=None, plane="default")}
    assert rows["occ1"]["session_kind"] == "automation" and rows["occ1"]["automation_id"] == "auto1"
    assert rows["c1"]["session_kind"] == "chat" and rows["c1"]["automation_id"] is None


def test_sampler_keeps_the_tag() -> None:
    payload = {"items": [
        {"run_id": "occ1", "status": "running", "activity": "running", "session_kind": "automation", "automation_id": "auto1"},
        {"run_id": "c1", "status": "running", "activity": "running", "session_kind": "chat"},
    ]}
    rows = {r.run_id: r for r in run_rows_from_listing(payload, now=0.0)}
    assert (rows["occ1"].session_kind, rows["occ1"].automation_id) == ("automation", "auto1")
    assert mm.is_automation_run(rows["occ1"]) and not mm.is_automation_run(rows["c1"])


def test_new_menu_lists_automation_runs_under_automations_only() -> None:
    snap = _snap(runs=(JOURNALIST, CHAT, WAKE, INDEPENDENT), automations=(DAILY,))
    conversations = v2.conversations_items(snap, observer=True)
    automations = v2.automations_items(snap, observer=True)
    assert "Running (2)" in _labels(conversations)
    assert not any("journalist" in label for label in _labels(conversations))
    assert not any("Weekly brief" in label for label in _labels(conversations))
    assert any("Refactor the sampler" in label for label in _labels(conversations))
    assert any("Remind me at noon" in label for label in _labels(conversations))
    labels = _labels(automations)
    assert labels[0] == "Running (1)"
    assert "journalist" in labels[1] and "step 5" in labels[1]
    assert "Active (1)" in labels


def test_new_menu_header_counts_automations_apart() -> None:
    snap = _snap(runs=(JOURNALIST, CHAT), automations=(DAILY,))
    assert v2.activity_line(snap).startswith("1 conversation running · 1 automation running")
    only_auto = _snap(runs=(JOURNALIST,), automations=(DAILY,))
    assert v2.activity_line(only_auto).startswith("1 automation running")


def test_classic_menu_lists_automation_runs_apart_from_conversations() -> None:
    snap = _snap(runs=(JOURNALIST, CHAT, INDEPENDENT), automations=(DAILY,))
    labels = _labels(mm.sessions_items(snap, observer=True))
    assert labels[0] == "Running (1)"
    assert "Refactor the sampler" in labels[1]
    at = labels.index("Automations running (1)")
    assert "journalist" in labels[at + 1]
    recent = [label for label in labels if "Weekly brief" in label]
    assert recent == []
