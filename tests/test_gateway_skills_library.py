"""Skills page routes (DESIGN-v3 §6.1): view, export, import (zip / folder), edit imported only,
duplicate curated to edit, archive / unarchive (never delete), and the import guards (limits,
absolute paths, `..`, symlinks, SKILL.md required)."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic
pytest.importorskip("abstractskill")

ADMIN = {"Authorization": "Bearer admin-token"}
CURATED_MD = "---\nname: demo-skill\ndescription: A curated demo teaching.\n---\n\n# Demo\nBody.\n"
IMPORTED_MD = (
    "---\nname: field-notes\ndescription: Write field notes from a site visit.\nlicense: MIT\n"
    "metadata:\n  version: 1.4.0\n---\n\n# Field notes\n\nSteps.\r\nKeep CRLF bytes too.\n"
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = tmp_path / "registry"
    (registry / "skills" / "demo-skill").mkdir(parents=True)
    (registry / "skills" / "demo-skill" / "SKILL.md").write_text(CURATED_MD, encoding="utf-8")
    for f in ("validations.yaml", "advisories.yaml", "guidance.yaml"):
        (registry / f).write_text(f"{f.split('.')[0]}: []\n", encoding="utf-8")
    (registry / "catalog.yaml").write_text('version: "2026.09.25"\nskills: []\n', encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_SKILLS_SHELF", str(registry))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.routes import entities_router, gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(entities_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    with TestClient(app) as c:
        yield c


def _zip(entries: dict, *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def _import_zip(client: TestClient, blob: bytes, headers=ADMIN):
    return client.post("/api/gateway/admin/skills/import", headers=headers, files={"file": ("skill.zip", blob, "application/zip")})


def _rows(client: TestClient, archived: bool = False) -> dict:
    r = client.get("/api/gateway/skills" + ("?include_archived=1" if archived else ""), headers=ADMIN)
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in r.json()["skills"]}


def test_import_export_round_trip_is_byte_equal(client: TestClient) -> None:
    blob = _zip({"field-notes/SKILL.md": IMPORTED_MD.encode(), "field-notes/references/guide.md": b"# Guide\n"})
    r = _import_zip(client, blob)
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "imported" and r.json()["editable"] is True

    rows = _rows(client)
    assert rows["field-notes"]["source_label"] == "Imported"
    assert rows["field-notes"]["version"] == "1.4.0"
    assert rows["field-notes"]["trust_level"] == "unverified"
    assert rows["demo-skill"]["origin"] == "curated" and rows["demo-skill"]["version"] == "2026.09.25"

    exported = client.get("/api/gateway/skills/field-notes/export", headers=ADMIN)
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as zf:
        assert sorted(zf.namelist()) == ["field-notes/SKILL.md", "field-notes/references/guide.md"]
        assert zf.read("field-notes/SKILL.md") == IMPORTED_MD.encode()

    detail = client.get("/api/gateway/skills/field-notes", headers=ADMIN).json()
    assert detail["skill_md"] == IMPORTED_MD
    assert [f["path"] for f in detail["files"]] == ["SKILL.md", "references/guide.md"]


def test_folder_upload_imports_with_relative_paths(client: TestClient) -> None:
    r = client.post(
        "/api/gateway/admin/skills/import",
        headers=ADMIN,
        files=[("files", ("SKILL.md", IMPORTED_MD.encode())), ("files", ("guide.md", b"g"))],
        data={"paths": ["field-notes/SKILL.md", "field-notes/refs/guide.md"]},
    )
    assert r.status_code == 200, r.text
    assert [f["path"] for f in r.json()["files"]] == ["SKILL.md", "refs/guide.md"]


def test_imported_skill_is_seen_by_run_resolution(client: TestClient, tmp_path: Path) -> None:
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()})).status_code == 200
    from abstractgateway.capability_inventories import resolve_run_skills

    out = resolve_run_skills(["field-notes"], data_dir=tmp_path / "runtime")
    # Unverified => held by the trust gate, but FOUND (not "missing from shelf").
    assert not any("missing from shelf" in v for v in out["verdicts"]), out["verdicts"]
    assert any("field-notes" in v for v in out["verdicts"]), out["verdicts"]


def test_curated_is_view_only_and_duplicate_makes_it_editable(client: TestClient) -> None:
    detail = client.get("/api/gateway/skills/demo-skill", headers=ADMIN).json()
    assert detail["editable"] is False
    assert detail["read_only_reason"] == "Curated skills are read-only; duplicate to edit."

    r = client.put("/api/gateway/admin/skills/demo-skill", headers=ADMIN, json={"description": "Changed."})
    assert r.status_code == 403
    assert r.json()["detail"]["message"] == "Curated skills are read-only; duplicate to edit."

    dup = client.post("/api/gateway/admin/skills/demo-skill/duplicate", headers=ADMIN, json={"name": "demo-mine"})
    assert dup.status_code == 200, dup.text
    assert dup.json()["editable"] is True and "name: demo-mine" in dup.json()["skill_md"]

    saved = client.put(
        "/api/gateway/admin/skills/demo-mine", headers=ADMIN,
        json={"skill_md": dup.json()["skill_md"].replace("Body.", "Better body."), "description": "My demo.", "version": "2.0.0"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert "Better body." in body["skill_md"]
    assert body["frontmatter"]["description"] == "My demo."
    assert body["version"] == "2.0.0"
    # The curated original is untouched.
    assert client.get("/api/gateway/skills/demo-skill", headers=ADMIN).json()["skill_md"] == CURATED_MD


def test_edit_refuses_a_rename_and_an_invalid_skill_md(client: TestClient) -> None:
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()})).status_code == 200
    r = client.put("/api/gateway/admin/skills/field-notes", headers=ADMIN, json={"name": "other"})
    assert r.status_code == 400 and "cannot change" in r.json()["detail"]["message"]
    r = client.put("/api/gateway/admin/skills/field-notes", headers=ADMIN, json={"skill_md": "no frontmatter"})
    assert r.status_code == 400 and "not a valid skill" in r.json()["detail"]["message"]


def test_archive_hides_and_unarchive_restores_never_deletes(client: TestClient, tmp_path: Path) -> None:
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()})).status_code == 200
    r = client.post("/api/gateway/admin/skills/field-notes/archive", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert "field-notes" not in _rows(client)
    archived = _rows(client, archived=True)["field-notes"]
    assert archived["archived"] is True
    assert (tmp_path / "runtime" / "skills" / "archived" / "field-notes" / "SKILL.md").read_bytes() == IMPORTED_MD.encode()
    # Archived is read-only and not editable.
    assert client.put("/api/gateway/admin/skills/field-notes", headers=ADMIN, json={"description": "x"}).status_code == 409
    # Curated skills cannot be archived.
    assert client.post("/api/gateway/admin/skills/demo-skill/archive", headers=ADMIN).status_code == 403
    r = client.post("/api/gateway/admin/skills/field-notes/unarchive", headers=ADMIN)
    assert r.status_code == 200 and "field-notes" in _rows(client)
    # There is no delete route.
    assert client.delete("/api/gateway/admin/skills/field-notes", headers=ADMIN).status_code in (404, 405)


def test_import_refuses_name_collisions(client: TestClient) -> None:
    r = _import_zip(client, _zip({"SKILL.md": CURATED_MD.encode()}))
    assert r.status_code == 409 and "curated skill already has this name" in r.json()["detail"]["message"]
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()})).status_code == 200
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()})).status_code == 409


@pytest.mark.parametrize(
    "entries,symlink,needle",
    [
        ({"field-notes/SKILL.md": IMPORTED_MD, "field-notes/../../evil.txt": "x"}, None, "leaves the skill folder"),
        ({"SKILL.md": IMPORTED_MD, "/etc/evil": "x"}, None, "absolute path"),
        ({"SKILL.md": IMPORTED_MD}, "link", "symlink"),
        ({"README.md": "no skill here"}, None, "no SKILL.md"),
        ({"SKILL.md": "---\nname: Bad Name\ndescription: x\n---\n"}, None, "not a valid skill"),
    ],
)
def test_import_guards(client: TestClient, tmp_path: Path, entries, symlink, needle) -> None:
    r = _import_zip(client, _zip({k: v.encode() for k, v in entries.items()}, symlink=symlink))
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]["message"]
    imported = tmp_path / "runtime" / "skills" / "imported"
    assert not imported.exists() or not any(imported.iterdir())
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "runtime" / "evil.txt").exists()


def test_import_limits(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.skills_library as lib

    monkeypatch.setattr(lib, "MAX_IMPORT_FILES", 3)
    many = {"SKILL.md": IMPORTED_MD, **{f"r/{i}.md": "x" for i in range(5)}}
    r = _import_zip(client, _zip({k: v.encode() for k, v in many.items()}))
    assert r.status_code == 400 and "at most 3" in r.json()["detail"]["message"]
    monkeypatch.setattr(lib, "MAX_IMPORT_FILES", 500)
    monkeypatch.setattr(lib, "MAX_IMPORT_BYTES", 1000)
    r = _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode(), "big.bin": b"0" * 5000}))
    assert r.status_code == 400 and "at most 1000 bytes" in r.json()["detail"]["message"], r.text


def test_non_admin_cannot_write_but_can_read(client: TestClient) -> None:
    created = client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
    assert created.status_code == 200, created.text
    user = {"Authorization": f"Bearer {created.json()['token']}"}
    assert _import_zip(client, _zip({"SKILL.md": IMPORTED_MD.encode()}), headers=user).status_code == 403
    assert client.put("/api/gateway/admin/skills/demo-skill", headers=user, json={}).status_code == 403
    assert client.post("/api/gateway/admin/skills/demo-skill/duplicate", headers=user).status_code == 403
    assert client.get("/api/gateway/skills/demo-skill", headers=user).status_code == 200
