"""The skills shelf setting (`skills.shelf`, CONTRACTS §X / A-7).

Resolution: stored > env (legacy) > <data dir>/skills/registry seeded from
the registry abstractskill ships > a framework checkout's registry (only when
the seeded copy is missing). A stored/env value that is not a registry is
unavailable with the reason, never a fall-through. Warnings are plain
sentences.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import skills_shelf


def _registry(path: Path, *names: str) -> Path:
    for n in names:
        (path / "skills" / n).mkdir(parents=True, exist_ok=True)
        (path / "skills" / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: {n} skill\n---\nbody\n")
    (path / "skills").mkdir(parents=True, exist_ok=True)
    return path


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(skills_shelf.ENV_NAME, raising=False)


def test_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    data = tmp_path / "data"
    checkout = tmp_path / "checkout"
    _registry(checkout / "abstractskill" / "src" / "abstractskill" / "registry", "a")
    r = skills_shelf.resolve_skills_shelf(data, checkout_root=checkout)
    assert r["source"] == "checkout" and r["registry"].name == "registry" and "src" in r["registry"].parts
    assert r["warnings"] and "has not been seeded" in r["warnings"][0]

    seeded = _registry(skills_shelf.seeded_registry_dir(data), "b")
    r = skills_shelf.resolve_skills_shelf(data, checkout_root=checkout)
    assert r["source"] == "seeded" and r["registry"] == seeded and not r["warnings"]

    env_shelf = _registry(tmp_path / "env-shelf", "c")
    r = skills_shelf.resolve_skills_shelf(data, env={skills_shelf.ENV_NAME: str(env_shelf)})
    assert r["source"] == "env" and r["registry"] == env_shelf.resolve()

    from abstractgateway.runtime_config import write_runtime_config

    saved = _registry(tmp_path / "saved-shelf", "d")
    write_runtime_config(data, {"skills.shelf": str(saved)}, actor="t")
    r = skills_shelf.resolve_skills_shelf(data, env={skills_shelf.ENV_NAME: str(env_shelf)})
    assert r["source"] == "stored" and r["registry"] == saved.resolve()


def test_a_wrong_saved_or_env_shelf_is_unavailable_not_skipped(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _registry(skills_shelf.seeded_registry_dir(data), "b")
    r = skills_shelf.resolve_skills_shelf(data, env={skills_shelf.ENV_NAME: str(tmp_path / "nope")})
    assert r["registry"] is None and r["source"] == "env" and "no folder at" in r["reason"]
    (data / "config").mkdir(parents=True, exist_ok=True)
    (data / "config" / "runtime_config.json").write_text(json.dumps({"skills": {"shelf": str(tmp_path)}}))
    r = skills_shelf.resolve_skills_shelf(data, env={})
    assert r["registry"] is None and r["source"] == "stored" and "has no skills/ folder" in r["reason"]


def test_write_validation(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, write_runtime_config

    data = tmp_path / "data"
    with pytest.raises(RuntimeConfigError, match="skills.shelf: no folder at"):
        write_runtime_config(data, {"skills.shelf": str(tmp_path / "missing")}, actor="t")
    with pytest.raises(RuntimeConfigError, match="has no skills/ folder"):
        write_runtime_config(data, {"skills": {"shelf": str(tmp_path)}}, actor="t")
    with pytest.raises(RuntimeConfigError, match="unknown setting"):
        write_runtime_config(data, {"skills.shelve": "x"}, actor="t")
    shelf = _registry(tmp_path / "s", "x")
    out = write_runtime_config(data, {"skills.shelf": str(shelf)}, actor="t")
    assert out["applied"] == {"skills.shelf": str(shelf.resolve())}
    assert out["skills"]["shelf"]["source"] == "stored" and out["skills"]["shelf"]["available"] is True
    out = write_runtime_config(data, {"skills.shelf": ""}, actor="t")
    assert out["applied"] == {"skills.shelf": None}


def test_seed_with_the_real_abstractskill(tmp_path: Path) -> None:
    """The installed abstractskill (a dependency) seeds the curated shelf."""
    from abstractskill.bundled import bundled_registry_version

    data = tmp_path / "data"
    out = skills_shelf.seed(data)
    assert out["ok"], out
    assert out["report"]["bundled_version"] == bundled_registry_version()
    assert (skills_shelf.seeded_registry_dir(data) / "skills" / "coredoc" / "SKILL.md").is_file()
    again = skills_shelf.seed(data)
    assert again["ok"] and again["report"]["changed"] is False and again["report"]["added"] == []


def test_an_old_abstractskill_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("abstractskill.bundled")
    monkeypatch.setitem(sys.modules, "abstractskill.bundled", fake)
    import abstractskill

    monkeypatch.setattr(abstractskill, "bundled", fake, raising=False)
    out = skills_shelf.seed(tmp_path / "data")
    assert out["ok"] is False and "0.3.0 or newer" in out["error"]
    assert skills_shelf.bundled_version() is None


def test_skills_route_and_reseed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_gateway_runs_list_endpoint import _write_min_bundle

    _clear_env(monkeypatch)
    bundles = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles, bundle_id="b", flow_id="root")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    h = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        # The boot seeded the gateway's own shelf.
        out = client.get("/api/gateway/skills", headers=h).json()
        assert out["shelf_source"] == "seeded", out
        assert out["shelf"].endswith("skills/registry/skills") and out["bundled_version"]
        assert {r["name"] for r in out["skills"]} >= {"coredoc", "backlog"}
        # Operator edit survives a reseed; the report says it was kept.
        skill_md = Path(out["shelf"]) / "coredoc" / "SKILL.md"
        skill_md.write_text(skill_md.read_text() + "\nlocal note\n")
        rep = client.post("/api/gateway/admin/skills/reseed", headers=h)
        assert rep.status_code == 200, rep.text
        assert "skills/coredoc" in rep.json()["kept_user_modified"]
        assert "local note" in skill_md.read_text()
        cfg = client.get("/api/gateway/admin/runtime-config", headers=h).json()
        assert cfg["skills"]["shelf"]["source"] == "seeded" and cfg["skills"]["shelf"]["available"] is True


def test_cli_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from abstractgateway import config_cli

    _clear_env(monkeypatch)
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    with pytest.raises(SystemExit) as refused:
        config_cli.main(["set", "skills.shelf", str(tmp_path / "missing"), "--data-dir", str(data)])
    assert refused.value.code == 2 and "no folder at" in capsys.readouterr().err
    shelf = _registry(tmp_path / "s", "x")
    with pytest.raises(SystemExit) as ok:
        config_cli.main(["set", "skills.shelf", str(shelf), "--data-dir", str(data)])
    assert ok.value.code == 0
    capsys.readouterr()
    config_cli.main(["get", "skills.shelf", "--data-dir", str(data), "--json"])
    row = json.loads(capsys.readouterr().out)
    assert row["source"] == "stored" and row["resolved"] == str(shelf.resolve())
    with pytest.raises(SystemExit):
        config_cli.main(["unset", "skills.shelf", "--data-dir", str(data)])
    capsys.readouterr()
    config_cli.main(["get", "skills.shelf", "--data-dir", str(data), "--json"])
    assert json.loads(capsys.readouterr().out)["source"] in {"seeded", "none", "checkout"}


def test_console_skills_shelf_block() -> None:
    from abstractgateway.console import gateway_console_html
    from test_gateway_console_offline import _console_script, _node, _slice_function

    assert 'id="skills-settings-root"' in gateway_console_html()
    source = _console_script()
    assert 'mountSkillsShelf("tab", $("skills-settings-root"))' in source
    harness = f"""
