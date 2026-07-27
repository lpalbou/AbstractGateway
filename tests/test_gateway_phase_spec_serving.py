"""THE ONE STATE GRAPH, served (laurent dm#79 via c3562: "gateway MUST serve
your state graph... there is only one state graph per entity and it MUST be
shared"). Entity holds the pen (spec/entity_phases.json); the gateway vendors
a byte copy and serves it at GET /entities/spec/phases; a drift pin compares
the vendored bytes against the source spec whenever the checkout is present
(sync-by-vigilance is the diary_type-clamp class — the pin makes drift loud).
"""

from __future__ import annotations

import copy
import hashlib
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


def test_spec_phases_route_serves_the_vendored_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "spec-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer spec-secret"}) as client:
        r = client.get("/api/gateway/entities/spec/phases")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vendored"] is True
        spec = body["spec"]
        # The ruled four, exactly (laurent c203: at all times one of these).
        assert sorted(spec["phases"].keys()) == ["personal", "sleep", "visit", "work"]
        assert spec["initial_phase"] == "sleep"
        # The sha is the honest sync token for consumers' drift warnings.
        assert len(body["sha256"]) == 64


def test_vendored_spec_matches_the_source_pen() -> None:
    """Drift pin: entity's spec/entity_phases.json is the ONE pen; the
    vendored copy must byte-match whenever the checkout is present (absent
    checkout = skip — a deployed wheel has no sibling repo). Resolution is
    SIBLING-RELATIVE (this repo's parent) + env override — never a
    machine-specific absolute path (sharedgraph adversary P1-4: a hardcoded
    /Users/... made the pin protect exactly one machine)."""
    import os

    env = str(os.getenv("ABSTRACTENTITY_SPEC_PATH") or "").strip()
    if env:
        source = Path(env).expanduser()
    else:
        source = Path(__file__).resolve().parents[2] / "abstractentity" / "spec" / "entity_phases.json"
    if not source.is_file():
        pytest.skip("abstractentity checkout not present — drift pin runs in the workspace only")
    from importlib import resources

    vendored = (resources.files("abstractgateway") / "assets" / "entity_phases.json").read_text(encoding="utf-8")
    src = source.read_text(encoding="utf-8")
    assert hashlib.sha256(vendored.encode()).hexdigest() == hashlib.sha256(src.encode()).hexdigest(), (
        "vendored entity_phases.json drifted from entity's pen — re-vendor "
        "(cp abstractentity/spec/entity_phases.json src/abstractgateway/assets/) "
        "per the bump protocol (entity announces, consumers re-vendor same-day)"
    )


def test_cognition_graph_route_serves_the_vendored_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE COGNITION MAP served (operator correction c5070): the editable
    target is the memory-cognition state graph, not the phase graph. Gateway
    serves the vendored artifact + sha as one-truth for the blueprint page +
    observer (the "bundled-only, unserved" gap closed). Serve-only today —
    the edit door is gated; the response says so honestly."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "cog-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer cog-secret"}) as client:
        r = client.get("/api/gateway/entities/spec/cognition-graph")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vendored"] is True
        assert len(body["sha256"]) == 64
        graph = body["graph"]
        # The artifact carries nodes + edges (the cognition topology).
        assert isinstance(graph.get("nodes"), list) and body["node_count"] == len(graph["nodes"])
        assert isinstance(graph.get("edges"), list) and body["edge_count"] == len(graph["edges"])
        # Serve-only honesty: the edit door is gated, and the response says so
        # (no false "editable" claim — the lying-save-note class, inverted).
        assert body["editable"] is False and body["operator_edited"] is False
        assert "gated" in body["edit_status"]
        # Distinct from the phase graph (different sha, different artifact).
        phases = client.get("/api/gateway/entities/spec/phases").json()
        assert body["sha256"] != phases["sha256"]


def test_vendored_cognition_graph_matches_the_source_pen() -> None:
    """Drift pin (the phase-graph pin's twin): entity's spec/cognition_graph.json
    is the ONE pen; the vendored copy must byte-match whenever the checkout is
    present. Sibling-relative + env override, never a machine path."""
    import os

    env = str(os.getenv("ABSTRACTENTITY_COGNITION_SPEC_PATH") or "").strip()
    if env:
        source = Path(env).expanduser()
    else:
        source = Path(__file__).resolve().parents[2] / "abstractentity" / "spec" / "cognition_graph.json"
    if not source.is_file():
        pytest.skip("abstractentity checkout not present — drift pin runs in the workspace only")
    from importlib import resources

    vendored = (resources.files("abstractgateway") / "assets" / "cognition_graph.json").read_text(encoding="utf-8")
    src = source.read_text(encoding="utf-8")
    assert hashlib.sha256(vendored.encode()).hexdigest() == hashlib.sha256(src.encode()).hexdigest(), (
        "vendored cognition_graph.json drifted from entity's pen — re-vendor "
        "(cp abstractentity/spec/cognition_graph.json src/abstractgateway/assets/) "
        "per the bump protocol (entity announces a widen, consumers re-vendor same-day)"
    )


