"""Tool grant store + vocabulary (tool-tiers grant-mode API, cycle-3 build).

The converged design this implements (plans/tool-tiers.md, laurent's order +
11-seat consensus):
- The gateway is the CONTROL CENTER for defaults; apps override narrower.
- Tiers are PRESETS over one per-tool grant model; `grant_mode: preset|custom`
  is PROVENANCE ("what the operator did"), never an enforcement branch
  (semantics c4505 — no code may branch on it).
- The tier VOCABULARY ships laurent's examples as PRESET DATA (words at
  rest; integers are display order): observe(1) / act(2) / outreach(3) /
  destroy(4). The fold that assigns a tool its tier is core-hosted +
  runtime-owned (derive_risk / annotate rows) — this module NEVER derives
  risk; it stores and serves GRANTS against the served vocabulary.
- Two stores, two natures (my cycle-1 A4): the DEFAULT grant lives in the
  settings registry (console+CLI edit the SAME store, config-supersedes-env);
  every grant CHANGE is additionally a recorded act in an append-only
  grants ledger (attribution: who, what, old->new, when, surface).
- Precedence at run start: the default grant INJECTS as
  `_runtime.tool_policy.auto_approve_max_risk_rank` ONLY when the caller
  sent no tool_policy of its own (client policy wins inside the ceiling —
  the clamp of client lists to the ceiling is the enforcement wave with
  runtime, deliberately not guessed at here).
- Per-APP overrides are BLOCKED on cycle-2 Q1 (what is an app to the
  gateway — no app registry exists; unverified identity may only narrow).
  The store shape reserves the key; no endpoint mints app grants yet.
"""

from __future__ import annotations

import json
import logging
import threading
import time

try:
    import fcntl  # POSIX flock: cross-PROCESS write exclusion (adversary F5)
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Laurent's ladder as PRESET DATA (words at rest; rank = display order only).
# teaching_line per continuum's served-field contract (c4398): the consent
# surface must TEACH what each word hands over, not assume folklore.
TIER_VOCABULARY_VERSION = 1
TIER_VOCABULARY: List[Dict[str, Any]] = [
    {
        "id": "observe",
        "rank": 1,
        "label": "Observe",
        "teaching_line": "read and search only: list/read files, web search — nothing changes anywhere",
    },
    {
        "id": "act",
        "rank": 2,
        "label": "Act",
        "teaching_line": "bounded writes: create/edit files in the workspace, fetch a URL — real effects, contained",
    },
    {
        "id": "outreach",
        "rank": 3,
        "label": "Outreach",
        "teaching_line": "real-world reach: send email/messages, capture photo/video, start monitors — acts as you toward others",
    },
    {
        "id": "destroy",
        "rank": 4,
        "label": "Destroy",
        "teaching_line": "irreversible: delete files, mutable git (reset/revert), destructive programs — cannot be undone",
    },
]
_VALID_TIER_IDS = {t["id"] for t in TIER_VOCABULARY}
_RANK_BY_ID = {t["id"]: int(t["rank"]) for t in TIER_VOCABULARY}

# The shipped default: Act. Rationale served with the grant (never folklore):
# observe-only cripples the default agent (write_file is the bread-and-butter
# verb); outreach/destroy must be a deliberate operator choice — DESTRUCTIVE
# IS NEVER A DEFAULT (code-tui cycle-1, room-converged).
_DEFAULT_TIER_ID = "act"

_GRANTS_FILENAME = "config/tool_grants.json"
_LEDGER_FILENAME = "config/tool_grants_ledger.jsonl"
_write_lock = threading.Lock()


def _grants_path(data_dir: Path) -> Path:
    return Path(data_dir) / _GRANTS_FILENAME


def _ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / _LEDGER_FILENAME


class ToolGrantError(ValueError):
    """A rejected grant write — routes map it to an operator-readable 4xx."""


