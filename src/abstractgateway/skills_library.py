"""Imported skills: the gateway's writable shelf next to the curated one (DESIGN-v3 §6.1).

Where things live (gateway-wide, the root data folder like the seeded shelf):

    <data_dir>/skills/registry/skills/<name>/   the curated shelf (skills_shelf.py; read-only here)
    <data_dir>/skills/imported/<name>/          skills an admin imported, duplicated or edited
    <data_dir>/skills/archived/<name>/          imported skills an admin archived (kept, never
                                                deleted; Unarchive moves them back)

Every shelf reader (the /skills inventory, run-start resolution, read_skill, the backlog
skills union) reads the curated shelf AND the imported folder through `shelf_roots()`, in that
order (abstractskill's later-root-wins precedence). An import never takes a name the curated
shelf, the imported folder or the archive already holds, so an imported skill never shadows a
curated one. Trust stays abstractskill's: an imported copy matches no validation record unless
it is byte-identical to a curated skill, so it is Unverified.

Curated skills are view-only: "Duplicate to edit" copies one into the imported folder under a
new name. Imports are checked before anything lands: size and file-count limits, no absolute
paths, no `..`, no symlinks, a SKILL.md that abstractskill parses, written to a temporary folder
and renamed into place.
"""
from __future__ import annotations

import io
import shutil
import stat
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_IMPORT_BYTES = 10 * 1024 * 1024  # uncompressed total
MAX_IMPORT_FILES = 500
MAX_FILE_BYTES = 5 * 1024 * 1024
SKILL_FILENAME = "SKILL.md"
_IGNORED_TOP = {"__MACOSX"}
_IGNORED_NAMES = {".DS_Store", "Thumbs.db"}

_lock = threading.Lock()


