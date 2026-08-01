"""RETIRING THE SECOND STORE: `<data_dir>/config/abstractcore.json` -> Core.

THE RULING (operator, 2026-08-01). AbstractCore holds the configuration for
provider, model, reasoning and routes; the Gateway is a layer on top that can
configure them directly, and a value changed from a Gateway console must change
AT THE SOURCE. The Gateway used to keep a base of its own under its data dir
whenever user auth was on, and on a machine where both were used the two stores
diverged: the Gateway served `endpoint:airelay/gpt-5.4` while `abstractcore
config defaults` on the same box answered `lmstudio/qwen3.5-9b`.

This module moves such a store INTO the Core store, once, loudly, and then
takes the legacy file out of circulation so the divergence cannot come back.

THE MERGE. Three-way, with AbstractCore's own merge
(through the `core_config` seam), against the UNTOUCHED default
document as the baseline:

  - a section the legacy store left at its framework default was never
    configured there, so the Core value stands (this is why a legacy file full
    of untouched defaults does not wipe Core's `default_models`, its longer
    `tool_timeout`, or its `provider_profiles`);
  - a section the legacy store actually changed WINS, because the legacy store
    was the surface actually being served;
  - anything present in only one store is carried over.

CAPABILITY ROUTES ARE MERGED BY THE ROW, NOT BY THE FIELD, when the two stores
name different providers for the same route. A route row is one address:
provider, base_url and model are chosen together. Field-wise merging of
`{lmstudio, qwen3.5-9b, base_url: localhost:1234}` under
`{endpoint:airelay, gpt-5.4}` yields an airelay route pointed at LM Studio --
an address neither entry point ever served. Same provider on both sides means
the fields ARE comparable, and there the legacy value wins field by field while
Core's unnamed fields (a base_url, a reasoning effort) survive.

SAFETY. Both files are backed up (timestamped) before anything is written, the
merge is published atomically, and only then is the legacy file renamed to
`abstractcore.json.migrated-<ts>`. Idempotent: the second run finds no legacy
store and reports that it has nothing to do.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core_config

LEGACY_STORE_BASENAME = "abstractcore.json"
MIGRATED_SUFFIX = "migrated"

_ROUTE_SECTION = "capability_defaults"


def legacy_gateway_core_config_path(data_dir: Optional[Path] = None) -> Path:
    """The retired Gateway-scoped Core store under a Gateway data dir."""

    if data_dir is None:
        from .users import gateway_data_dir_from_env

        data_dir = gateway_data_dir_from_env()
    return Path(data_dir).expanduser().resolve() / "config" / LEGACY_STORE_BASENAME


def migrate_legacy_gateway_core_config(
    *,
    data_dir: Optional[Path] = None,
    core_file: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Merge a legacy Gateway-scoped Core store into THE Core store.

    Returns a report; never raises for an ordinary condition (absent legacy
    store, split-server posture, unreadable file) so a startup call cannot take
    a Gateway down. `dry_run` computes and reports the merge without touching
    a single file.
    """

    report: Dict[str, Any] = {
        "ok": True,
        "status": "no-legacy-store",
        "legacy_file": None,
        "core_file": None,
        "backups": [],
        "renamed_to": None,
        "changes": [],
        "errors": [],
        "dry_run": bool(dry_run),
    }

    try:
        legacy_path = legacy_gateway_core_config_path(data_dir)
    except Exception as exc:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(f"could not resolve the legacy store path: {exc}")
        return report
    report["legacy_file"] = str(legacy_path)

    if os.getenv("ABSTRACTCORE_SERVER_BASE_URL", "").strip():
        # The store lives on another host; merging into this machine's Core
        # file would write to the wrong box AND destroy the legacy evidence.
        report["status"] = "skipped-split-server"
        return report

    if not legacy_path.is_file():
        return report

    try:
        core_path = Path(core_file).expanduser() if core_file is not None else core_config.core_store_path()
        if core_path is None:
            raise RuntimeError("AbstractCore did not resolve a config store path")
    except Exception as exc:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(f"could not resolve the AbstractCore store path: {exc}")
        return report
    report["core_file"] = str(core_path)

    if _same_path(legacy_path, core_path):
        # Already one store (an operator pointing ABSTRACTCORE_CONFIG_FILE at
        # the Gateway data dir is a legitimate way to have one path).
        report["status"] = "already-one-store"
        return report

    legacy_doc, error = _read_document(legacy_path)
    if error:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(error)
        return report
    core_doc, error = _read_document(core_path)
    if error:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(error)
        return report

    merged, changes = merge_legacy_into_core(legacy_doc, core_doc)
    report["changes"] = changes

    if not changes and merged == core_doc:
        # Nothing to move. The legacy file still has to leave the field, or the
        # next divergence starts here again.
        report["status"] = "retired-identical"
        if not dry_run:
            renamed = _retire_legacy(legacy_path)
            report["renamed_to"] = str(renamed) if renamed else None
        return report

    if dry_run:
        report["status"] = "dry-run"
        return report

    stamp = _timestamp()
    for path in (core_path, legacy_path):
        backup = _backup(path, stamp)
        if backup is not None:
            report["backups"].append(str(backup))
        elif path.is_file():
            # HARD PRECONDITION: a migration that cannot preserve an original
            # must not rewrite anything. Refuse loudly; both files stay put.
            report["ok"] = False
            report["status"] = "error"
            report["errors"].append(
                f"could not back up {path} before merging — migration aborted, nothing was changed"
            )
            return report

    try:
        _publish(core_path, merged)
    except Exception as exc:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(f"failed to publish the merged store to {core_path}: {exc}")
        return report

    renamed = _retire_legacy(legacy_path, stamp)
    report["renamed_to"] = str(renamed) if renamed else None
    report["status"] = "migrated"
    return report


