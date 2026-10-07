"""Account activity from the gateway audit log (DESIGN-v2 §2.4, §6).

    GET /api/gateway/admin/accounts/{id}/activity   (admin)
    GET /api/gateway/me/activity                    (the signed-in account)

Source: `<data_dir>/audit_log.jsonl` plus its rotated files `audit_log.<ts>.jsonl`
(security/gateway_security.py `_audit_append`), read BACKWARDS in fixed chunks under a byte
budget, newest file first, stopping as soon as `limit` events are found. A line is parsed only
when it carries the account id as a JSON string (a byte-level pre-check), then matched on exact
fields:

- request lines (written for POST / PUT / PATCH / DELETE under /api/gateway only):
  `principal_user_id` / `principal_tenant_id`, or the account a route named on its line —
  `signed_in` {user_id, tenant_id} (a sign-in request has no principal yet) and
  `account_change` {user_id, tenant_id} (an admin changed this account);
- typed email events (mail/audit.py): `user_id` / `tenant_id`.

Classification is two explicit tables: (method, route template) -> kind/title for request
lines, and event name -> kind/title for typed email events. Anything not in a table is not
shown. No text heuristics.

Not recorded (said in `note`): read-only requests (GET: page views, token use on reads), mail
received by the watcher, and what agents send through their email tools.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .users import gateway_data_dir_from_env

KINDS = ("sign_in", "token", "run", "automation", "email", "account")
DEFAULT_BYTE_BUDGET = 48 * 1024 * 1024
CHUNK = 256 * 1024
API = "/api/gateway"

NOTE = (
    "The gateway records sign-ins, changes, runs started and email events. Page views and reads are not "
    "recorded, nor mail received or what agents send with their email tools."
)

# How a mailbox was connected (`email.connected` audit fields auth_kind / provider, mail/accounts.py),
# in plain words. Explicit table; a pair not in it shows its raw value (never hidden).
CONNECTED_DETAIL: Dict[Tuple[str, str], str] = {
    ("password", ""): "IMAP · password sign-in",
    ("oauth2", "google"): "Google sign-in",
    ("oauth2", "microsoft"): "Microsoft sign-in",
}

# Notification kinds (mail/notifications.py `queue_notice` kinds and the test notice) in plain
# words, for `email.notification_sent` details. Explicit table; a kind not in it shows as-is.
NOTIFICATION_KIND_LABELS: Dict[str, str] = {
    "approval_needed": "Approval needed",
    "job_failed": "Job failed",
    "job_finished": "Job finished",
    "automation_result": "Automation result",
    "automation_failed": "Automation failed",
    "test": "Test notification",
}

# A run-start line written before the run routes recorded their run id on the audit line
# (routes/gateway.py `_audit_run_started`): said plainly, never guessed or joined by time.
RUN_ID_NOT_RECORDED = "Run id not recorded (before this version)"

# Observer links: the Observer's hash routes (abstractobserver src/ui/automations.ts
# `parse_app_hash`, wired in src/ui/app.tsx on load and hashchange): `#automations` (the
# Automations page) and, since abstractobserver round2 32926ba, `#run/<run_id>` (that run in
# Observe; the id is decodeURIComponent'ed, so it is quoted here). An Observer build older than
# that opens a uuid run id at load too (its last-hash-segment rule), without the not-found sentence.
OBSERVER_APP_ID = "observer"  # apps_manager.APPS
OBSERVER_AUTOMATIONS_HASH = "#automations"
OBSERVER_RUN_HASH = "#run/{run_id}"


def observer_app_path() -> str:
    """`/apps/observer/`: the gateway's mount of the Observer app (app_proxy.APPS_PREFIX + the
    app id). Fails loudly when the apps table has no Observer."""
    from .app_proxy import APPS_PREFIX
    from .apps_manager import APPS

    spec = next((a for a in APPS if a.id == OBSERVER_APP_ID), None)
    if spec is None:
        raise RuntimeError(f"the apps table has no {OBSERVER_APP_ID!r} app (apps_manager.APPS)")
    return f"{APPS_PREFIX}/{spec.id}/"


def observer_path_for(run_id: Optional[str]) -> Optional[str]:
    if not run_id:
        return None
    from urllib.parse import quote

    return observer_app_path() + OBSERVER_RUN_HASH.format(run_id=quote(str(run_id), safe=""))


def observer_automations_path() -> str:
    return observer_app_path() + OBSERVER_AUTOMATIONS_HASH


def _ts_local(ts: Any) -> Optional[str]:
    """The event time in the gateway's local time zone, ISO 8601 with its offset."""
    if not ts:
        return None
    try:
        import datetime

        at = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=datetime.timezone.utc)
        return at.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