class SkillLibraryError(Exception):
    """A refused skills action; `status` is the HTTP status, the message a sentence."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _data_dir(data_dir: Optional[Path]) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    from .users import gateway_data_dir_from_env

    return Path(gateway_data_dir_from_env()).expanduser().resolve()


def imported_root(data_dir: Optional[Path] = None) -> Path:
    return _data_dir(data_dir) / "skills" / "imported"


def archived_root(data_dir: Optional[Path] = None) -> Path:
    return _data_dir(data_dir) / "skills" / "archived"


def shelf_roots(skills_root: Path, data_dir: Optional[Path] = None) -> List[Path]:
    """The roots every shelf reader passes to abstractskill: curated first, imported last."""
    roots = [Path(skills_root)]
    imp = imported_root(data_dir)
    if imp.is_dir() and imp.resolve() != Path(skills_root).resolve():
        roots.append(imp)
    return roots


def _curated_skills_root() -> Optional[Path]:
    from .capability_inventories import _repo_root_for_shelf
    from .skills_union import _shelf_registry_dir

    registry_dir, _why = _shelf_registry_dir(_repo_root_for_shelf(_data_dir(None)))
    return (registry_dir / "skills") if registry_dir is not None else None


def _validate_name(name: str) -> str:
    from abstractskill.errors import SkillValidationError
    from abstractskill.validation import validate_skill_name

    try:
        return validate_skill_name(str(name or ""))
    except SkillValidationError as exc:
        raise SkillLibraryError(f"{name!r} is not a valid skill name: {exc}.") from None


def locate(name: str) -> Tuple[str, Path]:
    """(origin, folder) of a skill: imported > curated > archived. 404 when none."""
    clean = _validate_name(name)
    imp = imported_root() / clean
    if (imp / SKILL_FILENAME).is_file():
        return "imported", imp
    curated = _curated_skills_root()
    if curated is not None and (curated / clean / SKILL_FILENAME).is_file():
        return "curated", curated / clean
    arch = archived_root() / clean
    if (arch / SKILL_FILENAME).is_file():
        return "archived", arch
    raise SkillLibraryError(f"There is no skill named {clean!r} on this gateway.", status=404)


def _taken(name: str) -> Optional[str]:
    if (imported_root() / name).exists():
        return "an imported skill already has this name"
    curated = _curated_skills_root()
    if curated is not None and (curated / name).exists():
        return "a curated skill already has this name"
    if (archived_root() / name).exists():
        return "an archived skill has this name (unarchive it, or pick another name)"
    return None


def _files_of(folder: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name in _IGNORED_NAMES:
            continue
        out.append({"path": path.relative_to(folder).as_posix(), "size": path.stat().st_size})
    return out


def frontmatter_of(text: str) -> Dict[str, Any]:
    from abstractskill.parser import _split_frontmatter

    fm, _body = _split_frontmatter(text)
    return dict(fm)


def skill_detail(name: str) -> Dict[str, Any]:
    origin, folder = locate(name)
    text = (folder / SKILL_FILENAME).read_bytes().decode("utf-8", errors="replace")
    try:
        fm = frontmatter_of(text)
    except Exception as exc:  # noqa: BLE001 - shown, never a 500
        fm = {}
        problem = f"The SKILL.md frontmatter could not be read: {exc}."
    else:
        problem = None
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    out: Dict[str, Any] = {
        "name": folder.name,
        "origin": origin,
        "archived": origin == "archived",
        "editable": origin == "imported",
        "read_only_reason": None if origin == "imported" else (
            "Curated skills are read-only; duplicate to edit." if origin == "curated"
            else "Archived skills are read-only; unarchive to edit."
        ),
        "skill_md": text,
        "frontmatter": fm,
        "version": str(meta.get("version")) if meta.get("version") is not None else None,
        "files": _files_of(folder),
    }
    if problem:
        out["problem"] = problem
    return out


def export_zip(name: str) -> Tuple[str, bytes]:
    """(file name, zip bytes): every file of the skill under `<name>/`."""
    _origin, folder = locate(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in _files_of(folder):
            src = folder / row["path"]
            info = zipfile.ZipInfo(f"{folder.name}/{row['path']}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(info, src.read_bytes())
    return f"{folder.name}.zip", buf.getvalue()


# ---------------------------------------------------------------------------- import


def _clean_relpath(raw: str) -> Optional[PurePosixPath]:
    """A safe relative path, None for OS junk to skip; refuses absolute paths and `..`."""
    text = str(raw or "").replace("\\", "/")
    if not text or text.endswith("/"):
        return None
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise SkillLibraryError(f"The upload contains an absolute path ({raw}); skills hold relative paths only.")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise SkillLibraryError(f"The upload contains a path that leaves the skill folder ({raw}).")
    if not parts:
        return None
    if parts[0] in _IGNORED_TOP or parts[-1] in _IGNORED_NAMES:
        return None
    return PurePosixPath(*parts)


def files_from_zip(data: bytes) -> List[Tuple[str, bytes]]:
    """The (relative path, bytes) entries of a zip upload, checked for limits and symlinks."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillLibraryError("The upload is not a zip file.") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_IMPORT_FILES:
            raise SkillLibraryError(f"The zip holds {len(infos)} files; a skill may hold at most {MAX_IMPORT_FILES}.")
        declared = sum(i.file_size for i in infos)
        if declared > MAX_IMPORT_BYTES:
            raise SkillLibraryError(f"The zip expands to {declared} bytes; a skill may hold at most {MAX_IMPORT_BYTES} bytes.")
        out: List[Tuple[str, bytes]] = []
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise SkillLibraryError(f"The zip contains a symlink ({info.filename}); symlinks are not allowed in skills.")
            if info.file_size > MAX_FILE_BYTES:
                raise SkillLibraryError(f"{info.filename} is larger than {MAX_FILE_BYTES} bytes.")
            with zf.open(info) as fh:
                blob = fh.read(MAX_FILE_BYTES + 1)
            if len(blob) > MAX_FILE_BYTES:
                raise SkillLibraryError(f"{info.filename} is larger than {MAX_FILE_BYTES} bytes.")
            total += len(blob)
            if total > MAX_IMPORT_BYTES:
                raise SkillLibraryError(f"The zip expands past {MAX_IMPORT_BYTES} bytes; a skill may hold at most that.")
            out.append((info.filename, blob))
        return out


