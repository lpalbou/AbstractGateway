"""Admin runtime inventory — the RUNTIMES-FIRST serve (operator order
2026-07-14 12:24: "runtime tab = I SEE THE RUNTIMES FIRST ... when i click on
a runtime, i do see the associated sessions, then cache, then anything else
relevant").

WHAT A RUNTIME IS at this gateway (three kinds, all real data planes):
- default: the gateway's own data dir (the admin's plane; the tick loop's
  home when ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME holds).
- user: per-principal planes under <data_dir>/users/<tenant>/<runtime_id>/
  (F1 machinery — each carries its own runtime/ + flows/).
- entity: each summoned entity's OWN runtime inside its home
  (runtime_<slug>.sqlite3; the entity-topology ruling "each with its own
  runtime, named for the entity").

The LIST is cheap on purpose (progressive disclosure: sizes + owners now,
runs on drill-in): run listings are served lazily by `runtime_runs` when the
operator clicks a row. Sizes are bounded directory walks; failures degrade to
labeled warnings, never a 500 (an inventory must render even when one plane
is broken).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SIZE_WALK_CAP = 200_000  # files; beyond this the size is served as a labeled floor
# The whole inventory's size budget (wall clock). A list view must render in
# UI time; on a big data root (live find: 28s over a 6 GB runtime) the walk
# runs out of budget and the remaining planes serve size=null with a label —
# never a half-minute tab click.
_SIZE_TIME_BUDGET_S = 3.0


def _dir_size(path: Path, *, exclude: Tuple[str, ...] = (), deadline: Optional[float] = None) -> Tuple[int, bool]:
    """Bounded recursive size. Returns (bytes, truncated). `exclude` prunes
    top-level child dirs that are their OWN planes (the default runtime must
    not double-count users/ and entities/)."""
    total = 0
    seen = 0
    if not path.exists():
        return 0, False
    root_str = str(path)
    for root, dirs, files in os.walk(path):
        if root == root_str and exclude:
            dirs[:] = [d for d in dirs if d not in exclude]
        if deadline is not None and time.monotonic() > deadline:
            return total, True
        for name in files:
            seen += 1
            if seen > _SIZE_WALK_CAP:
                return total, True
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total, False


def _stores_for_dir(runtime_dir: Path):
    """Read-only stores over a runtime data dir (no runner, no ticking).
    Backend detection mirrors the service factory: gateway.sqlite3 present =
    sqlite, else the file backend."""
    from .stores import build_file_stores, build_sqlite_stores

    db = runtime_dir / "gateway.sqlite3"
    if db.exists():
        return build_sqlite_stores(base_dir=runtime_dir, db_path=db)
    return build_file_stores(base_dir=runtime_dir)


def _run_summary(run: Any) -> Dict[str, Any]:
    status = getattr(run, "status", None)
    return {
        "run_id": getattr(run, "run_id", None),
        "workflow_id": getattr(run, "workflow_id", None),
        "status": getattr(status, "value", None) or (str(status) if status else None),
        "session_id": getattr(run, "session_id", None),
        "parent_run_id": getattr(run, "parent_run_id", None),
        "created_at": getattr(run, "created_at", None),
        "updated_at": getattr(run, "updated_at", None),
        "actor_id": getattr(run, "actor_id", None),
    }


def _owned_by(row: Dict[str, Any], account: str, tenant: Optional[str]) -> bool:
    """Does `account` (a user id or an entity slug) own this plane? The
    owners list is the one the table shows, so the filter and the Owner
    column always agree."""
    if tenant and str(row.get("tenant_id") or "default") != tenant:
        return False
    if row.get("kind") == "entity" and str(row.get("entity") or "") == account:
        return True
    return any(str((o or {}).get("user_id") or "") == account for o in (row.get("owners") or []))


def list_runtimes(
    *,
    data_dir: Path,
    include_sizes: bool = True,
    entity_registry: Optional[Any] = None,
    account: Optional[str] = None,
    account_tenant: Optional[str] = None,
) -> Dict[str, Any]:
    """Compose the runtime inventory: default plane + user planes + entity
    planes, with owners joined from the user registry and the entity roster.
    Pure read; every per-plane failure is a labeled warning on the row.

    `account` (round 8, the Accounts Runtime link): keep only the planes that
    account owns (before the size walk, so a filtered list is cheap); the
    answer carries `filter: {account, tenant_id}`."""
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []

    # Owners: user registry bindings (runtime_id defaults to the user id).
    bindings: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    entity_role_users: Dict[str, Dict[str, Any]] = {}
    try:
        from .users import GatewayUserRegistry

        for rec in GatewayUserRegistry().list_users():
            pub = rec.public_dict()
            tenant = str(pub.get("tenant_id") or "default")
            rid = str(pub.get("runtime_id") or pub.get("user_id") or "")
            roles = [str(r) for r in (pub.get("roles") or [])]
            entry = {
                "user_id": pub.get("user_id"),
                "roles": roles,
                "enabled": bool(pub.get("enabled", True)),
            }
            if "entity" in roles:
                entity_role_users[str(pub.get("user_id") or "")] = entry
                continue  # entity principals surface on their entity row, not as user planes
            bindings.setdefault((tenant, rid), []).append(entry)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"#FALLBACK user registry unreadable: {e}")

    # Sizes are filled AFTER all rows exist, smallest-expected-cost planes
    # first (entities, users, then the big default root) — so the wall-clock
    # budget starves only the giant plane, never the meaningful small rows.
    size_jobs: List[Tuple[Dict[str, Any], Path, Tuple[str, ...]]] = []

    def _queue_size(row: Dict[str, Any], path: Path, exclude: Tuple[str, ...] = ()) -> None:
        if include_sizes:
            size_jobs.append((row, path, exclude))

    # 1) The default plane. users/ and entities/ are their OWN planes below —
    # excluded so the default row doesn't double-count (and doesn't walk the
    # biggest trees twice).
    default_owners = bindings.pop(("default", "default"), [])
    default_owners.extend(bindings.pop(("default", "admin"), []))
    default_row: Dict[str, Any] = {
        "kind": "default",
        "tenant_id": "default",
        "runtime_id": "default",
        "label": "Gateway default runtime",
        "owners": default_owners or [{"user_id": "admin", "roles": ["admin"], "enabled": True}],
        "data_dir": str(data_dir),
    }
    rows.append(default_row)
    _queue_size(default_row, data_dir, ("users", "entities"))

    # 2) User planes: registry bindings union on-disk dirs (a directory
    # without a live binding is a real plane — likely a deleted user's data;
    # it must be VISIBLE, that is what retained-reservations are about).
    users_root = data_dir / "users"
    on_disk: Dict[Tuple[str, str], Path] = {}
    if users_root.exists():
        try:
            for tenant_dir in sorted(users_root.iterdir()):
                if not tenant_dir.is_dir():
                    continue
                for rt_dir in sorted(tenant_dir.iterdir()):
                    if rt_dir.is_dir():
                        on_disk[(tenant_dir.name, rt_dir.name)] = rt_dir
        except OSError as e:
            warnings.append(f"#FALLBACK users dir scan failed: {e}")
    for key in sorted(set(bindings) | set(on_disk)):
        tenant, rid = key
        path = on_disk.get(key)
        row: Dict[str, Any] = {
            "kind": "user",
            "tenant_id": tenant,
            "runtime_id": rid,
            "label": f"{tenant}/{rid}",
            "owners": bindings.get(key, []),
            "data_dir": str(path) if path else None,
            "materialized": path is not None,  # a binding materializes on first use
        }
        if path is not None:
            _queue_size(row, path)
        if not bindings.get(key) and path is not None:
            row["note"] = "no live user binds this plane (deleted/reassigned user?) — see Retained runtimes"
        rows.append(row)

    # 3) Entity planes (each home carries its own runtime, named for the
    # entity). State + liveness ride so the roster reads honestly.
    try:
        from .entities import EntityRegistry, derived_liveness

        registry = entity_registry or EntityRegistry(data_dir=data_dir)
        for e in registry.list_entities():
            if e.get("error"):
                rows.append({"kind": "entity", "runtime_id": e.get("slug"), "error": e.get("error")})
                continue
            slug = str(e.get("slug") or "")
            home = data_dir / "entities" / slug
            st = e.get("state") or {}
            row = {
                "kind": "entity",
                "tenant_id": "default",
                "runtime_id": f"runtime_{slug}",
                "label": e.get("name") or slug,
                "entity": slug,
                "owners": [{"user_id": slug, "roles": ["entity"], "enabled": True}],
                "state": st.get("state"),
                "liveness": st.get("liveness") or derived_liveness(st.get("state")),
                "data_dir": str(home),
            }
            _queue_size(row, home)
            if slug in entity_role_users and not entity_role_users[slug].get("enabled", True):
                row["note"] = "entity principal disabled at the door"
            rows.append(row)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"#FALLBACK entity roster unreadable: {e}")

    want = str(account or "").strip()
    want_tenant = str(account_tenant or "").strip() or None
    if want:
        rows = [r for r in rows if not r.get("error") and _owned_by(r, want, want_tenant)]
        kept = {id(r) for r in rows}
        size_jobs = [j for j in size_jobs if id(j[0]) in kept]

    # Size pass, cheap planes first: entity homes and user planes are MBs;
    # the default root can be GBs — it goes last so the budget starves it
    # alone. Budget spent = size_bytes null with the label, never a slow tab.
    if include_sizes:
        deadline = time.monotonic() + _SIZE_TIME_BUDGET_S
        order = {"entity": 0, "user": 1, "default": 2}
        for row, path, exclude in sorted(size_jobs, key=lambda j: order.get(str(j[0].get("kind")), 3)):
            if time.monotonic() > deadline:
                row["size_bytes"] = None
                row["size_note"] = "#TRUNCATION size walk budget spent — this plane's size not computed this refresh"
                continue
            size, truncated = _dir_size(path, exclude=exclude, deadline=deadline)
            row["size_bytes"] = size
            if truncated:
                row["size_note"] = "#TRUNCATION size is a floor (walk capped)"

    out: Dict[str, Any] = {"runtimes": rows}
    if want:
        out["filter"] = {"account": want, "tenant_id": want_tenant}
    if warnings:
        out["warnings"] = warnings
    return out


def workflow_display_label(workflow_id: Any) -> str:
    """A workflow id a person can read.

    A catalog-published workflow runs under an internal id that carries its
    scope and tenant in base64 -- `__catalog__v2__tenant_catalog__ZGVmYXVsdA__
    YWJzdHJhY3Rhc3Npc3RhbnQtb3JjaGVzdHJhdG9y@0.0.3:c53b1579`. That is the
    right key for the store and the wrong string for a menu: the operator
    recognises "abstractassistant-orchestrator", which is exactly what those
    base64 chunks decode to. Anything we cannot parse is returned unchanged --
    an unreadable id beats a wrong one.
    """
    raw = str(workflow_id or "").strip()
    if not raw:
        return ""
    bundle, sep, flow = raw.partition(":")
    bundle = _strip_version(bundle)
    parsed = _catalog_bundle_name(bundle)
    if parsed is not None:
        bundle = _strip_version(parsed)
    return f"{bundle}{sep}{flow}" if sep else bundle


def _strip_version(bundle_id: str) -> str:
    """`name@0.0.3` -> `name`.

    The version rides OUTSIDE the encoded catalog id (`…<b64>@0.0.3`), so it
    has to come off before the parse or the base64 component never decodes.
    It also tells two runs apart far less often than it costs width, and the
    run id is the identity anyway — so it goes either way.
    """
    b = str(bundle_id or "")
    return b.rsplit("@", 1)[0] if "@" in b else b


def _catalog_bundle_name(bundle_id: str) -> Optional[str]:
    """The public bundle id inside a catalog-internal one, or None."""
    try:
        from .workflow_catalog import parse_catalog_internal_bundle_id

        parsed = parse_catalog_internal_bundle_id(str(bundle_id or ""))
    except Exception:
        return None
    return parsed[2] if parsed is not None else None


def is_internal_workflow_id(workflow_id: Any) -> bool:
    """Machinery, not work an operator started — the catalog's own rule.

    ONE definition, in the module that owns the id scheme: `/runs`, this
    host-wide listing and anything else that hides bookkeeping runs must agree,
    or a catalog-published workflow is visible in one list and missing from the
    next.
    """
    try:
        from .workflow_catalog import is_internal_workflow_id as _rule

        return _rule(workflow_id)
    except Exception:
        return str(workflow_id or "").strip().startswith("__")


_ACTIVE_SCAN_LIMIT = 200  # active runs are never cut by the menu's `limit`
_ROOT_WALK_MAX_DEPTH = 32
_FINISHED_SCAN_MAX = 2000  # index rows; the page widens up to this


def _is_turn_root(row: Dict[str, Any]) -> bool:
    """The runtime's own definition of a run a person started: parent-less and
    not an automation controller, OR an automation occurrence (which has the
    controller as its parent). `abstractruntime.core.run_attribution`."""
    try:
        from abstractruntime.core.run_attribution import is_turn_root

        return bool(is_turn_root(parent_run_id=row.get("parent_run_id"), role=row.get("role")))
    except Exception:  # an older runtime without the attribution module
        if row.get("role") == "occurrence":
            return True
        if row.get("role") == "controller":
            return False
        return not str(row.get("parent_run_id") or "").strip()


def _row_from_state(run: Any) -> Dict[str, Any]:
    """A RunState as an index-shaped row (the `list_runs` fallback and the
    parent walk): the summary plus the attribution `role`."""
    row = _run_summary(run)
    role = None
    try:
        from abstractruntime.core.run_attribution import automation_index_fields

        role = automation_index_fields(getattr(run, "vars", None), run_id=row.get("run_id")).get("role")
    except Exception:
        role = None
    row["role"] = role
    waiting = getattr(run, "waiting", None)
    reason = getattr(waiting, "reason", None) if waiting is not None else None
    row["wait_reason"] = getattr(reason, "value", None) or (str(reason) if reason else None)
    return row


def _index_rows(store: Any, *, status: Optional[str], root_only: bool, limit: int) -> List[Dict[str, Any]]:
    """Index rows, newest update first, filtered INSIDE the store.

    Filtering before the limit is the point: the old listing asked for the 25
    most recently updated runs of ANY kind and only then dropped children and
    machinery, so a few busy runs (whose sub-runs and automation occurrences
    are the rows that keep being updated) filled the page and the menu said
    "No runs in the last 24 hours" over a working machine."""
    fn = getattr(store, "list_run_index", None)
    if callable(fn):
        return [dict(r) for r in (fn(status=status, root_only=root_only, limit=limit) or []) if isinstance(r, dict)]
    # Stores without an index (in-memory, very old): scan a wider page and
    # filter here, so the limit still counts what we keep.
    out: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"limit": max(limit * 8, limit)}
    if status is not None:
        try:
            from abstractruntime.core.models import RunStatus

            kwargs["status"] = RunStatus(status)
        except Exception:
            kwargs["status"] = status
    try:
        runs = list(store.list_runs(**kwargs) or [])
    except TypeError:
        kwargs.pop("status", None)
        runs = list(store.list_runs(**kwargs) or [])
    for run in runs:
        row = _row_from_state(run)
        if status is not None and row.get("status") != status:
            continue
        if root_only and not _is_turn_root(row):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _load_row(store: Any, run_id: str) -> Optional[Dict[str, Any]]:
    try:
        run = store.load(run_id)
    except Exception:
        return None
    return _row_from_state(run) if run is not None else None


def _turn_root_of(store: Any, row: Dict[str, Any], known: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Walk parent links up to the turn root (bounded; cycles and gaps → None)."""
    cur: Optional[Dict[str, Any]] = row
    seen: set = set()
    for _ in range(_ROOT_WALK_MAX_DEPTH):
        if cur is None:
            return None
        rid = str(cur.get("run_id") or "")
        if rid in seen:
            return None
        seen.add(rid)
        if _is_turn_root(cur):
            return cur
        parent = str(cur.get("parent_run_id") or "").strip()
        if not parent:
            return None
        nxt = known.get(parent)
        if nxt is None:
            nxt = _load_row(store, parent)
            if nxt is not None:
                known[parent] = nxt
        cur = nxt
    return None