def _read_grants(data_dir: Path) -> Dict[str, Any]:
    p = _grants_path(data_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - a corrupt store REFUSES writes; reads degrade labeled
        logger.warning("tool_grants store unreadable (%s); serving built-in default", type(e).__name__)
        return {"_corrupt": True}


def read_tool_grants(data_dir: Path) -> Dict[str, Any]:
    """The served grant posture: vocabulary + version + the DEFAULT grant
    with its source (stored | builtin-default) + the reserved (empty) app
    overrides map. Never raises."""
    stored = _read_grants(data_dir)
    default_grant = stored.get("default")
    source = "stored"
    if not isinstance(default_grant, dict) or not default_grant:
        default_grant = {"mode": "preset", "tier_id": _DEFAULT_TIER_ID}
        source = "builtin-default"
    out: Dict[str, Any] = {
        "tier_vocabulary": TIER_VOCABULARY,
        "tier_vocabulary_version": TIER_VOCABULARY_VERSION,
        "default": dict(default_grant),
        "default_source": source,
        # Reserved: per-app grants await cycle-2 Q1 (app identity). Served
        # empty so consumers render the real state, never a guessed one.
        "app_overrides": dict(stored.get("app_overrides") or {}),
    }
    if stored.get("_corrupt"):
        out["warnings"] = ["#FALLBACK tool_grants store unreadable; serving built-in default (writes refuse)"]
    return out


def _validate_grant(grant: Any) -> Dict[str, Any]:
    """Validate a grant record {mode, tier_id?, tools?[]}. Vocabulary-version
    drift and unknown ids REFUSE loudly (fail-to-ask, never fuzzy-map —
    continuum's rule, room-converged)."""
    if not isinstance(grant, dict):
        raise ToolGrantError("grant must be an object {mode, tier_id?, tools?}")
    mode = str(grant.get("mode") or "").strip().lower()
    if mode not in ("preset", "custom"):
        raise ToolGrantError(f"unknown grant mode {grant.get('mode')!r} (preset | custom)")
    out: Dict[str, Any] = {"mode": mode}
    if mode == "preset":
        tier_id = str(grant.get("tier_id") or "").strip().lower()
        if tier_id not in _VALID_TIER_IDS:
            raise ToolGrantError(
                f"unknown tier_id {grant.get('tier_id')!r} — the vocabulary offers {sorted(_VALID_TIER_IDS)}"
            )
        if _RANK_BY_ID[tier_id] >= 4:
            # DESTRUCTIVE IS NEVER A DEFAULT and cannot be a standing preset
            # grant — it is a per-tool/per-program pin or a per-run consent,
            # always a separate explicit act (code-tui cycle-1, converged).
            raise ToolGrantError(
                "the destroy tier cannot be a standing default grant — grant it per-tool "
                "(custom mode) or per-run; destructive access is a separate explicit act"
            )
        out["tier_id"] = tier_id
    else:
        tools = grant.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) and t.strip() for t in tools):
            raise ToolGrantError("custom mode requires tools: [non-empty tool names]")
        if len(tools) > 200:
            raise ToolGrantError(f"custom grant lists cap at 200 names (got {len(tools)})")
        # A custom grant that equals a band's set still records custom —
        # provenance records what the operator DID (semantics c4505).
        out["tools"] = sorted({t.strip() for t in tools})
    return out


def write_default_grant(data_dir: Path, grant: Any, *, actor: str, surface: str = "api") -> Dict[str, Any]:
    """Persist the DEFAULT grant (validated) + append the recorded act to
    the grants ledger. The ledger is append-only attribution — who changed
    what, old->new, when, via which surface (my cycle-1 A4: grants are ACTS,
    not scalar config; audit falls out)."""
    clean = _validate_grant(grant)
    with _write_lock:
        lock_fh = None
        if fcntl is not None:
            lock_path = _grants_path(data_dir).parent / ".tool_grants.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fh = lock_path.open("a")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        stored = _read_grants(data_dir)
        if stored.get("_corrupt"):
            raise ToolGrantError(
                "tool_grants store is unreadable — refusing to overwrite it (repair the file first; "
                "a write must never destroy grants it did not name)"
            )
        old = stored.get("default")
        if old is None:
            # The effective prior state was the builtin display default —
            # record it, never None (adversary F8: attribution honesty).
            old = {"mode": "preset", "tier_id": _DEFAULT_TIER_ID, "source": "builtin-default"}
        stored["default"] = clean
        p = _grants_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp-{int(time.time() * 1000)}")
        tmp.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        # The recorded act (append-only; a failed ledger append is LOUD in
        # the response but does not roll back the store write — the store
        # is the truth, the ledger is the attribution trail).
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scope": "default",
            "old": old,
            "new": clean,
            "actor": str(actor or "unknown"),
            "surface": str(surface or "api"),
        }
        ledger_warning = None
        try:
            lp = _ledger_path(data_dir)
            lp.parent.mkdir(parents=True, exist_ok=True)
            with lp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            ledger_warning = f"#FALLBACK grant ledger append failed ({type(e).__name__}) — the change applied but is unattributed"
            logger.warning(ledger_warning)
        finally:
            if lock_fh is not None:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    lock_fh.close()
                except Exception:  # noqa: BLE001
                    pass
    out = read_tool_grants(data_dir)
    out["applied_by"] = entry["actor"]
    if ledger_warning:
        out.setdefault("warnings", []).append(ledger_warning)
    return out


