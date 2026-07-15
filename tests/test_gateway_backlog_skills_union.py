"""Skills-union spawn wiring (operator confirmation 2026-07-13 23:09 via
continuum c1731; slot named c1749; "default-REQUESTED, never trust-bypassed"
per skill c1733).

Pins: every framework backlog execution payload records skills.requested =
[coredoc, backlog] ∪ member skills with resolved_tree_hashes + verbatim
verdicts (resolution at PAYLOAD BUILD — the durable request record);
degradations are labeled verdicts, never blocked runs; spawn env carries the
shelf only for ACTIVE skills (trust-gated, never bypassed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _make_app(*, monkeypatch: pytest.MonkeyPatch, gateway_base_dir: Path) -> FastAPI:
    import abstractgateway.routes.gateway as gateway_routes

    class _Stores:
        def __init__(self, base_dir: Path) -> None:
            self.base_dir = base_dir

    class _Service:
        def __init__(self, base_dir: Path) -> None:
            self.stores = _Stores(base_dir)

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(gateway_base_dir))
    monkeypatch.setattr(gateway_routes, "backlog_exec_runner_status", lambda: {"alive": True, "error": None})
    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def test_execute_payload_records_the_skills_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("abstractskill")
    gateway_dir = tmp_path / "gateway"
    (gateway_dir / "backlog_exec_queue").mkdir(parents=True, exist_ok=True)
    # The FRAMEWORK repo root: the real curated shelf rides the checkout.
    framework_root = Path(__file__).resolve().parents[2]
    item_dir = tmp_path / "repo" / "docs" / "backlog" / "planned"
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "730-framework-skills.md").write_text("# 730 — skills union\n\nBody.\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "1")
    # Point the shelf at the real registry (the repo-root inference would
    # miss it because the triage root here is the test's scratch repo).
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(framework_root / "abstractskill" / "registry"))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        r = client.post("/api/gateway/backlog/planned/730-framework-skills.md/execute")
        assert r.status_code == 200, r.text
        qfiles = list((gateway_dir / "backlog_exec_queue").glob("*.json"))
        assert len(qfiles) == 1
        payload = json.loads(qfiles[0].read_text(encoding="utf-8"))
        skills = payload["skills"]
        assert skills["requested"][:2] == ["coredoc", "backlog"]
        assert skills["source"] == "item-class-defaults∪member"
        # The curated shelf resolves both defaults: active + hash-pinned, or
        # (if the local registry holds no validation record) verbatim held
        # verdicts — never silence either way.
        resolved = set(skills["active"])
        if resolved:
            assert resolved <= set(skills["requested"])
            for name in resolved:
                assert skills["resolved_tree_hashes"].get(name), "active skills carry tree hashes"
        else:
            assert skills["verdicts"], "no active skills must come with verbatim verdicts"


def test_missing_shelf_is_a_labeled_verdict_never_a_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    (gateway_dir / "backlog_exec_queue").mkdir(parents=True, exist_ok=True)
    item_dir = tmp_path / "repo" / "docs" / "backlog" / "planned"
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "731-framework-noshelf.md").write_text("# 731\n\nBody.\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(tmp_path / "nowhere"))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        r = client.post("/api/gateway/backlog/planned/731-framework-noshelf.md/execute")
        assert r.status_code == 200, r.text  # a missing teaching never blocks the run
        payload = json.loads(next((gateway_dir / "backlog_exec_queue").glob("*.json")).read_text(encoding="utf-8"))
        skills = payload["skills"]
        assert skills["requested"][:2] == ["coredoc", "backlog"]
        assert skills["active"] == []
        assert any("#FALLBACK" in v for v in skills["verdicts"])


def test_spawn_env_carries_shelf_plus_trust_registry_for_active_skills_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skill co-review c1783: ROOTS alone = the executor discovers the shelf
    but evaluates against an EMPTY trust registry (everything held-as-
    unverified) — the registry halves must ride beside it."""
    from abstractgateway.skills_union import spawn_env_for_skills

    registry_dir = tmp_path / "registry"
    (registry_dir / "skills").mkdir(parents=True)
    (registry_dir / "validations.yaml").write_text("records: []\n", encoding="utf-8")
    (registry_dir / "advisories.yaml").write_text("advisories: []\n", encoding="utf-8")
    shelf = str(registry_dir / "skills")

    monkeypatch.delenv("ABSTRACTCODE_SKILLS_ROOTS", raising=False)
    env = spawn_env_for_skills({"shelf": shelf, "active": ["coredoc"]})
    assert env["ABSTRACTCODE_SKILLS_ROOTS"] == shelf
    assert env["ABSTRACTCODE_SKILLS_VALIDATIONS"] == str(registry_dir / "validations.yaml")
    assert env["ABSTRACTCODE_SKILLS_ADVISORIES"] == str(registry_dir / "advisories.yaml")

    # Nothing active (all held/blocked) -> NO env: trust gate never bypassed.
    assert spawn_env_for_skills({"shelf": shelf, "active": []}) == {}
    assert spawn_env_for_skills(None) == {}

    # Existing roots are appended to, never clobbered.
    monkeypatch.setenv("ABSTRACTCODE_SKILLS_ROOTS", "/user/skills")
    env2 = spawn_env_for_skills({"shelf": shelf, "active": ["backlog"]})
    assert env2["ABSTRACTCODE_SKILLS_ROOTS"] == f"/user/skills:{shelf}"


def test_held_verdicts_unpack_name_and_reasons_never_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """held/blocked are (name, TrustVerdict) PAIRS — the payload must carry
    'name — reasons', never a Python repr (skill c1783 gap 1). Forced by
    pointing the resolver at a shelf with NO validation records: every
    skill lands held-as-unverified."""
    pytest.importorskip("abstractskill")
    from abstractgateway.skills_union import resolve_backlog_skills

    framework_root = Path(__file__).resolve().parents[2]
    real_registry = framework_root / "abstractskill" / "registry"
    if not real_registry.is_dir():
        pytest.skip("curated shelf not present in this checkout")
    # Shelf = the real skills dir, but an EMPTY (present, record-less)
    # registry: every skill lands held-as-unverified -> the pair-unpacking
    # path runs for real. (A MISSING registry file refuses loudly at load —
    # different, also-correct behavior covered by the missing-shelf test.)
    bare = tmp_path / "registry"
    (bare / "skills").mkdir(parents=True)
    import shutil

    for skill_name in ("coredoc", "backlog"):
        src = real_registry / "skills" / skill_name
        if src.is_dir():
            shutil.copytree(src, bare / "skills" / skill_name)
    (bare / "validations.yaml").write_text("validations: []\n", encoding="utf-8")
    (bare / "advisories.yaml").write_text("advisories: []\n", encoding="utf-8")
    (bare / "guidance.yaml").write_text("guidance: []\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(bare))

    out = resolve_backlog_skills()
    assert out["active"] == []
    held = [v for v in out["verdicts"] if v.startswith("held: ")]
    assert held, out["verdicts"]
    for v in held:
        assert "TrustVerdict(" not in v, f"repr leaked into the payload: {v}"
