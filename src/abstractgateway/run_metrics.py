"""Per-turn metrics for `GET /runs?include_metrics=true`.

A listed run's `steps` / `llm_calls` / `tool_calls` / `tokens_total` are the
totals of that run AND every run below it (subworkflows, agent sub-runs), read
from the ledger — the durable record of what executed. Clients show them as
they are (AbstractCode's conversation card adds its turns' `tool_calls`); none
of them counts ledger records itself.

Per-run counts come from the ledger store's `metrics_many` when it has one
(SQLite), otherwise from its records (`list`, the JSONL file store). A terminal
run never changes, so its subtree total is cached by `(run_id, updated_at)`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

METRIC_KEYS: Tuple[str, ...] = ("steps", "llm_calls", "tool_calls", "tokens_total")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
#: Bound on the runs one subtree walk visits (a runaway tree must not pin the request).
MAX_SUBTREE_RUNS = 5000
_CACHE_MAX = 4096

_cache: "OrderedDict[Tuple[str, str], Dict[str, int]]" = OrderedDict()
_cache_lock = threading.Lock()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def metrics_from_records(records: Iterable[Any]) -> Dict[str, int]:
    """The SQLite `metrics_many` rules over plain ledger records: completed steps only;
    an `llm_call` effect counts one call and its usage tokens; a `tool_calls` effect
    counts the calls it carries."""
    out = {key: 0 for key in METRIC_KEYS}
    for rec in records or []:
        if not isinstance(rec, dict) or str(rec.get("status") or "") != "completed":
            continue
        out["steps"] += 1
        effect = rec.get("effect") if isinstance(rec.get("effect"), dict) else {}
        kind = str(effect.get("type") or "")
        if kind == "llm_call":
            out["llm_calls"] += 1
            result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            out["tokens_total"] += _int(usage.get("total_tokens"))
        elif kind == "tool_calls":
            payload = effect.get("payload") if isinstance(effect.get("payload"), dict) else {}
            calls = payload.get("tool_calls")
            if isinstance(calls, list):
                out["tool_calls"] += len([c for c in calls if c is not None])
    return out


def _self_metrics(ledger_store: Any, run_ids: List[str]) -> Dict[str, Dict[str, int]]:
    """Each run's own counts (its ledger only)."""
    ids = [rid for rid in run_ids if rid]
    if not ids or ledger_store is None:
        return {}
    many = getattr(ledger_store, "metrics_many", None)
    if callable(many):
        try:
            raw = many(ids)
        except Exception:
            raw = None
        # A wrapper over a store without the query answers {} — fall through to the records.
        if isinstance(raw, dict) and raw:
            # SQLite omits a run with no completed record: its counts are zeros.
            return {
                rid: {key: _int((raw.get(rid) or {}).get(key)) for key in METRIC_KEYS}
                for rid in ids
            }
    lister = getattr(ledger_store, "list", None)
    if not callable(lister):
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for rid in ids:
        try:
            records = lister(rid)
        except Exception:
            records = []
        out[rid] = metrics_from_records(records)
    return out


def _children(run_store: Any, run_id: str) -> List[Any]:
    list_children = getattr(run_store, "list_children", None)
    if not callable(list_children):
        return []
    try:
        return list(list_children(parent_run_id=run_id) or [])
    except Exception:
        return []


def _status_of(item: Dict[str, Any]) -> str:
    status = item.get("status")
    return str(getattr(status, "value", status) or "").strip().lower()


def subtree_metrics(run_store: Any, ledger_store: Any, run_id: str) -> Dict[str, int]:
    """Totals for `run_id` and every run below it (breadth first, bounded)."""
    order: List[str] = []
    seen: set = set()
    queue = deque([run_id])
    while queue and len(seen) < MAX_SUBTREE_RUNS:
        rid = str(queue.popleft() or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        order.append(rid)
        for child in _children(run_store, rid):
            cid = str(getattr(child, "run_id", "") or "").strip()
            if cid and cid not in seen:
                queue.append(cid)
    per_run = _self_metrics(ledger_store, order)
    total = {key: 0 for key in METRIC_KEYS}
    for rid in order:
        row = per_run.get(rid) or {}
        for key in METRIC_KEYS:
            total[key] += _int(row.get(key))
    return total


def attach_turn_metrics(items: List[Dict[str, Any]], run_store: Any, ledger_store: Any) -> None:
    """Set the four metric fields on each listed run summary (in place). Without a
    ledger store the fields are present and null (unknown), never zero."""
    for item in items:
        rid = str(item.get("run_id") or "").strip()
        if not rid or ledger_store is None:
            for key in METRIC_KEYS:
                item.setdefault(key, None)
            continue
        cache_key: Optional[Tuple[str, str]] = None
        if _status_of(item) in TERMINAL_STATUSES:
            cache_key = (rid, str(item.get("updated_at") or ""))
            with _cache_lock:
                hit = _cache.get(cache_key)
                if hit is not None:
                    _cache.move_to_end(cache_key)
            if hit is not None:
                item.update(hit)
                continue
        totals = subtree_metrics(run_store, ledger_store, rid)
        item.update(totals)
        if cache_key is not None:
            with _cache_lock:
                _cache[cache_key] = dict(totals)
                while len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
