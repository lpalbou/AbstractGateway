"""Card 0087: run-level skills selection at POST /runs/start.

Flow ruled the transport (c2254: `input_data.skills` = names, run-level),
agent owns the terminus (`_runtime.skills_block` + the `read_skill`
progressive-disclosure tool). These pins cover the gateway half:

- resolution goes through abstractskill's trust gate (the SAME shelf and
  gate as /skills and the workforce spawn lane): validated skills activate,
  unverified are HELD, advisory-blocked NEVER ride — all as labeled
  verdicts, never silent;
- the resolved index lands in `_runtime.skills_block` at start (byte-stable
  per run) with bookkeeping in `_runtime.skills_resolution`;
- `read_skill` executes against the shelf with a TRUST RE-CHECK at read
  time and bounded output;
- a caller-set `_runtime.skills_block` is never overwritten (the input key
  is then ignored with a labeled verdict).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractskill")
pytest.importorskip("abstractruntime")

from abstractgateway.capability_inventories import (  # noqa: E402
    read_skill_body,
    resolve_run_skills,
)

_BODY = "# Demo\n\nStep one: read the docs. Step two: apply them carefully."


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ABSTRACTGATEWAY_SKILLS_SHELF", "ABSTRACT_TRIAGE_REPO_ROOT", "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"):
        monkeypatch.delenv(key, raising=False)


def _write_shelf(root: Path, *, validate: bool = True, block: bool = False) -> Path:
    """A curated shelf with one skill; optionally validated (=> active) or
    advisory-blocked. Trust binds to the tree hash, so the validation record
    is computed from the REAL bytes."""
    registry = root / "registry"
    skill_dir = registry / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo-skill\ndescription: A demo teaching for run-start tests.\n---\n\n{_BODY}\n",
        encoding="utf-8",
    )
    from abstractskill.tree import hash_skill_tree

    tree_hash = hash_skill_tree(skill_dir)

    validations = "validations: []\n"
    if validate:
        validations = (
            "validations:\n"
            "  - name: demo-skill\n"
            "    source: test-suite\n"
            f"    tree_hash: {tree_hash}\n"
            "    level: adopted\n"
            "    method: manual-review\n"
            "    validated_by: test\n"
            "    validated_at: '2026-07-15'\n"
        )
    advisories = "advisories: []\n"
    if block:
        advisories = (
            "advisories:\n"
            "  - name: demo-skill\n"
            "    source: test-suite\n"
            f"    tree_hash: {tree_hash}\n"
            "    official_intent: demo teaching\n"
            "    hidden_issue: exfiltrates workspace secrets in step two\n"
            "    severity: critical\n"
            "    reference: https://example.test/advisory\n"
        )
    (registry / "validations.yaml").write_text(validations, encoding="utf-8")
    (registry / "advisories.yaml").write_text(advisories, encoding="utf-8")
    (registry / "guidance.yaml").write_text("guidance: []\n", encoding="utf-8")
    return registry


# --------------------------------------------------------------- resolution


def test_validated_skill_resolves_into_a_skills_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    out = resolve_run_skills(["demo-skill", "demo-skill", ""], data_dir=tmp_path / "runtime")
    assert out["requested"] == ["demo-skill"]
    assert out["active"] == ["demo-skill"]
    assert "demo-skill" in (out["skills_block"] or "")
    assert out["resolved_tree_hashes"].get("demo-skill")


def test_unverified_skill_is_held_never_silently_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, validate=False)))
    out = resolve_run_skills(["demo-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == []
    assert out["skills_block"] is None
    assert any(v.startswith("held: demo-skill") for v in out["verdicts"])


def test_blocked_skill_never_rides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, block=True)))
    out = resolve_run_skills(["demo-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == [] and out["skills_block"] is None
    assert any(v.startswith("blocked: demo-skill") for v in out["verdicts"])


def test_missing_shelf_is_a_labeled_verdict_not_a_crash(tmp_path: Path) -> None:
    out = resolve_run_skills(["demo-skill"], data_dir=tmp_path / "runtime")
    assert out["active"] == []
    assert any("no curated shelf" in v for v in out["verdicts"])


# --------------------------------------------------------------- read_skill


def test_read_skill_serves_an_active_body_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    out = read_skill_body("demo-skill", data_dir=tmp_path / "runtime")
    assert out["success"] is True
    assert "Step one" in out["body"] and out["truncated"] is False

    capped = read_skill_body("demo-skill", data_dir=tmp_path / "runtime", max_chars=1000)
    assert capped["success"] is True  # body shorter than the floor cap stays whole
    assert capped["truncated"] is False


def test_read_skill_refuses_blocked_and_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, block=True)))
    blocked = read_skill_body("demo-skill", data_dir=tmp_path / "runtime")
    assert blocked["success"] is False and "blocked" in blocked["error"]

    unknown = read_skill_body("no-such-skill", data_dir=tmp_path / "runtime")
    assert unknown["success"] is False


# ------------------------------------------------------------ start_run seam


def _min_bundle_bytes() -> bytes:
    import io

    flow = {
        "id": "root",
        "name": "root",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0, "y": 0},
                "data": {"nodeType": "on_flow_start", "inputs": [], "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 10, "y": 0},
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "skills-seam",
        "bundle_version": "0.0.1",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {"root": "flows/root.json"},
        "metadata": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))
    return buf.getvalue()


def _host(tmp_path: Path):
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(exist_ok=True)
    (bundles_dir / "skills-seam@0.0.1.flow").write_bytes(_min_bundle_bytes())
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )


def test_start_run_resolves_input_skills_into_runtime_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    host = _host(tmp_path)
    run_id = host.start_run(flow_id="root", bundle_id="skills-seam", input_data={"skills": ["demo-skill"]})
    vars0 = host.runtime.get_state(run_id).vars
    rt = vars0["_runtime"]
    assert "demo-skill" in str(rt.get("skills_block") or "")
    resolution = rt.get("skills_resolution")
    assert resolution["requested"] == ["demo-skill"] and resolution["active"] == ["demo-skill"]


def test_start_run_records_verdicts_for_unresolvable_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path, validate=False)))
    host = _host(tmp_path)
    run_id = host.start_run(flow_id="root", bundle_id="skills-seam", input_data={"skills": ["demo-skill"]})
    rt = host.runtime.get_state(run_id).vars["_runtime"]
    assert "skills_block" not in rt  # held => nothing attaches
    assert any(v.startswith("held:") for v in rt["skills_resolution"]["verdicts"])


def test_start_run_never_overwrites_a_caller_skills_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    host = _host(tmp_path)
    run_id = host.start_run(
        flow_id="root",
        bundle_id="skills-seam",
        input_data={"skills": ["demo-skill"], "_runtime": {"skills_block": "caller-owned block"}},
    )
    rt = host.runtime.get_state(run_id).vars["_runtime"]
    assert rt["skills_block"] == "caller-owned block"
    assert any("ignored" in v for v in rt["skills_resolution"]["verdicts"])


def test_start_run_extends_a_caller_allowlist_with_read_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    host = _host(tmp_path)
    run_id = host.start_run(
        flow_id="root",
        bundle_id="skills-seam",
        input_data={"skills": ["demo-skill"], "_runtime": {"allowed_tools": ["read_file"]}},
    )
    rt = host.runtime.get_state(run_id).vars["_runtime"]
    assert rt["allowed_tools"] == ["read_file", "read_skill"]


# ----------------------------------------------- end-to-end (both halves)


def _agent_bundle_bytes() -> bytes:
    """start -> Agent node -> end: the operator-visible shape whose subrun
    the runtime passthrough (c2429) must reach."""
    import io

    flow = {
        "id": "root",
        "name": "root",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0, "y": 0},
                "data": {"nodeType": "on_flow_start", "inputs": [], "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "agent-1",
                "type": "agent",
                "position": {"x": 10, "y": 0},
                "data": {
                    "nodeType": "agent",
                    "inputs": [
                        {"id": "exec-in", "type": "execution"},
                        {"id": "task", "label": "task", "type": "string"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "type": "execution"},
                        {"id": "result", "label": "result", "type": "string"},
                    ],
                    "pinDefaults": {"task": "say hello"},
                    "agentConfig": {"provider": "lmstudio", "model": "dummy", "tools": ["read_file"]},
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 20, "y": 0},
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "agent-1", "targetHandle": "exec-in"},
            {"id": "e2", "source": "agent-1", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "skills-agent",
        "bundle_version": "0.0.1",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {"root": "flows/root.json"},
        "metadata": {},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))
    return buf.getvalue()


def test_skills_block_reaches_the_agent_node_child_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 0087 seam, BOTH halves through the real gateway host: gateway
    resolves `input_data.skills` into the root `_runtime.skills_block`
    (c2286 half) and the runtime compiler carries it into the Agent-node
    SUBRUN verbatim with `read_skill` appended to the explicit child
    allowlist (c2429 half). This is the end-to-end receipt observer's
    Launch section gates on."""
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(_write_shelf(tmp_path)))
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(exist_ok=True)
    (bundles_dir / "skills-agent@0.0.1.flow").write_bytes(_agent_bundle_bytes())
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )

    run_id = host.start_run(flow_id="root", bundle_id="skills-agent", input_data={"skills": ["demo-skill"]})
    parent_block = host.runtime.get_state(run_id).vars["_runtime"]["skills_block"]
    assert "demo-skill" in parent_block

    # Tick the parent until the Agent node spawns its subrun (the parent
    # parks WAITING(SUBWORKFLOW); the child needs no ticking — its vars are
    # written at spawn).
    runtime, wf = host.runtime_and_workflow_for_run(run_id)
    runtime.tick(workflow=wf, run_id=run_id, max_steps=20)

    children = host.run_store.list_children(parent_run_id=run_id)
    assert children, "the Agent node must have spawned a child run"
    child_rt = children[0].vars.get("_runtime")
    assert isinstance(child_rt, dict)
    assert child_rt.get("skills_block") == parent_block, "verbatim ride (prompt-cache contract)"
    tools = child_rt.get("allowed_tools")
    assert isinstance(tools, list)
    assert "read_skill" in tools, "the executor half must be reachable in the child"
    assert "read_file" in tools, "existing allowlist intact"
