from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..maintenance.action_tokens import verify_action_token
from ..maintenance.triage import apply_decision_action


router = APIRouter(prefix="/triage")


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return str(v).strip()
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return str(v2).strip()
    return None


def _action_secret() -> str:
    return str(_env("ABSTRACTGATEWAY_TRIAGE_ACTION_SECRET", "ABSTRACT_TRIAGE_ACTION_SECRET") or "").strip()


@router.get("/action/{token}", response_class=HTMLResponse)
async def triage_action_preview(token: str):
    secret = _action_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Triage action links are disabled (missing TRIAGE_ACTION_SECRET)")

    payload, err = verify_action_token(token=token, secret=secret)
    if err:
        raise HTTPException(status_code=401, detail=err)
    decision_id = str(payload.get("decision_id") or "").strip()
    action = str(payload.get("action") or "").strip()
    days = payload.get("days")
    days_s = f"{days}d" if isinstance(days, int) else ""

    safe_action = html.escape(action)
    safe_decision = html.escape(decision_id)

    return HTMLResponse(
        content=(
            "<html><head><title>Triage Action</title>"
            "<meta name=\"robots\" content=\"noindex,nofollow\"/>"
            "</head><body style=\"font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 40px auto;\">"
            "<h2>Confirm triage action</h2>"
            f"<p><b>Decision</b>: <code>{safe_decision}</code></p>"
            f"<p><b>Action</b>: <code>{safe_action}</code> {html.escape(days_s)}</p>"
            "<p>This page exists to avoid accidental activation by link previews.</p>"
            f"<form method=\"post\" action=\"/api/triage/action/{html.escape(token)}\">"
            "<button type=\"submit\" style=\"padding: 10px 14px;\">Confirm</button>"
            "</form>"
            "</body></html>"
        )
    )


@router.post("/action/{token}", response_class=HTMLResponse)
async def triage_action_apply(token: str, request: Request):
    del request
    secret = _action_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Triage action links are disabled (missing TRIAGE_ACTION_SECRET)")

    payload, err = verify_action_token(token=token, secret=secret)
    if err:
        raise HTTPException(status_code=401, detail=err)

    decision_id = str(payload.get("decision_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    days = payload.get("days")
    defer_days = int(days) if isinstance(days, int) and days > 0 else None

    data_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime/gateway")).expanduser().resolve()
    repo_root_raw = _env("ABSTRACT_TRIAGE_REPO_ROOT", "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT")
    repo_root = Path(repo_root_raw).expanduser().resolve() if repo_root_raw else None

    decision, err2 = apply_decision_action(
        gateway_data_dir=data_dir,
        decision_id=decision_id,
        action=action,
        repo_root=repo_root,
        defer_days=defer_days,
    )
    if err2:
        safe = html.escape(err2)
        return HTMLResponse(
            content=f"<html><body><h3>Failed</h3><pre>{safe}</pre></body></html>",
            status_code=400,
        )

    safe_status = html.escape(decision.status if decision else "unknown")
    safe_decision = html.escape(decision_id)
    safe_draft = html.escape(decision.draft_relpath if decision and decision.draft_relpath else "")
    draft_line = f"<p><b>Draft</b>: <code>{safe_draft}</code></p>" if safe_draft else ""

    return HTMLResponse(
        content=(
            "<html><head><title>Triage Applied</title>"
            "<meta name=\"robots\" content=\"noindex,nofollow\"/>"
            "</head><body style=\"font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 40px auto;\">"
            "<h2>Triage action applied</h2>"
            f"<p><b>Decision</b>: <code>{safe_decision}</code></p>"
            f"<p><b>Status</b>: <code>{safe_status}</code></p>"
            f"{draft_line}"
            "</body></html>"
        )
    )

