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


# ---------------------------------------------------------------------------
# The desktop tray's Sessions menu (2026-10-15): a row carries the SESSION's
# title and, when it waits, WHAT it waits for -- typed from the wait record's
# structure, never from its text.
# ---------------------------------------------------------------------------

TITLE_MAX = 60  # a menu row; the Observer's card clamps the same text at 80
WAIT_SUMMARY_MAX = 120
#: The wait kinds a person must act on; every other kind stays "running".
ATTENTION_WAIT_KINDS = ("tool_approval", "ask_user")
#: Where a turn root keeps the text a person typed: the start `input_data`
#: keys (`run.vars`), in the order the Observer reads them.
_PROMPT_KEYS = ("prompt", "message", "task")


def _first_line(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    line = s.splitlines()[0].strip() if s else ""
    if len(line) <= limit:
        return line
    return line[: max(1, limit - 1)].rstrip() + "…"


def _load_run(store: Any, run_id: str) -> Optional[Any]:
    if store is None or not run_id:
        return None
    try:
        return store.load(run_id)
    except Exception:
        return None


def _run_vars(run: Any) -> Dict[str, Any]:
    vars_obj = getattr(run, "vars", None)
    return vars_obj if isinstance(vars_obj, dict) else {}


def _automation_title(store: Any, automation_id: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """`vars._meta.automation.title` of the controller run, loaded once per listing."""
    if automation_id in cache:
        return cache[automation_id]
    controller = _load_run(store, automation_id)
    meta = _run_vars(controller).get("_meta") if controller is not None else None
    definition = meta.get("automation") if isinstance(meta, dict) and isinstance(meta.get("automation"), dict) else {}
    title = definition.get("title")
    cache[automation_id] = str(title).strip() if isinstance(title, str) and title.strip() else None
    return cache[automation_id]


def run_title(run: Any, row: Dict[str, Any], *, store: Any, controllers: Dict[str, Optional[str]]) -> Tuple[str, str]:
    """(title, title_source): the automation's title for an occurrence
    ("automation"); else the first line of the prompt a person typed, cut at
    `TITLE_MAX` ("prompt"); else the workflow's display label ("workflow").
    An occurrence's own prompt is never used: it carries the trigger's framing
    header, not what the person asked (the Observer's rule)."""
    meta = _run_vars(run).get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    occurrence = meta.get("occurrence") if isinstance(meta.get("occurrence"), dict) else None
    if occurrence is not None:
        automation_id = str(occurrence.get("automation_id") or "").strip()
        title = _automation_title(store, automation_id, controllers) if automation_id else None
        if title:
            return _first_line(title, TITLE_MAX), "automation"
    else:
        vars_obj = _run_vars(run)
        for key in _PROMPT_KEYS:
            value = vars_obj.get(key)
            if isinstance(value, str) and value.strip():
                line = _first_line(value, TITLE_MAX)
                if line:
                    return line, "prompt"
    label = str(row.get("label") or workflow_display_label(row.get("workflow_id")) or row.get("workflow_id") or "").strip()
    return (label or str(row.get("run_id") or "")), "workflow"


def _wait_reason(run: Any) -> Optional[str]:
    waiting = getattr(run, "waiting", None)
    reason = getattr(waiting, "reason", None) if waiting is not None else None
    if reason is None:
        return None
    return str(getattr(reason, "value", None) or reason)


def _wait_holder(store: Any, run: Any) -> Any:
    """The run that holds a waiting root's wait: the root itself, or -- when it
    waits on its sub-workflow -- the newest-updated waiting descendant, found
    one `list_children(status=waiting)` per level. A sub-run's approval
    belongs to the parent row."""
    from abstractruntime.core.models import RunStatus

    cur = run
    seen: set = set()
    for _ in range(_ROOT_WALK_MAX_DEPTH):
        if _wait_reason(cur) != "subworkflow":
            return cur
        rid = str(getattr(cur, "run_id", "") or "")
        if not rid or rid in seen:
            return cur
        seen.add(rid)
        try:
            children = list(store.list_children(parent_run_id=rid, status=RunStatus.WAITING) or [])
        except Exception:
            return cur
        waiting = [c for c in children if getattr(c, "waiting", None) is not None]
        if not waiting:
            return cur
        cur = max(waiting, key=lambda c: str(getattr(c, "updated_at", "") or ""))
    return cur


def _tool_calls_of(details: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`[{name, arguments, call_id?}]` from a tool-approval wait record (the
    runtime's `details.tool_calls`), arguments in full."""
    out: List[Dict[str, Any]] = []
    for call in details.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        item: Dict[str, Any] = {
            "name": str(call.get("name") or ""),
            "arguments": dict(call.get("arguments")) if isinstance(call.get("arguments"), dict) else {},
        }
        call_id = call.get("call_id") or call.get("id")
        if isinstance(call_id, str) and call_id:
            item["call_id"] = call_id
        out.append(item)
    return out


def wait_of_run(holder: Any) -> Dict[str, Any]:
    """`{kind, summary, tool, wait_key, run_id}` of a waiting run, typed from
    the wait record's STRUCTURE: `paused` (the run was paused: the runtime's
    pause flag or `pause:<run_id>` key), `tool_approval` (`details.mode ==
    "approval_required"`, the TOOL_CALLS approval wait), `ask_user` (any other
    USER wait), `event`, else `other` (a timer, a job, a sub-run). `summary`
    is what is asked for the two attention kinds, else the "waiting for …"
    phrase; `tool` names the first call awaiting approval (`+N` when more);
    `run_id` is the run that HOLDS the wait."""
    import json as _json

    from abstractruntime.core.vars import is_paused_vars

    waiting = getattr(holder, "waiting", None)
    run_id = str(getattr(holder, "run_id", "") or "")
    wait_key = getattr(waiting, "wait_key", None) if waiting is not None else None
    wait_key = str(wait_key) if isinstance(wait_key, str) and wait_key else None
    details = getattr(waiting, "details", None) if waiting is not None else None
    details = details if isinstance(details, dict) else {}
    reason = _wait_reason(holder)
    out: Dict[str, Any] = {"kind": "other", "summary": "", "tool": None, "wait_key": wait_key, "run_id": run_id}
    if is_paused_vars(_run_vars(holder)) or (wait_key is not None and wait_key == f"pause:{run_id}") or details.get("kind") == "pause":
        out.update(kind="paused", summary="paused")
    elif reason == "user" and details.get("mode") == "approval_required":
        calls = _tool_calls_of(details)
        names = [c["name"] for c in calls if c.get("name")]
        tool = (names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")) if names else None
        first = calls[0] if calls else {}
        args = _json.dumps(first.get("arguments") or {}, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
        out.update(kind="tool_approval", summary=_first_line(f"{first.get('name') or 'tool call'} {args}", WAIT_SUMMARY_MAX), tool=tool)
    elif reason == "user":
        out.update(kind="ask_user", summary=_first_line(getattr(waiting, "prompt", None), WAIT_SUMMARY_MAX) or "a question")
    elif reason == "event":
        # The runtime's event key is `evt:<scope>:<scope id>:<name>`; the name is the last part.
        name = wait_key.split(":", 3)[3] if (wait_key and wait_key.startswith("evt:") and wait_key.count(":") >= 3) else None
        out.update(kind="event", summary=f"waiting for {name or wait_key or 'an event'}")
    elif reason == "until":
        out.update(summary="waiting for a timer")
    elif reason == "subworkflow":
        out.update(summary="waiting for a sub-run")
    elif reason == "job":
        out.update(summary="waiting for a job")
    else:
        out.update(summary=f"waiting for {reason or 'something'}")
    return out


def enrich_host_rows(rows: List[Dict[str, Any]], stores: Dict[str, Any]) -> None:
    """`title`, `title_source` and (waiting rows) `wait` on the SURVIVING rows
    of the host listing. Cost: one run-document load per row, one controller
    load per automation per plane, and for a root waiting on its sub-run one
    `list_children(status=waiting)` per level down to the wait's holder."""
    controllers: Dict[str, Dict[str, Optional[str]]] = {}
    for row in rows:
        plane = str(row.get("plane") or "")
        store = stores.get(plane)
        run = _load_run(store, str(row.get("run_id") or ""))
        row["title"], row["title_source"] = run_title(run, row, store=store, controllers=controllers.setdefault(plane, {}))
        if row.get("activity") == "waiting" and run is not None and getattr(run, "waiting", None) is not None:
            row["wait"] = wait_of_run(_wait_holder(store, run))


def _cheap_plane_stores(data_dir: Path, default_run_store: Optional[Any]) -> List[Tuple[str, Any]]:
    """(plane, run store) for the planes `recent_runs_host_wide` reads: the
    default plane and every materialized user plane. Entity planes are never
    opened (see there)."""
    planes: List[Tuple[str, Any]] = []
    try:
        stores = _stores_for_dir(Path(data_dir))
        planes.append(("default", default_run_store if default_run_store is not None else stores.run_store))
    except Exception:  # noqa: BLE001 - an unreadable default plane leaves the user planes readable
        pass
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
                        planes.append((f"{tenant_dir.name}/{rt_dir.name}", _stores_for_dir(runtime_dir).run_store))
                    except Exception:  # noqa: BLE001
                        continue
        except OSError:
            pass
    return planes


def host_run_wait(*, data_dir: Path, run_id: str, default_run_store: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """The pending wait of ONE run on this machine, for the tray's approval
    dialog: `{run_id, plane, waiting: true, kind, summary, tool, wait_key,
    reason, prompt?, choices?, tool_calls: [{name, arguments, call_id?}]}`
    with the arguments IN FULL; `{run_id, plane, waiting: false, status}` when
    the run is not waiting; None when no cheap plane holds it."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    for plane, store in _cheap_plane_stores(Path(data_dir), default_run_store):
        run = _load_run(store, rid)
        if run is None:
            continue
        status = getattr(run, "status", None)
        status_s = str(getattr(status, "value", None) or status or "")
        waiting = getattr(run, "waiting", None)
        if status_s != "waiting" or waiting is None:
            return {"run_id": rid, "plane": plane, "waiting": False, "status": status_s}
        out: Dict[str, Any] = {"run_id": rid, "plane": plane, "waiting": True, **wait_of_run(run), "reason": _wait_reason(run)}
        details = waiting.details if isinstance(getattr(waiting, "details", None), dict) else {}
        out["tool_calls"] = _tool_calls_of(details) if out["kind"] == "tool_approval" else []
        if getattr(waiting, "prompt", None):
            out["prompt"] = str(waiting.prompt)
        if getattr(waiting, "choices", None):
            out["choices"] = [str(c) for c in waiting.choices]
        return out
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
    # The session's title and what a waiting row waits for: on the survivors
    # only, for the same reason (one document load per row).
    enrich_host_rows(rows, dict(planes))
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