# ---------------------------------------------------------------------------
# Provider endpoint profiles: the same ruling, the same move
# ---------------------------------------------------------------------------


LEGACY_PROFILES_BASENAME = "provider_endpoint_profiles.json"


def legacy_gateway_profiles_path(data_dir: Optional[Path] = None) -> Path:
    """The retired Gateway-scoped endpoint-profile file under a Gateway data dir."""

    if data_dir is None:
        from .users import gateway_data_dir_from_env

        data_dir = gateway_data_dir_from_env()
    return Path(data_dir).expanduser().resolve() / "config" / LEGACY_PROFILES_BASENAME


def migrate_legacy_gateway_provider_profiles(
    *,
    data_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Move gateway-root endpoint profiles into Core's `provider_profiles`.

    A profile is provider configuration, so it belongs to the Core store; the
    Gateway's root file was a second store of exactly the kind the ruling
    retires (on the operator's machine `endpoint:airelay` existed ONLY there,
    so `abstractcore` could not resolve the provider its own text route named).

    The Core row wins nothing by default and loses nothing either: a profile id
    present in both keeps whatever Core does NOT carry and takes the Gateway's
    value for every field the Gateway file states, because the Gateway file was
    the served one. Ids present only in Core stay untouched.
    """

    report: Dict[str, Any] = {
        "ok": True,
        "status": "no-legacy-store",
        "legacy_file": None,
        "backups": [],
        "renamed_to": None,
        "changes": [],
        "errors": [],
        "dry_run": bool(dry_run),
    }

    try:
        legacy_path = legacy_gateway_profiles_path(data_dir)
    except Exception as exc:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(f"could not resolve the legacy profile store path: {exc}")
        return report
    report["legacy_file"] = str(legacy_path)

    if os.getenv("ABSTRACTCORE_SERVER_BASE_URL", "").strip():
        report["status"] = "skipped-split-server"
        return report
    if not legacy_path.is_file():
        return report

    document, error = _read_document(legacy_path)
    if error:
        report["ok"] = False
        report["status"] = "error"
        report["errors"].append(error)
        return report

    rows = document.get("profiles")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    existing = {
        str(row.get("id") or "").strip().lower(): row
        for row in core_config.list_core_provider_profiles()
        if isinstance(row, dict)
    }

    planned: List[Dict[str, Any]] = []
    for row in rows:
        profile_id = str(row.get("id") or "").strip()
        if not profile_id:
            continue
        verb = "updated" if profile_id.lower() in existing else "added"
        planned.append({"id": profile_id, "verb": verb, "row": row})
        report["changes"].append(
            f"provider profile {profile_id}: {verb} in the Core store "
            f"({str(row.get('provider_family') or '-')} @ {str(row.get('base_url') or '-')})"
        )

    if dry_run:
        report["status"] = "dry-run"
        return report

    stamp = _timestamp()
    if planned:
        backup = _backup(legacy_path, stamp)
        if backup is not None:
            report["backups"].append(str(backup))
        elif legacy_path.is_file():
            report["ok"] = False
            report["status"] = "error"
            report["errors"].append(
                f"could not back up {legacy_path} before moving profiles — aborted, nothing was changed"
            )
            return report

    for entry in planned:
        row = entry["row"]
        try:
            core_config.save_core_provider_profile(
                str(row.get("id")),
                display_name=str(row.get("display_name") or row.get("id") or ""),
                description=str(row.get("description") or ""),
                provider_family=str(row.get("provider_family") or "openai-compatible"),
                base_url=str(row.get("base_url") or ""),
                api_key=str(row.get("api_key") or ""),
                clear_api_key=not str(row.get("api_key") or "").strip(),
                allowed_models=[str(m) for m in (row.get("allowed_models") or []) if str(m).strip()],
                enabled=bool(row.get("enabled", True)),
                scope=str(row.get("scope") or "gateway"),
                capabilities=[str(c) for c in (row.get("capabilities") or []) if str(c).strip()] or None,
                created_at=str(row.get("created_at") or "") or None,
            )
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(f"provider profile {row.get('id')!r} could not be written to the Core store: {exc}")

    if not report["ok"]:
        # Leave the legacy file in place: a partially-migrated store is
        # recoverable only while the source is still where it was.
        report["status"] = "error"
        return report

    renamed = _retire_legacy(legacy_path, stamp)
    report["renamed_to"] = str(renamed) if renamed else None
    report["status"] = "migrated" if planned else "retired-empty"
    return report


def migrate_legacy_gateway_stores(
    *,
    data_dir: Optional[Path] = None,
    core_file: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Both retirements, in order: capability routes first, then profiles.

    Order matters only for the log: a route naming `endpoint:<id>` reads better
    beside the profile that resolves it.
    """

    return {
        "capability_defaults": migrate_legacy_gateway_core_config(
            data_dir=data_dir, core_file=core_file, dry_run=dry_run
        ),
        "provider_profiles": migrate_legacy_gateway_provider_profiles(data_dir=data_dir, dry_run=dry_run),
    }


def merge_legacy_into_core(
    legacy_doc: Dict[str, Any],
    core_doc: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """The merge, as a pure function over two documents. See the module docstring."""

    try:
        baseline = core_config.default_core_config_document()
    except Exception:
        baseline = {}

    legacy_rest = {k: v for k, v in legacy_doc.items() if k != _ROUTE_SECTION}
    core_rest = {k: v for k, v in core_doc.items() if k != _ROUTE_SECTION}
    baseline_rest = {k: v for k, v in baseline.items() if k != _ROUTE_SECTION}

    try:
        merged = core_config.merge_core_config_documents(baseline_rest, legacy_rest, core_rest)
    except Exception:
        merged = dict(core_rest)
    changes = _document_changes(core_rest, merged)

    routes_merged, route_changes = _merge_routes(
        _routes_of(core_doc),
        _routes_of(legacy_doc),
    )
    changes.extend(route_changes)

    section = copy.deepcopy(core_doc.get(_ROUTE_SECTION))
    if not isinstance(section, dict):
        section = copy.deepcopy(legacy_doc.get(_ROUTE_SECTION))
    if not isinstance(section, dict):
        section = {"version": 1}
    legacy_section = legacy_doc.get(_ROUTE_SECTION)
    if isinstance(legacy_section, dict):
        for key, value in legacy_section.items():
            if key != "routes" and key not in section:
                section[key] = copy.deepcopy(value)
    section["routes"] = routes_merged
    merged[_ROUTE_SECTION] = section
    return merged, changes


def _merge_routes(
    core_routes: Dict[str, Any],
    legacy_routes: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    merged: Dict[str, Any] = {key: copy.deepcopy(value) for key, value in core_routes.items()}
    changes: List[str] = []

    for key in sorted(legacy_routes):
        theirs = legacy_routes.get(key)
        if not isinstance(theirs, dict):
            continue
        ours = merged.get(key)
        if not isinstance(ours, dict):
            merged[key] = copy.deepcopy(theirs)
            changes.append(f"route {key}: added {_row_summary(theirs)} from the gateway store")
            continue
        if _row_summary(ours) == _row_summary(theirs) and _options_of(ours) == _options_of(theirs):
            continue
        if _provider_of(ours) != _provider_of(theirs):
            merged[key] = copy.deepcopy(theirs)
            changes.append(
                f"route {key}: replaced {_row_summary(ours)} with {_row_summary(theirs)} "
                "(different provider — the row moves whole)"
            )
            continue
        row = copy.deepcopy(ours)
        for field, value in theirs.items():
            if field == "options":
                continue
            row[field] = copy.deepcopy(value)
        options = dict(_options_of(ours))
        options.update(_options_of(theirs))
        if options:
            row["options"] = options
        elif "options" in row:
            row.pop("options")
        if row != ours:
            merged[key] = row
            changes.append(f"route {key}: {_row_summary(ours)} -> {_row_summary(row)} (same provider — field-preserving)")

    return merged, changes


def _routes_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    section = doc.get(_ROUTE_SECTION)
    if not isinstance(section, dict):
        return {}
    routes = section.get("routes")
    return {str(k): v for k, v in routes.items()} if isinstance(routes, dict) else {}


def _provider_of(row: Dict[str, Any]) -> str:
    return str(row.get("provider") or "").strip().lower()


def _options_of(row: Dict[str, Any]) -> Dict[str, Any]:
    options = row.get("options")
    return dict(options) if isinstance(options, dict) else {}


def _row_summary(row: Dict[str, Any]) -> str:
    provider = str(row.get("provider") or "-").strip()
    model = str(row.get("model") or "-").strip()
    extra = []
    if str(row.get("base_url") or "").strip():
        extra.append(f"base_url={str(row['base_url']).strip()}")
    if str(row.get("reasoning") or "").strip():
        extra.append(f"reasoning={str(row['reasoning']).strip()}")
    options = _options_of(row)
    if options:
        extra.append("options=" + json.dumps(options, sort_keys=True))
    suffix = f" [{', '.join(extra)}]" if extra else ""
    return f"{provider}/{model}{suffix}"


def _document_changes(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    before_leaves = _leaves(before)
    after_leaves = _leaves(after)
    lines: List[str] = []
    for path in sorted(after_leaves):
        new = after_leaves[path]
        if path not in before_leaves:
            lines.append(f"{path}: added {json.dumps(new, default=str)}")
        elif before_leaves[path] != new:
            lines.append(f"{path}: {json.dumps(before_leaves[path], default=str)} -> {json.dumps(new, default=str)}")
    return lines


def _leaves(doc: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(doc, dict):
        for key, value in doc.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_leaves(value, path))
            else:
                out[path] = value
    elif prefix:
        out[prefix] = doc
    return out


def _read_document(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    try:
        if not path.is_file():
            return {}, None
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except json.JSONDecodeError as exc:
        return {}, f"{path} is not valid JSON ({exc}); refusing to merge a store that cannot be read"
    except Exception as exc:
        return {}, f"{path} could not be read ({exc}); refusing to merge"
    if not isinstance(data, dict):
        return {}, f"{path} does not hold a JSON object; refusing to merge"
    return data, None


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _backup(path: Path, stamp: str) -> Optional[Path]:
    try:
        if not path.is_file():
            return None
        target = path.with_name(f"{path.name}.bak-{stamp}")
        if target.exists():
            target = path.with_name(f"{path.name}.bak-{stamp}-{uuid.uuid4().hex[:6]}")
        target.write_bytes(path.read_bytes())
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
        return target
    except Exception:
        return None


def _publish(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def _retire_legacy(path: Path, stamp: Optional[str] = None) -> Optional[Path]:
    """Rename the legacy store out of the way. It must never be read again."""

    stamp = stamp or _timestamp()
    try:
        target = path.with_name(f"{path.name}.{MIGRATED_SUFFIX}-{stamp}")
        if target.exists():
            target = path.with_name(f"{path.name}.{MIGRATED_SUFFIX}-{stamp}-{uuid.uuid4().hex[:6]}")
        path.replace(target)
        return target
    except Exception:
        return None


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def format_migration_report(report: Dict[str, Any]) -> List[str]:
    """The report as lines a startup log or a CLI can print verbatim."""

    if "status" not in report:
        lines: List[str] = []
        for value in report.values():
            if isinstance(value, dict):
                lines.extend(format_migration_report(value))
        return lines

    status = str(report.get("status") or "")
    if status == "no-legacy-store":
        return []
    head = f"[core-config] legacy gateway store {report.get('legacy_file')}: {status}"
    lines = [head]
    if status in {"skipped-split-server", "already-one-store"}:
        return lines
    if report.get("core_file"):
        lines.append(f"[core-config]   core store: {report.get('core_file')}")
    for change in report.get("changes") or []:
        lines.append(f"[core-config]   {change}")
    for backup in report.get("backups") or []:
        lines.append(f"[core-config]   backup: {backup}")
    if report.get("renamed_to"):
        lines.append(f"[core-config]   legacy store renamed to: {report['renamed_to']}")
    for error in report.get("errors") or []:
        lines.append(f"[core-config]   ERROR: {error}")
    return lines