def _skill_tree(files: Iterable[Tuple[str, bytes]]) -> Dict[PurePosixPath, bytes]:
    """Relative path -> bytes, rooted at the folder that holds SKILL.md."""
    tree: Dict[PurePosixPath, bytes] = {}
    total = 0
    for raw, blob in files:
        rel = _clean_relpath(raw)
        if rel is None:
            continue
        if rel in tree:
            raise SkillLibraryError(f"The upload contains {rel} twice.")
        total += len(blob)
        if len(blob) > MAX_FILE_BYTES:
            raise SkillLibraryError(f"{rel} is larger than {MAX_FILE_BYTES} bytes.")
        tree[rel] = blob
    if not tree:
        raise SkillLibraryError("The upload is empty.")
    if len(tree) > MAX_IMPORT_FILES:
        raise SkillLibraryError(f"The upload holds {len(tree)} files; a skill may hold at most {MAX_IMPORT_FILES}.")
    if total > MAX_IMPORT_BYTES:
        raise SkillLibraryError(f"The upload holds {total} bytes; a skill may hold at most {MAX_IMPORT_BYTES} bytes.")
    if PurePosixPath(SKILL_FILENAME) in tree:
        return tree
    tops = {p.parts[0] for p in tree}
    if len(tops) == 1:
        top = next(iter(tops))
        if PurePosixPath(top, SKILL_FILENAME) in tree:
            return {PurePosixPath(*p.parts[1:]): b for p, b in tree.items() if len(p.parts) > 1}
    raise SkillLibraryError(
        "The upload has no SKILL.md at its top (a skill is a folder with SKILL.md; zip that folder, or upload it)."
    )


def import_skill(files: Iterable[Tuple[str, bytes]]) -> Dict[str, Any]:
    """Land an uploaded skill in the imported folder; returns its detail."""
    from abstractskill.errors import SkillParseError, SkillValidationError
    from abstractskill.parser import parse_skill_md

    tree = _skill_tree(files)
    try:
        text = tree[PurePosixPath(SKILL_FILENAME)].decode("utf-8")
    except UnicodeDecodeError:
        raise SkillLibraryError("SKILL.md is not UTF-8 text.") from None
    try:
        doc = parse_skill_md(text)
    except (SkillParseError, SkillValidationError) as exc:
        raise SkillLibraryError(f"SKILL.md is not a valid skill: {exc}.") from None
    name = _validate_name(doc.metadata.name)
    _write_tree(name, tree)
    return skill_detail(name)


def _write_tree(name: str, tree: Dict[PurePosixPath, bytes]) -> None:
    root = imported_root()
    with _lock:
        taken = _taken(name)
        if taken:
            raise SkillLibraryError(f"The skill {name!r} cannot be added: {taken}.", status=409)
        root.mkdir(parents=True, exist_ok=True)
        # Staged OUTSIDE the imported folder so a concurrent shelf scan never sees a half copy.
        staging_parent = root.parent / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(staging_parent)))
        try:
            for rel, blob in tree.items():
                dest = staging.joinpath(*rel.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
            staging.rename(root / name)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise


# ------------------------------------------------------------------------ edit/copy


def _imported_folder(name: str) -> Path:
    origin, folder = locate(name)
    if origin == "curated":
        raise SkillLibraryError("Curated skills are read-only; duplicate to edit.", status=403)
    if origin == "archived":
        raise SkillLibraryError("Archived skills are read-only; unarchive to edit.", status=409)
    return folder


def _with_fields(text: str, fields: Dict[str, Optional[str]]) -> str:
    """SKILL.md text with frontmatter fields set (only rewritten when a value changes)."""
    import yaml

    fm = frontmatter_of(text)
    changed = False
    for key in ("name", "description", "license"):
        if key in fields and fields[key] is not None:
            value = str(fields[key]).strip()
            current = fm.get(key)
            if value == "" and key == "license":
                if "license" in fm:
                    fm.pop("license")
                    changed = True
            elif value != (str(current).strip() if current is not None else None):
                fm[key] = value
                changed = True
    if "version" in fields and fields["version"] is not None:
        value = str(fields["version"]).strip()
        meta = dict(fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}
        current = meta.get("version")
        if value == "" and "version" in meta:
            meta.pop("version")
            changed = True
        elif value and value != (str(current) if current is not None else None):
            meta["version"] = value
            changed = True
        if changed:
            if meta:
                fm["metadata"] = meta
            else:
                fm.pop("metadata", None)
    if not changed:
        return text
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    closing = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    body = "\n".join(lines[closing + 1:])
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000).rstrip("\n")
    return f"---\n{dumped}\n---\n{body}"


