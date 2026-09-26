"""Per-entity skills selection (laurent c2857; committed shape c2838).

Selection persists in the home (`<home>/skills.yaml`), writes are
marker-first (`skills_selection_changed`), resolution is server-side
through the same trust gate as every other skills lane, and the GET serves
selection + resolved roster + the PhaseCapabilityMatrix payload so both
UIs render one truth. Delivery into entity prompts is deliberately absent
(runtime's election, c2859 ask 1).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def test_selection_file_round_trip_and_validation(tmp_path: Path) -> None:
    from abstractgateway.entity_skills import (
        read_skills_selection,
        validate_skills_selection,
        write_skills_selection,
    )

    home = tmp_path / "home"
    home.mkdir()
    assert read_skills_selection(home) == {"exists": False, "skills": []}

    write_skills_selection(home, [{"name": "entity-self-knowledge"}, {"name": "deep-research", "phases": ["work"]}])
    sel = read_skills_selection(home)
    assert sel["exists"] is True
    assert sel["skills"] == [
        {"name": "entity-self-knowledge"},
        {"name": "deep-research", "phases": ["work"]},
    ]

    # Phases validate against the ruled four (imported, never a copy).
    with pytest.raises(ValueError):
        validate_skills_selection([{"name": "x", "phases": ["rest"]}])
    # Empty phases list is a footgun (omit = everywhere), refused loudly.
    with pytest.raises(ValueError):
        validate_skills_selection([{"name": "x", "phases": []}])
    with pytest.raises(ValueError):
        validate_skills_selection([{"name": "x"}, {"name": "x"}])

    # Malformed file reads as empty WITH a label, never a crash.
    (home / "skills.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    sel2 = read_skills_selection(home)
    assert sel2["skills"] == [] and "#FALLBACK" in str(sel2.get("warning") or "")


def test_skills_endpoints_marker_first_resolution_and_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "skills-marker-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer skills-marker-secret"}) as client:
        # Create with EXPLICIT empty skills to test the endpoint's empty→set→
        # clear lifecycle: a default create now seeds entity-self-knowledge
        # (laurent seq 156), so the honest-empty state requires opting out.
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor"), "skills": []}).status_code == 201

        # Empty state: honest empty selection; matrix rides the kit envelope
        # (schema_version 1 + ruled-four phases declared in-payload — uic's
        # executable corrections c2894).
        r0 = client.get("/api/gateway/entities/Castor/skills")
        assert r0.status_code == 200, r0.text
        body0 = r0.json()
        assert body0["selection"] == {"exists": False, "skills": []}
        assert body0["matrix"]["schema_version"] == 1
        from abstractruntime import PHASES as _PHASES

        assert [p["id"] for p in body0["matrix"]["phases"]] == list(_PHASES)
        assert body0["matrix"]["sections"][0]["id"] == "skills"

        # Write a selection: one global, one phase-scoped, one typo'd name.
        r1 = client.put(
            "/api/gateway/entities/Castor/skills",
            json={"skills": [
                {"name": "entity-self-knowledge"},
                {"name": "no-such-skill-xyz", "phases": ["work"]},
            ]},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        # The response is the RESOLVED view: an unresolvable name is visible
        # immediately — with a shelf it is named ("missing from shelf: …"),
        # without one the labeled no-shelf fallback speaks; either way the
        # verdicts are non-empty and the row reads active=false, never a
        # silent no-op.
        assert body1["resolved"]["verdicts"], body1
        typo_row = next(s for s in body1["resolved"]["skills"] if s["name"] == "no-such-skill-xyz")
        assert typo_row["active"] is False
        names = [s["name"] for s in body1["selection"]["skills"]]
        assert names == ["entity-self-knowledge", "no-such-skill-xyz"]

        # Matrix (kit-validated shape, c2894): items nest inside sections;
        # global selection renders the ruled four with identical cells;
        # phase-scoped renders denied+assigned:false elsewhere; kit enums
        # only (denied, trust_gated+trust_state — never private words);
        # required booleans on every cell.
        from abstractruntime import PHASES

        items = {i["id"]: i for i in body1["matrix"]["sections"][0]["items"]}
        global_cells = items["entity-self-knowledge"]["cells"]
        assert set(global_cells.keys()) == set(PHASES)
        assert len({json.dumps(c, sort_keys=True) for c in global_cells.values()}) == 1
        for cell in global_cells.values():
            assert isinstance(cell["assigned"], bool) and isinstance(cell["resolved_value"], bool)
            assert cell["availability"] in {"granted", "denied", "structurally_unavailable", "trust_gated"}
        scoped = items["no-such-skill-xyz"]["cells"]
        assert scoped["work"]["availability"] == "trust_gated"
        assert scoped["work"]["trust_state"] in {"requires_review", "blocked"}
        assert scoped["work"]["assigned"] is True and scoped["work"]["resolved_value"] is False
        non_work = [p for p in PHASES if p != "work"]
        for p in non_work:
            assert scoped[p]["availability"] == "denied"
            assert scoped[p]["assigned"] is False and scoped[p]["resolved_value"] is False

        # Marker-first: old=[] -> new selection, principal-stamped.
        from abstractgateway.service import get_gateway_service

        markers_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "castor.jsonl"
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "skills_selection_changed"]
        assert len(changed) == 1
        assert changed[0]["old"] == []
        assert [s["name"] for s in changed[0]["new"]] == ["entity-self-knowledge", "no-such-skill-xyz"]
        assert changed[0]["by"] == "person:admin"

        # Whole-document replace: an empty list deselects everything, and
        # the marker timeline records the transition.
        r2 = client.put("/api/gateway/entities/Castor/skills", json={"skills": []})
        assert r2.status_code == 200
        assert r2.json()["selection"]["skills"] == []
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "skills_selection_changed"]
        assert len(changed) == 2 and changed[1]["new"] == []

        # Bad phases refuse BEFORE any marker lands (P2-2 rule).
        r3 = client.put(
            "/api/gateway/entities/Castor/skills",
            json={"skills": [{"name": "x", "phases": ["rest"]}]},
        )
        assert r3.status_code == 400
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        assert len([m for m in rows if m.get("payload", {}).get("kind") == "skills_selection_changed"]) == 2


def test_matrix_payload_validates_against_the_real_kit_validator(tmp_path: Path) -> None:
    """Cross-package pin (uic's c2894 offer, adopted): the served matrix
    payload runs through the REAL compiled kit validator + cell-view
    resolver, so a spelling drift fails a test instead of a render. Skips
    when node or the built kit is absent (sibling-checkout dependency)."""
    import json as _json
    import shutil
    import subprocess

    kit = Path(__file__).resolve().parents[2] / "abstractuic" / "ui-kit" / "dist" / "phase_capability_matrix_core.js"
    from node_requirement import require_node

    node = require_node()
    if not kit.is_file():
        pytest.skip("the built abstractuic kit is not available in this checkout")

    from abstractgateway.entity_skills import resolve_entity_skills, write_skills_selection

    home = tmp_path / "home"
    home.mkdir()
    write_skills_selection(home, [{"name": "entity-self-knowledge"}, {"name": "missing-skill", "phases": ["work"]}])
    matrix = resolve_entity_skills(home, data_dir=tmp_path)["matrix"]

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(_json.dumps(matrix), encoding="utf-8")
    script = f"""
import {{ readFileSync }} from 'node:fs';
const core = await import({_json.dumps(str(kit))});
const payload = JSON.parse(readFileSync({_json.dumps(str(payload_file))}, 'utf8'));
const r = core.validateMatrixPayload(payload);
if (!r.ok) {{ console.error('REFUSED: ' + r.reason); process.exit(1); }}
for (const item of payload.sections[0].items) {{
  for (const phase of Object.keys(item.cells)) {{
    const view = core.resolveCellView(payload, [], 'skills', item.id, phase);
    if (!view) {{ console.error('cell view failed: ' + item.id + '/' + phase); process.exit(1); }}
  }}
}}
console.log('ok');
"""
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"kit validator refused the served payload: {proc.stdout} {proc.stderr}"


def test_create_entity_carries_birth_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "skills-birth-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer skills-birth-secret"}) as client:
        # Invalid birth selection refuses BEFORE creating anything.
        r0 = client.post(
            "/api/gateway/entities",
            json={"name": "Pollux", "spark": _spark("Pollux"), "skills": [{"name": "x", "phases": ["nap"]}]},
        )
        assert r0.status_code == 400
        assert client.get("/api/gateway/entities/Pollux/skills").status_code == 404

        # Valid birth selection lands with the birth marker.
        r1 = client.post(
            "/api/gateway/entities",
            json={"name": "Pollux", "spark": _spark("Pollux"), "skills": [{"name": "entity-self-knowledge"}]},
        )
        assert r1.status_code == 201, r1.text
        assert r1.json().get("skills_selected") == ["entity-self-knowledge"]

        sel = client.get("/api/gateway/entities/Pollux/skills").json()["selection"]
        assert sel["skills"] == [{"name": "entity-self-knowledge"}]

        from abstractgateway.service import get_gateway_service

        markers_path = Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "pollux.jsonl"
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m["payload"] for m in rows if m.get("payload", {}).get("kind") == "skills_selection_changed"]
        assert len(changed) == 1 and changed[0].get("at_birth") is True


def test_birth_defaults_to_entity_self_knowledge_and_installs_map(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Default skills (laurent seq 156 "all entities must know how their
    memory work"): a create with NO skills is born selecting
    entity-self-knowledge AND carrying capability_map.md from the shelf,
    marker-first. An explicit selection is honored verbatim; an explicit
    empty list opts out of both."""
    from fastapi.testclient import TestClient

    # Point the shelf at the real checkout so the map resolves.
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "birthdef-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(Path(__file__).resolve().parents[2]))
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer birthdef-secret"}) as client:
        r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert "entity-self-knowledge" in (body.get("skills_selected") or []), "birth defaults to the mind-skill"
        # The map installs from the shelf when reachable (a 64-hex sha,
        # truncated to 16 in the response); if the shelf is unreachable the
        # honest warning stands instead.
        if "capability_map_installed" in body:
            assert len(body["capability_map_installed"]) == 16
            got = client.get("/api/gateway/entities/Castor/capability-map").json()
            assert got.get("sha256", "").startswith(body["capability_map_installed"])
        else:
            assert "capability_map_warning" in body

        # The birth skills selection is on the home.
        sk = client.get("/api/gateway/entities/Castor/skills").json()
        assert any(s.get("name") == "entity-self-knowledge" for s in sk["selection"]["skills"])


def test_explicit_empty_skills_opts_out_of_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who sends skills:[] explicitly gets NO default (absence is
    filled, an explicit empty is honored) — and no map install follows."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "emptyskills-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(Path(__file__).resolve().parents[2]))
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer emptyskills-secret"}) as client:
        r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor"), "skills": []})
        assert r.status_code == 201, r.text
        body = r.json()
        assert not body.get("skills_selected"), "explicit empty selection is honored, not defaulted"
        assert "capability_map_installed" not in body, "no map install without the mind-skill selected"