def _runs_from_store(store: Any, *, limit: int, since_epoch: Optional[float], plane: str) -> List[Dict[str, Any]]:
    """One plane's rows for the host listing: ACTIVE turn roots (whatever their
    age), then turn roots that finished inside the window.

    - A turn root is running when it, or any run below it, is running (a
      parent waiting on its sub-workflow is working, not idle).
    - A turn root waiting for a person or an event is shown while it was
      touched inside the window (an ask from August is history, not "now").
    - Finished roots are windowed on their LAST update: a run that started
      yesterday and finished an hour ago belongs to the last 24 hours.
    """
    known: Dict[str, Dict[str, Any]] = {}
    roots: Dict[str, Dict[str, Any]] = {}

    def _keep(row: Dict[str, Any], activity: str) -> None:
        rid = str(row.get("run_id") or "")
        if not rid or is_internal_workflow_id(row.get("workflow_id")):
            return
        prev = roots.get(rid)
        rank = {"running": 0, "waiting": 1, "done": 2}
        if prev is not None and rank[prev["activity"]] <= rank[activity]:
            return
        roots[rid] = {**row, "activity": activity}

    for row in _index_rows(store, status="running", root_only=False, limit=_ACTIVE_SCAN_LIMIT):
        known[str(row.get("run_id") or "")] = row
    for row in list(known.values()):
        root = _turn_root_of(store, row, known)
        if root is not None:
            _keep(root, "running")

    for row in _index_rows(store, status="waiting", root_only=True, limit=_ACTIVE_SCAN_LIMIT):
        touched = _epoch_or_none(row.get("updated_at")) or _epoch_or_none(row.get("created_at"))
        if since_epoch is not None and touched is not None and touched < since_epoch:
            continue
        _keep(row, "waiting")

    # Finished roots: `limit + 1` of them (the +1 makes has_more honest). The
    # newest-updated roots are often the active ones and machinery, so the
    # page widens until it holds enough finished rows, reaches the window's
    # edge, or the store has no more.
    want = max(1, limit) + 1
    page = want + len(roots) + 8
    while True:
        batch = _index_rows(store, status=None, root_only=True, limit=page)
        finished = 0
        past_window = False
        for row in batch:
            if str(row.get("status") or "") in {"running", "waiting"}:
                continue  # decided above (running via its tree; waiting windowed)
            touched = _epoch_or_none(row.get("updated_at")) or _epoch_or_none(row.get("created_at"))
            if since_epoch is not None and touched is not None and touched < since_epoch:
                past_window = True
                continue
            if is_internal_workflow_id(row.get("workflow_id")):
                continue
            finished += 1
            if finished <= want:
                _keep(row, "done")
        if finished >= want or past_window or len(batch) < page or page >= _FINISHED_SCAN_MAX:
            break
        page = min(page * 4, _FINISHED_SCAN_MAX)

    out: List[Dict[str, Any]] = []
    for row in roots.values():
        summary = {k: row.get(k) for k in ("run_id", "workflow_id", "status", "session_id", "parent_run_id", "created_at", "updated_at", "actor_id")}
        summary["role"] = row.get("role")
        summary["activity"] = row["activity"]
        summary["plane"] = plane
        summary["label"] = workflow_display_label(row.get("workflow_id"))
        summary["started_epoch"] = _epoch_or_none(row.get("created_at"))
        summary["updated_epoch"] = _epoch_or_none(row.get("updated_at"))
        out.append(summary)
    return out