def update_skill(name: str, *, skill_md: Optional[str], fields: Dict[str, Optional[str]]) -> Dict[str, Any]:
    from abstractskill.errors import SkillParseError, SkillValidationError
    from abstractskill.parser import parse_skill_md

    folder = _imported_folder(name)
    path = folder / SKILL_FILENAME
    text = skill_md if skill_md is not None else path.read_bytes().decode("utf-8")
    if "name" in fields and fields["name"] is not None and str(fields["name"]).strip() != folder.name:
        raise SkillLibraryError("A skill's name cannot change; duplicate it under the new name instead.")
    try:
        text = _with_fields(text, {k: v for k, v in fields.items() if k != "name"})
        parse_skill_md(text, directory_name=folder.name)
    except (SkillParseError, SkillValidationError, StopIteration) as exc:
        raise SkillLibraryError(f"SKILL.md is not a valid skill: {exc}.") from None
    with _lock:
        tmp = path.with_name(f".{SKILL_FILENAME}.tmp")
        tmp.write_bytes(text.encode("utf-8"))
        tmp.replace(path)
    return skill_detail(folder.name)


def duplicate_skill(name: str, new_name: Optional[str] = None) -> Dict[str, Any]:
    _origin, folder = locate(name)
    target = _validate_name(new_name or f"{folder.name}-copy")
    tree: Dict[PurePosixPath, bytes] = {}
    for row in _files_of(folder):
        tree[PurePosixPath(row["path"])] = (folder / row["path"]).read_bytes()
    text = tree[PurePosixPath(SKILL_FILENAME)].decode("utf-8")
    tree[PurePosixPath(SKILL_FILENAME)] = _with_fields(text, {"name": target}).encode("utf-8")
    _write_tree(target, tree)
    return skill_detail(target)


def archive_skill(name: str) -> Dict[str, Any]:
    folder = _imported_folder(name)
    dest = archived_root() / folder.name
    with _lock:
        if dest.exists():
            raise SkillLibraryError(f"An archived skill named {folder.name!r} already exists.", status=409)
        dest.parent.mkdir(parents=True, exist_ok=True)
        folder.rename(dest)
    return skill_detail(folder.name)


def unarchive_skill(name: str) -> Dict[str, Any]:
    clean = _validate_name(name)
    src = archived_root() / clean
    if not (src / SKILL_FILENAME).is_file():
        raise SkillLibraryError(f"There is no archived skill named {clean!r}.", status=404)
    with _lock:
        if (imported_root() / clean).exists():
            raise SkillLibraryError(f"An imported skill named {clean!r} already exists.", status=409)
        curated = _curated_skills_root()
        if curated is not None and (curated / clean).exists():
            raise SkillLibraryError(f"A curated skill named {clean!r} now exists; duplicate the archived one under another name.", status=409)
        imported_root().mkdir(parents=True, exist_ok=True)
        src.rename(imported_root() / clean)
    return skill_detail(clean)


def archived_rows() -> List[Dict[str, Any]]:
    """Rows for archived skills (name, description, version) — shown with "Show archived"."""
    from abstractskill import FilesystemSkillLoader

    out: List[Dict[str, Any]] = []
    root = archived_root()
    if not root.is_dir():
        return out
    for meta in FilesystemSkillLoader(root).discover():
        md = dict(meta.metadata or {})
        out.append({
            "name": meta.name,
            "description": meta.description,
            "version": str(md["version"]) if md.get("version") is not None else None,
            "origin": "archived",
            "source_label": "Imported (archived)",
            "archived": True,
            "editable": False,
        })
    return out
