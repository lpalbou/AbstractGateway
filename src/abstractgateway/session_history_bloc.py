"""Session history bloc assembly (laurent c5551 / bloc-streaming claim).

One HTTP response returns a cursor-bounded bloc of session root turns, each
with an inline replay history bundle — replacing N per-turn history_bundle
round-trips on thin clients.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from abstractgateway.service import run_summary


def parse_created_at_cursor(value: Optional[str]) -> Optional[str]:
    """Validate and normalize an ISO-8601 created_at cursor for lexicographic compare."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError(f"Invalid ISO-8601 cursor: {value}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _created_at_key(value: Any) -> str:
    return str(value or "").strip()


def list_session_root_turns(
    run_store: Any,
    session_id: str,
    *,
    scan_limit: int = 500,
    include_drafts: bool = False,
) -> List[Dict[str, Any]]:
    """The session's turns, newest first by created_at.

    Turns come from AbstractRuntime's ONE selector (`select_session_turns`,
    automations contract C3/E): parent-less runs plus automation occurrence
    runs, never controllers, descendants, internal or legacy wrapper runs —
    the same turns every other history path (history bundles, session
    replay, growing-mode seeding) sees. A store without the run index fails
    loudly there.
    """
    from abstractruntime.session_turns import select_session_turns

    sid = str(session_id or "").strip()
    runs = select_session_turns(run_store, sid, include_drafts=bool(include_drafts), limit=int(scan_limit))
    items = [run_summary(run) for run in runs]
    items.sort(key=lambda row: _created_at_key(row.get("created_at")), reverse=True)
    return items


def select_bloc_turns(
    turns: List[Dict[str, Any]],
    *,
    before: Optional[str],
    limit: int,
) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    """Pick one bloc; return (turns, cursor_after, older_remaining)."""
    filtered = turns
    if before is not None:
        cursor = _created_at_key(before)
        filtered = [t for t in turns if _created_at_key(t.get("created_at")) < cursor]

    bloc = filtered[: int(limit)]
    if not bloc:
        return [], None, 0

    cursor_after = _created_at_key(bloc[-1].get("created_at"))
    older_remaining = sum(1 for t in turns if _created_at_key(t.get("created_at")) < cursor_after)
    return bloc, cursor_after or None, int(older_remaining)


def assemble_session_history_bloc(
    *,
    run_store: Any,
    ledger_store: Any,
    artifact_store: Any,
    session_id: str,
    before: Optional[str],
    limit: int,
    detail: str,
    include_subruns: bool,
    ledger_mode: str,
    ledger_max_items: int,
    include_drafts: bool,
    export_bundle: Any,
) -> Dict[str, Any]:
    from abstractruntime import export_run_history_bundle

    export_fn = export_bundle or export_run_history_bundle
    warnings: List[Dict[str, Any]] = []
    turns_all = list_session_root_turns(
        run_store,
        session_id,
        include_drafts=bool(include_drafts),
    )
    bloc_summaries, cursor_after, older_remaining = select_bloc_turns(
        turns_all,
        before=before,
        limit=int(limit),
    )

    turn_rows: List[Dict[str, Any]] = []
    for summary in bloc_summaries:
        rid = str(summary.get("run_id") or "").strip()
        row: Dict[str, Any] = {
            "run_id": rid,
            "created_at": summary.get("created_at"),
            "status": summary.get("status"),
        }
        if not rid:
            row["error"] = "missing run_id"
            warnings.append({"code": "turn_skipped", "run_id": rid, "detail": "missing run_id"})
            turn_rows.append(row)
            continue
        try:
            bundle = export_fn(
                run_id=rid,
                run_store=run_store,
                ledger_store=ledger_store,
                artifact_store=artifact_store,
                include_subruns=bool(include_subruns),
                include_session=False,
                session_turn_limit=0,
                ledger_mode=str(ledger_mode),
                ledger_max_items=int(ledger_max_items),
                detail=str(detail),
            )
            if not isinstance(bundle, dict):
                raise RuntimeError("export_run_history_bundle returned non-dict")
            row["bundle"] = bundle
            bundle_warnings = bundle.get("warnings")
            if isinstance(bundle_warnings, list) and bundle_warnings:
                for w in bundle_warnings:
                    if isinstance(w, dict):
                        tagged = dict(w)
                        tagged.setdefault("run_id", rid)
                        warnings.append(tagged)
        except KeyError as e:
            row["error"] = str(e)
            warnings.append({"code": "bundle_not_found", "run_id": rid, "detail": str(e)})
        except Exception as e:
            row["error"] = str(e)
            warnings.append({"code": "bundle_export_failed", "run_id": rid, "detail": str(e)})
        turn_rows.append(row)

    return {
        "session_id": str(session_id),
        "cursor_before": before,
        "cursor_after": cursor_after,
        "older_remaining": int(older_remaining),
        "warnings": warnings,
        "turns": turn_rows,
    }