def _observer_path(run_id: Any) -> Optional[str]:
    """`/apps/observer/#run/<id>`: the same link the console's activity rows carry."""
    from .account_activity import observer_path_for

    return observer_path_for(str(run_id or "") or None)


def _host_row_order(row: Dict[str, Any]) -> Tuple[int, float]:
    """Running first, then waiting, then finished; inside each group the most
    recent first (started for active rows, last update for finished ones). A
    row with no readable time sorts last in its group, never dropped."""
    group = {"running": 0, "waiting": 1}.get(str(row.get("activity") or ""), 2)
    when = row.get("started_epoch") if group < 2 else (row.get("updated_epoch") or row.get("started_epoch"))
    return (group, -(when if isinstance(when, (int, float)) else float("-inf")))


def _ledger_len(ledger_store: Any, run_id: str) -> Optional[int]:
    """How many ledger entries a run has — its steps. None when unknowable.

    A missing count is NOT a zero: a store that cannot answer must leave the
    column empty rather than report an active run as having done nothing.
    """
    if ledger_store is None or not run_id:
        return None
    try:
        count_fn = getattr(ledger_store, "count", None)
        if callable(count_fn):
            return int(count_fn(run_id))
        records = ledger_store.list(run_id)
        return int(len(records)) if isinstance(records, list) else None
    except Exception:
        return None


