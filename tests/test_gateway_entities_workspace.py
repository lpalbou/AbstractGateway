"""Workspace browse/read + mounts whitelist + per-phase tool policy
(maintainer asks, 2026-07-08): the operator's window into the entity's
territory, and the walls' configuration — all through the gateway door.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-workspace-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_workspace_browse_read_and_mounts(tmp_path):
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        ws_dir = registry.entities_dir / "castor" / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "home.md").write_text("# Home\n", encoding="utf-8")
        (ws_dir / "notes").mkdir(exist_ok=True)
        (ws_dir / "notes" / "day1.md").write_text("first day\n", encoding="utf-8")

        listing = client.get("/api/gateway/entities/Castor/workspace").json()
        names = {e["name"]: e for e in listing["entries"]}
        assert names["home.md"]["kind"] == "file"
        assert names["notes"]["kind"] == "dir"

        sub = client.get("/api/gateway/entities/Castor/workspace", params={"path": "notes"}).json()
        assert [e["path"] for e in sub["entries"]] == ["notes/day1.md"]

        f = client.get("/api/gateway/entities/Castor/workspace/file", params={"path": "notes/day1.md"}).json()
        assert f["text"] == "first day\n" and f["truncated"] is False

        # Containment: an escape reads as 400, never a traceback.
        esc = client.get("/api/gateway/entities/Castor/workspace/file", params={"path": "../manifest.json"})
        assert esc.status_code == 400

        # Mounts: whitelist a read-only dir, see it in the listing, read
        # through it; validation refuses a missing path loudly.
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "brief.txt").write_text("operator brief\n", encoding="utf-8")
        put = client.put(
            "/api/gateway/entities/Castor/workspace/mounts",
            json={"mounts": [{"name": "shared", "path": str(shared), "mode": "ro"}]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["mounts"][0]["mode"] == "ro"

        root = client.get("/api/gateway/entities/Castor/workspace").json()
        mounts = [e for e in root["entries"] if e["kind"] == "mount"]
        assert mounts and mounts[0]["path"] == "mounts/shared"

        through = client.get(
            "/api/gateway/entities/Castor/workspace/file", params={"path": "mounts/shared/brief.txt"}
        ).json()
        assert through["text"] == "operator brief\n"

        bad = client.put(
            "/api/gateway/entities/Castor/workspace/mounts",
            json={"mounts": [{"name": "ghost", "path": str(tmp_path / "missing"), "mode": "ro"}]},
        )
        assert bad.status_code == 400
        assert "not an existing directory" in bad.json()["detail"]


def test_tool_policy_get_put_roundtrip():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractruntime.identity.tool_policy import (
            ALL_TOOL_NAMES,
            SLEEP_DEFAULT_TOOL_NAMES,
        )

        before = client.get("/api/gateway/entities/Castor/tool-policy").json()
        assert before["phases"]["visit"]["source"] == "default"
        # The served vocabulary is the RULED four phases (laurent c786:
        # visit/work/personal/sleep). Ruled defaults (Q1 c684: work+personal
        # = full set), EXACT lists against the imported constants (order
        # included — a wrong extra tool or a reorder must fail, not pass a
        # loose membership check).
        assert set(before["phases"]) == {"visit", "work", "personal", "sleep"}
        assert before["phases"]["visit"]["tools"] == list(ALL_TOOL_NAMES)
        assert before["phases"]["work"]["tools"] == list(ALL_TOOL_NAMES)
        assert before["phases"]["personal"]["tools"] == list(ALL_TOOL_NAMES)
        assert before["phases"]["sleep"]["tools"] == list(SLEEP_DEFAULT_TOOL_NAMES)
        assert set(before["tiers"]) == {"tier1", "workspace"}

        put = client.put(
            "/api/gateway/entities/Castor/tool-policy",
            json={"policy": {"visit": ["diary_list", "diary_read"], "personal": ["diary_list"], "sleep": []}},
        )
        assert put.status_code == 200, put.text
        after = put.json()
        assert after["phases"]["visit"] == {"tools": ["diary_list", "diary_read"], "source": "policy-file", "notes": []}
        assert after["phases"]["personal"]["tools"] == ["diary_list"]

        # Migration window (N7 contract, runtime c672 + c786 rename): a legacy
        # spelling ("own_time", now aliasing personal) stays WRITE-accepted
        # through the door and lands NORMALIZED under personal — the served
        # object never carries a second at-rest spelling. The alias dies
        # before release; when runtime flips it to expects-raise this leg
        # flips with it.
        legacy = client.put(
            "/api/gateway/entities/Castor/tool-policy",
            json={"policy": {"own_time": ["diary_read"]}},
        )
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["phases"]["personal"]["tools"] == ["diary_read"]
        assert "own_time" not in legacy.json()["phases"]

        refused = client.put(
            "/api/gateway/entities/Castor/tool-policy",
            json={"policy": {"visit": ["rm_rf"]}},
        )
        assert refused.status_code == 400
        assert "unknown tool" in refused.json()["detail"]
