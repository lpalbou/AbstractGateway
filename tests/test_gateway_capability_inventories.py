"""Skills + MCP inventories (operator directive 2026-07-15 16:22, observer
c2233 asks 1+2): `GET /api/gateway/skills` serves the abstractskill shelf
with trust verdicts (the ruled roster row —
decision:workforce-capabilities-homes); `GET /api/gateway/mcp/servers`
serves the declared MCP registry with `probed: false` honesty.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractskill")

from abstractgateway.capability_inventories import (  # noqa: E402
    mcp_servers_inventory,
    skills_inventory,
)

_TOKEN = "capability-inventories-secret"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ABSTRACTGATEWAY_SKILLS_SHELF",
        "ABSTRACT_TRIAGE_REPO_ROOT",
        "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_shelf(root: Path, *, with_scripts: bool = False) -> Path:
    """A minimal curated shelf: registry dir with skills/ + empty trust files."""
    registry = root / "registry"
    skills = registry / "skills"
    skill_dir = skills / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo teaching for inventory tests.\n---\n\n# Demo\nBody.\n",
        encoding="utf-8",
    )
    if with_scripts:
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        (scripts / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (registry / "validations.yaml").write_text("validations: []\n", encoding="utf-8")
    (registry / "advisories.yaml").write_text("advisories: []\n", encoding="utf-8")
    (registry / "guidance.yaml").write_text("guidance: []\n", encoding="utf-8")
    return registry


# ------------------------------------------------------------------- skills


def test_skills_inventory_serves_roster_rows_with_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _write_shelf(tmp_path)
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(registry))

    out = skills_inventory(data_dir=tmp_path / "runtime")
    assert out["shelf"] == str(registry / "skills")
    rows = out["skills"]
    assert len(rows) == 1
    row = rows[0]
    # The ruled roster shape (decision:workforce-capabilities-homes).
    assert row["name"] == "demo-skill"
    assert row["description"].startswith("A demo teaching")
    assert row["trust_level"] == "unverified"  # empty registry => fail closed
    assert row["blocked"] is False
    assert row["requires_review"] is True
    assert isinstance(row["tree_hash"], str) and len(row["tree_hash"]) == 64
    assert row["has_scripts"] is False
    assert any("unverified" in r for r in row["reasons"])
    # The forbidden word never renders on this surface.
    assert "safe" not in json.dumps(out).lower()


def test_skills_inventory_marks_scripts_for_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _write_shelf(tmp_path, with_scripts=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(registry))

    row = skills_inventory(data_dir=tmp_path / "runtime")["skills"][0]
    assert row["has_scripts"] is True
    assert row["requires_review"] is True


def test_skills_inventory_absent_shelf_is_an_honest_empty(tmp_path: Path) -> None:
    out = skills_inventory(data_dir=tmp_path / "runtime")
    assert out["skills"] == [] and out["shelf"] is None
    assert any("no curated shelf" in w for w in out["warnings"])


# ---------------------------------------------------------------------- mcp


def test_mcp_inventory_serves_declared_rows_unprobed(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    cfg = data_dir / "config"
    cfg.mkdir(parents=True)
    (cfg / "mcp_servers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {"name": "docs-mcp", "url": "http://127.0.0.1:9000", "description": "Docs server", "auth_required": True, "tags": ["docs"]},
                    {"name": "docs-mcp", "url": "http://dup"},
                    {"url": "http://nameless"},
                    "not-an-object",
                ],
            }
        ),
        encoding="utf-8",
    )

    out = mcp_servers_inventory(data_dir=data_dir)
    assert out["probed"] is False
    assert len(out["servers"]) == 1
    row = out["servers"][0]
    assert row == {
        "name": "docs-mcp",
        "url": "http://127.0.0.1:9000",
        "description": "Docs server",
        "auth_required": True,
        "tags": ["docs"],
    }
    # Malformed/duplicate rows are labeled, never silently dropped.
    assert sum("skipped" in w for w in out["warnings"]) == 3


def test_mcp_inventory_absent_registry_is_an_honest_empty(tmp_path: Path) -> None:
    out = mcp_servers_inventory(data_dir=tmp_path / "runtime")
    assert out["servers"] == [] and out["source"] is None
    assert any("no MCP server registry declared" in w for w in out["warnings"])


# --------------------------------------------------------------------- http


def test_inventory_routes_require_auth_and_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _write_shelf(tmp_path)
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(registry))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    (tmp_path / "flows").mkdir()

    from abstractgateway.app import app

    with TestClient(app) as anon:
        assert anon.get("/api/gateway/skills").status_code in (401, 403)
        assert anon.get("/api/gateway/mcp/servers").status_code in (401, 403)

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as client:
        skills = client.get("/api/gateway/skills")
        assert skills.status_code == 200, skills.text
        assert [r["name"] for r in skills.json()["skills"]] == ["demo-skill"]

        mcp = client.get("/api/gateway/mcp/servers")
        assert mcp.status_code == 200, mcp.text
        assert mcp.json()["servers"] == [] and mcp.json()["probed"] is False