def _epoch_or_none(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def recent_runs_host_wide(
    *,
    data_dir: Path,
    limit: int = 25,
    since_epoch: Optional[float] = None,
    default_run_store: Optional[Any] = None,
) -> Dict[str, Any]:
    """The runs on this MACHINE, across data planes: every ACTIVE turn root
    first (running, then waiting — whatever their age), then the turn roots
    that finished inside the window, newest first.

    WHY THIS EXISTS: `GET /runs` answers for the CALLING PRINCIPAL's plane,
    which is the right answer for a user and the wrong one for a host view.
    The desktop tray asked it and reported "no runs in the last 24 hours" on a
    machine that was mid-conversation -- the work was on the gateway's default
    plane, the tray's token on another. Memory, GPU and loaded models are all
    host-wide there; the run list has to be too or it is simply lying.

    CHEAP PLANES ONLY. The default plane and materialized user planes are
    plain store reads. ENTITY planes are not touched: reaching one goes
    through the entity registry, which opens homes and wires embedders -- work
    that must never ride a background poll. They are named in `skipped` so the
    payload says what it did not look at rather than implying it saw
    everything.

    WHAT IS A ROW (2026-10-04, the tray said "No runs in the last 24 hours"
    with three runs active): a TURN ROOT in the runtime's own sense
    (`is_turn_root`): parent-less non-controller runs AND automation
    occurrences, which have their controller as parent and were all dropped
    by the old parent-less test. Filtering happens inside the store before
    any limit, active runs are never windowed or cut by `limit`, and a root
    whose sub-run is running counts as running.
    """
    limit = max(1, min(int(limit or 25), 200))
    planes: List[Tuple[str, Any]] = []
    ledgers: Dict[str, Any] = {}
    warnings: List[str] = []
    skipped: List[str] = []

    try:
        stores = _stores_for_dir(Path(data_dir))
        planes.append(("default", default_run_store if default_run_store is not None else stores.run_store))
        ledgers["default"] = getattr(stores, "ledger_store", None)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"#FALLBACK default plane unreadable: {e}")

    users_root = Path(data_dir) / "users"
    if users_root.exists():
        try:
            for tenant_dir in sorted(users_root.iterdir()):
                if not tenant_dir.is_dir():
                    continue
                for rt_dir in sorted(tenant_dir.iterdir()):
                    runtime_dir = rt_dir / "runtime"
                    if not runtime_dir.is_dir():
                        continue
                    try:
                        plane = f"{tenant_dir.name}/{rt_dir.name}"
                        stores = _stores_for_dir(runtime_dir)
                        planes.append((plane, stores.run_store))
                        ledgers[plane] = getattr(stores, "ledger_store", None)
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"#FALLBACK user plane {tenant_dir.name}/{rt_dir.name} unreadable: {e}")
        except OSError as e:
            warnings.append(f"#FALLBACK users dir scan failed: {e}")

    entities_root = Path(data_dir) / "entities"
    if entities_root.exists():
        try:
            skipped = sorted(d.name for d in entities_root.iterdir() if d.is_dir())
        except OSError:
            skipped = []

    rows: List[Dict[str, Any]] = []
    for plane, store in planes:
        try:
            rows.extend(_runs_from_store(store, limit=limit, since_epoch=since_epoch, plane=plane))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"#FALLBACK runs unreadable on plane {plane}: {e}")

    # Running, waiting, finished ACROSS planes. Active rows are all kept (the
    # menu leads with them); finished ones fill the rest of `limit`.
    rows.sort(key=_host_row_order)
    active = [r for r in rows if r.get("activity") in {"running", "waiting"}]
    finished = [r for r in rows if r.get("activity") not in {"running", "waiting"}]
    more = len(finished) > max(0, limit - len(active))
    rows = active[:_ACTIVE_SCAN_LIMIT] + finished[: max(0, limit - len(active))]
    for row in rows:
        row["observer_path"] = _observer_path(row.get("run_id"))
    # Step counts LAST, on the survivors only: counting the ledger of every
    # run we then threw away is the expensive mistake this ordering avoids.
    for row in rows:
        row["ledger_len"] = _ledger_len(ledgers.get(str(row.get("plane"))), str(row.get("run_id") or ""))
    out: Dict[str, Any] = {
        "ok": True,
        "items": rows,
        "count": len(rows),
        "active_count": len(active),
        "has_more": more,
        "planes": [p for p, _ in planes],
    }
    if skipped:
        out["skipped_entity_planes"] = skipped
    if warnings:
        out["warnings"] = warnings
    return out