def test_serving_boundary_phase_words_are_graph_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """The serving-boundary pin (entity's c3562 point 3, structural): every
    phase word /cognition can serve is a GRAPH word — None survives only
    under the kill switch (liveness=stopped)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "boundary-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer boundary-secret"}) as client:
        spec = client.get("/api/gateway/entities/spec/phases").json()["spec"]
        graph_words = set(spec["phases"].keys())

        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        # Newborn (asleep), idle-after-wake, and paused — the three resting
        # shapes; each serve must answer with a graph word or stopped-None.
        for state, expect_alive in ((None, True), ("awake", True), ("paused", False)):
            if state is not None:
                assert client.post("/api/gateway/entities/Castor/state", json={"state": state}).status_code == 200
            cog = client.get("/api/gateway/entities/Castor/cognition").json()
            if expect_alive:
                assert cog["phase"] in graph_words, f"served {cog['phase']!r} is not a graph word"
            else:
                assert cog["phase"] is None and cog["liveness"] == "stopped"


def test_operator_blueprint_edit_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """THE EDITABLE BLUEPRINT, v12 P0-3 shape (entity c354 ask 3): dial
    OVERLAYS beside the structural graph. Structural sha untouched by
    operator edits (drift warns never fire on a modulation); CAS via
    if_match/edit_seq; known-keys-only + bounds from tunables_meta; the
    full-replace arm is GONE; the derived effective file is what detached
    loops read; blueprint_edited markers land per entity."""
    import json as _json

    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "spec-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer spec-secret"}) as client:
        # An entity exists so the edit lands a biography marker.
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        base = client.get("/api/gateway/entities/spec/phases").json()
        assert base["operator_edited"] is False
        assert "tunables_overlay" not in base
        structural_sha = base["sha256"]
        base_window = base["spec"]["tunables"]["personal_cycle"]["personal_window_h"]
        assert base["effective_tunables"]["personal_cycle"]["personal_window_h"] == base_window

        # Dial edit: laurent modulates the personal window 2.0 -> 3.0.
        edited = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"personal_cycle": {"personal_window_h": 3.0}}, "reason": "test modulation", "if_match": 0},
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["edit_seq"] == 1 and body["markers_recorded"] == 1
        # STRUCTURAL sha untouched — the whole point of the overlay shape.
        assert body["sha256"] == structural_sha
        assert body["effective_tunables"]["personal_cycle"]["personal_window_h"] == 3.0
        assert body["effective_tunables"]["personal_cycle"]["sleep_window_h"] == base["spec"]["tunables"]["personal_cycle"]["sleep_window_h"]

        # GET: structural spec byte-stable; overlay + effective beside it.
        served = client.get("/api/gateway/entities/spec/phases").json()
        assert served["sha256"] == structural_sha
        assert served["spec"]["tunables"]["personal_cycle"]["personal_window_h"] == base_window  # structural untouched
        assert served["operator_edited"] is True
        assert served["overlay"]["edit_seq"] == 1
        assert served["tunables_overlay"] == {"personal_cycle": {"personal_window_h": 3.0}}
        assert served["effective_tunables"]["personal_cycle"]["personal_window_h"] == 3.0

        # The derived effective FILE (what detached loops read) carries the
        # merged tunables atomically.
        stored = _json.loads((tmp_path / "runtime" / "config" / "entity_phases.json").read_text(encoding="utf-8"))
        assert stored["tunables"]["personal_cycle"]["personal_window_h"] == 3.0
        assert stored["_operator"]["derived"] is True
        overlay_file = _json.loads((tmp_path / "runtime" / "config" / "entity_phases_overlay.json").read_text(encoding="utf-8"))
        assert overlay_file["edit_seq"] == 1 and overlay_file["tunables"] == {"personal_cycle": {"personal_window_h": 3.0}}

        # The biography marker landed (write-first, then marker; payload
        # flattens details beside `kind`).
        stream = (tmp_path / "runtime" / "entities" / ".host_stream" / "castor.jsonl").read_text(encoding="utf-8")
        marks = [_json.loads(line)["payload"] for line in stream.splitlines()]
        edits = [m for m in marks if m.get("kind") == "blueprint_edited"]
        assert edits and edits[-1]["edit_seq"] == 1
        assert edits[-1]["reason"] == "test modulation"
        assert edits[-1]["structural_sha256"] == structural_sha

        # CAS: a stale if_match races out with 409; the right one lands.
        raced = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"unattended_wake_cadence_h": 4.0}, "if_match": 0},
        )
        assert raced.status_code == 409, raced.text
        again = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"unattended_wake_cadence_h": 4.0}, "if_match": 1},
        )
        assert again.status_code == 200 and again.json()["edit_seq"] == 2

        # Known-keys-only: a typo'd dial refuses, never defaults silently.
        typo = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"personal_cycle": {"personal_windw_h": 3.0}}, "if_match": 2},
        )
        assert typo.status_code == 400 and "unknown dial" in typo.json()["detail"]
        # Bounds from tunables_meta: over-max refuses.
        over = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"personal_cycle": {"sleep_window_h": 12.0}}, "if_match": 2},
        )
        assert over.status_code == 400 and "maximum" in over.json()["detail"]
        # tunables_meta itself is the structural pen's.
        meta_edit = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"tunables_meta": {"x": 1}}, "if_match": 2},
        )
        assert meta_edit.status_code == 400

        # The full-replace arm is GONE (v12 item 4): `spec` is not a field.
        legacy = client.put(
            "/api/gateway/entities/spec/phases",
            json={"spec": dict(base["spec"]), "if_match": 2},
        )
        assert legacy.status_code in (400, 422), legacy.text

        # Non-admin principals are refused by the policy table.
        made = client.post(
            "/api/gateway/admin/users",
            json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]},
        )
        token = made.json().get("token")
        refused = client.put(
            "/api/gateway/entities/spec/phases",
            headers={"Authorization": f"Bearer {token}"},
            json={"tunables": {"sleep_bound_h": 0.5}, "if_match": 2},
        )
        assert refused.status_code == 403, refused.text


def test_corrupt_overlay_degrades_loudly_never_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """dm#112 edit-adversary risk 5 (entity G3): a corrupt overlay must never
    silently read as {} — dials fall to structural seeds VISIBLY (a wire
    warning + server log), because the day toggles ride this file a silent
    empty-read is a behavior flip."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "spec-secret")

    cfg = tmp_path / "runtime" / "config"
    cfg.mkdir(parents=True)
    (cfg / "entity_phases_overlay.json").write_text("{ this is not json", encoding="utf-8")

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer spec-secret"}) as client:
        body = client.get("/api/gateway/entities/spec/phases").json()
        # Structural dials serve (seeds), and the degradation is LABELED.
        assert body["operator_edited"] is False
        assert body["effective_tunables"]["personal_cycle"]["personal_window_h"] == body["spec"]["tunables"]["personal_cycle"]["personal_window_h"]
        warnings = body.get("warnings") or []
        assert any("UNREADABLE" in w and "#FALLBACK" in w for w in warnings), body.get("warnings")