const HTML_ESCAPES = {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
{_slice_function(source, "uiPill")}
{_slice_function(source, "skillsShelfSourcePill")}
{_slice_function(source, "skillsShelfMarkup")}
{_slice_function(source, "skillsShelfCountText")}
{_slice_function(source, "seedReportText")}
const skillsShelfStore = {{ data: null, error: "", saving: false, saved: null, draft: null, views: new Map() }};
const out = [];
skillsShelfStore.data = {{ writable: true, skills: {{ shelf: {{ label: "Skills shelf", help: "h", source: "seeded", value: null,
  resolved: "/d/skills/registry", available: true, bundled_version: "2026.09.25", default_path: "/d/skills/registry" }} }} }};
out.push({{ k: "seeded", html: skillsShelfMarkup() }});
skillsShelfStore.data = {{ writable: false, skills: {{ shelf: {{ source: "stored", value: "/x", available: false,
  reason: "the saved skills.shelf is not usable: no folder at /x" }} }} }};
out.push({{ k: "bad", html: skillsShelfMarkup() }});
out.push({{ k: "rep", text: seedReportText({{ bundled_version: "v", added: ["a"], updated: [], unchanged: ["b", "c"], kept: {{ "skills/x": "kept_user_modified" }} }}) }});
console.log(JSON.stringify(out));
"""
    rows = {r["k"]: r for r in _node(harness)}
    assert "The gateway&#39;s own copy" in rows["seeded"]["html"] and "/d/skills/registry" in rows["seeded"]["html"]
    assert "data-skills-shelf-reseed" in rows["seeded"]["html"]
    assert "Not available: the saved skills.shelf is not usable" in rows["bad"]["html"]
    assert "data-skills-shelf-save" not in rows["bad"]["html"] and "Only an admin" in rows["bad"]["html"]
    assert rows["rep"]["text"].startswith("Curated shelf v: 1 added, 0 updated, 2 unchanged, 1 kept")
