"""Versioned operator spark templates (operator directive 2026-07-13: "if
it's a template, i must be able to create new ones and modify them later on
- every template should be versioned").

A TEMPLATE is a reusable spark BLUEPRINT the operator edits freely before
summoning. It is NOT an entity's engraved spark: the create path COPIES the
template's spark and engrams it at summon (v1-for-life per entity), so
editing a template NEVER touches any living entity — a template is a
starting document, never a live link. This module owns only the blueprint
lifecycle.

VERSIONING (append-only, the framework's discipline): every save writes a
NEW version file; nothing is overwritten. `meta.json` records the current
version pointer + provenance; `vNNN.yaml` files are the immutable history.
A template's identity is its id (the directory name); its content evolves
by version, current-wins, history intact.

THE BUILTIN IS THE FLOOR: the framework-default spark comes from code
(abstractmemory.DEFAULT_SPARK_TEMPLATE), not a file — it cannot be edited or
deleted. "Editing the builtin" means CREATING an operator template seeded
from it; the floor stays.

VALIDATION: a template must lint as a valid spark (abstractmemory.lint_spark)
at SAVE time — an operator must never discover a broken template only when an
entity summoned from it refuses. Core values (class=core, e.g.
shared_vulnerability) are the framework floor; lint refuses their removal, so
an edit that strips one is rejected at save, loudly.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Reserved ids: the builtin's id (never shadowable) + the legacy flat-file
# stem the read path still honors. Keep in sync with the builtin below.
BUILTIN_ID = "framework-default"


class TemplateError(ValueError):
    """A rejected template operation — the route maps it to a 4xx."""


def _templates_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "entity_templates"


def _template_home(data_dir: Path, template_id: str) -> Path:
    return _templates_dir(data_dir) / template_id


def _core_values(doc: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for v in (doc.get("values") or []):
        if isinstance(v, dict) and str(v.get("class") or "") == "core":
            nm = str(v.get("name") or "").strip()
            if nm:
                out.append(nm)
    return out


def _builtin_template() -> Dict[str, Any]:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    builtin = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    builtin["name"] = ""  # the operator fills the name at summon
    return {
        "id": BUILTIN_ID,
        "name": "Framework default",
        "description": (
            "The AbstractFramework default spark — the shared-vulnerability core "
            "value + intellectual honesty, ready to name and summon."
        ),
        "source": "builtin",
        "editable": False,  # the floor: seed a new template from it instead
        "version": 0,
        "spark": builtin,
        "core_values": _core_values(builtin),
    }


def _lint_spark(spark: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Lint the spark, returning (errors, warnings). A template must be a
    valid spark at save time; a blank name is filled at summon, so lint
    against a named copy so a blank template name is not itself flagged.

    lint_spark returns a FLAT list of strings prefixed 'ERROR'/'WARNING'
    (verified against the engine): only ERROR lines block a save (a core
    value strip is an ERROR); WARNING lines (e.g. a non-behavioral purpose
    statement) pass through and are surfaced, never blocking — a template is
    a draft the operator refines, not a summon-time gate."""
    try:
        from abstractmemory import lint_spark
    except Exception:  # pragma: no cover - abstractmemory is a hard dep
        return [], []
    probe = copy.deepcopy(spark)
    if not str(probe.get("name") or "").strip():
        probe["name"] = "TemplateProbe"
    try:
        result = lint_spark(probe)
    except Exception as e:  # noqa: BLE001
        return [f"spark lint raised: {e}"], []
    lines = [str(x) for x in result] if isinstance(result, (list, tuple)) else []
    errors = [ln for ln in lines if ln.strip().upper().startswith("ERROR")]
    warnings = [ln for ln in lines if not ln.strip().upper().startswith("ERROR")]
    return errors, warnings


def _read_meta(home: Path) -> Dict[str, Any]:
    meta_path = home / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _version_path(home: Path, version: int) -> Path:
    return home / f"v{int(version):03d}.yaml"


def _read_version_spark(home: Path, version: int) -> Dict[str, Any]:
    import yaml

    path = _version_path(home, version)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise TemplateError(f"version {version} of the template did not parse to a mapping")
    return dict(doc)