def grant_ceiling_rank(data_dir: Path) -> Optional[int]:
    """The default grant's risk-tier ceiling as a rank, or None when the
    default is a custom name list (a name list has no single rank — the
    injection sends the names instead)."""
    posture = read_tool_grants(data_dir)
    grant = posture.get("default") or {}
    if grant.get("mode") == "preset":
        return _RANK_BY_ID.get(str(grant.get("tier_id") or ""), None)
    return None


def inject_default_grant(data_dir: Path, rt_ns: Dict[str, Any]) -> Optional[str]:
    """Run-start injection: when the caller sent NO tool_policy of its own,
    the gateway's default grant rides in — preset => the tier ceiling
    (runtime's shipped auto_approve_max_risk_rank consumer, c4566/c4619); custom
    => the name list. CLIENT POLICY ALWAYS WINS when present (per-run policy
    operates inside the ceiling; the clamp of client lists is the
    enforcement wave, not this injection). Returns a note for run vars, or
    None when nothing was injected."""
    try:
        # KEY PRESENCE suppresses (adversary F6): a client sending
        # tool_policy: {} means "no overrides, static defaults" — an explicit
        # statement, never to be overwritten.
        if not isinstance(rt_ns, dict) or "tool_policy" in rt_ns:
            return None
        posture = read_tool_grants(data_dir)
        # STORED-ONLY injection (adversary F1, the P0): grants are RECORDED
        # ACTS — the builtin default is a display posture, not an operator
        # choice, and injecting it would silently flip ask->auto for writes
        # on every bridge/scheduled run (the 2026-02-21 approval defaults
        # stay authoritative until the operator ACTS). A corrupt store also
        # serves builtin (source != stored), so corruption never widens the
        # grant (F2).
        if posture.get("default_source") != "stored" or posture.get("warnings"):
            return None
        grant = posture.get("default") or {}
        if grant.get("mode") == "preset":
            rank = _RANK_BY_ID.get(str(grant.get("tier_id") or ""))
            if rank is None:
                return None
            # ONE spelling (runtime c4619: the _tier alias is DROPPED —
            # rank semantics, rank name; my injection tracks the consumer
            # key verbatim per the c4592 commitment).
            rt_ns["tool_policy"] = {"auto_approve_max_risk_rank": int(rank)}
            note = f"gateway-default:{grant.get('tier_id')} (tier ceiling {rank})"
        elif grant.get("mode") == "custom":
            tools = [t for t in (grant.get("tools") or []) if isinstance(t, str)]
            if not tools:
                return None
            rt_ns["tool_policy"] = {"auto_approve_tools": tools}
            note = f"gateway-default:custom ({len(tools)} tools)"
        else:
            return None
        # Receipt half (code-tui c4473 ask d): the injected policy names its
        # authority so a server-side auto-approval is attributable.
        rt_ns["tool_policy"]["source"] = "gateway-default"
        rt_ns["tool_policy"]["grant_mode"] = str(grant.get("mode"))
        return note
    except Exception as e:  # noqa: BLE001 - injection is additive; a broken store never blocks a start
        logger.warning("default grant injection skipped: %s", type(e).__name__)
        return None
