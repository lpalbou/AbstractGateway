"""Optional answer metrics, derived from the run's durable execution tree."""
from collections import deque
from datetime import datetime
import math
from typing import Any

from .run_metrics import MAX_SUBTREE_RUNS, TERMINAL_STATUSES


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def stats_from_records(records):
    """Only completed effects count; waiting approval elections did not execute.

    Missing usage stays unknown. Synthetic approval completions have the same
    idempotency key as the original effect and are counted exactly once.
    """
    inputs = outputs = tools = 0
    input_known = output_known = tools_known = True
    seen = set()
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "completed":
            continue
        key = record.get("idempotency_key") or record.get("step_id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        effect = record.get("effect") or {}
        result = record.get("result") or {}
        if not isinstance(effect, dict) or not isinstance(result, dict):
            continue
        if effect.get("type") == "llm_call":
            usage = result.get("usage") or {}
            usage = usage if isinstance(usage, dict) else {}
            a = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
            b = _count(usage.get("completion_tokens", usage.get("output_tokens")))
            input_known &= a is not None
            output_known &= b is not None
            inputs += a or 0
            outputs += b or 0
        elif effect.get("type") == "tool_calls":
            rows = result.get("results")
            if not isinstance(rows, list):
                tools_known = False
                continue
            for row in rows:
                if not isinstance(row, dict):
                    tools_known = False
                    continue
                # Current runtime denial/scope results explicitly carry no output.
                error = str(row.get("error") or "").lower()
                blocked = row.get("executed") is False or row.get("blocked") is True or error == "denied by user" or error.startswith(("tool call refused", "tool disabled", "tool is off", "tool not authorized")) or "not allowed for this run" in error
                if not blocked:
                    tools += 1
    return {"input_tokens": inputs if input_known else None,
            "output_tokens": outputs if output_known else None,
            "tool_calls": tools if tools_known else None}


def message_stats(run_store: Any, ledger_store: Any, run_id: str):
    run = run_store.load(run_id)
    if run is None:
        raise KeyError(run_id)
    status = str(getattr(run.status, "value", run.status))
    start, end = _time(run.created_at), _time(run.updated_at)
    elapsed = (end - start).total_seconds() if start and end and status in TERMINAL_STATUSES else None
    if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
        elapsed = None
    out = {"wall_time_seconds": elapsed, "input_tokens": None, "output_tokens": None, "tool_calls": None}
    if ledger_store is None:
        return out
    queue, seen, records = deque([run_id]), set(), []
    while queue:
        rid = queue.popleft()
        if rid in seen:
            continue
        if len(seen) >= MAX_SUBTREE_RUNS:
            raise ValueError("Execution tree exceeds statistics limit")
        seen.add(rid)
        # Fold separately so an identical child-local idempotency key is distinct.
        records.append(stats_from_records(ledger_store.list(rid)))
        children = getattr(run_store, "list_children", None)
        if callable(children):
            queue.extend(child.run_id for child in children(parent_run_id=rid) or [])
    for key in ("input_tokens", "output_tokens", "tool_calls"):
        values = [row[key] for row in records]
        out[key] = sum(values) if all(v is not None for v in values) else None
    return out


def visit_message_stats(ledger_store: Any, run_id: str):
    """Latest completed visit turn, between its two visitor-input parks.

    A visit is a multi-turn run: using the run's lifetime totals would charge
    every preceding answer to the current message. The resume record after the
    earlier park starts elapsed time, excluding the visitor's idle time.
    """
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    records = ledger_store.list(run_id)
    parks = []
    for index, record in enumerate(records):
        result = record.get("result") if isinstance(record, dict) else None
        wait = result.get("wait") if isinstance(result, dict) else None
        if isinstance(wait, dict) and wait.get("wait_key") == VISITOR_WAIT_KEY and record.get("status") == "waiting":
            parks.append(index)
    if len(parks) < 2:
        return None
    turn = records[parks[-2] + 1:parks[-1] + 1]
    out = stats_from_records(turn)
    starts = [_time(r.get("started_at")) for r in turn if isinstance(r, dict)]
    ends = [_time(r.get("ended_at")) for r in turn if isinstance(r, dict)]
    starts = [v for v in starts if v is not None]
    ends = [v for v in ends if v is not None]
    out["wall_time_seconds"] = max(0, (max(ends) - min(starts)).total_seconds()) if starts and ends else None
    return out
