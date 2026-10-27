"""Session tool policy, gateway half (contract v1; runtime: `abstractruntime.storage.session_tool_policy`).

A person's standing tool decision for one conversation ("approve all", "approve this kind",
"deny this", reset), held server-side so the runtime never emits an approval wait for a call the
session already approved (the gate used to flash in every app: the client answered it after it
arrived).

- ONE durable store per data root (`<data_root>/session_tool_policy.sqlite3`), beside the run
  stores, so per-principal data roots isolate accounts for free (the steer sidecar precedent).
- Every Runtime the host builds gets the store attached (`attach_session_tool_policy_store`), so
  every run of the session reads it at decision time: turns, delegated children, resumes, and
  runs after a restart.
- THE GATEWAY CLAMPS (`build_policy`): approve selectors are expanded at write time to the tools
  the gateway authorizes for the caller (the enabled rows `GET /discovery/tools` serves them);
  anything requested but not authorized is dropped and listed in `clamped`, never silently.
  Deny selectors need no authorization (narrowing is always allowed).
"""

from __future__ import annotations

import datetime
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("abstractgateway.session_tool_policy")

POLICY_FILENAME = "session_tool_policy.sqlite3"
MAX_NAMES = 512
DEFAULT_UNRANKED = 4

_STORES: Dict[str, Any] = {}
_LOCK = threading.Lock()


def session_tool_policy_store(data_root: Path | str) -> Any:
    """The durable store for one data root (cached per path).

    Raises when the installed abstractruntime has no session tool policy: the gateway pins a
    runtime that has it, and a gateway that silently lost the feature would bring the flash back.
    """
    key = str(Path(data_root).expanduser().resolve())
    with _LOCK:
        store = _STORES.get(key)
        if store is None:
            from abstractruntime.storage.session_tool_policy import SqliteSessionToolPolicyStore

            store = SqliteSessionToolPolicyStore(str(Path(key) / POLICY_FILENAME))
            _STORES[key] = store
        return store


def evict_session_tool_policy_store(data_root: Path | str) -> None:
    key = str(Path(data_root).expanduser().resolve())
    with _LOCK:
        _STORES.pop(key, None)


def attach_session_tool_policy_store(runtime: Any, data_root: Path | str) -> None:
    """Give a Runtime this data root's store (its TOOL_CALLS handler reads it before any wait)."""
    setter = getattr(runtime, "set_session_tool_policy_store", None)
    if not callable(setter):
        raise RuntimeError(
            "abstractruntime has no session tool policy (Runtime.set_session_tool_policy_store); "
            "upgrade abstractruntime"
        )
    setter(session_tool_policy_store(data_root))


class PolicyRequestError(ValueError):
    """A malformed body (routes answer 422)."""


