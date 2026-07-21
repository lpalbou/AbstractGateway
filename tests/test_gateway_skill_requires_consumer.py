"""abstractskill-0008 consumer half (laurent ruled it active 2026-07-21):
skills declaring `metadata.requires_mcp` / `metadata.requires_tools` activate
ONLY when this gateway can serve them.

The contract (posted to skill at c3964):
- unmet requires => the skill DROPS from active with a labeled verdict naming
  the missing dependency + a structured `requires_unmet` map — never a silent
  activation, never a run block;
- the skills_block teaches only surviving skills;
- verdict wording is honest to the check substrate: the MCP registry is
  DECLARED-only (no probe lane), so the word is "not declared", never a
  fabricated "not reachable";
- a broken registry/tool universe SKIPS the check with a #FALLBACK verdict —
  degraded knowledge must never read as a missing dependency;
- the entity render half serves requires/requires_unmet on resolved rows and
  the phase-matrix cell goes `structurally_unavailable` with the dependency
  named in reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("abstractskill")

from abstractgateway.capability_inventories import resolve_run_skills

pytestmark = pytest.mark.basic

_BODY = "Teach the demo procedure.\n\n## Steps\n1. Do the thing carefully."


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ABSTRACTGATEWAY_SKILLS_SHELF", "ABSTRACT_TRIAGE_REPO_ROOT", "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"):
        monkeypatch.delenv(key, raising=False)


def _write_shelf(root: Path, *, requires_mcp: str = "", requires_tools: str = "") -> Path:
    """A curated shelf with one VALIDATED skill that declares dependencies."""
    registry = root / "registry"
    skill_dir = registry / "skills" / "needy-skill"
    skill_dir.mkdir(parents=True)
    meta_lines = ""
    if requires_mcp or requires_tools:
        meta_lines = "metadata:\n"
        if requires_mcp:
            meta_lines += f"  requires_mcp: [{requires_mcp}]\n"
        if requires_tools:
            meta_lines += f"  requires_tools: [{requires_tools}]\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: needy-skill\ndescription: A teaching that needs infrastructure.\n{meta_lines}---\n\n{_BODY}\n",
        encoding="utf-8",
    )
    from abstractskill.tree import hash_skill_tree

    tree_hash = hash_skill_tree(skill_dir)
    (registry / "validations.yaml").write_text(
        "validations:\n"
        "  - name: needy-skill\n"
        "    source: test-suite\n"
        f"    tree_hash: {tree_hash}\n"
        "    level: adopted\n"
        "    method: manual-review\n"
        "    validated_by: test\n"
        "    validated_at: '2026-07-21'\n",
        encoding="utf-8",
    )
    (registry / "advisories.yaml").write_text("advisories: []\n", encoding="utf-8")
    (registry / "guidance.yaml").write_text("guidance: []\n", encoding="utf-8")
    return registry


def _declare_mcp(data_dir: Path, *names: str) -> None:
    cfg = data_dir / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "mcp_servers.json").write_text(
        json.dumps({"version": 1, "servers": [{"name": n, "url": f"http://x/{n}"} for n in names]}),
        encoding="utf-8",
    )


def test_unmet_mcp_requirement_drops_with_reason_never_silently_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, requires_mcp="meshvault-mcp")))
    data_dir = tmp_path / "runtime"
    _declare_mcp(data_dir, "some-other-server")  # registry exists; dependency absent

    out = resolve_run_skills(["needy-skill"], data_dir=data_dir)
    assert out["active"] == [], "an unmet requires_mcp must drop the skill from active"
    assert out["skills_block"] is None, "the block must teach only surviving skills"
    unmet = out.get("requires_unmet") or {}
    assert unmet.get("needy-skill", {}).get("mcp_servers") == ["meshvault-mcp"]
    verdict = next((v for v in out["verdicts"] if v.startswith("requires_unmet: needy-skill")), "")
    assert "meshvault-mcp" in verdict
    # Honest wording: the registry is declared-only — no probe lane exists,
    # so "not reachable" would be a fabricated claim.
    assert "not declared" in verdict and "not reachable" not in verdict


def test_met_mcp_requirement_activates_and_serves_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, requires_mcp="meshvault-mcp")))
    data_dir = tmp_path / "runtime"
    _declare_mcp(data_dir, "meshvault-mcp")

    out = resolve_run_skills(["needy-skill"], data_dir=data_dir)
    assert out["active"] == ["needy-skill"]
    assert "needy-skill" in (out["skills_block"] or "")
    assert out.get("requires", {}).get("needy-skill", {}).get("mcp_servers") == ["meshvault-mcp"]
    assert "requires_unmet" not in out


def test_unmet_tool_requirement_drops_with_the_tool_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, requires_tools="no_such_tool_xyz"))
    )
    out = resolve_run_skills(["needy-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == []
    unmet = out.get("requires_unmet") or {}
    assert unmet.get("needy-skill", {}).get("tools") == ["no_such_tool_xyz"]
    verdict = next((v for v in out["verdicts"] if v.startswith("requires_unmet")), "")
    assert "no_such_tool_xyz" in verdict


def test_met_tool_requirement_activates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # read_file is a stable member of the run-lane default tool universe.
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, requires_tools="read_file")))
    out = resolve_run_skills(["needy-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == ["needy-skill"]


def test_skill_with_no_declaration_never_pays_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent metadata = no requires row = no check — the overwhelmingly
    common case stays byte-identical to the pre-0008 resolve."""
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    out = resolve_run_skills(["needy-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == ["needy-skill"]
    assert "requires" not in out and "requires_unmet" not in out


def test_entity_render_serves_requires_and_structurally_unavailable_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entity half: resolved rows carry requires/requires_unmet, and a
    selected-but-unmet skill's matrix cells serve structurally_unavailable
    with the missing dependency in reason (kit enum; the console renders
    blocked-with-reason from this, never a silent grant)."""
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, requires_mcp="meshvault-mcp")))
    data_dir = tmp_path / "runtime"
    # Registry exists but lacks the dependency.
    _declare_mcp(data_dir, "unrelated-server")

    from abstractgateway.entity_skills import resolve_entity_skills, write_skills_selection

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    write_skills_selection(home_dir, [{"name": "needy-skill"}])

    resolved = resolve_entity_skills(home_dir, data_dir=data_dir)
    rows = {r["name"]: r for r in resolved["resolved"]["skills"]}
    assert rows["needy-skill"]["active"] is False
    assert rows["needy-skill"]["requires"]["mcp_servers"] == ["meshvault-mcp"]
    assert rows["needy-skill"]["requires_unmet"]["mcp_servers"] == ["meshvault-mcp"]

    matrix = resolved["matrix"]
    items = {i["id"]: i for s in matrix["sections"] for i in s["items"]}
    cells = items["needy-skill"]["cells"]
    for phase, cell in cells.items():
        assert cell["availability"] == "structurally_unavailable", (phase, cell)
        assert "meshvault-mcp" in str(cell.get("reason") or "")
        assert cell["resolved_value"] is False