# (method, route template) -> (kind, title). Templates are matched segment by segment; a
# `{name}` segment matches any one segment.
REQUEST_EVENTS: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("POST", f"{API}/session/login"): ("sign_in", "Signed in"),
    ("POST", f"{API}/session/claim"): ("sign_in", "Signed in"),
    ("POST", f"{API}/session/recovery/redeem"): ("sign_in", "Signed in"),
    ("POST", f"{API}/apps/desktop-handover"): ("sign_in", "Signed in"),
    ("POST", f"{API}/session/logout"): ("sign_in", "Signed out"),
    ("POST", f"{API}/runs/start"): ("run", "Run started"),
    ("POST", f"{API}/runs/schedule"): ("run", "Scheduled run started"),
    ("POST", f"{API}/automations"): ("automation", "Automation created"),
    ("PATCH", f"{API}/automations/{{automation_id}}"): ("automation", "Automation changed"),
    ("POST", f"{API}/automations/{{automation_id}}/commands"): ("automation", "Automation command"),
    ("PATCH", f"{API}/admin/users/{{user_id}}"): ("account", "Account changed"),
    ("PUT", f"{API}/admin/accounts/{{account_id}}/active"): ("account", "Account changed"),
    ("PUT", f"{API}/accounts/{{account}}/preferences"): ("account", "Preferences changed"),
}

SIGN_IN_DETAIL: Dict[str, str] = {
    f"{API}/session/login": "With a token.",
    f"{API}/session/claim": "With a setup link.",
    f"{API}/session/recovery/redeem": "With a code sent by email.",
    f"{API}/apps/desktop-handover": "From the Assistant.",
}

AUTOMATION_COMMAND_TITLES: Dict[str, str] = {
    "automation.create": "Automation created",
    "automation.revise": "Automation changed",
    "automation.pause": "Automation paused",
    "automation.resume": "Automation resumed",
    "automation.run_now": "Automation run started",
    "automation.stop_current": "Automation run stopped",
    "automation.archive": "Automation archived",
}

# Typed email events (mail/audit.py callers) -> (kind, title). Internal bookkeeping events
# (cursor resets, migrations, OAuth client edits) are deliberately absent.
EMAIL_EVENTS: Dict[str, Tuple[str, str]] = {
    "email.connected": ("email", "Mailbox connected"),
    "email.disconnected": ("email", "Mailbox disconnected"),
    "email.tested": ("email", "Mailbox tested"),
    "email.folder_changed": ("email", "Watch folder changed"),
    "email.user_switch": ("email", "Mailbox switched"),
    "email.agent_tools_changed": ("email", "Agent email tools switched"),
    "email.capability_changed": ("email", "Mailbox access changed by an admin"),
    "email.notification_sent": ("email", "Notification sent"),
    "email.notification_failed": ("email", "Notification not sent"),
    "email.sent_from_console": ("email", "Email sent"),
    "email.message_unprocessable": ("email", "An incoming email could not be read"),
    "email.recovery_code_issued": ("sign_in", "Sign-in code sent by email"),
    "email.recovery_code_refused": ("sign_in", "Sign-in code refused"),
    "email.address_changed": ("account", "Email address changed"),
    # Archive (round 3): typed events written by the archive routes (routes/gateway.py).
    "account.archived": ("account", "Archived"),
    "account.unarchived": ("account", "Unarchived"),
    # Your own token rotation (POST /me/token/rotate).
    "token.rotated": ("token", "Token rotated"),
}


def _segments(path: str) -> List[str]:
    return [s for s in str(path or "").split("?")[0].split("/") if s]


_TEMPLATES = [(m, _segments(t), t, v) for (m, t), v in REQUEST_EVENTS.items()]


def _match_template(method: str, path: str) -> Optional[Tuple[str, Tuple[str, str]]]:
    segs = _segments(path)
    for m, tsegs, template, value in _TEMPLATES:
        if m != method or len(tsegs) != len(segs):
            continue
        if all(t.startswith("{") and t.endswith("}") or t == s for t, s in zip(tsegs, segs)):
            return template, value
    return None


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------