def _names(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyRequestError(f"{field} must be a list of strings")
    out = sorted({v.strip() for v in value if v.strip()})
    if len(out) > MAX_NAMES:
        raise PolicyRequestError(f"{field} lists more than {MAX_NAMES} names")
    return out


def normalize_request(body: Any) -> Dict[str, Any]:
    """The request as a canonical dict: {approve:{tools,kinds,all,max_risk_rank}, deny:{tools,kinds}, ask}."""
    if not isinstance(body, dict):
        raise PolicyRequestError("body must be an object")
    unknown = set(body) - {"approve", "deny", "ask", "client", "if_revision"}
    if unknown:
        raise PolicyRequestError(f"unknown field(s): {sorted(unknown)}")
    approve = body.get("approve") or {}
    deny = body.get("deny") or {}
    if not isinstance(approve, dict) or not isinstance(deny, dict):
        raise PolicyRequestError("approve and deny must be objects")
    if set(approve) - {"tools", "kinds", "all", "max_risk_rank"}:
        raise PolicyRequestError(f"unknown approve field(s): {sorted(set(approve) - {'tools', 'kinds', 'all', 'max_risk_rank'})}")
    if set(deny) - {"tools", "kinds"}:
        raise PolicyRequestError(f"unknown deny field(s): {sorted(set(deny) - {'tools', 'kinds'})}")
    all_ = approve.get("all", False)
    if not isinstance(all_, bool):
        raise PolicyRequestError("approve.all must be a boolean")
    rank = approve.get("max_risk_rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 4):
        raise PolicyRequestError("approve.max_risk_rank must be an integer from 1 to 4")
    return {
        "approve": {
            "tools": _names(approve.get("tools"), "approve.tools"),
            "kinds": _names(approve.get("kinds"), "approve.kinds"),
            "all": all_,
            "max_risk_rank": rank,
        },
        "deny": {"tools": _names(deny.get("tools"), "deny.tools"), "kinds": _names(deny.get("kinds"), "deny.kinds")},
        "ask": _names(body.get("ask"), "ask"),
    }


def merge_requests(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """POST semantics (provenance of the merged policy): union of approve and deny selectors and
    of ask; `all` sticks once set; `max_risk_rank` is the wider of the two (no rank = unbounded).
    A merge never narrows; narrowing is a PUT."""
    if not isinstance(old, dict):
        return new
    oa, na = old.get("approve") or {}, new["approve"]
    od, nd = old.get("deny") or {}, new["deny"]
    return {
        "approve": {
            "tools": sorted(set(oa.get("tools") or []) | set(na["tools"])),
            "kinds": sorted(set(oa.get("kinds") or []) | set(na["kinds"])),
            "all": bool(oa.get("all")) or na["all"],
            "max_risk_rank": None
            if na["max_risk_rank"] is None or oa.get("max_risk_rank") is None
            else max(int(na["max_risk_rank"]), int(oa["max_risk_rank"])),
        },
        "deny": {
            "tools": sorted(set(od.get("tools") or []) | set(nd["tools"])),
            "kinds": sorted(set(od.get("kinds") or []) | set(nd["kinds"])),
        },
        "ask": sorted(set(old.get("ask") or []) | set(new["ask"])),
    }


def _rank(row: Dict[str, Any]) -> int:
    try:
        value = row.get("risk_rank")
        return int(value) if value is not None and not isinstance(value, bool) else DEFAULT_UNRANKED
    except (TypeError, ValueError):
        return DEFAULT_UNRANKED


def build_policy(
    request: Dict[str, Any],
    rows: Iterable[Dict[str, Any]],
    *,
    set_by: Dict[str, Any],
) -> Dict[str, Any]:
    """Expand and clamp a normalized request against the caller's tool inventory rows.

    `rows`: the `GET /discovery/tools` items for the caller; a row is authorized when its
    `enabled` is True. Returns the policy dict the runtime store keeps (without revision).
    """
    all_rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"].strip()]
    authorized = {r["name"].strip(): r for r in all_rows if r.get("enabled") is True}
    known_kinds = {str(r.get("toolset") or "") for r in all_rows} - {""}
    clamped: List[Dict[str, str]] = []
    ceiling = request["approve"]["max_risk_rank"]
    approve: Dict[str, str] = {}

    def _within(row: Dict[str, Any]) -> bool:
        return ceiling is None or _rank(row) <= int(ceiling)

    if request["approve"]["all"]:
        for name, row in sorted(authorized.items()):
            if _within(row):
                approve.setdefault(name, "all")
    for kind in request["approve"]["kinds"]:
        members = [(n, r) for n, r in sorted(authorized.items()) if str(r.get("toolset") or "") == kind]
        if not members:
            clamped.append({"selector": "kinds", "name": kind,
                            "reason": "not_authorized" if kind in known_kinds else "unknown_kind"})
            continue
        for name, row in members:
            if _within(row):
                approve[name] = f"kind:{kind}"
    for name in request["approve"]["tools"]:
        if name in authorized:
            approve[name] = "tool"
        else:
            clamped.append({"selector": "tools", "name": name, "reason": "not_authorized"})

    deny: Dict[str, str] = {name: "tool" for name in request["deny"]["tools"]}
    for kind in request["deny"]["kinds"]:
        for row in all_rows:
            if str(row.get("toolset") or "") == kind:
                deny.setdefault(row["name"].strip(), f"kind:{kind}")
    for name in list(approve):
        if name in deny or name in request["ask"]:
            approve.pop(name)
    return {
        "approve": dict(sorted(approve.items())),
        "deny": dict(sorted(deny.items())),
        "ask": list(request["ask"]),
        "request": request,
        "clamped": clamped,
        "set_by": dict(set_by),
        "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def merge_policies(current: Optional[Dict[str, Any]], new: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    """POST: the stored policy plus the NEW selectors' expansion, never a re-expansion of the old
    ones (an earlier `all` must not silently grow to tools enabled since it was written) and never
    narrower than before. Deny and ask still win over approve. `clamped` is this write's."""
    if not isinstance(current, dict):
        return {**new, "request": request}
    approve = {**dict(current.get("approve") or {}), **dict(new.get("approve") or {})}
    deny = {**dict(current.get("deny") or {}), **dict(new.get("deny") or {})}
    ask = sorted(set(current.get("ask") or []) | set(new.get("ask") or []))
    for name in list(approve):
        if name in deny or name in ask:
            approve.pop(name)
    return {**new, "approve": dict(sorted(approve.items())), "deny": dict(sorted(deny.items())), "ask": ask, "request": request}


SCOPE = {
    "approve": "every run of this conversation (turns and their delegated child runs), except automation runs, runs "
    "that read text other people wrote, and entity visits",
    "deny": "every run of this conversation, automation runs and untrusted-input runs included",
}


def split_client(body: Any) -> Tuple[Optional[str], Optional[int]]:
    """(client label, if_revision) from a raw body, validated."""
    if not isinstance(body, dict):
        return None, None
    client = body.get("client")
    if client is not None and (not isinstance(client, str) or len(client) > 64):
        raise PolicyRequestError("client must be a string of at most 64 characters")
    rev = body.get("if_revision")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int) or rev < 0):
        raise PolicyRequestError("if_revision must be a non-negative integer")
    return (client.strip() or None) if isinstance(client, str) else None, rev


def capability() -> Dict[str, Any]:
    """`capabilities.session_tool_policy` on GET /discovery/capabilities."""
    try:
        from abstractruntime.core.runtime import Runtime
        from abstractruntime.storage.session_tool_policy import SESSION_TOOL_POLICY_VERSION

        supported = callable(getattr(Runtime, "set_session_tool_policy_store", None))
    except Exception:  # noqa: BLE001 - older runtime: the feature is absent, say so
        return {"supported": False, "reason": "abstractruntime has no session tool policy"}
    return {
        "supported": bool(supported),
        "version": int(SESSION_TOOL_POLICY_VERSION),
        "endpoint": "/api/gateway/sessions/{session_id}/tool-policy",
        "methods": ["GET", "PUT", "POST", "DELETE"],
        "approve": ["tools", "kinds", "all", "max_risk_rank"],
        "deny": ["tools", "kinds"],
        "ask": True,
        "record": "abstract.tool_policy_decision",
    }