def runtime_runs(
    *,
    data_dir: Path,
    kind: str,
    tenant_id: str,
    runtime_id: str,
    limit: int = 50,
    offset: int = 0,
    default_run_store: Optional[Any] = None,
    entity_registry: Optional[Any] = None,
) -> Dict[str, Any]:
    """The drill-in: bounded most-recent-first run summaries for ONE runtime.
    Lazy by design — the list never pays this cost. Raises KeyError for an
    unknown plane (the route maps it to 404); entity maintenance holds
    propagate (409 at the route, same as every other held door)."""
    kind = str(kind or "").strip().lower()
    # Page size stays bounded; OFFSET is unbounded (operator ruling
    # 2026-08-19: every list must reach every item, pages of <=200).
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    want = offset + limit + 1  # +1 = honest has_more

    def _page(runs_list):
        rows = list(runs_list or [])
        more = len(rows) > offset + limit
        return [_run_summary(r) for r in rows[offset: offset + limit]], more

    if kind == "default":
        store = default_run_store
        if store is None:
            store = _stores_for_dir(Path(data_dir)).run_store
        items, more = _page(store.list_runs(limit=want))
        return {"kind": kind, "runtime_id": "default", "items": items, "offset": offset, "has_more": more}

    if kind == "user":
        from .service import safe_principal_component

        tenant = safe_principal_component(tenant_id, default="default")
        rid = safe_principal_component(runtime_id, default="")
        root = Path(data_dir) / "users" / tenant / rid
        runtime_dir = root / "runtime"
        if not rid or not runtime_dir.exists():
            raise KeyError(f"user runtime {tenant}/{runtime_id} has no materialized data plane")
        stores = _stores_for_dir(runtime_dir)
        items, more = _page(stores.run_store.list_runs(limit=want))
        return {"kind": kind, "tenant_id": tenant, "runtime_id": rid, "items": items, "offset": offset, "has_more": more}

    if kind == "entity":
        if entity_registry is None:
            from .entities import EntityRegistry

            entity_registry = EntityRegistry(data_dir=Path(data_dir))
        slug = str(runtime_id or "").strip().lower()
        slug = slug[len("runtime_"):] if slug.startswith("runtime_") else slug
        er = entity_registry.get_entity_runtime(slug)  # KeyError -> 404; hold -> 409 at the route
        items, more = _page(er.run_store.list_runs(limit=want))
        return {"kind": kind, "runtime_id": f"runtime_{slug}", "entity": slug, "items": items, "offset": offset, "has_more": more}

    raise KeyError(f"unknown runtime kind {kind!r} (default|user|entity)")
