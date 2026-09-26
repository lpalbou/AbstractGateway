"""Default agent workflow per agent interface (setting `agents.default_workflow`).

One gateway-wide setting maps an agent INTERFACE id (what a client speaks,
e.g. `abstractcode.agent.v1`) to the workflow that answers it on this
gateway, written `[scope:]bundle[@version]:flow` where scope is `private`
(the gateway's own workflows, the default) or `catalog` (the tenant
catalog); no version = the latest published version at each start. Clients
that pick "Gateway default" send `flow_id: "@default"` + `interface` and the
gateway resolves it here, at every start, so a change applies to the next
new turn.

Resolution never falls back silently: a saved value that no longer resolves
(bundle removed, flow missing, entrypoint deprecated, interface not declared)
is reported `available: false` with the reason, and a `@default` start
refuses with 409 naming the setting and its source. It never quietly runs
the built-in default instead.

Precedence: stored value (`source: "stored"`) > built-in default
(`source: "default"`). There is no launch flag and no environment rung for
this setting.

Built-in defaults:
- `abstractcode.agent.v1`       -> the default entrypoint of the shipped
                                   `basic-agent` bundle (latest version) when it
                                   is on this gateway, else unavailable;
- `abstractassistant.agent.v1`  -> none: the Assistant's own orchestrator is
                                   per user, so unset = unavailable and the
                                   Assistant runs its built-in orchestrator;
- any other interface           -> unavailable until an admin saves one.

The resolver works on an ENTRYPOINT INDEX (a list of plain rows), built
either from a live bundle host (`host_entrypoint_index`) or from a flows
folder on disk (`disk_entrypoint_index`, used by the CLI when no gateway is
serving the data dir). That keeps the rules in one place for every door.

This setting is different from three older "default" fields:
- `/bundles` `default_bundle_id`: the single private bundle a bare
  `flow_id` falls back to when exactly one bundle is loaded;
- `/workflow-catalog` `is_default`: the default VERSION of a catalog bundle;
- `default_entrypoint` of a bundle: which entrypoint a bundle-only start
  runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

SETTING_KEY = "agents.default_workflow"
DEFAULT_SENTINEL = "@default"
CODE_AGENT_INTERFACE = "abstractcode.agent.v1"
ASSISTANT_AGENT_INTERFACE = "abstractassistant.agent.v1"
# Interfaces always listed (with their built-in default), whatever is loaded.
ALWAYS_LISTED_INTERFACES = (CODE_AGENT_INTERFACE, ASSISTANT_AGENT_INTERFACE)
# interface -> bundle whose default entrypoint is the built-in default.
BUILTIN_DEFAULT_BUNDLES: Dict[str, str] = {
    CODE_AGENT_INTERFACE: "basic-agent",
}
# interface -> the sentence an unset value reports when there is no built-in.
UNSET_REASONS: Dict[str, str] = {
    ASSISTANT_AGENT_INTERFACE: (
        "no host workflow declares abstractassistant.agent.v1; the Assistant uses its built-in orchestrator"
    ),
}
SCOPE_WORDS = {"private": "private", "catalog": "tenant_catalog"}

REGISTRY_PRIVATE = "private"
REGISTRY_TENANT_CATALOG = "tenant_catalog"


class DefaultWorkflowError(ValueError):
    """A rejected `agents.default_workflow.<interface>` value (the reason is
    operator-readable; routes map it to 400)."""


@dataclass(frozen=True)
class Resolved:
    interface: str
    value: str
    source: str
    bundle_id: str
    bundle_version: str
    flow_id: str
    registry_scope: str
    name: str

    @property
    def available(self) -> bool:
        return True

    @property
    def workflow_id(self) -> str:
        return f"{self.bundle_id}@{self.bundle_version}:{self.flow_id}"

    def resolved_dict(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "bundle_version": self.bundle_version,
            "flow_id": self.flow_id,
            "registry_scope": self.registry_scope,
            "workflow_id": self.workflow_id,
            "name": self.name,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source, "available": True, "reason": None, "resolved": self.resolved_dict()}


@dataclass(frozen=True)
class Unavailable:
    interface: str
    value: Optional[str]
    source: str
    reason: str

    @property
    def available(self) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source, "available": False, "reason": self.reason, "resolved": None}


Resolution = Union[Resolved, Unavailable]


# ---------------------------------------------------------------- parsing


def parse_workflow_ref(raw: Any) -> tuple[str, str, Optional[str], str]:
    """`[scope:]bundle[@version]:flow` -> (registry_scope, bundle_id,
    version | None, flow_id); scope `private` (default) or `catalog`.
    Raises DefaultWorkflowError in words."""
    text = str(raw if raw is not None else "").strip()
    hint = "write [private:|catalog:]bundle[@version]:flow, e.g. basic-agent:81795ea9 or coding-agent@0.2.7:coder"
    parts = [p.strip() for p in text.split(":")]
    if len(parts) == 3:
        scope_word, prefix, flow_id = parts
        if scope_word not in SCOPE_WORDS:
            raise DefaultWorkflowError(f"{text!r}: unknown scope {scope_word!r} (private or catalog); {hint}")
        scope = SCOPE_WORDS[scope_word]
    elif len(parts) == 2:
        scope, (prefix, flow_id) = SCOPE_WORDS["private"], parts
    else:
        raise DefaultWorkflowError(f"{text!r} is not a workflow reference; {hint}")
    bundle_id, at, version = prefix.partition("@")
    bundle_id, version = bundle_id.strip(), version.strip()
    if not bundle_id or not flow_id or (at and not version) or "@" in version:
        raise DefaultWorkflowError(f"{text!r} is not a workflow reference; {hint}")
    return scope, bundle_id, (version or None), flow_id


def format_workflow_ref(bundle_id: str, version: Optional[str], flow_id: str, registry_scope: str = "private") -> str:
    ref = f"{bundle_id}@{version}:{flow_id}" if version else f"{bundle_id}:{flow_id}"
    return ref if registry_scope == REGISTRY_PRIVATE else f"catalog:{ref}"


def _is_draft(version: Any) -> bool:
    return str(version or "").strip().lower().startswith("draft.")


def _semver_key(version: str) -> tuple:
    parts: List[Any] = []
    for bit in str(version or "").replace("-", ".").split("."):
        parts.append((0, int(bit)) if bit.isdigit() else (1, bit))
    return tuple(parts)


# ---------------------------------------------------------------- the index
#
# One row per (bundle, version, entrypoint):
# {bundle_id, bundle_version, flow_id, name, interfaces[], deprecated,
#  deprecated_reason, registry_scope, default_entrypoint, is_latest}


def _row(
    *,
    bundle_id: str,
    bundle_version: str,
    ep: Any,
    default_entrypoint: str,
    registry_scope: str,
    deprecated: bool,
    deprecated_reason: Optional[str],
) -> Dict[str, Any]:
    get = ep.get if isinstance(ep, dict) else (lambda k, d=None: getattr(ep, k, d))
    flow_id = str(get("flow_id", "") or "").strip()
    return {
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "flow_id": flow_id,
        "name": str(get("name", "") or "").strip() or flow_id,
        "interfaces": [str(i).strip() for i in (get("interfaces", None) or []) if str(i).strip()],
        "deprecated": bool(deprecated),
        "deprecated_reason": deprecated_reason,
        "registry_scope": registry_scope,
        "default_entrypoint": default_entrypoint,
        "is_latest": False,
    }


def _mark_latest(rows: List[Dict[str, Any]], latest: Dict[tuple, str]) -> List[Dict[str, Any]]:
    for r in rows:
        r["is_latest"] = latest.get((r["registry_scope"], r["bundle_id"])) == r["bundle_version"]
    return rows


def host_entrypoint_index(
    host: Any,
    *,
    principal: Any = None,
    catalog_store: Any = None,
    tenant_id: str = "default",
) -> List[Dict[str, Any]]:
    """Entrypoints a live bundle host serves: the PRIVATE registry (drafts
    excluded — a draft only runs as a draft test), then the tenant catalog
    records `principal` may run (when a catalog store is given)."""
    rows: List[Dict[str, Any]] = []
    latest: Dict[tuple, str] = {}
    bundles = getattr(host, "bundles", None)
    if not isinstance(bundles, dict):
        raise RuntimeError("this gateway does not serve workflow bundles, so no agent workflow can resolve")
    sources = getattr(host, "bundle_sources", None) or {}
    dep_store = getattr(host, "deprecation_store", None)
    host_latest = getattr(host, "latest_bundle_versions", None) or {}
    for bid, versions in bundles.items():
        if not isinstance(versions, dict):
            continue
        served: List[str] = []
        for ver, bundle in versions.items():
            ver = str(ver or "").strip()
            if not ver or _is_draft(ver):
                continue
            meta = ((sources.get(str(bid)) or {}).get(ver) if isinstance(sources, dict) else None) or {}
            if isinstance(meta, dict) and str(meta.get("registry_scope") or REGISTRY_PRIVATE) != REGISTRY_PRIVATE:
                continue  # catalog copies are listed from the catalog below, under their public id
            man = getattr(bundle, "manifest", None)
            if man is None:
                continue
            served.append(ver)
            default_ep = str(getattr(man, "default_entrypoint", "") or "").strip()
            eps = list(getattr(man, "entrypoints", None) or [])
            if not default_ep and len(eps) == 1:
                default_ep = str(getattr(eps[0], "flow_id", "") or "").strip()
            for ep in eps:
                fid = str(getattr(ep, "flow_id", "") or "").strip()
                if not fid:
                    continue
                rec = None
                if dep_store is not None:
                    try:
                        rec = dep_store.get_record(bundle_id=str(bid), flow_id=fid)
                    except Exception:  # noqa: BLE001 - an unreadable store is reported as not deprecated
                        rec = None
                rows.append(
                    _row(
                        bundle_id=str(bid),
                        bundle_version=ver,
                        ep=ep,
                        default_entrypoint=default_ep,
                        registry_scope=REGISTRY_PRIVATE,
                        deprecated=rec is not None,
                        deprecated_reason=(str(rec.get("reason") or "").strip() or None) if isinstance(rec, dict) else None,
                    )
                )
        if served:
            want = str(host_latest.get(str(bid)) or "").strip()
            latest[(REGISTRY_PRIVATE, str(bid))] = want if want in served else max(served, key=_semver_key)
    if catalog_store is not None:
        try:
            from .workflow_catalog import CATALOG_SCOPE_TENANT

            records = catalog_store.list_records(principal=principal, scope=CATALOG_SCOPE_TENANT, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 - the private registry still answers; say why the catalog did not
            logger.warning("default agent workflow: the tenant catalog could not be read (%s)", exc)
            records = []
        for rec in records:
            bid = str(rec.get("bundle_id") or "").strip()
            ver = str(rec.get("bundle_version") or "").strip()
            if not bid or not ver:
                continue
            default_ep = str(rec.get("default_entrypoint") or "").strip()
            eps = rec.get("entrypoints") if isinstance(rec.get("entrypoints"), list) else []
            if not default_ep and len(eps) == 1 and isinstance(eps[0], dict):
                default_ep = str(eps[0].get("flow_id") or "").strip()
            for ep in eps:
                if isinstance(ep, dict) and str(ep.get("flow_id") or "").strip():
                    rows.append(
                        _row(
                            bundle_id=bid,
                            bundle_version=ver,
                            ep=ep,
                            default_entrypoint=default_ep,
                            registry_scope=REGISTRY_TENANT_CATALOG,
                            deprecated=False,
                            deprecated_reason=None,
                        )
                    )
            if rec.get("is_default"):
                latest[(REGISTRY_TENANT_CATALOG, bid)] = ver
    return _mark_latest(rows, latest)


def disk_entrypoint_index(flows_dirs: Iterable[Path]) -> List[Dict[str, Any]]:
    """Entrypoints of the `.flow` files in `flows_dirs` (private registry),
    for the CLI when no gateway serves the data dir. Unreadable files are
    skipped with a warning, like the host skips them."""
    from abstractruntime.workflow_bundle import open_workflow_bundle

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    versions_by_bundle: Dict[str, List[str]] = {}
    for d in flows_dirs:
        directory = Path(d)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.flow")):
            try:
                man = open_workflow_bundle(path).manifest
            except Exception as exc:  # noqa: BLE001
                logger.warning("default agent workflow: %s is not loadable (%s)", path, exc)
                continue
            bid = str(getattr(man, "bundle_id", "") or "").strip()
            ver = str(getattr(man, "bundle_version", "") or "").strip()
            if not bid or not ver or _is_draft(ver) or (bid, ver) in seen:
                continue
            seen.add((bid, ver))
            versions_by_bundle.setdefault(bid, []).append(ver)
            eps = list(getattr(man, "entrypoints", None) or [])
            default_ep = str(getattr(man, "default_entrypoint", "") or "").strip()
            if not default_ep and len(eps) == 1:
                default_ep = str(getattr(eps[0], "flow_id", "") or "").strip()
            for ep in eps:
                if str(getattr(ep, "flow_id", "") or "").strip():
                    rows.append(
                        _row(
                            bundle_id=bid,
                            bundle_version=ver,
                            ep=ep,
                            default_entrypoint=default_ep,
                            registry_scope=REGISTRY_PRIVATE,
                            deprecated=False,
                            deprecated_reason=None,
                        )
                    )
    latest = {(REGISTRY_PRIVATE, bid): max(vs, key=_semver_key) for bid, vs in versions_by_bundle.items()}
    return _mark_latest(rows, latest)


# ---------------------------------------------------------------- resolution


def stored_default_workflows(data_dir: Path) -> Dict[str, str]:
    """{interface: "bundle[@ver]:flow"} as saved in the runtime-config store."""
    from .runtime_config import _read_store

    agents = _read_store(Path(data_dir)).get("agents")
    mapping = agents.get("default_workflow") if isinstance(agents, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items() if isinstance(v, str) and v.strip()}


def known_interfaces(index: List[Dict[str, Any]], stored: Optional[Dict[str, str]] = None) -> List[str]:
    """The two framework agent interfaces, every interface a loaded
    entrypoint declares, and every interface with a saved value."""
    others = set()
    for r in index:
        others.update(r.get("interfaces") or [])
    others.update((stored or {}).keys())
    return list(ALWAYS_LISTED_INTERFACES) + sorted(others - set(ALWAYS_LISTED_INTERFACES))


def offline_entrypoint_index() -> List[Dict[str, Any]]:
    """The index of the flows folder the gateway would serve from (same
    resolution as `serve`), for a door that runs without a live gateway."""
    from .config import GatewayHostConfig

    cfg = GatewayHostConfig.from_env()
    dirs = [Path(cfg.flows_dir)]
    fw = getattr(cfg, "framework_flows_dir", None)
    if fw is not None and Path(fw) != Path(cfg.flows_dir):
        dirs.append(Path(fw))
    return disk_entrypoint_index(dirs)


def eligible_entrypoints(index: List[Dict[str, Any]], interface: str) -> List[Dict[str, Any]]:
    """Latest-version, non-deprecated entrypoints declaring `interface` —
    what a settings picker offers. Each carries the version-less `value`
    (follows new versions) and the exact `workflow_id`."""
    out = []
    for r in index:
        if not r.get("is_latest") or r.get("deprecated") or interface not in (r.get("interfaces") or []):
            continue
        out.append(
            {
                "value": format_workflow_ref(r["bundle_id"], None, r["flow_id"], r["registry_scope"]),
                "workflow_id": f"{r['bundle_id']}@{r['bundle_version']}:{r['flow_id']}",
                "bundle_id": r["bundle_id"],
                "bundle_version": r["bundle_version"],
                "flow_id": r["flow_id"],
                "name": r["name"],
                "registry_scope": r["registry_scope"],
            }
        )
    out.sort(key=lambda e: (e["registry_scope"] != REGISTRY_PRIVATE, e["bundle_id"], e["flow_id"]))
    return out


def _builtin_value(index: List[Dict[str, Any]], interface: str) -> tuple[Optional[str], Optional[str]]:
    """(value, reason-when-none) of the built-in default for `interface`."""
    bid = BUILTIN_DEFAULT_BUNDLES.get(interface)
    if not bid:
        reason = UNSET_REASONS.get(interface) or (
            f"no default workflow is set for {interface} (an admin sets {SETTING_KEY}.{interface})"
        )
        return None, reason
    latest = [r for r in index if r["bundle_id"] == bid and r["registry_scope"] == REGISTRY_PRIVATE and r.get("is_latest")]
    if not latest:
        return None, f"not set, and the built-in default bundle '{bid}' is not on this gateway"
    default_ep = latest[0].get("default_entrypoint") or ""
    if not default_ep:
        return None, f"not set, and the built-in default bundle '{bid}' names no default entrypoint"
    return format_workflow_ref(bid, None, default_ep), None


def resolve_ref(index: List[Dict[str, Any]], interface: str, value: str, *, source: str) -> Resolution:
    """Resolve one `[scope:]bundle[@ver]:flow` value for `interface` on
    `index`, strictly inside its scope."""
    try:
        scope, bid, ver, fid = parse_workflow_ref(value)
    except DefaultWorkflowError as exc:
        return Unavailable(interface, value, source, str(exc))
    where = "this gateway" if scope == REGISTRY_PRIVATE else "this gateway's tenant catalog"
    rows = [r for r in index if r["bundle_id"] == bid and r["registry_scope"] == scope]
    if not rows:
        return Unavailable(interface, value, source, f"workflow bundle '{bid}' is not on {where}")
    if ver:
        at = [r for r in rows if r["bundle_version"] == ver]
        if not at:
            have = sorted({r["bundle_version"] for r in rows}, key=_semver_key)
            return Unavailable(interface, value, source, f"'{bid}' has no published version {ver} on {where} (it has {', '.join(have)})")
    else:
        at = [r for r in rows if r.get("is_latest")]
        if not at:
            return Unavailable(interface, value, source, f"'{bid}' has no published default version on {where}")
    ep = next((r for r in at if r["flow_id"] == fid), None)
    if ep is None:
        return Unavailable(
            interface, value, source,
            f"'{bid}@{at[0]['bundle_version']}' has no entrypoint '{fid}' (it has {', '.join(sorted(r['flow_id'] for r in at))})",
        )
    if ep.get("deprecated"):
        why = f": {ep['deprecated_reason']}" if ep.get("deprecated_reason") else ""
        return Unavailable(interface, value, source, f"'{bid}:{fid}' is deprecated on this gateway{why}")
    if interface not in (ep.get("interfaces") or []):
        declared = ", ".join(ep.get("interfaces") or []) or "no interface"
        return Unavailable(interface, value, source, f"'{bid}@{ep['bundle_version']}:{fid}' declares {declared}, not {interface}")
    return Resolved(
        interface=interface,
        value=value,
        source=source,
        bundle_id=bid,
        bundle_version=ep["bundle_version"],
        flow_id=fid,
        registry_scope=ep["registry_scope"],
        name=ep["name"],
    )


def resolve_default_agent_workflow(
    interface: str,
    *,
    index: List[Dict[str, Any]],
    data_dir: Optional[Path] = None,
    stored: Optional[Dict[str, str]] = None,
) -> Resolution:
    """THE resolver: saved value > built-in default; never a silent fallback
    from a saved value that does not resolve."""
    iface = str(interface or "").strip()
    if not iface:
        return Unavailable("", None, "default", "an agent interface id is required (e.g. abstractcode.agent.v1)")
    if stored is None:
        stored = stored_default_workflows(data_dir) if data_dir is not None else {}
    saved = stored.get(iface)
    if saved:
        return resolve_ref(index, iface, saved, source="stored")
    value, reason = _builtin_value(index, iface)
    if value is None:
        declaring = eligible_entrypoints(index, iface)
        if iface in UNSET_REASONS and declaring:
            # The contract sentence says no host workflow declares it; on a
            # gateway where some do, say that none is chosen instead.
            reason = (
                f"no default is set for {iface} ({len(declaring)} workflow(s) on this gateway declare it; an admin "
                f"chooses one with {SETTING_KEY}.{iface}); the Assistant uses its built-in orchestrator"
            )
        return Unavailable(iface, None, "default", str(reason))
    return resolve_ref(index, iface, value, source="default")


def validate_default_workflow_value(interface: str, value: Any, *, index: List[Dict[str, Any]]) -> str:
    """The write-time check (every door): the value must resolve on this
    gateway AND its entrypoint must declare `interface`. Returns the value
    to store (as written: a version-less value keeps following new versions)."""
    iface = str(interface or "").strip()
    if not iface or any(c.isspace() for c in iface):
        raise DefaultWorkflowError(f"{SETTING_KEY}.<interface> needs an interface id such as {CODE_AGENT_INTERFACE} (got {interface!r})")
    text = str(value if value is not None else "").strip()
    scope, bid, ver, fid = parse_workflow_ref(text)
    normalized = format_workflow_ref(bid, ver, fid, scope)
    res = resolve_ref(index, iface, normalized, source="stored")
    if isinstance(res, Unavailable):
        raise DefaultWorkflowError(f"{SETTING_KEY}.{iface} = {text!r} refused: {res.reason}")
    return normalized


def default_workflows_payload(index: List[Dict[str, Any]], data_dir: Path) -> Dict[str, Any]:
    """The `agents` block of GET /admin/runtime-config."""
    stored = stored_default_workflows(data_dir)
    out: Dict[str, Any] = {}
    for iface in known_interfaces(index, stored):
        row = resolve_default_agent_workflow(iface, index=index, stored=stored).to_dict()
        builtin, _ = _builtin_value(index, iface)
        row.update(
            {
                "key": f"{SETTING_KEY}.{iface}",
                "default": builtin,
                "eligible": eligible_entrypoints(index, iface),
            }
        )
        out[iface] = row
    return {
        "default_workflow": out,
        "label": "Default agent workflow",
        "help": "The workflow that answers each agent interface when a client picks \"Gateway default\". "
        "Written [catalog:]bundle[@version]:flow; without a version the latest published version runs.",
    }


def discovery_envelope(index: List[Dict[str, Any]], data_dir: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """(`default_agent_workflows`, `default_agent_workflows_unavailable`) for
    the non-admin discovery envelopes (/bundles, /workflow-catalog)."""
    stored = stored_default_workflows(data_dir)
    ok: Dict[str, Any] = {}
    missing: Dict[str, Any] = {}
    for iface in known_interfaces(index, stored):
        res = resolve_default_agent_workflow(iface, index=index, stored=stored)
        if isinstance(res, Resolved):
            ok[iface] = {**res.resolved_dict(), "source": res.source}
        else:
            missing[iface] = {"source": res.source, "value": res.value, "reason": res.reason}
    return ok, missing


def unavailable_detail(res: Unavailable) -> str:
    """The 409 sentence of a `@default` start that cannot resolve."""
    where = "saved setting" if res.source == "stored" else "built-in default"
    value = f" = {res.value!r}" if res.value else ""
    return (
        f"the gateway default workflow for {res.interface} cannot run: {res.reason} "
        f"(setting {SETTING_KEY}.{res.interface}{value}, source: {res.source} [{where}]). "
        f"An admin changes it in the console (Workflows > Default agent workflow) or with "
        f"`abstractgateway config set {SETTING_KEY}.{res.interface} <bundle[@version]:flow>`."
    )