def audit_files(data_dir: Optional[Path] = None) -> List[Path]:
    """Newest first: the live file, then the rotated ones (their names sort by time)."""

    root = Path(data_dir) if data_dir is not None else gateway_data_dir_from_env()
    live = root / "audit_log.jsonl"
    rotated = sorted(root.glob("audit_log.*.jsonl"), key=lambda p: p.name, reverse=True)
    return ([live] if live.is_file() else []) + [p for p in rotated if p.is_file()]


def _lines_backwards(path: Path, budget: List[int]) -> Iterator[Tuple[bytes, bool]]:
    """Complete lines of `path`, last first. Yields (line, at_file_start). Stops when the
    shared byte budget runs out (budget[0] <= 0)."""

    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        carry = b""
        while pos > 0 and budget[0] > 0:
            step = min(CHUNK, pos, max(budget[0], 1))
            pos -= step
            fh.seek(pos)
            block = fh.read(step)
            budget[0] -= step
            buf = block + carry
            parts = buf.split(b"\n")
            carry = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line, False
        if pos == 0 and carry:
            yield carry, True


def _ts_of(line: bytes) -> Optional[str]:
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    ts = doc.get("ts") if isinstance(doc, dict) else None
    return str(ts) if ts else None


# ---------------------------------------------------------------------------------------
# Matching + classification
# ---------------------------------------------------------------------------------------


def _tenant(value: Any) -> str:
    return str(value or "default")


