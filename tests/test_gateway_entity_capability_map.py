"""Capability-map install lane (laurent c2710; skill's entity-self-knowledge).

One teaching file per home (`<home>/capability_map.md`), served by GET/PUT
/{name}/capability-map. The PUT is marker-first (`capability_map_changed`
with old/new sha256, principal-stamped) — what a mind is TAUGHT must be
answerable from the replay stream, exactly like a substrate swap. Runtime's
compose_system_base presents the installed file verbatim on every summon.
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


def test_capability_map_put_is_a_durable_marked_event(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "capmap-marker-secret")
    from abstractgateway.app import app

    teaching_v1 = "# How your memory works\n\nA record keeps a one-line digest.\n"
    teaching_v2 = "# How your memory works (amended)\n\nDreams are born as their words.\n"

    with TestClient(app, headers={"Authorization": "Bearer capmap-marker-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        # Absent by default — an uninstalled map reads honestly, never 404s
        # (the entity exists; the teaching just is not installed).
        r0 = client.get("/api/gateway/entities/Castor/capability-map")
        assert r0.status_code == 200, r0.text
        assert r0.json() == {"installed": False, "size": 0, "sha256": None, "content": None}

        # Install: marker-first write.
        r1 = client.put("/api/gateway/entities/Castor/capability-map", json={"content": teaching_v1})
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["installed"] is True
        assert body["sha256"] == hashlib.sha256(teaching_v1.encode("utf-8")).hexdigest()

        # The file rests in the home where runtime's read_capability_map looks.
        from abstractgateway.service import get_gateway_service

        data_dir = Path(get_gateway_service().config.data_dir)
        installed = data_dir / "entities" / "castor" / "capability_map.md"
        assert installed.read_text(encoding="utf-8") == teaching_v1

        # GET round-trips the exact bytes.
        r2 = client.get("/api/gateway/entities/Castor/capability-map")
        assert r2.status_code == 200
        assert r2.json()["content"] == teaching_v1

        # Update: the marker timeline carries old -> new hashes, principal-stamped.
        r3 = client.put("/api/gateway/entities/Castor/capability-map", json={"content": teaching_v2})
        assert r3.status_code == 200, r3.text

        markers_path = data_dir / "entities" / ".host_stream" / "castor.jsonl"
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m for m in rows if m.get("payload", {}).get("kind") == "capability_map_changed"]
        assert len(changed) == 2
        first, second = changed[0]["payload"], changed[1]["payload"]
        assert first["old_sha256"] is None
        assert first["new_sha256"] == hashlib.sha256(teaching_v1.encode("utf-8")).hexdigest()
        assert second["old_sha256"] == first["new_sha256"]
        assert second["new_sha256"] == hashlib.sha256(teaching_v2.encode("utf-8")).hexdigest()
        assert first["by"] == "person:admin"

        # Blank-content installs are refused BEFORE any marker lands (the
        # substrate P2-2 lesson: a recorded change that never happened).
        r4 = client.put("/api/gateway/entities/Castor/capability-map", json={"content": "   "})
        assert r4.status_code == 400
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        assert len([m for m in rows if m.get("payload", {}).get("kind") == "capability_map_changed"]) == 2


def test_capability_map_runtime_delivery_reads_the_installed_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The install lane's whole point: runtime's read_capability_map picks up
    exactly the bytes the endpoint wrote (one file, one composition slot)."""
    read_capability_map = pytest.importorskip("abstractruntime.identity.chat").read_capability_map

    home = tmp_path / "someone"
    home.mkdir()
    assert read_capability_map(home) == ""
    (home / "capability_map.md").write_text("# teaching\n", encoding="utf-8")
    assert read_capability_map(home).strip() == "# teaching"