def list_templates(data_dir: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The gallery: the builtin floor + every operator template at its
    CURRENT version. Read-only; an unreadable template is skipped with a
    labeled warning (one bad template never breaks the gallery)."""
    import yaml

    templates: List[Dict[str, Any]] = [_builtin_template()]
    warnings: List[str] = []
    seen = {BUILTIN_ID}
    tdir = _templates_dir(data_dir)
    if not tdir.is_dir():
        return templates, warnings

    # Versioned operator templates (directories with meta.json).
    for home in sorted(p for p in tdir.iterdir() if p.is_dir()):
        tid = home.name
        if tid in seen:
            warnings.append(f"#FALLBACK skipped template {tid!r}: id collides with an already-served template")
            continue
        try:
            meta = _read_meta(home)
            version = int(meta.get("current_version") or 0)
            if version < 1:
                continue  # a directory with no committed version is not a template yet
            spark = _read_version_spark(home, version)
            spark.setdefault("name", "")
            templates.append({
                "id": tid,
                "name": str(meta.get("name") or tid),
                "description": str(meta.get("description") or ""),
                "source": "operator",
                "editable": True,
                "version": version,
                "spark": spark,
                "core_values": _core_values(spark),
            })
            seen.add(tid)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK skipped unreadable template {tid!r}: {e}")

    # Legacy flat files (pre-versioning): served as operator templates but
    # NOT editable (a save creates a versioned dir; flat files are frozen —
    # migrate by re-creating). Collisions skip loudly by filename (the picker
    # selects by id, so a shadowed entry would be silently unreachable).
    for path in sorted(tdir.glob("*.y*ml")):
        tid = path.stem
        if tid in seen:
            warnings.append(
                f"#FALLBACK skipped template {path.name}: id {tid!r} collides with an already-served template"
            )
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("template did not parse to a mapping")
            doc = dict(doc)
            doc.setdefault("name", "")
            templates.append({
                "id": tid,
                "name": str(doc.get("_template_name") or tid),
                "description": str(doc.get("_template_description") or ""),
                "source": "operator",
                "editable": False,  # flat files are frozen; re-create to version
                "version": 0,
                "spark": {k: v for k, v in doc.items() if not str(k).startswith("_template_")},
                "core_values": _core_values(doc),
            })
            seen.add(tid)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK skipped unreadable template {path.name}: {e}")

    return templates, warnings


def get_template(data_dir: Path, template_id: str, *, version: Optional[int] = None) -> Dict[str, Any]:
    """One template's FULL detail (the operator's 'view' — the whole spark,
    not just a description). `version=None` = current; a specific version
    reads that historical spark verbatim."""
    tid = str(template_id or "").strip()
    if tid == BUILTIN_ID:
        if version not in (None, 0):
            raise TemplateError("the builtin framework-default has no version history (it is code, the floor)")
        return _builtin_template()
    home = _template_home(data_dir, tid)
    meta = _read_meta(home)
    current = int(meta.get("current_version") or 0)
    if current < 1:
        # Legacy flat file?
        for path in (_templates_dir(data_dir) / f"{tid}.yaml", _templates_dir(data_dir) / f"{tid}.yml"):
            if path.exists():
                for t in list_templates(data_dir)[0]:
                    if t["id"] == tid:
                        return t
        raise KeyError(f"template {tid!r} not found")
    target = current if version is None else int(version)
    if target < 1 or target > current:
        raise TemplateError(f"template {tid!r} has versions 1..{current}; asked for {version}")
    spark = _read_version_spark(home, target)
    spark.setdefault("name", "")
    return {
        "id": tid,
        "name": str(meta.get("name") or tid),
        "description": str(meta.get("description") or ""),
        "source": "operator",
        "editable": True,
        "version": target,
        "current_version": current,
        "spark": spark,
        "core_values": _core_values(spark),
    }


def template_versions(data_dir: Path, template_id: str) -> List[Dict[str, Any]]:
    """The append-only version history (operator directive: every template
    versioned). Each entry: {version, saved_at, saved_by, note}."""
    tid = str(template_id or "").strip()
    if tid == BUILTIN_ID:
        return []  # the floor has no history — it is code
    home = _template_home(data_dir, tid)
    meta = _read_meta(home)
    history = meta.get("history")
    return list(history) if isinstance(history, list) else []


def save_template(
    data_dir: Path,
    *,
    template_id: str,
    spark: Dict[str, Any],
    name: str = "",
    description: str = "",
    actor: str = "person:operator",
    note: str = "",
    expect_new: bool = False,
) -> Dict[str, Any]:
    """Create (expect_new=True) or edit (expect_new=False → append a new
    version) an operator template. LINTS the spark before writing — a broken
    template never lands (the operator's worst UX is discovering it at
    summon). Append-only: every save is a new immutable version; meta.json's
    current pointer moves; history grows.

    The name inside the spark is DROPPED (blank) — a template is named at
    summon; the operator-facing template NAME + description live in meta."""
    tid = str(template_id or "").strip().lower()
    if not _ID_RE.match(tid):
        raise TemplateError(
            f"template id {template_id!r} must be lowercase letters/digits/_/-, 1-64 chars"
        )
    if tid == BUILTIN_ID:
        raise TemplateError(
            f"{BUILTIN_ID!r} is the framework floor (code, not editable) — create a new template seeded from it instead"
        )
    if not isinstance(spark, dict) or not spark:
        raise TemplateError("spark must be a non-empty mapping")

    # Lint BEFORE any write — ERRORS block (a core-value strip refuses at
    # save, never at a later summon); WARNINGS pass and ride the result.
    errors, lint_warnings = _lint_spark(spark)
    if errors:
        raise TemplateError("spark does not lint: " + "; ".join(errors))
    # Gateway belt for the class=core floor (adversary find, memory-lane
    # fix filed): lint checks the NAME set only, so shared_vulnerability
    # with class=revisable lints clean while the console's lock chips
    # silently unlock the framework floor. Until lint_spark pins the class,
    # refuse the demotion here.
    for v in (spark.get("values") or []):
        if isinstance(v, dict) and str(v.get("name") or "").strip() == "shared_vulnerability":
            if str(v.get("class") or "") != "core":
                raise TemplateError(
                    "shared_vulnerability is the framework floor and must keep class=core — "
                    "a template demoting it to revisable would unlock the non-removable value"
                )

    home = _template_home(data_dir, tid)
    meta = _read_meta(home)
    current = int(meta.get("current_version") or 0)
    if expect_new and current >= 1:
        raise TemplateError(f"template {tid!r} already exists (at v{current}) — edit it or pick a new id")
    if expect_new:
        # A create colliding with a LEGACY flat file would shadow it in the
        # gallery (dirs serve first, the flat file then skips loudly) — a
        # write that silently displaces an existing template is the worst
        # failure shape; refuse instead.
        for legacy in (_templates_dir(data_dir) / f"{tid}.yaml", _templates_dir(data_dir) / f"{tid}.yml"):
            if legacy.exists():
                raise TemplateError(
                    f"template id {tid!r} collides with the legacy file {legacy.name} — pick another id "
                    "(legacy flat files are frozen; re-create their content under a new id to version them)"
                )
    if not expect_new and current < 1 and not home.exists():
        raise KeyError(f"template {tid!r} not found — create it first")

    import yaml

    stored = {k: v for k, v in copy.deepcopy(spark).items()}
    stored["name"] = ""  # named at summon, never in the blueprint

    # IDEMPOTENT SAVE (adversary fold): re-saving byte-identical content is
    # a no-op, not a version bump — console re-saves must not churn the
    # append-only history. Canonical hash is the one shared definition.
    content_hash = ""
    try:
        from abstractmemory import canonical_spark_hash

        content_hash = str(canonical_spark_hash(stored))
        history_prev = list(meta.get("history") or [])
        if not expect_new and current >= 1 and history_prev:
            prev_hash = str(history_prev[-1].get("hash") or "")
            if prev_hash and prev_hash == content_hash:
                out = get_template(data_dir, tid)
                out["unchanged"] = True
                return out
    except TemplateError:
        raise
    except Exception:  # noqa: BLE001 - hashing is provenance, never a save blocker
        content_hash = ""

    home.mkdir(parents=True, exist_ok=True)
    new_version = current + 1
    _version_path(home, new_version).write_text(
        yaml.safe_dump(stored, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    now = datetime.now(timezone.utc).isoformat()
    history = list(meta.get("history") or [])
    entry = {"version": new_version, "saved_at": now, "saved_by": str(actor), "note": str(note or "")}
    if content_hash:
        entry["hash"] = content_hash
    history.append(entry)
    meta.update({
        "id": tid,
        "name": str(name or meta.get("name") or tid),
        "description": str(description if description is not None else meta.get("description") or ""),
        "current_version": new_version,
        "created_at": meta.get("created_at") or now,
        "updated_at": now,
        "history": history,
    })
    tmp = home / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(home / "meta.json")  # atomic pointer move
    out = get_template(data_dir, tid)
    if lint_warnings:
        out["lint_warnings"] = lint_warnings  # surfaced, never blocking
    return out