def _classify(doc: Dict[str, Any], user_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    event = doc.get("event")
    if isinstance(event, str):
        if str(doc.get("user_id") or "") != user_id or _tenant(doc.get("tenant_id")) != tenant_id:
            return None
        spec = EMAIL_EVENTS.get(event)
        if spec is None:
            return None
        return _email_event(doc, event, spec)

    method = str(doc.get("method") or "").upper()
    path = str(doc.get("path") or "")
    hit = _match_template(method, path)
    if hit is None:
        return None
    template, (kind, title) = hit
    actor = str(doc.get("principal_user_id") or "")
    actor_tenant = _tenant(doc.get("principal_tenant_id"))
    signed = doc.get("signed_in") if isinstance(doc.get("signed_in"), dict) else None
    change = doc.get("account_change") if isinstance(doc.get("account_change"), dict) else None
    mine_as_actor = actor == user_id and actor_tenant == tenant_id
    mine_as_signed_in = bool(signed) and str(signed.get("user_id") or "") == user_id and _tenant(signed.get("tenant_id")) == tenant_id
    mine_as_target = bool(change) and str(change.get("user_id") or "") == user_id and _tenant(change.get("tenant_id")) == tenant_id
    if not (mine_as_actor or mine_as_signed_in or mine_as_target):
        return None
    status = int(doc.get("status") or 0)
    ok = 0 < status < 400
    out: Dict[str, Any] = {
        "ts": doc.get("ts"),
        "kind": kind,
        "title": title,
        "detail": None,
        "run_id": None,
        "observer_path": None,
        "ok": ok,
    }
    if kind == "sign_in":
        if template.endswith("/logout"):
            pass
        else:
            if not mine_as_signed_in and not mine_as_actor:
                return None
            out["detail"] = SIGN_IN_DETAIL.get(template)
            rec = doc.get("recovery") if isinstance(doc.get("recovery"), dict) else None
            if rec and rec.get("token_rotated"):
                out["kind"], out["title"] = "token", "Token reset with a code sent by email"
    elif kind == "run":
        run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
        out["run_id"] = str(run.get("run_id") or "") or None
        out["observer_path"] = observer_path_for(out["run_id"])
        out["detail"] = str(run.get("workflow") or "") or None
        if ok and out["run_id"] is None:
            out["detail"] = RUN_ID_NOT_RECORDED
    elif kind == "automation":
        auto = doc.get("automation") if isinstance(doc.get("automation"), dict) else {}
        command = str(auto.get("command") or "")
        out["title"] = AUTOMATION_COMMAND_TITLES.get(command, title)
        out["detail"] = str(auto.get("automation_id") or "") or None
        out["observer_path"] = observer_automations_path()
    elif kind == "account":
        changes = (change or {}).get("changes") if isinstance((change or {}).get("changes"), dict) else {}
        target = str((change or {}).get("user_id") or "")
        parts: List[str] = []
        if changes.get("token_rotated"):
            out["kind"], out["title"] = "token", "Token rotated"
        if "active" in changes:
            parts.append("activated" if changes.get("active") else "deactivated")
        if "email" in changes:
            parts.append("email address changed")
        if "roles" in changes:
            parts.append("role changed")
        if "runtime_id" in changes:
            parts.append("runtime changed")
        if "entity_state" in changes:
            parts.append(f"entity {changes.get('entity_state')}")
        what = ", ".join(parts)
        if mine_as_target and not mine_as_actor:
            out["detail"] = (what[:1].upper() + what[1:] + f" by {actor}." if what else f"By {actor}.") if actor else (what or None)
        elif target and target != user_id:
            out["detail"] = f"{target}: {what}." if what else f"{target}."
        else:
            out["detail"] = (what[:1].upper() + what[1:] + ".") if what else None
    if not ok and status:
        out["detail"] = ((out["detail"] + " ") if out["detail"] else "") + f"Refused (HTTP {status})."
    return out


def _email_event(doc: Dict[str, Any], event: str, spec: Tuple[str, str]) -> Dict[str, Any]:
    kind, title = spec
    outcome = str(doc.get("outcome") or "")
    ok = True
    detail: Optional[str] = None
    if event == "email.tested":
        ok = outcome == "ok"
        title = "Mailbox test passed" if ok else "Mailbox test failed"
        detail = str(doc.get("code") or "") or None
    elif event == "email.notification_failed":
        ok = False
        detail = str(doc.get("cause") or doc.get("code") or "") or None
    elif event == "email.recovery_code_refused":
        ok = False
        detail = str(doc.get("reason") or doc.get("code") or "") or None
    elif event == "email.address_changed":
        title = "Email address cleared" if outcome == "cleared" else "Email address changed"
    elif event in ("email.user_switch", "email.agent_tools_changed", "email.capability_changed"):
        if isinstance(doc.get("enabled"), bool):
            detail = "On." if doc.get("enabled") else "Off."
    elif event == "email.notification_sent":
        code = str(doc.get("kind") or "")
        detail = NOTIFICATION_KIND_LABELS.get(code, code) or None
    elif event == "email.connected":
        auth = str(doc.get("auth_kind") or "")
        provider = str(doc.get("provider") or "") if auth == "oauth2" else ""
        detail = CONNECTED_DETAIL.get((auth, provider)) or " · ".join(x for x in (auth, provider) if x) or None
    elif event == "email.folder_changed":
        detail = str(doc.get("folder") or "") or None
    elif event == "email.message_unprocessable":
        ok = False
        detail = str(doc.get("cause") or "") or None
    elif event in ("account.archived", "account.unarchived", "token.rotated"):
        actor = str(doc.get("actor") or "")
        detail = f"By {actor}." if actor else None
    return {"ts": doc.get("ts"), "kind": kind, "title": title, "detail": detail, "run_id": None, "observer_path": None, "ok": ok}


def account_activity(
    user_id: str,
    *,
    tenant_id: str = "default",
    limit: int = 100,
    kinds: Optional[List[str]] = None,
    data_dir: Optional[Path] = None,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
) -> Dict[str, Any]:
    uid = str(user_id or "")
    tenant = _tenant(tenant_id)
    wanted = set(kinds) if kinds else None
    limit = max(1, min(int(limit or 100), 1000))
    needle = json.dumps(uid, ensure_ascii=False).encode("utf-8")
    budget = [int(byte_budget)]
    events: List[Dict[str, Any]] = []
    oldest_line: Optional[bytes] = None
    reached_start = True
    files = audit_files(data_dir)
    for index, path in enumerate(files):
        reached_file_start = False
        try:
            for line, at_start in _lines_backwards(path, budget):
                oldest_line = line
                reached_file_start = at_start
                if needle not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(doc, dict):
                    continue
                ev = _classify(doc, uid, tenant)
                if ev is None or (wanted is not None and ev["kind"] not in wanted):
                    continue
                ev["ts_local"] = _ts_local(ev.get("ts"))
                events.append(ev)
                if len(events) >= limit:
                    break
        except OSError:
            continue
        if len(events) >= limit:
            reached_start = False
            break
        if budget[0] <= 0 and not reached_file_start:
            reached_start = False
            break
        if budget[0] <= 0 and index < len(files) - 1:
            reached_start = False
            break
    oldest_ts = _ts_of(oldest_line) if oldest_line else None
    return {
        "events": events,
        "source": "audit_log",
        "oldest_ts": oldest_ts,
        "truncated": not reached_start,
        "note": NOTE,
    }
