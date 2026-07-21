"""G3 door half — the per-home task inbox (plan v18 gateway §1).

Tasks left with an entity are durable, marker-first FACTS in the home:
append-only events in `<home>/task_inbox.jsonl`, folded at read; origin is
stamped from the authenticated principal (payload claims have no field to
ride); the roster carries `pending_tasks` render-when-present. Ruling-
neutral under D1 — the door records facts, runtime's R-C owns what the
work phase does with them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def test_task_inbox_fold_survives_torn_lines(tmp_path: Path) -> None:
    """Poll-shaped reads must survive a torn tail (scan-lane rule): the fold
    skips unparseable lines with a labeled warning, never raises."""
    from abstractgateway.entity_tasks import append_task, read_task_inbox

    home = tmp_path / "home"
    home.mkdir()
    append_task(home, title="a", origin="person:admin", by="person:admin")
    with open(home / "task_inbox.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"event": "added", "task_id": "torn')  # crash mid-append
    folded = read_task_inbox(home)
    assert folded["exists"] is True
    assert len(folded["tasks"]) == 1
    assert "#FALLBACK" in str(folded.get("warning") or "")


def test_task_status_words_are_the_closed_set(tmp_path: Path) -> None:
    from abstractgateway.entity_tasks import TASK_STATUSES, append_task, set_task_status

    home = tmp_path / "home"
    home.mkdir()
    assert TASK_STATUSES == ("pending", "taken", "done", "parked")
    ev = append_task(home, title="a", origin="person:admin", by="person:admin")
    with pytest.raises(ValueError):
        set_task_status(home, task_id=ev["task_id"], status="cancelled", by="person:admin")
    with pytest.raises(KeyError):
        set_task_status(home, task_id="nope", status="done", by="person:admin")


def test_task_endpoints_marker_first_and_roster_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "tasks-marker-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer tasks-marker-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        # No inbox yet: GET reads honestly; the roster carries NO field
        # (render-when-present — absent, not zero).
        r0 = client.get("/api/gateway/entities/Castor/tasks")
        assert r0.status_code == 200 and r0.json() == {"exists": False, "tasks": [], "pending": 0}
        roster = client.get("/api/gateway/entities").json()["entities"]
        assert all("pending_tasks" not in e for e in roster)

        # Leave a task: origin/by are STAMPED from the principal — the
        # request body carries no origin field to forge.
        r1 = client.post(
            "/api/gateway/entities/Castor/tasks",
            json={"title": "Research lighthouse keepers", "brief": "one page, cite sources", "backlog_ref": "plan-v18-g3"},
        )
        assert r1.status_code == 200, r1.text
        task = r1.json()["task"]
        assert task["by"] == "person:admin"
        assert task["origin"].startswith("person:admin via POST")
        assert r1.json()["pending"] == 1

        # The roster now carries the count (three-consumer contract c2801).
        roster = client.get("/api/gateway/entities").json()["entities"]
        castor = next(e for e in roster if e.get("slug") == "castor")
        assert castor["pending_tasks"] == 1

        # Status advance folds; pending drops.
        r2 = client.post(
            f"/api/gateway/entities/Castor/tasks/{task['task_id']}/status",
            json={"status": "done", "note": "handled in visit"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["pending"] == 0
        r3 = client.get("/api/gateway/entities/Castor/tasks")
        assert r3.json()["tasks"][0]["status"] == "done"

        # Every write landed marker-first in the replay stream.
        from abstractgateway.service import get_gateway_service

        markers_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "castor.jsonl"
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "task_inbox_changed"]
        assert len(changed) == 2
        assert changed[0]["change"] == "added" and changed[0]["by"] == "person:admin"
        assert changed[1]["change"] == "status" and changed[1]["status"] == "done"

        # Unknown task / bad status refuse BEFORE any marker lands (the
        # substrate P2-2 rule: a recorded change that never happened is
        # worse than a 4xx) — the marker count stays at 2.
        r4 = client.post("/api/gateway/entities/Castor/tasks/nope/status", json={"status": "done"})
        assert r4.status_code == 404
        r5 = client.post(f"/api/gateway/entities/Castor/tasks/{task['task_id']}/status", json={"status": "cancelled"})
        assert r5.status_code == 400
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        assert len([m for m in rows if m.get("payload", {}).get("kind") == "task_inbox_changed"]) == 2
