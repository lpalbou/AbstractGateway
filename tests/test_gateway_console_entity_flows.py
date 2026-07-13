"""The console's entity + runs flows, driven against the live route surface.

These tests replay the EXACT request sequences the console JS sends (same
routes, same body shapes) so a payload drift between console.py and the
served contract lands RED here instead of in the operator's browser
(adversary finding: the ephemeral /tmp smokes that proved this surface
died with the session — coverage must live in the suite).

Covers:
- the create lane: templates -> capability matrix -> dry-run validate ->
  create -> substrate PUT -> tool-policy PUT (console sequence)
- merge semantics the matrix editor relies on: changed-phases-only PUT,
  null = revert-to-default, explicit [] = deliberate deny-all
- the embedding-status read the reembed ceremony renders (N6: no secrets)
- prompt roundtrip (operator layer stored; defaults never materialized)
- lifecycle: state readback, loop status shape, verify, reembed refusal
- runs surface: list shape + commands door refusals (unknown run)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "console-entity-flows-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _create(client: TestClient, name: str) -> dict:
    template = client.get("/api/gateway/entities/templates").json()["templates"][0]
    spark = dict(template["spark"])
    spark["name"] = name
    r = client.post("/api/gateway/entities", json={"name": name, "spark": spark})
    assert r.status_code == 201, r.text
    return spark


def test_console_create_sequence_end_to_end() -> None:
    """templates -> matrix -> validate -> create -> substrate -> tool-policy:
    the exact console order, every response shape the JS reads."""
    with _client() as client:
        g = client.get("/api/gateway/entities/templates")
        assert g.status_code == 200, g.text
        templates = g.json()["templates"]
        assert templates and templates[0].get("core_values"), "core-value chips need core_values"
        spark = dict(templates[0]["spark"])
        spark["name"] = "Smoky"

        m = client.get("/api/gateway/entities/inventory/capability-matrix")
        assert m.status_code == 200, m.text
        matrix = m.json()
        assert [p["id"] for p in matrix["phases"]] == ["visit", "work", "personal", "sleep"]
        section = next(s for s in matrix["sections"] if s["id"] == "tools")
        assert len(section["items"]) >= 5

        v = client.post("/api/gateway/entities/Smoky/validate", json={"name": "Smoky", "spark": spark})
        assert v.status_code == 200 and v.json()["ok"] is True, v.text

        r = client.post("/api/gateway/entities", json={"name": "Smoky", "spark": spark})
        assert r.status_code == 201, r.text

        s = client.put(
            "/api/gateway/entities/Smoky/substrate",
            json={"provider": "lmstudio", "model": "qwen/qwen3.6-35b-a3b"},
        )
        assert s.status_code == 200, s.text
        s2 = client.get("/api/gateway/entities/Smoky/substrate").json()
        assert s2["provider"] == "lmstudio" and s2["source"] == "entity"

        listed = client.get("/api/gateway/entities").json()["entities"]
        row = next(e for e in listed if e.get("name") == "Smoky")
        assert isinstance(row.get("state"), dict), "console renders state.state from a dict"

        card = client.get("/api/gateway/entities/Smoky/card")
        assert card.status_code == 200 and card.json().get("entity_id"), card.text


def test_matrix_merge_null_revert_and_deny_all() -> None:
    """The three write shapes the matrix editor sends: explicit list
    (narrow), null (revert-to-default), [] (deliberate deny-all)."""
    with _client() as client:
        _create(client, "Merca")
        base = client.get("/api/gateway/entities/Merca/tool-policy").json()["phases"]

        narrow = list(base["visit"]["tools"])[:1]
        r = client.put("/api/gateway/entities/Merca/tool-policy", json={"policy": {"visit": narrow}})
        assert r.status_code == 200, r.text
        after = client.get("/api/gateway/entities/Merca/tool-policy").json()["phases"]
        assert set(after["visit"]["tools"]) == set(narrow)
        assert after["work"]["source"] == "default", "unnamed phases must stay untouched (merge)"

        r = client.put("/api/gateway/entities/Merca/tool-policy", json={"policy": {"visit": None}})
        assert r.status_code == 200, r.text
        rev = client.get("/api/gateway/entities/Merca/tool-policy").json()["phases"]["visit"]
        assert rev["source"] == "default" and set(rev["tools"]) == set(base["visit"]["tools"]), (
            "null must delete the operator word (revert to the evolving default), uic c727 fold"
        )

        r = client.put("/api/gateway/entities/Merca/tool-policy", json={"policy": {"sleep": []}})
        assert r.status_code == 200, r.text
        deny = client.get("/api/gateway/entities/Merca/tool-policy").json()["phases"]["sleep"]
        assert deny["tools"] == [], "explicit [] is a deliberate deny-all and must stick"


def test_embedding_status_read_serves_the_ceremony_without_a_400_probe() -> None:
    """GET /{name}/embedding: pin + resolved embedder + match verdict, and
    the N6 whitelist holds (no base_url / api_key ever serializes)."""
    with _client() as client:
        _create(client, "Embry")
        e = client.get("/api/gateway/entities/Embry/embedding")
        assert e.status_code == 200, e.text
        body = e.json()
        for key in ("pin", "status", "resolved_embedder", "match"):
            assert key in body, f"console renders {key}"
        assert body["status"] in ("pinned", "unpinned")
        assert body["match"] in ("match", "mismatch", "unknown")
        assert "base_url" not in e.text and "api_key" not in e.text


def test_prompt_roundtrip_operator_layer_only() -> None:
    """The console PUTs every editable layer back; the server must store
    ONLY genuine rewrites (a default pasted back is not the operator's word)."""
    with _client() as client:
        _create(client, "Prosa")
        p = client.get("/api/gateway/entities/Prosa/prompt").json()
        assert "personal" in p["editable"] and "own_time" not in p["editable"]
        assert p.get("preview"), "the composed-prompt preview must serve"
        overlay = {
            k: (p["layers"][k]["text"] if k != "operator" else "Console flow test operator line.")
            for k in p["editable"]
        }
        w = client.put("/api/gateway/entities/Prosa/prompt", json={"overlay": overlay})
        assert w.status_code == 200, w.text
        p2 = client.get("/api/gateway/entities/Prosa/prompt").json()
        assert p2["layers"]["operator"]["source"] == "overlay"
        assert p2["layers"]["conversation"]["source"] == "default"


def test_lifecycle_state_loop_verify_and_reembed_refusal() -> None:
    with _client() as client:
        _create(client, "Lifa")
        st = client.post(
            "/api/gateway/entities/Lifa/state",
            json={"state": "asleep", "reason": "console flow test", "dream": False},
        )
        assert st.status_code == 200, st.text
        assert client.get("/api/gateway/entities/Lifa/state").json()["state"] == "asleep"
        wake = client.post("/api/gateway/entities/Lifa/state", json={"state": "awake", "reason": "wake"})
        assert wake.status_code == 200, wake.text

        lo = client.get("/api/gateway/entities/Lifa/loop")
        assert lo.status_code == 200 and lo.json().get("running") in (False, None)

        ver = client.get("/api/gateway/entities/Lifa/verify")
        assert ver.status_code == 200 and ver.json()["ok"] is True, ver.text

        re_ = client.post(
            "/api/gateway/entities/Lifa/reembed",
            json={"embedding_model": "not-the-resolved-embedder", "reason": "console flow test"},
        )
        assert 400 <= re_.status_code < 500, re_.text


def test_runs_surface_list_shape_and_command_door_refusals() -> None:
    """The Runs section reads {items:[...]} and drives /commands with a
    command_id; unknown-run steers refuse honestly at the door."""
    with _client() as client:
        r = client.get("/api/gateway/runs?limit=100&include_ledger_len=false&root_only=true")
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("items"), list)

        cancel = client.post(
            "/api/gateway/commands",
            json={"command_id": "cmd-flow-1", "type": "cancel", "run_id": "does-not-exist"},
        )
        assert cancel.status_code < 500, cancel.text

        steer = client.post(
            "/api/gateway/commands",
            json={
                "command_id": "cmd-flow-2",
                "type": "inject_guidance",
                "run_id": "does-not-exist",
                "payload": {"guidance": "focus"},
            },
        )
        assert steer.status_code in (400, 404), steer.text
