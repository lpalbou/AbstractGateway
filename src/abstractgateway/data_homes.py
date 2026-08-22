"""Gateway data-home registration — the WRITER half of the Data & Caches
lane (operator priority 2026-07-13 18:19, agency c1580 ask 1; ruled split:
core owns the registry primitive, gateway registers its homes + serves the
ONE management view, observer/continuum take read-only telemetry).

Core's registry (abstractcore.utils.data_registry) was BUILT but had ZERO
writers — "no idea what is here and where I should dig" stayed true. This
module registers every gateway-owned data home so the registry answers it.

WHAT REGISTERS (per data root):
- artifacts store   -> kind=artifacts, safe_to_purge=FALSE: the offloading
  run/ledger stores resolve payloads from it — purging would amputate run
  history (load-bearing store, per the semantics c1302 per-store rule).
- every entity home -> kind=entity-home, safe_to_purge=FALSE BY CONSTRUCTION
  (the never-purge rule made registry-visible; c1297).
- logs              -> kind=logs, safe_to_purge=TRUE (regenerable).
- workspaces        -> kind=runs, safe_to_purge=FALSE in v1: per-run scratch
  MAY hold unique agent outputs; declaring it purgeable would let a cache
  cleanup eat work products. Visibility first; a purge posture needs an
  operator ruling, not a default.

HONEST GAPS (labeled, not silently absent): run_*.json / ledger_*.jsonl live
FLAT at the data root — a directory registry cannot register the root
without swallowing the entity homes (core's nesting guard refuses, and it is
right to). Those files stay visible through /runs, unregistered here until
they move under a dedicated directory (a layout change this wave does not
smuggle in).

All registration is BEST-EFFORT (core's ensure_* lane): a broken registry
must never break the gateway boot or an entity creation.

BOUNDARY (0059): gateway never imports abstractcore directly — registry
access flows through the runtime facade
(`abstractruntime.integrations.abstractcore.data_registry_facade`). Until
runtime ships it (asked, exact contract posted), every function here
degrades to a labeled no-op/None — the wave lights up the moment the
facade lands, zero gateway change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_OWNER = "abstractgateway"

FACADE_MISSING_NOTE = (
    "#FALLBACK data registry unavailable: abstractruntime does not ship "
    "integrations.abstractcore.data_registry_facade yet (boundary 0059 — "
    "gateway never imports abstractcore directly). Rows appear when the "
    "facade lands."
)


def _facade() -> Tuple[Optional[Any], Optional[str]]:
    """The runtime data-registry facade, or (None, labeled reason)."""
    try:
        from abstractruntime.integrations.abstractcore import data_registry_facade

        return data_registry_facade, None
    except Exception:
        return None, FACADE_MISSING_NOTE


def _ensure_fn() -> Optional[Callable[..., Any]]:
    facade, _ = _facade()
    if facade is None:
        return None
    fn = getattr(facade, "ensure_data_home_registered", None)
    return fn if callable(fn) else None


def _slug_component(data_dir: Path) -> str:
    """A short, stable per-data-root disambiguator for row names (two data
    roots must not fight over one row name — e.g. root runtime vs per-user
    runtimes)."""
    import hashlib

    return hashlib.sha256(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:8]


def register_gateway_data_homes(data_dir: Path) -> List[Dict[str, Any]]:
    """Register this data root's gateway-owned homes (idempotent, best-effort).

    Returns the rows registered BY THIS CALL (ensure dedups per process);
    callers use the return only for logging/tests.
    """
    ensure_data_home_registered = _ensure_fn()
    if ensure_data_home_registered is None:
        return []  # facade absent — labeled at the serving route

    base = Path(data_dir).expanduser().resolve()
    tag = _slug_component(base)
    out: List[Dict[str, Any]] = []

    def _reg(name: str, *, path: Path, kind: str, safe: bool, description: str, meta: Dict[str, Any] | None = None) -> None:
        if not path.is_dir():
            return  # register only homes that exist — no phantom rows
        row = ensure_data_home_registered(
            name, path=str(path), kind=kind, owner=_OWNER,
            safe_to_purge=safe, description=description, meta=meta,
        )
        if row is not None:
            out.append(row.to_dict() if hasattr(row, "to_dict") else dict(row))

    _reg(
        f"gateway-artifacts-{tag}", path=base / "artifacts", kind="artifacts", safe=False,
        description=(
            "Gateway artifact store (content-addressed): run media PLUS offloaded run/ledger "
            "payloads — the run history resolves through it, so purging amputates replay. "
            "Load-bearing; pruning is a designed lifecycle act, never a cache cleanup."
        ),
        meta={"data_root": str(base)},
    )
    _reg(
        f"gateway-logs-{tag}", path=base / "logs", kind="logs", safe=True,
        description="Gateway serving/launcher logs — regenerable; safe to purge.",
        meta={"data_root": str(base)},
    )
    _reg(
        f"gateway-workspaces-{tag}", path=base / "workspaces", kind="runs", safe=False,
        description=(
            "Per-run default workspaces (agent working directories). May hold unique agent "
            "outputs — review manually; declaring this purgeable needs an operator ruling."
        ),
        meta={"data_root": str(base)},
    )

    # Every entity home: one row per LIFE, safe_to_purge=False by construction.
    entities_dir = base / "entities"
    if entities_dir.is_dir():
        for home in sorted(p for p in entities_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            _reg(
                f"gateway-entity-{home.name}-{tag}", path=home, kind="entity-home", safe=False,
                description=(
                    f"Entity home for {home.name!r} — a LIFE (memory + book + spark + artifacts "
                    "+ runtime). Never purged through the registry by construction; entity "
                    "lifecycle is the door's, not a cleanup's."
                ),
                meta={"data_root": str(base), "slug": home.name},
            )

    return out


def register_entity_home_on_create(data_dir: Path, slug: str) -> None:
    """Register-at-first-write for a NEWBORN entity home (called by the
    create path after the manifest lands). Best-effort, never raises."""
    ensure_data_home_registered = _ensure_fn()
    if ensure_data_home_registered is None:
        return
    base = Path(data_dir).expanduser().resolve()
    home = base / "entities" / slug
    if not home.is_dir():
        return
    ensure_data_home_registered(
        f"gateway-entity-{slug}-{_slug_component(base)}",
        path=str(home), kind="entity-home", owner=_OWNER, safe_to_purge=False,
        description=(
            f"Entity home for {slug!r} — a LIFE (memory + book + spark + artifacts + runtime). "
            "Never purged through the registry by construction."
        ),
        meta={"data_root": str(base), "slug": slug},
    )


def list_homes(*, include_sizes: bool = True) -> Tuple[List[Dict[str, Any]], List[str]]:
    """All registered rows via the facade, (rows, warnings). With
    `include_sizes=False` the registry answers WITHOUT walking any tree —
    the fast first paint for consoles (the full walk over large stores can
    take tens of seconds; sizes then arrive in a second, sized call)."""
    facade, reason = _facade()
    if facade is None:
        return [], [reason or FACADE_MISSING_NOTE]
    fn = getattr(facade, "list_data_homes", None)
    if not callable(fn):
        return [], [FACADE_MISSING_NOTE]
    try:
        rows = list(fn(include_sizes=include_sizes) or [])
        # Core stamps `exists` only on sized rows; the fast pass needs the
        # same honesty (a console must say "missing", never "?"). Cheap:
        # one is_dir per row, no walk.
        for row in rows:
            if isinstance(row, dict) and "exists" not in row:
                try:
                    row["exists"] = Path(str(row.get("path") or "")).is_dir()
                except Exception:
                    row["exists"] = False
        return rows, []
    except Exception as e:  # noqa: BLE001 - a broken registry degrades labeled
        return [], [f"#FALLBACK data registry unreadable: {e}"]


def list_homes_with_sizes() -> Tuple[List[Dict[str, Any]], List[str]]:
    """All registered rows with live sizes, via the facade. (rows, warnings)."""
    return list_homes(include_sizes=True)


def forget_home(name: str) -> bool:
    """Remove ONE registry ROW (disk untouched) — the stale-registration
    cleanup. Raises RuntimeError when the facade predates unregister (older
    abstractruntime): callers degrade to label-only UI."""
    facade, reason = _facade()
    if facade is None:
        raise RuntimeError(reason or FACADE_MISSING_NOTE)
    fn = getattr(facade, "unregister_data_home", None)
    if not callable(fn):
        raise RuntimeError(
            "this abstractruntime predates unregister_data_home — update it to forget stale rows"
        )
    return bool(fn(name))


def purge_home(name: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Purge (or dry-run) one registered home via the facade. Refusals from
    the registry (unknown row, owner-protected, symlink swap) propagate
    VERBATIM — the console renders them, never rewrites them."""
    facade, reason = _facade()
    if facade is None:
        raise RuntimeError(reason or FACADE_MISSING_NOTE)
    fn = getattr(facade, "purge_data_home", None)
    if not callable(fn):
        raise RuntimeError(FACADE_MISSING_NOTE)
    return fn(name, dry_run=bool(dry_run))
