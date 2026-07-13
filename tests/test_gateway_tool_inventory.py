"""Serve-time tool-inventory composition + served routes (descriptor contract
v6, plan (a) gateway lane P0-1).

Gateway authorship is ONE fact: `executes_via` (the containment). Everything
else derives verbatim from core's + runtime's enumerations. These pin the
composition (union validation, total order, collision → two rows, walled-only
matrix) and the two ENTITY-INDEPENDENT served routes the creation modal reads
before the entity exists.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractcore.tools")

from abstractgateway.tool_inventory import (  # noqa: E402
    CONTAINMENT_CORE_REGISTRY,
    CONTAINMENT_ENTITY_WALLED,
    compose_tool_inventory,
    default_phase_grants,
    entity_walled_inventory,
    phase_capability_matrix,
)

_TOKEN = "entity-inventory-secret"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


# ---------------------------------------------------------------- composition


def _facade_present() -> bool:
    try:
        from abstractruntime.integrations.abstractcore.tool_inventory_facade import (  # noqa: F401
            core_registry_tool_rows,
        )

        return True
    except Exception:  # noqa: BLE001
        return False


def test_composition_is_the_union_with_gateway_attaching_containment() -> None:
    from abstractruntime.identity.tools import walled_tool_rows

    composed = compose_tool_inventory()
    inv = composed["tools"]
    walled_n = len(walled_tool_rows())

    if _facade_present():
        assert composed["degraded"] is False and composed["warnings"] == []
        assert len(inv) > walled_n, "with the facade the union carries core rows too"
    else:
        # Honest degradation: the core-registry conduit isn't shipped in this
        # runtime — walled rows serve completely, the absence is LABELED.
        assert composed["degraded"] is True and composed["warnings"], "facade absence must be labeled"
        assert len(inv) == walled_n

    # Gateway's ONE authorship: every row carries a containment, and the value
    # matches which enumeration it came from.
    for r in inv:
        assert r["executes_via"] in (CONTAINMENT_CORE_REGISTRY, CONTAINMENT_ENTITY_WALLED)
        if r["executes_via"] == CONTAINMENT_CORE_REGISTRY:
            assert r["owner"] == "core"
            assert r["grant_lane"] is None and r["capability_class"] is None
        else:
            assert r["owner"] == "runtime"
            assert r["grant_lane"] in ("tier1", "workspace")  # walled rows carry the grant lane


def test_total_order_is_executes_via_owner_name() -> None:
    inv = compose_tool_inventory()["tools"]
    keys = [(r["executes_via"], r["owner"], r["name"]) for r in inv]
    assert keys == sorted(keys), "contract rule 4: total order (executes_via, owner, name)"


def test_colliding_name_is_two_rows_never_one() -> None:
    # web_search exists in BOTH the core registry and the entity walled set —
    # the identity is (executes_via, name), so it is TWO rows, never one with
    # a silent resolver (contract rule 2). Only meaningful when the core
    # facade is present (both containments served).
    if not _facade_present():
        pytest.skip("core-registry facade not shipped in this runtime; only walled rows served")
    inv = compose_tool_inventory()["tools"]
    web = [r for r in inv if r["name"] == "web_search"]
    containments = {r["executes_via"] for r in web}
    assert containments == {CONTAINMENT_CORE_REGISTRY, CONTAINMENT_ENTITY_WALLED}
    walled_web = next(r for r in web if r["executes_via"] == CONTAINMENT_ENTITY_WALLED)
    registry_web = next(r for r in web if r["executes_via"] == CONTAINMENT_CORE_REGISTRY)
    assert walled_web["grant_lane"] == "tier1" and registry_web["grant_lane"] is None


def test_parameters_are_isolated_copies() -> None:
    # A consumer scribble on a served row's parameters must never rewrite the
    # process-wide native schema (core c901's isolation pin, extended through
    # the gateway composition). web_search is always a walled row, facade or not.
    inv1 = compose_tool_inventory()["tools"]
    row = next(r for r in inv1 if r["name"] == "web_search" and r["executes_via"] == CONTAINMENT_ENTITY_WALLED)
    row["parameters"]["__scribble__"] = {"type": "string"}  # shape-agnostic top-level poke
    inv2 = compose_tool_inventory()["tools"]
    row2 = next(r for r in inv2 if r["name"] == "web_search" and r["executes_via"] == CONTAINMENT_ENTITY_WALLED)
    assert "__scribble__" not in row2["parameters"], "served parameters must be isolated copies"


# --------------------------------------------------------------- phase matrix


def _items(m: dict) -> dict:
    """tool_id -> {phase_id -> cell} from the item-major MatrixPayload."""
    section = next(s for s in m["sections"] if s["id"] == "tools")
    return {it["id"]: it["cells"] for it in section["items"]}


def test_matrix_envelope_is_uic_shape_walled_rows_only() -> None:
    walled_names = {r["name"] for r in entity_walled_inventory()}
    m = phase_capability_matrix(None)
    # phases is an ORDERED ARRAY of metadata (uic c929), not a dict.
    assert [p["id"] for p in m["phases"]] == ["visit", "work", "personal", "sleep"]
    assert all("label" in p for p in m["phases"])
    # tools are section ITEMS (rule 2b: walled rows only), cells keyed by phase.
    items = _items(m)
    assert set(items.keys()) == walled_names, "matrix must offer ONLY walled rows (rule 2b)"
    for tool, cells in items.items():
        assert set(cells.keys()) == {"visit", "work", "personal", "sleep"}


def test_matrix_default_cells_are_assigned_false_resolved_by_default() -> None:
    # uic c929 semantics: a birth default is assigned=false (no stored
    # operator word) with resolved_value reflecting the framework default.
    defaults = default_phase_grants()
    items = _items(phase_capability_matrix(None))
    for tool, cells in items.items():
        for phase, cell in cells.items():
            assert cell["assigned"] is False, "no policy = no stored operator word"
            assert cell["provenance"] == "default"
            assert cell["resolved_value"] is (tool in set(defaults[phase]))
    # Q1: sleep is read-only-minus-diary — write_file/diary_read resolve false.
    assert items["write_file"]["sleep"]["resolved_value"] is False
    assert items["diary_read"]["sleep"]["resolved_value"] is False
    assert items["web_search"]["visit"]["resolved_value"] is True


def test_matrix_cells_carry_descriptor_fields_server_side() -> None:
    cell = _items(phase_capability_matrix(None))["web_search"]["visit"]
    for key in ("grant_lane", "capability_class", "mutating", "remote_write_capable", "act_only", "executable"):
        assert key in cell, f"cell missing server-declared field {key!r}"
    assert cell["executable"] is True


def test_operator_policy_marks_provenance_and_narrows() -> None:
    items = _items(phase_capability_matrix({"visit": ["diary_list"]}))
    # visit is operator-worded: only diary_list is assigned+resolved; the rest
    # are operator-provenance but resolved false (narrowed below the default).
    assert items["diary_list"]["visit"] == {
        **items["diary_list"]["visit"],
        "assigned": True,
        "resolved_value": True,
        "provenance": "operator",
    }
    assert items["web_search"]["visit"]["assigned"] is False
    assert items["web_search"]["visit"]["resolved_value"] is False
    assert items["web_search"]["visit"]["provenance"] == "operator"
    # Unnamed phases stay default-provenance + default-resolved.
    assert items["web_search"]["work"]["provenance"] == "default"
    assert items["web_search"]["work"]["resolved_value"] is True


def test_matrix_conforms_to_uic_compiled_validator() -> None:
    """The conformance fixture (agency c927/c910, uic c929): the producer
    output must pass uic's SHIPPED validateMatrixPayload — the check that
    catches an envelope drift at co-review instead of at console integration.
    Skips when node or the compiled dist validator is unavailable."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node required for the uic MatrixPayload conformance fixture")
    validator = (
        Path(__file__).resolve().parents[2]
        / "abstractuic" / "ui-kit" / "dist" / "phase_capability_matrix_core.js"
    )
    if not validator.exists():
        pytest.skip(f"uic compiled validator not present at {validator}")

    for payload in (phase_capability_matrix(None), phase_capability_matrix({"visit": ["diary_list"]})):
        harness = (
            f"import({json.dumps(str(validator))}).then(m => {{"
            f"  const r = m.validateMatrixPayload({json.dumps(payload)});"
            f"  if (!r.ok) {{ console.error(r.reason || 'invalid'); process.exit(3); }}"
            f"}}).catch(e => {{ console.error(e.message); process.exit(2); }});"
        )
        result = subprocess.run([node, "--input-type=module", "-e", harness], capture_output=True, text=True)
        assert result.returncode == 0, f"uic validator rejected the producer payload: {result.stderr}"


# --------------------------------------------------------------- served routes


def test_inventory_route_serves_walled_rows_always_and_registry_when_faced() -> None:
    with _client() as client:
        r = client.get("/api/gateway/entities/inventory/tools")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == 1
        names_by_containment = {(t["executes_via"], t["name"]) for t in body["tools"]}
        # Walled rows always serve (runtime is always present).
        assert (CONTAINMENT_ENTITY_WALLED, "diary_list") in names_by_containment
        if _facade_present():
            assert body["degraded"] is False
            assert (CONTAINMENT_CORE_REGISTRY, "execute_command") in names_by_containment
        else:
            assert body["degraded"] is True and body["warnings"]


def test_capability_matrix_route_is_entity_independent() -> None:
    # No entity created — the modal reads this BEFORE the entity exists.
    with _client() as client:
        r = client.get("/api/gateway/entities/inventory/capability-matrix")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [p["id"] for p in body["phases"]] == ["visit", "work", "personal", "sleep"]
        assert body["containment"] == CONTAINMENT_ENTITY_WALLED
        assert any(s["id"] == "tools" for s in body["sections"])