def test_structural_edge_ops_edit_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """STRUCTURAL GRAPH EDITING through the PUT overlay (operator build,
    laurent dm#276 via c4837; schema confirmed both ends c4865/c4870):
    graph.edge_ops beside tunables — validate against the artifact's own
    graph_overlay_contract, merge at ONE point, write the effective file
    carrying structural_sha256 (runtime's H2 handshake input) + the graph
    block (their idempotent re-derivation), refuse illegal ops with the
    ARTIFACT's code, document-ownership semantics (present = replace,
    absent = untouched, empty list = clear)."""
    import json as _json

    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "spec-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer spec-secret"}) as client:
        # An entity exists so the edit lands a biography marker.
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        base = client.get("/api/gateway/entities/spec/phases").json()
        structural_sha = base["sha256"]
        base_edge_count = len(base["spec"]["transitions"])

        # ILLEGAL FIRST (the boundary invariant): removing a LOCKED edge
        # refuses with the ARTIFACT's code — body sibling + header — and
        # NOTHING persists (no overlay, no effective file).
        refused = client.put(
            "/api/gateway/entities/spec/phases",
            json={"graph": {"edge_ops": [{"op": "remove", "edge": "sleep->visit#visit_open"}]}, "if_match": 0},
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "constitutional"
        assert refused.headers.get("X-Gateway-Error-Code") == "constitutional"
        assert "R4" in refused.json()["detail"]  # the artifact's own ruling text rides
        assert not (tmp_path / "runtime" / "config" / "entity_phases_overlay.json").exists()

        # LEGAL BATCH: remove a consultable loop edge, redirect the
        # consultable-redirect visit-close edge, add a new edge on a
        # shipped cause with steering prose.
        ops = [
            {"op": "remove", "edge": "sleep->work#cadence_need_check"},
            {"op": "redirect", "edge": "visit->work#visit_close", "to": "personal",
             "instruction": "after visits, decompress personally before work"},
            {"op": "add", "from": "work", "to": "personal", "cause": "task_complete",
             "instruction": "take personal time when a task completes", "bound_h": 1.0},
        ]
        edited = client.put(
            "/api/gateway/entities/spec/phases",
            json={"graph": {"edge_ops": ops}, "reason": "structural test", "if_match": 0},
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["changed"] == "graph"
        assert body["sha256"] == structural_sha  # STRUCTURAL sha untouched (drift warns stay quiet)
        assert body["overlay"]["graph"]["edge_ops"] == ops
        eff = {t["edge_id"] if "edge_id" in t else f"{t['from']}->{t['to']}#{t['cause']}": t
               for t in body["effective_transitions"]}
        assert "sleep->work#cadence_need_check" not in eff
        assert "visit->personal#visit_close" in eff  # redirected (dedups onto the existing edge_id space)
        assert "work->personal#task_complete" in eff and eff["work->personal#task_complete"]["overlay_op"] == "add"

        # The EFFECTIVE FILE (what detached loops read): merged transitions,
        # the graph block (runtime's idempotent re-derivation input), and
        # structural_sha256 (the H2 handshake).
        stored = _json.loads((tmp_path / "runtime" / "config" / "entity_phases.json").read_text(encoding="utf-8"))
        assert stored["structural_sha256"] == structural_sha
        assert stored["graph"] == {"edge_ops": ops}
        stored_ids = {t.get("edge_id") or f"{t['from']}->{t['to']}#{t['cause']}" for t in stored["transitions"]}
        assert "sleep->work#cadence_need_check" not in stored_ids
        assert "work->personal#task_complete" in stored_ids
        # remove(-1) + add(+1) + redirect DEDUPS onto the baseline
        # visit->personal#visit_close edge (-1; the validator's
        # redirect-onto-existing-target pin, now through the full PUT lane)
        assert len(stored["transitions"]) == base_edge_count - 1

        # The overlay stores ops + provenance sha.
        ov = _json.loads((tmp_path / "runtime" / "config" / "entity_phases_overlay.json").read_text(encoding="utf-8"))
        assert ov["graph"] == {"edge_ops": ops} and ov["structural_sha256"] == structural_sha

        # The biography marker carries the op count.
        stream = (tmp_path / "runtime" / "entities" / ".host_stream" / "castor.jsonl").read_text(encoding="utf-8")
        marks = [_json.loads(line)["payload"] for line in stream.splitlines()]
        edits = [m for m in marks if m.get("kind") == "blueprint_edited"]
        assert edits and edits[-1]["changed"] == "graph" and edits[-1]["graph_ops"] == 3

        # GET is symmetric: overlay.graph + effective_transitions serve.
        served = client.get("/api/gateway/entities/spec/phases").json()
        assert served["overlay"]["graph"]["edge_ops"] == ops
        assert {t.get("edge_id") or f"{t['from']}->{t['to']}#{t['cause']}" for t in served["effective_transitions"]} == stored_ids
        assert served["spec"]["transitions"] == base["spec"]["transitions"]  # structural untouched

        # DIALS-ONLY PUT: stored ops ride along UNCHANGED (absent = untouched).
        dials = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"unattended_wake_cadence_h": 4.0}, "if_match": 1},
        )
        assert dials.status_code == 200 and dials.json()["changed"] == "tunables"
        assert dials.json()["overlay"]["graph"]["edge_ops"] == ops
        stored2 = _json.loads((tmp_path / "runtime" / "config" / "entity_phases.json").read_text(encoding="utf-8"))
        assert stored2["graph"] == {"edge_ops": ops}
        assert stored2["tunables"]["unattended_wake_cadence_h"] == 4.0

        # EMPTY LIST CLEARS (document ownership): the structural graph is
        # whole again; the graph block leaves the overlay + effective file.
        cleared = client.put(
            "/api/gateway/entities/spec/phases",
            json={"graph": {"edge_ops": []}, "if_match": 2},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["changed"] == "graph"
        assert "graph" not in cleared.json()["overlay"]
        stored3 = _json.loads((tmp_path / "runtime" / "config" / "entity_phases.json").read_text(encoding="utf-8"))
        assert "graph" not in stored3
        assert len(stored3["transitions"]) == base_edge_count
        assert stored3["tunables"]["unattended_wake_cadence_h"] == 4.0  # dials survive the clear


def test_effective_spec_crash_window_and_vendor_drift_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The overlay + effective file are two atomic writes (c4859): a crash
    between them, or a re-vendor under a standing overlay, leaves the FILE
    detached loops read stale. GET heals it (boot heals via the same
    function with an explicit data_dir); stored ops that no longer validate
    degrade to dials-only LOUDLY — never an incoherent graph in front of a
    loop."""
    import json as _json

    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "spec-secret")

    cfg = tmp_path / "runtime" / "config"
    cfg.mkdir(parents=True)
    # CRASH WINDOW: the overlay landed (edit_seq 3, one valid op) but the
    # effective file never rewrote — it still says edit_seq 1 and carries
    # no structural_sha256 (also the pre-build shape: self-migrates once).
    ops = [{"op": "remove", "edge": "sleep->work#cadence_need_check"}]
    (cfg / "entity_phases_overlay.json").write_text(
        _json.dumps({"edit_seq": 3, "edited_by": "person:op", "edited_at": "2026-07-23T00:00:00+00:00",
                     "tunables": {"unattended_wake_cadence_h": 5.0}, "graph": {"edge_ops": ops}}),
        encoding="utf-8",
    )
    (cfg / "entity_phases.json").write_text(
        _json.dumps({"stale": True, "_operator": {"edit_seq": 1}}), encoding="utf-8"
    )

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer spec-secret"}) as client:
        served = client.get("/api/gateway/entities/spec/phases").json()
        assert served["overlay"]["edit_seq"] == 3
        healed = _json.loads((cfg / "entity_phases.json").read_text(encoding="utf-8"))
        assert healed["_operator"]["edit_seq"] == 3
        assert healed["structural_sha256"] == served["sha256"]
        assert healed["tunables"]["unattended_wake_cadence_h"] == 5.0
        healed_ids = {t.get("edge_id") or f"{t['from']}->{t['to']}#{t['cause']}" for t in healed["transitions"]}
        assert "sleep->work#cadence_need_check" not in healed_ids

        # VENDOR DRIFT: the overlay carries an op referencing an edge the
        # current artifact does not declare (a re-vendor removed it). GET
        # serves the warning + NO effective_transitions; a dials-only PUT
        # refuses 409 naming the drift (never silently carries broken ops).
        (cfg / "entity_phases_overlay.json").write_text(
            _json.dumps({"edit_seq": 4, "edited_by": "person:op", "edited_at": "2026-07-23T00:00:00+00:00",
                         "tunables": {}, "graph": {"edge_ops": [{"op": "remove", "edge": "gone->away#never"}]}}),
            encoding="utf-8",
        )
        served2 = client.get("/api/gateway/entities/spec/phases").json()
        assert "effective_transitions" not in served2
        assert any("no longer valid" in w or "invalid against" in w for w in served2.get("warnings") or []), served2.get("warnings")
        # The healed effective file degraded to dials-only — structural
        # transitions, no graph block (a loop never reads a broken merge).
        degraded = _json.loads((cfg / "entity_phases.json").read_text(encoding="utf-8"))
        assert "graph" not in degraded
        assert degraded["_operator"]["edit_seq"] == 4

        drifted = client.put(
            "/api/gateway/entities/spec/phases",
            json={"tunables": {"unattended_wake_cadence_h": 6.0}, "if_match": 4},
        )
        assert drifted.status_code == 409, drifted.text
        assert "no longer valid" in drifted.json()["detail"]

        # Boot lane: the same reconcile with an explicit data_dir (no
        # service build) heals a stale file identically.
        (cfg / "entity_phases.json").write_text(
            _json.dumps({"stale": True, "_operator": {"edit_seq": 1}}), encoding="utf-8"
        )
        from abstractgateway.routes.entities import reconcile_effective_spec_file

        reconcile_effective_spec_file(data_dir=tmp_path / "runtime")
        boot_healed = _json.loads((cfg / "entity_phases.json").read_text(encoding="utf-8"))
        assert boot_healed["_operator"]["edit_seq"] == 4
        assert "structural_sha256" in boot_healed
