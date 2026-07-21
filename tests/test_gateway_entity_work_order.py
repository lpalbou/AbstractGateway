"""The work-phase lane's operator write surface (laurent seq 155, 2026-07-19:
"the entity must be able to work and execute commands when it works").

Runtime shipped the loop half — work_order.md's PRESENCE shifts the next
day-open to phase=work, the WORK column of tool_policy.yaml applies (execute
included where granted), and the entity declares done/blocked. The gateway
owns the WRITE (the entity never writes its own order — the tool_policy.yaml
authority split): GET/PUT /entities/{name}/work-order, marker-first
(work_order_changed), clearing archives visibly (never a silent delete).
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


def test_work_order_set_clear_marker_first_and_runtime_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "work-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer work-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        # No order = personal by default (the honest empty state).
        r0 = client.get("/api/gateway/entities/Castor/work-order")
        assert r0.status_code == 200 and r0.json()["active"] is False and r0.json()["order"] is None

        # Empty set is refused (a work order needs text or clear).
        assert client.put("/api/gateway/entities/Castor/work-order", json={}).status_code == 400

        # Set the order — marker-first, and RUNTIME's own reader sees it (the
        # loop half's contract: presence shifts phase=work).
        r1 = client.put("/api/gateway/entities/Castor/work-order",
                        json={"order": "Run the coherence tests and report what stands."})
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["active"] is True
        assert "coherence tests" in body["order"]

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import read_work_order

        registry = get_gateway_service().entity_registry
        home_dir = registry.entities_dir / "castor"
        assert "coherence tests" in (read_work_order(home_dir) or ""), "runtime's day-open reader must see the order the door wrote"

        # Marker landed (work_order_changed, set, no prior).
        markers_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "castor.jsonl"
        rows = [json.loads(l) for l in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "work_order_changed"]
        assert len(changed) == 1 and changed[0]["change"] == "set" and changed[0]["had_prior"] is False
        # The order TEXT never rides the marker (hashes-not-content).
        assert "coherence tests" not in json.dumps(changed[0])

        # Clear — archives visibly (work_order.done.md), personal returns,
        # second marker (cleared, had_prior).
        r2 = client.put("/api/gateway/entities/Castor/work-order", json={"clear": True})
        assert r2.status_code == 200 and r2.json()["active"] is False
        assert read_work_order(home_dir) is None, "clearing removes the standing order"
        assert (home_dir / "work_order.done.md").exists(), "the cleared order archives, never deletes"
        r3 = client.get("/api/gateway/entities/Castor/work-order")
        assert "coherence tests" in r3.json().get("done_history", ""), "the operator sees the archived order"
        rows = [json.loads(l) for l in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "work_order_changed"]
        assert len(changed) == 2 and changed[1]["change"] == "cleared" and changed[1]["had_prior"] is True


def test_work_order_admin_gated() -> None:
    """The write surface is admin-gated in the route table (a mission on an
    entity is operator authority) — pinned against the pattern like the
    other entity-mutation doors."""
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES
    import re

    admin_patterns = [p for p in GATEWAY_ROUTE_POLICIES if getattr(p, "pattern", None) and "work-order" in p.pattern]
    assert admin_patterns, "work-order must be in an admin-gated route pattern"
    pat = re.compile(admin_patterns[0].pattern)
    assert pat.match("/api/gateway/entities/castor/work-order")


def test_console_carries_the_work_order_surface() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    for el in ("entity-workorder-text", "entity-workorder-save", "entity-workorder-clear", "entity-workorder-history"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert "entityWorkOrderSave" in html and "phase=work" in html
    assert '"entity-workorder-save", "entity-workorder-clear"' in html  # admin-gated in the UI too
