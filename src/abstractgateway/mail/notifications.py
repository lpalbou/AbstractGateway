"""Email notifications: per-user preferences, a durable outbox, fixed templates, a collector.

Who gets what (D4/D5): a notice goes to the user's registered address (else their mailbox
address), sent THROUGH THEIR OWN ACCOUNT with `guarded_send` — so the user's recipient
policy and send limits apply exactly as for an agent's send. No account, email turned off
by the user or by an administrator: no email notifications (no gateway-wide sender).

The user has TWO switches (preferences v2, 2026-09-30), both ON by default; nothing is sent
until a mailbox is connected and in use:

    job_failed          "Job failed": an automation of theirs failed after its retries
    approval_needed     "Approval needed": a run of theirs waits on a person (tool approval, question)

The explicit opt-ins stand on their own, with no global preference involved:

    automation result   an occurrence asked to notify (`notify` in its output) and the automation
                        delivers to email (`notify.channels` holds "email", schema v2)
    job finished/failed a run started with `_runtime.notify = {on: ["finished" | "failed"],
                        channels: ["email"]}` ("email me when done")
    notify me           a flow's Notify me step (a published alert) whose channels include
                        email (absent channels = every channel): queued when the step runs
                        (`queue_notify_me`, from the publish host), outbox key
                        `idempotency_key("notify_me", <record_key>)`

Preferences v1 (five kinds, all OFF by default) are read as v2: `job_failed = job_failed OR
automation_failed`, `approval_needed = approval_needed`; a file never saved gets the v2 defaults.

The outbox (`<plane>/email/outbox.sqlite3`) makes delivery exactly-once-or-visible:

- `idempotency_key = sha256(kind, subject id, sequence)` is the primary key: a notice is
  queued once however many times the collector sees its event;
- `queued -> sending -> sent | failed`; `sending` is committed BEFORE the SMTP exchange, so a
  crash mid-send leaves `sending`, which the next start turns into `unknown` — never resent
  automatically (the user may have received it);
- SMTP 4xx / network problems are retried with backoff; 5xx, sign-in and policy refusals are
  `failed` with the typed cause and fix (and shown in the status);
- over the send limits (100/hour, 1000/day by default, the user's own), notices wait for the
  window and then go out as ONE digest message.

Every notice is automatic mail (RFC 3834): it carries `Auto-Submitted: auto-generated` and the
framework marker (`X-AbstractFramework-Automation: notification:<key>`), and its Message-ID is
recorded here (`sent_messages`, with every other automatic send through the account: automation
occurrences, sign-in codes). The mail watcher never admits the account's own marked mail nor a
recorded Message-ID, so a notice can never trigger an automation (the 0.7.0 self-trigger loop).

Templates are fixed: one branded layout (inline styles, no remote resources) for every kind,
with a plain-text twin carrying the same facts and links. The only model-authored text is an
automation's own `notify` title/body, labelled as such. No approve/deny links (D8): the buttons
open the run's page in the console (and the session in AbstractCode when that app is installed).
An approval notice is queued once per WAIT (`wait_identity`): re-issuing the same approval at
the same node sends nothing new.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import ipaddress
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .core_mail import EmailError, EmailRateLimited, OutgoingMessage, guarded_send

from .accounts import (
    REASON_MAILBOX_NEEDS_RECONNECT,
    EmailPlane,
    SETTINGS_LABEL,
    account_store,
    email_context,
    email_usable,
    mailbox_unavailable_reason,
    _read_json,
    _write_private_json,
)
from .audit import audit_email_event

EVENTS = ("job_failed", "approval_needed")
# The two notifications are the events that need the user; both ON by default (they only
# send once a mailbox is connected and in use).
DEFAULT_PREFERENCES = {"job_failed": True, "approval_needed": True}
EVENT_LABELS = {"job_failed": "Job failed", "approval_needed": "Approval needed"}
EVENT_DESCRIPTIONS = {
    "job_failed": "A run or automation of yours failed, after its retries.",
    "approval_needed": "A run is waiting for your answer.",
}
# Preferences v1 kinds, still accepted in a PUT body and mapped (`automation_failed` ->
# `job_failed`); `automation_result` and `job_finished` are explicit per-automation / per-run
# opt-ins now and are ignored as preferences.
LEGACY_EVENTS = ("automation_result", "automation_failed", "job_finished")
PREFERENCES_VERSION = 2
RETRY_BASE_S = 60.0
RETRY_MAX_S = 3600.0
MAX_TRANSIENT_ATTEMPTS = 12
SUBJECT_PREFIX = "[AbstractFramework]"

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _plane_lock(plane: EmailPlane) -> threading.Lock:
    with _LOCKS_GUARD:
        key = "notify:" + str(plane.root)
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def idempotency_key(kind: str, subject_id: str, seq: Any = "") -> str:
    canonical = json.dumps([str(kind), str(subject_id), str(seq)], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------------------


def _prefs_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "notifications.json"


def read_preferences(plane: EmailPlane) -> Dict[str, bool]:
    """The two preferences. A v1 file (five kinds) reads as `job_failed = job_failed OR
    automation_failed`, `approval_needed = approval_needed`; a never-saved one gets the defaults."""

    doc = _read_json(_prefs_path(plane))
    stored = doc.get("email") if isinstance(doc.get("email"), dict) else None
    if stored is None:
        return dict(DEFAULT_PREFERENCES)
    if int(doc.get("version") or 1) < PREFERENCES_VERSION:
        return {
            "job_failed": bool(stored.get("job_failed")) or bool(stored.get("automation_failed")),
            "approval_needed": bool(stored.get("approval_needed")),
        }
    return {k: bool(stored.get(k, DEFAULT_PREFERENCES[k])) for k in EVENTS}


def map_legacy_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """A v1 body (`{kind: bool}` over five kinds) as v2 changes: `automation_failed` counts for
    `job_failed` (either true = on); `automation_result` / `job_finished` are dropped. Unknown
    kinds and non-booleans are a ValueError."""

    known = set(EVENTS) | set(LEGACY_EVENTS)
    unknown = sorted(k for k in changes if k not in known)
    if unknown:
        raise ValueError(f"unknown notification event(s): {', '.join(unknown)} (known: {', '.join(EVENTS)})")
    for k, v in changes.items():
        if not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
    out: Dict[str, Any] = {}
    failed = [changes[k] for k in ("job_failed", "automation_failed") if k in changes]
    if failed:
        out["job_failed"] = any(failed)
    if "approval_needed" in changes:
        out["approval_needed"] = changes["approval_needed"]
    return out


def write_preferences(plane: EmailPlane, changes: Dict[str, Any]) -> Dict[str, bool]:
    mapped = map_legacy_changes(changes)
    with _plane_lock(plane):
        current = read_preferences(plane)
        current.update({k: bool(v) for k, v in mapped.items()})
        _write_private_json(_prefs_path(plane), {"version": PREFERENCES_VERSION, "email": current, "updated_at": _now_iso()})
    return current


def preferences_public(plane: EmailPlane) -> Dict[str, Any]:
    prefs = read_preferences(plane)
    usable = email_usable(plane)
    st = None
    try:
        st = account_store(plane).settings()
    except EmailError:
        st = None
    to = st.self_address if st is not None else ""
    return {
        "channels": {"email": {"available": usable, "to": to if usable else ""}},
        "events": [{"id": k, "label": EVENT_LABELS[k], "description": EVENT_DESCRIPTIONS[k], "email": prefs[k]} for k in EVENTS],
        "email": prefs,
        "unavailable_reason": "" if usable else (mailbox_unavailable_reason(plane) or "Connect a mailbox first."),
        "outbox": NotificationOutbox(plane).summary(),
    }


# ---------------------------------------------------------------------------------------
# Templates (fixed)
# ---------------------------------------------------------------------------------------


def _has_named_host(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not host or "*" in url or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _console_link(origin: str = "") -> str:
    """Prefer an explicitly configured hostname, matching this browser when possible.

    Never turn an arbitrary Host/Forwarded/Origin header into a link in an email.
    With several configured names and no match, keep the recorded console URL.
    """
    recorded = ""
    try:
        from ..first_run import read_serve_record
        from ..users import gateway_data_dir_from_env

        rec = read_serve_record(gateway_data_dir_from_env()) or {}
        recorded = str(rec.get("console_url") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..network_exposure import live_reverse_proxy, normalize_origin, proxy_env_facts

        env_origins = proxy_env_facts().get("origins_env")
        configured = list((env_origins or {}).get("value") or []) + list(live_reverse_proxy().extra_origins)
        candidates = []
        for raw in configured:
            try:
                normalized = normalize_origin(raw)
                if not _has_named_host(normalized):
                    continue
                if normalized not in candidates:
                    candidates.append(normalized)
            except ValueError:
                continue
        if origin in candidates:
            return origin + "/console"
        if recorded and _has_named_host(recorded):
            return recorded
        if len(candidates) == 1:
            return candidates[0] + "/console"
    except Exception:  # noqa: BLE001 - unavailable network settings keep the recorded URL
        pass
    return recorded


def _gateway_base(origin: str = "") -> str:
    """The safe public origin for links in an email (`_console_link` without its `/console`)."""

    link = _console_link(origin)
    if not link:
        return ""
    parsed = urlsplit(link)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def run_console_path(run_id: str) -> str:
    """The gateway page of ONE run: `/apps/observer/#run/<id>` — the link the console's activity
    rows, `GET /host/runs` and the tray carry ("Open in Observer"). Signed out, the apps proxy
    sends the page through the console sign-in and back to that run."""

    from ..account_activity import observer_path_for

    return observer_path_for(run_id) or ""


def session_code_path(session_id: str) -> str:
    """The AbstractCode page of a session: `/apps/code/#session=<id>` (abstractcode
    web/src/workspace/app.tsx reads `session` from the hash)."""

    from urllib.parse import quote

    from ..app_proxy import APPS_PREFIX

    return f"{APPS_PREFIX}/code/#session={quote(str(session_id), safe='')}"


def _code_app_installed() -> bool:
    try:
        from ..apps_manager import AppsManager
        from ..users import gateway_data_dir_from_env

        return bool(AppsManager(gateway_data_dir_from_env()).installed_version("code"))
    except Exception:  # noqa: BLE001 - no apps state: no AbstractCode button
        return False


def recipient_time_zone(plane: EmailPlane) -> str:
    """The recipient's IANA zone: the account's saved `time_zone`, else this host's zone."""

    try:
        from ..account_preferences import stored_time_zone
        from ..users import gateway_data_dir_from_env

        zone = stored_time_zone(gateway_data_dir_from_env(), tenant_id=plane.tenant_id, user_id=plane.user_id)
        if zone:
            return str(zone)
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..automation_schedule import host_time_zone

        return str(host_time_zone() or "UTC")
    except Exception:  # noqa: BLE001
        return "UTC"


def when_sentence(iso: str, time_zone: str = "") -> str:
    """`2026-10-18 14:05 UTC · 16:05 Europe/Paris` (the recipient's zone when it is not UTC)."""

    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        at = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    utc = at.astimezone(datetime.timezone.utc)
    out = f"{utc:%Y-%m-%d %H:%M} UTC"
    zone = str(time_zone or "").strip()
    if zone and zone != "UTC":
        try:
            from zoneinfo import ZoneInfo

            local = at.astimezone(ZoneInfo(zone))
            if local.date() == utc.date():
                out += f" · {local:%H:%M} {zone}"
            else:
                out += f" · {local:%Y-%m-%d %H:%M} {zone}"
        except Exception:  # noqa: BLE001 - an unknown zone: UTC alone
            pass
    return out


# Plain-language labels of the shipped tools; any other tool reads as its name with spaces.
TOOL_LABELS = {
    "fetch_url": "Fetch a web page",
    "web_search": "Search the web",
    "browser_probe": "Open a page in the browser",
    "execute_command": "Run a command",
    "send_email": "Send an email",
    "read_file": "Read a file",
    "write_file": "Write a file",
    "edit_file": "Edit a file",
    "list_files": "List files",
    "search_files": "Search files",
    "skim": "Skim a document",
}


def tool_label(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw:
        return "a tool"
    label = TOOL_LABELS.get(raw)
    if label:
        return label
    words = raw.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "a tool"


def _argument_rows(arguments: Any) -> List[Tuple[str, str]]:
    """`[(name, value)]` of a tool call's arguments, each value whole (a person decides on what the
    tool will actually do, so nothing is cut)."""

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return [("arguments", _preview(arguments))]
    if isinstance(arguments, dict):
        return [(str(k), _preview(v)) for k, v in arguments.items()]
    if arguments in (None, "", [], {}):
        return []
    return [("arguments", _preview(arguments))]


def _preview(value: Any) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (bool, int, float)) or value is None:
        text = json.dumps(value)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    # Whole, never shortened: the person decides on what the tool will actually do (ADR-0026's
    # no-truncation rule); the mail client wraps or scrolls a long value.
    return text.strip()


def wait_identity(wait: Dict[str, Any]) -> str:
    """ONE email per wait: the part of a wait that does not change when the same approval is
    re-issued at the same node.

    A tool-approval wait key (`tool_approval:<run>:<node>:<identity>`, AbstractRuntime
    `build_tool_approval_wait_key`) changes with the runtime's effect-issuance counter, so a
    retry of the same node would be a new key and a new email. The identity used here is the
    node and the semantic tool calls (name + arguments); a question's is its wait key plus the
    prompt and choices. A different batch at the same node is a new wait (a new email)."""

    kind = str(wait.get("kind") or "")
    run_id = str(wait.get("run_id") or "")
    key = str(wait.get("wait_key") or "")
    if kind == "tool_approval":
        node = key
        prefix = f"tool_approval:{run_id}:"
        if run_id and key.startswith(prefix):
            node = key[len(prefix):].rsplit(":", 1)[0] or key
        calls = [
            {"name": str(c.get("name") or ""), "arguments": c.get("arguments")}
            for c in (wait.get("details") or [])
            if isinstance(c, dict)
        ]
        digest = hashlib.sha256(json.dumps(calls, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return f"tool_approval:{node}:{digest}"
    content = json.dumps([wait.get("prompt") or "", list(wait.get("choices") or [])], default=str)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{key}:{digest}"


# ---- the one layout (every kind) -------------------------------------------------------

BRAND = "AbstractFramework"
WEBSITE = "https://abstractframework.ai"
GITHUB = "https://github.com/lpalbou/AbstractFramework"
CONTACT = "contact@abstractframework.ai"
# The ui-kit palette (console theme tokens): navy band, accent red; the card is light so it
# reads the same in light and dark mail clients (every block names its own colours).
C_BAND = "#1a1a2e"
C_DEEP = "#0f3460"
C_ACCENT = "#e94560"
C_PAGE = "#f3f5f8"
C_CARD = "#ffffff"
C_TEXT = "#202938"
C_MUTED = "#526071"
C_LINE = "#dce1e8"
C_SOFT = "#f6f8fb"
C_WARN_LINE = "#b45309"
C_WARN_BG = "#fff8ed"

KIND_LABELS = {
    "approval_needed": "Needs your answer",
    "automation_result": "Automation result",
    "automation_failed": "Automation failed",
    "job_finished": "Run finished",
    "job_failed": "Run failed",
    "notify_me": "Flow notification",
    "test": "Test notification",
}


def _h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _block_p(text: str, *, muted: bool = False) -> str:
    colour = C_MUTED if muted else C_TEXT
    size = "14px" if muted else "16px"
    return f'<p style="margin:0 0 14px;color:{colour};font-size:{size};line-height:1.55">{_h(text)}</p>'


def _block_rows(rows: List[Tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:{C_MUTED};font-size:13px;vertical-align:top;white-space:nowrap">{_h(k)}</td>'
        f'<td style="padding:6px 0;color:{C_TEXT};font-size:14px;vertical-align:top;overflow-wrap:anywhere">{_h(v)}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 16px;border-collapse:collapse">{cells}</table>'


def _block_call(label: str, name: str, rows: List[Tuple[str, str]]) -> str:
    args = "".join(
        f'<tr><td style="padding:3px 10px 3px 0;color:{C_MUTED};font-size:13px;vertical-align:top;white-space:nowrap">{_h(k)}</td>'
        f'<td style="padding:3px 0;color:{C_TEXT};font-size:13px;font-family:Menlo,Consolas,monospace;vertical-align:top;overflow-wrap:anywhere">{_h(v)}</td></tr>'
        for k, v in rows
    )
    table = f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 0;border-collapse:collapse">{args}</table>' if rows else ""
    return (
        f'<div style="margin:0 0 12px;padding:12px 14px;background:{C_SOFT};border:1px solid {C_LINE};border-left:4px solid {C_ACCENT};border-radius:6px">'
        f'<div style="color:{C_TEXT};font-size:15px;font-weight:bold">{_h(label)}</div>'
        f'<div style="color:{C_MUTED};font-size:12px;font-family:Menlo,Consolas,monospace">{_h(name)}</div>{table}</div>'
    )


def _block_question(prompt: str, choices: List[str]) -> str:
    quote = (
        f'<div style="margin:0 0 12px;padding:12px 14px;background:{C_SOFT};border:1px solid {C_LINE};border-left:4px solid {C_DEEP};'
        f'border-radius:6px;color:{C_TEXT};font-size:16px;line-height:1.5;white-space:pre-wrap">{_h(prompt)}</div>'
    ) if prompt else ""
    items = "".join(f'<li style="margin:0 0 6px;color:{C_TEXT};font-size:15px">{_h(c)}</li>' for c in choices)
    listing = f'<p style="margin:0 0 6px;color:{C_MUTED};font-size:13px">Choices</p><ol style="margin:0 0 16px;padding-left:22px">{items}</ol>' if items else ""
    return quote + listing


def _block_model(title: str, body: str, *, label: str = "Written by the automation (model-authored text)") -> str:
    return (
        f'<div style="margin:0 0 16px;padding:12px 14px;background:{C_SOFT};border:1px solid {C_LINE};border-radius:6px">'
        f'<p style="margin:0 0 6px;color:{C_MUTED};font-size:12px">{_h(label)}</p>'
        + (f'<p style="margin:0 0 6px;color:{C_TEXT};font-size:15px;font-weight:bold">{_h(title)}</p>' if title else "")
        + (f'<div style="color:{C_TEXT};font-size:15px;line-height:1.5;white-space:pre-wrap">{_h(body)}</div>' if body else "")
        + "</div>"
    )


def _block_warning(heading: str, text: str) -> str:
    return (
        f'<div style="margin:0 0 16px;padding:14px 16px;border-left:4px solid {C_WARN_LINE};background:{C_WARN_BG};color:{C_TEXT};font-size:14px;line-height:1.5">'
        f'<strong>{_h(heading)}</strong><p style="margin:8px 0 0;color:{C_TEXT}">{_h(text)}</p></div>'
    )


def _buttons_html(buttons: List[Tuple[str, str, bool]]) -> str:
    if not buttons:
        return ""
    cells = []
    for label, url, primary in buttons:
        bg = C_ACCENT if primary else C_DEEP
        cells.append(
            f'<td style="padding:0 10px 10px 0"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{bg}" style="background:{bg};border-radius:6px"><a href="{_h(url)}" '
            f'style="display:inline-block;padding:12px 22px;color:#ffffff;font-size:15px;font-weight:bold;text-decoration:none;border-radius:6px">{_h(label)}</a></td>'
            "</tr></table></td>"
        )
    urls = "".join(
        f'<p style="margin:0 0 4px;color:{C_MUTED};font-size:12px;overflow-wrap:anywhere">{_h(label)}: <a href="{_h(url)}" style="color:{C_DEEP}">{_h(url)}</a></p>'
        for label, url, _ in buttons
    )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0 8px"><tr>{"".join(cells)}</tr></table>{urls}'
    )


def _layout(*, eyebrow: str, title: str, body_html: str, buttons: List[Tuple[str, str, bool]], footer: List[str], ref: str = "") -> str:
    footer_html = "".join(f'<p style="margin:0 0 8px;color:{C_MUTED};font-size:13px;line-height:1.5">{_h(line)}</p>' for line in footer)
    ref_html = f'<p style="margin:0 0 8px;color:{C_MUTED};font-size:12px;font-family:Menlo,Consolas,monospace;overflow-wrap:anywhere">Reference: {_h(ref)}</p>' if ref else ""
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light"></head>'
        f'<body bgcolor="{C_PAGE}" style="margin:0;padding:24px 12px;background:{C_PAGE};color:{C_TEXT};'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="max-width:600px;width:100%;margin:0 auto">'
        f'<tr><td bgcolor="{C_BAND}" style="background:{C_BAND};padding:18px 28px;border-radius:10px 10px 0 0">'
        f'<span style="color:#ffffff;font-size:20px;font-weight:bold;letter-spacing:0.2px">Abstract</span>'
        f'<span style="color:{C_ACCENT};font-size:20px;font-weight:bold;letter-spacing:0.2px">Framework</span></td></tr>'
        f'<tr><td bgcolor="{C_CARD}" style="background:{C_CARD};padding:28px;border:1px solid {C_LINE};border-top:0;border-radius:0 0 10px 10px">'
        f'<p style="margin:0 0 6px;color:{C_ACCENT};font-size:12px;font-weight:bold;letter-spacing:0.6px;text-transform:uppercase">{_h(eyebrow)}</p>'
        f'<h1 style="margin:0 0 18px;color:{C_TEXT};font-size:24px;line-height:1.3;font-weight:bold">{_h(title)}</h1>'
        f"{body_html}{_buttons_html(buttons)}"
        f'<hr style="border:0;border-top:1px solid {C_LINE};margin:22px 0">'
        f"{footer_html}{ref_html}"
        f'<p style="margin:12px 0 0;color:{C_MUTED};font-size:12px"><a href="{WEBSITE}" style="color:{C_DEEP}">abstractframework.ai</a> · '
        f'<a href="{GITHUB}" style="color:{C_DEEP}">GitHub</a> · <a href="mailto:{CONTACT}" style="color:{C_DEEP}">{CONTACT}</a></p>'
        "</td></tr></table></body></html>"
    )


def _render_test_notice(console_url: str) -> Tuple[str, str, str]:
    title = "Email notifications are working"
    intro = "Your AbstractFramework gateway successfully sent this test notification."
    why = "You requested this test from Accounts → Email → Send a test."
    security = (
        "If you did not request this test, someone else may have access to your gateway account. "
        "Rotate your gateway sign-in token, change your email password, and contact your gateway administrator."
    )
    lines = [BRAND, "", title, "", intro, ""]
    if console_url:
        lines.extend([f"Open your console: {console_url}", ""])
    lines.extend([why, "", "DIDN'T REQUEST THIS?", security, "", f"{WEBSITE} | GitHub: {GITHUB} | Email: {CONTACT}"])
    html_body = _layout(
        eyebrow=KIND_LABELS["test"],
        title=title,
        body_html=_block_p(intro) + _block_warning("Didn’t request this?", security),
        buttons=[("Open your console", console_url, True)] if console_url else [],
        footer=[why],
    )
    return f"{SUBJECT_PREFIX} Test notification", "\n".join(lines) + "\n", html_body


def _calls_of(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`[{name, arguments}]`: `calls` when the collector had the wait record, else the bare `tools` names."""

    calls = [c for c in (facts.get("calls") or []) if isinstance(c, dict) and str(c.get("name") or "")]
    if calls:
        return calls
    return [{"name": str(t), "arguments": {}} for t in (facts.get("tools") or []) if str(t)]


def _unique_labels(calls: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for c in calls:
        label = tool_label(c.get("name"))
        if label not in out:
            out.append(label)
    return out


def render_notice(kind: str, facts: Dict[str, Any]) -> Tuple[str, str, str]:
    """(subject, text, html) for one notice: ONE branded layout for every kind, the text part
    carrying the same facts and the same links.

    `facts`: `title` (the display name, never an id), `context` (`[[label, value], …]`: the
    automation or session, who started it, when it started), `event_at` (ISO) and `time_zone`
    (the recipient's), `run_id` (the deep link + the Reference line), `session_id` and
    `code_app` (an "Open in AbstractCode" button), `wait_kind` with `calls` (`[{name,
    arguments}]`) or `prompt` / `choices`, `cause` / `fix`, `model_title` / `model_body` (the
    automation's own notify text, labelled; for `notify_me` the flow's Notify me title/body, labelled
    "Written by the flow:"), `origin` (the browser's, matched against the configured public names
    only)."""

    if kind == "test":
        return _render_test_notice(_console_link(str(facts.get("origin") or "")))
    name = str(facts.get("title") or "").strip() or "Untitled"
    what = str(facts.get("wait_kind") or "")
    calls = _calls_of(facts) if kind == "approval_needed" and what == "tool_approval" else []
    labels = _unique_labels(calls)
    prompt = str(facts.get("prompt") or "").strip()
    choices = [str(c) for c in (facts.get("choices") or []) if str(c).strip()]
    cause = str(facts.get("cause") or "").strip()
    fix = str(facts.get("fix") or "").strip()
    model_title = str(facts.get("model_title") or "").strip()
    model_body = str(facts.get("model_body") or "").strip()
    run_id = str(facts.get("run_id") or "").strip()
    session_id = str(facts.get("session_id") or "").strip()
    ref = str(facts.get("ref") or "").strip()
    when = when_sentence(str(facts.get("event_at") or ""), str(facts.get("time_zone") or ""))

    # Subject + headline, in plain language (never an id).
    if kind == "approval_needed" and what == "tool_approval":
        subject = f"{SUBJECT_PREFIX} {name} needs your approval: {', '.join(labels) or 'a tool'}"
        headline = f"{name} needs your approval"
        intro = f"{name} waits for your approval before running {', '.join(labels) or 'a tool'}."
    elif kind == "approval_needed" and what == "ask_user":
        subject = f"{SUBJECT_PREFIX} {name} asked you a question"
        headline = f"{name} asked you a question"
        intro = f"{name} asked you a question and waits for your answer."
    elif kind == "approval_needed":
        subject = f"{SUBJECT_PREFIX} {name} waits for you"
        headline = f"{name} waits for you"
        intro = f"{name} waits for you before it continues."
    elif kind == "automation_result":
        subject = f"{SUBJECT_PREFIX} {name} finished"
        headline = f"{name} finished"
        intro = f"The automation {name} finished an occurrence."
    elif kind == "automation_failed":
        subject = f"{SUBJECT_PREFIX} {name} failed"
        headline = f"{name} failed"
        intro = f"The automation {name} failed after its retries. It is not paused: the next occurrence runs as scheduled."
    elif kind == "job_finished":
        subject = f"{SUBJECT_PREFIX} {name} finished"
        headline = f"{name} finished"
        intro = f"The run {name} finished."
    elif kind == "job_failed":
        subject = f"{SUBJECT_PREFIX} {name} failed"
        headline = f"{name} failed"
        intro = f"The run {name} failed."
    elif kind == "notify_me":
        subject = f"{SUBJECT_PREFIX} {name}: notification"
        headline = f"{name} notified you"
        intro = f"“{name}” notified you."
    else:
        subject = f"{SUBJECT_PREFIX} {name}: {kind}"
        headline = name
        intro = ""

    # Context rows: what asked, who started it, when.
    rows: List[Tuple[str, str]] = []
    for item in facts.get("context") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and str(item[1] or "").strip():
            rows.append((str(item[0]), str(item[1]).strip()))
    started = when_sentence(str(facts.get("started_at") or ""), str(facts.get("time_zone") or ""))
    if started:
        rows.append(("Started", started))
    if when:
        rows.append(("Waiting since" if kind == "approval_needed" else "When", when))

    # Buttons: the exact wait (the run's page) and, for a session the AbstractCode app can show, its page.
    base = _gateway_base(str(facts.get("origin") or ""))
    buttons: List[Tuple[str, str, bool]] = []
    if base and run_id:
        buttons.append(("Open in the console", base + run_console_path(run_id), True))
    elif base:
        buttons.append(("Open in the console", base + "/console", True))
    if base and session_id and bool(facts.get("code_app")):
        buttons.append(("Open in AbstractCode", base + session_code_path(session_id), False))

    # Text twin.
    lines: List[str] = [BRAND, "", headline, ""]
    if intro:
        lines.extend([intro, ""])
    for k, v in rows:
        lines.append(f"{k}: {v}")
    if rows:
        lines.append("")
    body_html = _block_p(intro) if intro else ""
    if rows:
        body_html += _block_rows(rows)
    if calls:
        for c in calls:
            arg_rows = _argument_rows(c.get("arguments"))
            label = tool_label(c.get("name"))
            lines.append(f"{label} ({c.get('name')})")
            for k, v in arg_rows:
                lines.append(f"  {k}: {v}")
            lines.append("")
            body_html += _block_call(label, str(c.get("name") or ""), arg_rows)
    if kind == "approval_needed" and what != "tool_approval" and (prompt or choices):
        if prompt:
            lines.extend([prompt, ""])
        if choices:
            lines.append("Choices:")
            lines.extend(f"  {i}. {c}" for i, c in enumerate(choices, 1))
            lines.append("")
        body_html += _block_question(prompt, choices)
    if kind == "approval_needed":
        action = "Open the console to approve, deny or answer."
        lines.extend([action, ""])
        body_html += _block_p(action)
    if cause:
        lines.append(f"Cause: {cause}")
        body_html += _block_rows([("Cause", cause)] + ([("Fix", fix)] if fix else []))
    elif fix:
        body_html += _block_rows([("Fix", fix)])
    if fix:
        lines.append(f"Fix: {fix}")
    if cause or fix:
        lines.append("")
    if model_title or model_body:
        model_label = "Written by the flow" if kind == "notify_me" else "Written by the automation (model-authored text)"
        lines.append(model_label + ":")
        if model_title and model_title != name:
            lines.append(model_title)
        if model_body:
            lines.append(model_body)
        lines.append("")
        body_html += _block_model(model_title if model_title != name else "", model_body, label=model_label)
    for label, url, _ in buttons:
        lines.append(f"{label}: {url}")
    if buttons:
        lines.append("")
    footer = [_why_line(kind), "Replying to this email does nothing."]
    lines.extend(footer)
    if ref:
        lines.extend(["", f"Reference: {ref}"])
    text = "\n".join(lines).strip() + "\n"
    html_body = _layout(
        eyebrow=KIND_LABELS.get(kind, kind), title=headline, body_html=body_html, buttons=buttons, footer=footer, ref=ref,
    )
    return subject, text, html_body


def _why_line(kind: str) -> str:
    if kind == "automation_result":
        return "You receive this because this automation is set to \u201cEmail result\u201d."
    if kind in ("job_finished", "job_failed"):
        return "You receive this because this run was started with \u201cemail me when done\u201d."
    if kind == "notify_me":
        return "You receive this because this flow has a Notify me step that reaches you by email."
    if kind == "automation_failed":
        return f"You receive this because \u201cJob failed\u201d is on in {SETTINGS_LABEL} \u2192 Notifications."
    if kind == "approval_needed":
        return f"You receive this because \u201cApproval needed\u201d is on in {SETTINGS_LABEL} \u2192 Notifications."
    return f"You receive this because you asked for a test notification in {SETTINGS_LABEL}."


def render_digest(rows: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} {len(rows)} notifications"
    lines = [
        f"{len(rows)} notifications were held back by your send limits and are grouped here.",
        "",
    ]
    for row in rows:
        lines.append(f"— {row['subject'][len(SUBJECT_PREFIX):].strip() if row['subject'].startswith(SUBJECT_PREFIX) else row['subject']}")
        for line in str(row.get("text") or "").splitlines():
            if line.startswith("You receive this because"):
                continue
            lines.append(f"   {line}" if line else "")
        lines.append("")
    lines.append(f"You receive this because email notifications are on in {SETTINGS_LABEL} → Notifications.")
    text = "\n".join(lines).strip() + "\n"
    html_body = "<!doctype html><html><body style=\"font-family:sans-serif\">" + "".join(
        f"<p>{html.escape(line)}</p>" for line in lines if line
    ) + "</body></html>"
    return subject, text, html_body


# ---------------------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------------------


class NotificationOutbox:
    STATES = ("queued", "sending", "sent", "failed", "unknown")

    def __init__(self, plane: EmailPlane, *, clock: Callable[[], float] = time.time) -> None:
        self.plane = plane
        self.path = plane.email_dir / "outbox.sqlite3"
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notices ("
            " idempotency_key TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL,"
            " subject TEXT NOT NULL,"
            " text TEXT NOT NULL,"
            " html TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL,"
            " state TEXT NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " next_attempt_ts REAL NOT NULL DEFAULT 0,"
            " coalesce_flag INTEGER NOT NULL DEFAULT 0,"
            " sent_at TEXT NOT NULL DEFAULT '',"
            " message_id TEXT NOT NULL DEFAULT '',"
            " smtp_code INTEGER,"
            " error_code TEXT NOT NULL DEFAULT '',"
            " error_cause TEXT NOT NULL DEFAULT '',"
            " error_fix TEXT NOT NULL DEFAULT '',"
            " to_self INTEGER NOT NULL DEFAULT 1)"
        )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(notices)")}
        # recipients_json (round 3); subject_ref (round 16): what a notice is about,
        # "automation:<id>" for an automation's notices, so its summary can show the last one.
        for column, ddl in (
            ("recipients_json", "recipients_json TEXT NOT NULL DEFAULT '[\"self\"]'"),
            ("subject_ref", "subject_ref TEXT NOT NULL DEFAULT ''"),
        ):
            if column in columns:
                continue
            try:
                conn.execute(f"ALTER TABLE notices ADD COLUMN {ddl}")
                conn.commit()
            except sqlite3.OperationalError:
                # Another connection may have completed the same migration.
                if column not in {r[1] for r in conn.execute("PRAGMA table_info(notices)")}:
                    raise
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_messages ("
            " message_id TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL,"
            " marker TEXT NOT NULL DEFAULT '',"
            " sent_at TEXT NOT NULL)"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    # -- writes ----------------------------------------------------------------------------

    def enqueue(self, key: str, kind: str, subject: str, text: str, html_body: str = "", *, recipients: Optional[List[str]] = None, subject_ref: str = "") -> bool:
        """Queue one notice. False when this idempotency key was queued before (any state)."""

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO notices(idempotency_key, kind, subject, text, html, created_at, recipients_json, subject_ref, state)"
                    " VALUES (?,?,?,?,?,?,?,?, 'queued')",
                    (str(key), str(kind), str(subject), str(text), str(html_body or ""), _now_iso(), json.dumps(recipients or ["self"]), str(subject_ref or "")),
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def record_sent(self, message_id: str, *, kind: str, marker: str = "") -> None:
        """Remember a Message-ID this account sent automatically (the watcher skips it).

        Never takes the plane lock: it runs from inside a send (`EmailContext.on_sent`),
        possibly while `deliver` holds that lock.
        """

        mid = str(message_id or "").strip()
        if not mid:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO sent_messages(message_id, kind, marker, sent_at) VALUES (?,?,?,?)",
                    (mid, str(kind), str(marker or ""), _now_iso()),
                )
        finally:
            conn.close()

    def was_sent(self, message_id: str) -> bool:
        """Did the framework send this Message-ID from this account automatically (recorded by
        the account context's `on_sent` for every marked send, notices included)? False when
        nothing was ever sent (no outbox file)."""

        mid = str(message_id or "").strip()
        if not mid or not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM sent_messages WHERE message_id=? LIMIT 1", (mid,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def recover_interrupted(self) -> int:
        """`sending` rows left by a crash become `unknown`: the SMTP exchange may have
        completed, so they are never resent automatically."""

        if not self.path.exists():
            return 0  # lazy: no outbox file until a notice exists
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE notices SET state='unknown', error_code='email_send_interrupted',"
                    " error_cause='The gateway stopped while this notice was being sent; it may or may not have arrived.',"
                    " error_fix='Nothing to do; it is not resent automatically so you never get it twice.'"
                    " WHERE state='sending'"
                )
                n = int(cur.rowcount or 0)
        finally:
            conn.close()
        return n

    def _set(self, conn: sqlite3.Connection, keys: Iterable[str], **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        for key in keys:
            conn.execute(f"UPDATE notices SET {cols} WHERE idempotency_key=?", (*fields.values(), key))

    # -- reads -----------------------------------------------------------------------------

    def rows(self, *, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            if state:
                cur = conn.execute("SELECT * FROM notices WHERE state=? ORDER BY created_at, rowid LIMIT ?", (state, int(limit)))
            else:
                cur = conn.execute("SELECT * FROM notices ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def summary(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"queued": 0, "sent": 0, "failed": 0, "unknown": 0, "last_failure": None}
        conn = self._connect()
        try:
            counts = {r[0]: int(r[1]) for r in conn.execute("SELECT state, COUNT(*) FROM notices GROUP BY state")}
            last = conn.execute(
                "SELECT kind, error_code, error_cause, error_fix, created_at FROM notices WHERE state='failed'"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        waiting = self._waiting_on_limit()
        return {
            "rate_limited": waiting,
            "queued": counts.get("queued", 0) + counts.get("sending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "unknown": counts.get("unknown", 0),
            "last_failure": (
                {"kind": last["kind"], "code": last["error_code"], "cause": last["error_cause"], "fix": last["error_fix"], "at": last["created_at"]}
                if last
                else None
            ),
        }

    def _waiting_on_limit(self) -> Optional[Dict[str, Any]]:
        """Queued notices held back by the send limit: {count, cause, resets_at} or None."""

        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(next_attempt_ts) AS ts, MAX(error_cause) AS cause FROM notices"
                " WHERE state='queued' AND error_code='email_rate_limited'"
            ).fetchone()
        finally:
            conn.close()
        if not row or not int(row["n"] or 0):
            return None
        return {"count": int(row["n"]), "cause": str(row["cause"] or ""), "resets_at": _iso_from_ts(float(row["ts"] or 0.0))}

    def last_for(self, subject_ref: str, kind: str) -> Optional[Dict[str, Any]]:
        """The newest notice of `kind` about `subject_ref` (e.g. "automation:<id>"), or None."""

        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM notices WHERE subject_ref=? AND kind=? ORDER BY rowid DESC LIMIT 1", (str(subject_ref), str(kind))
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def queued_before(self, key: str) -> int:
        """Queued notices other than `key` (the ones a new notice waits behind)."""

        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM notices WHERE state='queued' AND idempotency_key<>?", (key,)).fetchone()
        finally:
            conn.close()
        return int(row[0] or 0) if row else 0

    # -- delivery --------------------------------------------------------------------------

    def deliver(self, *, send: Optional[Callable[[Any, OutgoingMessage], Any]] = None) -> Dict[str, int]:
        """Send what is due. `send(ctx, message)` defaults to `guarded_send` (tests inject
        a failing one to prove crash semantics)."""

        with _plane_lock(self.plane):
            return self._deliver_locked(send or guarded_send)

    def _deliver_locked(self, send: Callable[[Any, OutgoingMessage], Any]) -> Dict[str, int]:
        out = {"sent": 0, "failed": 0, "deferred": 0}
        if not self.path.exists():
            return out
        now = self._clock()
        conn = self._connect()
        try:
            ready = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM notices WHERE state='queued' AND next_attempt_ts <= ? ORDER BY created_at, rowid", (now,)
                ).fetchall()
            ]
        finally:
            conn.close()
        if not ready:
            return out
        try:
            ctx = email_context(self.plane)
            to = str(ctx.registered_address or ctx.account.address or "").strip()
            if not to:
                raise EmailError(
                    "There is no address to send notifications to.",
                    f"Set your registered email (Users → your account) or connect a mailbox ({SETTINGS_LABEL}).",
                    code="email_no_recipient",
                )
        except EmailError as err:
            # No usable account (D5: no gateway-wide sender): these notices cannot go out.
            self._mark_failed([r["idempotency_key"] for r in ready], err)
            out["failed"] += len(ready)
            return out

        groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        for row in ready:
            recipients = tuple(sorted(set(to if address == "self" else address for address in json.loads(row["recipients_json"]))))
            groups.setdefault(recipients, []).append(row)
        batches: List[List[Dict[str, Any]]] = []
        for rows in groups.values():
            coalesce = len(rows) > 1 and any(int(r.get("coalesce_flag") or 0) for r in rows)
            batches.extend([rows] if coalesce else [[r] for r in rows])
        for batch in batches:
            keys = [r["idempotency_key"] for r in batch]
            if len(batch) == 1:
                subject, text, html_body = batch[0]["subject"], batch[0]["text"], batch[0]["html"]
            else:
                subject, text, html_body = render_digest(batch)
            marker = f"notification:{keys[0]}" if len(keys) == 1 else f"notification-digest:{keys[0]}"
            message = OutgoingMessage(
                to=tuple(sorted(set(to if address == "self" else address for address in json.loads(batch[0]["recipients_json"])))), subject=subject, text=text, html=html_body,
                auto_submitted="auto-generated", automation_marker=marker[:200],
            )
            conn = self._connect()
            try:
                with conn:  # committed (fsynced) BEFORE the SMTP exchange
                    self._set(conn, keys, state="sending", attempts=int(batch[0].get("attempts") or 0) + 1)
            finally:
                conn.close()
            try:
                result = send(ctx, message)
            except EmailRateLimited as err:
                retry = float((err.details or {}).get("retry_after_s") or 0.0) or RETRY_BASE_S
                # The rows keep WHY they wait (code + cause + fix) and WHEN they go
                # (next_attempt_ts), so a later read can say "hourly limit reached - resets at 14:05".
                self._requeue(keys, delay=retry, coalesce=True, err=err)
                out["deferred"] += len(keys)
                d = err.details or {}
                out["rate_limited"] = {
                    "window": d.get("window"), "limit": d.get("limit"), "used": d.get("used"),
                    "resets_at_ts": self._clock() + max(1.0, retry),
                }
                # Everything else waits for the same window: stop here.
                remaining = [r["idempotency_key"] for b in batches[batches.index(batch) + 1 :] for r in b]
                if remaining:
                    self._requeue(remaining, delay=retry, coalesce=True, err=err)
                    out["deferred"] += len(remaining)
                break
            except EmailError as err:
                attempts = int(batch[0].get("attempts") or 0) + 1
                if err.retryable and attempts < MAX_TRANSIENT_ATTEMPTS:
                    self._requeue(keys, delay=min(RETRY_MAX_S, RETRY_BASE_S * (2 ** (attempts - 1))), coalesce=False, err=err)
                    out["deferred"] += len(keys)
                else:
                    self._mark_failed(keys, err)
                    out["failed"] += len(keys)
                continue
            except Exception as exc:  # noqa: BLE001 - unknown outcome: never resend
                self._mark_unknown(keys, exc)
                continue
            message_id = str(getattr(result, "message_id", "") or "")
            conn = self._connect()
            try:
                with conn:
                    self._set(conn, keys, state="sent", sent_at=_now_iso(), message_id=message_id, error_code="", error_cause="", error_fix="")
            finally:
                conn.close()
            out["sent"] += len(keys)
            self._stream_outcome("sent", batch, message_id=message_id)
            for row in batch:
                audit_email_event(
                    "email.notification_sent",
                    tenant_id=self.plane.tenant_id,
                    user_id=self.plane.user_id,
                    kind=row["kind"],
                    idempotency_key=row["idempotency_key"],
                    message_id=message_id,
                    outcome="sent" if len(batch) == 1 else "sent_in_digest",
                )
        return out

    def _requeue(self, keys: List[str], *, delay: float, coalesce: bool, err: Optional[EmailError] = None) -> None:
        conn = self._connect()
        try:
            with conn:
                fields: Dict[str, Any] = {"state": "queued", "next_attempt_ts": self._clock() + max(1.0, float(delay))}
                if coalesce:
                    fields["coalesce_flag"] = 1
                if err is not None:
                    fields.update(error_code=err.code, error_cause=err.cause, error_fix=err.fix)
                self._set(conn, keys, **fields)
        finally:
            conn.close()

    def _stream_outcome(self, outcome: str, rows: List[Dict[str, Any]], **data: Any) -> None:
        """`notification.sent|failed {channel: email}` in the plane owner's event stream
        (backlog 1046), one per notice, after the outbox row changed. Never raises: the outbox
        row is the delivery truth."""
        try:
            from ..events_stream import record_notification, stream_for

            stream = stream_for(self.plane.runtime_data_dir, self.plane.tenant_id, self.plane.user_id)
            for row in rows:
                record_notification(
                    stream, outcome, channel="email", notice=str(row.get("idempotency_key") or ""),
                    data={"kind": row.get("kind"), "subject_ref": row.get("subject_ref") or None, **data},
                )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception("notification.%s stream append failed", outcome)

    def _rows_for(self, keys: List[str]) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            marks = ",".join("?" for _ in keys)
            return [dict(r) for r in conn.execute(f"SELECT idempotency_key, kind, subject_ref FROM notices WHERE idempotency_key IN ({marks})", tuple(keys)).fetchall()]
        finally:
            conn.close()

    def _mark_failed(self, keys: List[str], err: EmailError) -> None:
        conn = self._connect()
        try:
            with conn:
                smtp_code = (err.details or {}).get("smtp_code")
                self._set(
                    conn, keys, state="failed", error_code=err.code, error_cause=err.cause, error_fix=err.fix,
                    smtp_code=int(smtp_code) if isinstance(smtp_code, int) else None,
                )
        finally:
            conn.close()
        if keys:
            self._stream_outcome("failed", self._rows_for(keys), code=err.code, cause=err.cause)
        for key in keys:
            audit_email_event(
                "email.notification_failed",
                tenant_id=self.plane.tenant_id,
                user_id=self.plane.user_id,
                idempotency_key=key,
                code=err.code,
                cause=err.cause,
                fix=err.fix,
            )
        try:
            # needs_reconnect is the mailbox's own state (its reason is shown); not a send error.
            if err.code not in ("email_not_configured", "email_disabled", "email_needs_reconnect"):
                account_store(self.plane).record_error(err)
        except Exception:  # noqa: BLE001
            pass

    def _mark_unknown(self, keys: List[str], exc: BaseException) -> None:
        conn = self._connect()
        try:
            with conn:
                self._set(
                    conn, keys, state="unknown", error_code="email_send_outcome_unknown",
                    error_cause=f"Sending failed in an unexpected way ({type(exc).__name__}); the notice may or may not have arrived.",
                    error_fix="Nothing to do; it is not resent automatically so you never get it twice.",
                )
        finally:
            conn.close()
        if keys:
            self._stream_outcome("failed", self._rows_for(keys), code="email_send_outcome_unknown", cause="the notice may or may not have arrived")


# ---------------------------------------------------------------------------------------
# Collector: events -> queued notices
# ---------------------------------------------------------------------------------------


def _collector_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "notify_cursor.json"


def queue_notice(plane: EmailPlane, kind: str, key: str, facts: Dict[str, Any]) -> bool:
    subject, text, html_body = render_notice(kind, facts)
    return NotificationOutbox(plane).enqueue(
        key, kind, subject, text, html_body, recipients=facts.get("recipients"), subject_ref=str(facts.get("subject_ref") or "")
    )


def notify_me_key(record_key: str) -> str:
    """The outbox key of a Notify me email (one per published alert, a replayed step queues nothing)."""
    return idempotency_key("notify_me", record_key)


def queue_notify_me(plane: EmailPlane, *, record_key: str, run: Any, title: str, body: Optional[str] = None) -> bool:
    """Queue the email of a published alert (a flow's Notify me with email among its channels) when
    the person's mailbox is usable; the mail worker sends it and records `notification.sent|failed
    {channel: email}`. False when nothing was queued (no usable mailbox, or queued before)."""
    if not email_usable(plane):
        return False
    rid = str(getattr(run, "run_id", "") or "")
    facts: Dict[str, Any] = {"title": _run_title(run), "model_title": title, "model_body": body or "",
                             "ref": f"run {rid}", "subject_ref": f"run:{rid}", "recipients": ["self"],
                             "run_id": rid, "session_id": str(getattr(run, "session_id", "") or ""),
                             "code_app": _code_app_installed(), "context": _run_context(run, None),
                             "started_at": str(getattr(run, "created_at", "") or ""),
                             "event_at": _now_iso(), "time_zone": recipient_time_zone(plane)}
    return queue_notice(plane, "notify_me", notify_me_key(record_key), facts)


_LAST_NOTIFICATION_WORDS = {
    "sent": "Email result sent",
    "queued": "Email result queued",
    "sending": "Email result sending",
    "failed": "Email result failed",
    "unknown": "Email result: outcome unknown",
}


def last_automation_notification(plane: EmailPlane, automation_id: str) -> Optional[Dict[str, Any]]:
    """The automation summary's `last_notification` (round 16): the newest "Email result" notice
    of this automation, `{channel: "email", status, at, sent_at, code, sentence, text}` — `text` is
    THE line clients show ("Email result failed \u2014 <cause>", "Email result sent"); None when
    the automation never asked for an email."""

    row = NotificationOutbox(plane).last_for(f"automation:{automation_id}", "automation_result")
    if row is None:
        return None
    state = str(row.get("state") or "")
    cause = str(row.get("error_cause") or "").strip()
    words = _LAST_NOTIFICATION_WORDS.get(state, f"Email result {state}")
    text = f"{words} \u2014 {cause}" if cause and state in ("failed", "unknown") else words
    return {
        "channel": "email",
        "status": state,
        "at": str(row.get("created_at") or "") or None,
        "sent_at": str(row.get("sent_at") or "") or None,
        "code": str(row.get("error_code") or "") or None,
        "sentence": cause or None,
        "text": text,
    }


def _iso_from_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).replace(microsecond=0).isoformat()


def _local_hhmm(ts: float, *, now: Optional[float] = None) -> str:
    """The gateway's local time, "14:05" (today) / "tomorrow at 14:05" / "Oct 3 at 14:05"."""

    at = datetime.datetime.fromtimestamp(float(ts))
    today = datetime.datetime.fromtimestamp(float(now if now is not None else time.time())).date()
    if at.date() == today:
        return at.strftime("%H:%M")
    if at.date() == today + datetime.timedelta(days=1):
        return "tomorrow at " + at.strftime("%H:%M")
    return at.strftime("%b %-d at %H:%M")


_WINDOW_WORDS = {"hour": ("hourly", "this hour"), "day": ("daily", "today")}


def _limit_sentence(limit: Dict[str, Any], resets_ts: float) -> str:
    adjective, span = _WINDOW_WORDS.get(str(limit.get("window") or ""), ("send", "in this window"))
    return f"{adjective} limit reached ({limit.get('used')} of {limit.get('limit')} {span}) \u2014 resets at {_local_hhmm(resets_ts)}"


def _smtp_where(plane: EmailPlane) -> Tuple[str, str]:
    try:
        st = account_store(plane).settings()
    except EmailError:
        return "", ""
    smtp = st.account.smtp if st.account is not None else None
    if smtp is None:
        return "", ""
    host = str(getattr(smtp, "host", "") or "")
    port = getattr(smtp, "port", None)
    return host, (f"{host}:{port}" if port else host)


def _send_failed_sentence(plane: EmailPlane, code: str, cause: str) -> str:
    """"Not sent: <why>." for a failed send, from the error code (an explicit table)."""

    host, where = _smtp_where(plane)
    cause = str(cause or "").strip().rstrip(".")
    if code == "email_no_recipient":
        return "Not sent: there is no address to send it to \u2014 set your email address first."
    if code == "email_auth_failed" and host:
        return f"Not sent: {host} refused the sign-in \u2014 check the mailbox password ({cause})."
    if code in ("email_unreachable", "email_transient") and where:
        return f"Not sent: couldn't reach {where} ({cause})."
    if code == "email_tls_failed" and where:
        return f"Not sent: couldn't set up a secure connection to {where} ({cause})."
    if code == "email_policy_refused":
        return f"Not sent: your recipient rules refused it ({cause})."
    if host:
        return f"Not sent: {host} refused the message ({cause})." if cause else f"Not sent: {host} refused the message."
    return f"Not sent: {cause}." if cause else "Not sent."


def send_test_notification(plane: EmailPlane, *, origin: str = "") -> Dict[str, Any]:
    """Queue and deliver one test notice now (the click is the request). The answer always
    carries a sentence (`message`) and a `reason_code` (null when sent): no_mailbox |
    mailbox_paused | rate_limited | queued_behind | send_failed; `limit` {window, limit, used,
    resets_at} when a send limit held it back. `state` / `error` / `delivery` stay as before."""

    base: Dict[str, Any] = {"sent": False, "reason_code": None, "message": "", "limit": None}
    if not email_usable(plane):
        try:
            email_context(plane)  # raises the precise typed reason (not connected / turned off)
        except EmailError as err:
            from .accounts import admin_email_enabled

            if err.code == "email_not_configured":
                reason, msg = "no_mailbox", "Not sent: no mailbox connected."
            elif err.code == "email_disabled" and not admin_email_enabled(plane):
                reason, msg = "send_failed", "Not sent: your admin turned mailboxes off for your account."
            elif err.code == "email_disabled":
                reason, msg = "mailbox_paused", "Not sent: your mailbox is paused."
            else:
                reason, msg = "send_failed", _send_failed_sentence(plane, err.code, err.cause)
            return {
                **base, "ok": False, "state": "not sent", "reason_code": reason, "message": msg,
                "error": {"code": err.code, "cause": err.cause, "fix": err.fix},
            }
    key = idempotency_key("test", plane.key, time.time_ns())
    queue_notice(plane, "test", key, {"title": "Test", "origin": origin})
    outbox = NotificationOutbox(plane)
    result = outbox.deliver()
    row = next((r for r in outbox.rows(limit=50) if r["idempotency_key"] == key), None)
    state = row["state"] if row else "unknown"
    out: Dict[str, Any] = {**base, "ok": state == "sent", "state": state, "delivery": result}
    if row and state != "sent":
        out["error"] = {"code": row["error_code"], "cause": row["error_cause"], "fix": row["error_fix"]}
    if state == "sent":
        try:
            ctx = email_context(plane)
            to = str(ctx.registered_address or ctx.account.address or "").strip()
        except EmailError:
            to = ""
        out.update(sent=True, message=f"Sent to {to}." if to else "Sent.")
        return out
    code = str((row or {}).get("error_code") or "")
    if state == "queued" and code == "email_rate_limited":
        rl = result.get("rate_limited") or {}
        resets_ts = float(rl.get("resets_at_ts") or (row or {}).get("next_attempt_ts") or time.time())
        limit = {"window": rl.get("window"), "limit": rl.get("limit"), "used": rl.get("used"), "resets_at": _iso_from_ts(resets_ts)}
        out["limit"] = limit
        behind = outbox.queued_before(key)
        if behind:
            noun = "notification" if behind == 1 else "notifications"
            out.update(
                reason_code="queued_behind", queued_behind=behind,
                message=f"Queued behind {behind} earlier {noun}; they go out when the limit resets at {_local_hhmm(resets_ts)}.",
            )
        else:
            out.update(reason_code="rate_limited", message=f"Not sent: {_limit_sentence(limit, resets_ts)}.")
        return out
    if state == "queued":
        # A transient failure: retried automatically.
        sentence = _send_failed_sentence(plane, code, str((row or {}).get("error_cause") or ""))
        out.update(reason_code="send_failed", message=sentence[:-1] + "; it is retried automatically.")
        return out
    out.update(reason_code="send_failed", message=_send_failed_sentence(plane, code, str((row or {}).get("error_cause") or "")))
    return out


def _own_title(run: Any) -> str:
    """The run's own title (`_meta.title`, `vars.title`), "" when it has none."""

    vars0 = getattr(run, "vars", None) or {}
    meta = vars0.get("_meta") if isinstance(vars0, dict) else None
    for candidate in (
        (meta or {}).get("title") if isinstance(meta, dict) else None,
        vars0.get("title") if isinstance(vars0, dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _load_run(run_store: Any, run_id: Any) -> Any:
    load = getattr(run_store, "load", None)
    if not callable(load) or not run_id:
        return None
    try:
        return load(str(run_id))
    except Exception:  # noqa: BLE001
        return None


def _root_run(run: Any, run_store: Any, *, max_depth: int = 8) -> Any:
    """The top of the run's parent chain (the one a person started), `run` itself at the top."""

    current = run
    seen = set()
    for _ in range(max_depth):
        parent_id = getattr(current, "parent_run_id", None)
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        parent = _load_run(run_store, parent_id)
        if parent is None:
            break
        current = parent
    return current


def _run_title(run: Any, run_store: Any = None) -> str:
    """The display name: the run's own title, else the title of the run a person started (a
    sub-run of a flow is named after its node, which is not a name), else that run's workflow."""

    own = _own_title(run)
    if own:
        return own
    root = _root_run(run, run_store) if run_store is not None else run
    for candidate in (_own_title(root) if root is not run else "", getattr(root, "workflow_id", None), getattr(run, "workflow_id", None)):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(getattr(run, "run_id", "") or "run")


def _started_by(run: Any) -> str:
    """Who started the run, from its `actor_id`: `person:<id>` → `<id>`, `entity:<name>` →
    `<name> (entity)`, "" for the gateway's own starts and unknown actors."""

    actor = str(getattr(run, "actor_id", "") or "").strip()
    if not actor or actor == "gateway":
        return ""
    if actor.startswith("person:"):
        return actor[len("person:"):] or ""
    if actor.startswith("entity:"):
        return f"{actor[len('entity:'):]} (entity)"
    return actor


def _run_context(run: Any, run_store: Any, *, label: str = "Workflow") -> List[List[str]]:
    """`[[label, value], …]` for the context block: the workflow (or automation), who started
    the run a person started, and when it started."""

    rows: List[List[str]] = []
    if run is None:
        return rows
    root = _root_run(run, run_store)
    title = _run_title(run, run_store)
    if title:
        rows.append([label, title])
    who = _started_by(root) or _started_by(run)
    if who:
        rows.append(["Started by", who])
    return rows


def _started_at(run: Any, run_store: Any) -> str:
    """When the run a person started began (ISO), "" when unknown."""

    if run is None:
        return ""
    root = _root_run(run, run_store)
    return str(getattr(root, "created_at", "") or getattr(run, "created_at", "") or "")


def _wait_facts(wait: Dict[str, Any], run: Any, run_store: Any, *, title: str, context: List[List[str]], ref: str, time_zone: str, code_app: bool) -> Dict[str, Any]:
    """The approval notice's facts from a typed wait (`typed_wait` / `pending_waits`: prompt,
    choices, the tool calls with their arguments) and the run record when it loads."""

    kind = str(wait.get("kind") or "")
    details = [c for c in (wait.get("details") or []) if isinstance(c, dict)] if kind == "tool_approval" else []
    facts: Dict[str, Any] = {
        "title": title,
        "wait_kind": kind,
        "tools": [str(c.get("name") or "") for c in details],
        "calls": [{"name": str(c.get("name") or ""), "arguments": c.get("arguments")} for c in details],
        "prompt": wait.get("prompt") or "",
        "choices": list(wait.get("choices") or []),
        "context": context,
        "run_id": str(wait.get("run_id") or ""),
        "session_id": str(getattr(run, "session_id", "") or "") if run is not None else "",
        "code_app": code_app,
        "started_at": _started_at(run, run_store),
        "event_at": str(getattr(run, "updated_at", "") or "") if run is not None else "",
        "time_zone": time_zone,
        "ref": ref,
    }
    return facts


def _run_notify(run: Any) -> Dict[str, Any]:
    vars0 = getattr(run, "vars", None) or {}
    rt = vars0.get("_runtime") if isinstance(vars0, dict) else None
    notify = rt.get("notify") if isinstance(rt, dict) else None
    return notify if isinstance(notify, dict) else {}


def _run_error(run: Any) -> Tuple[str, str]:
    err = getattr(run, "error", None)
    if isinstance(err, dict):
        return str(err.get("cause") or err.get("message") or ""), str(err.get("fix") or "")
    if isinstance(err, str):
        return err.strip(), ""
    return "", ""


def _change_signal(store: Any) -> Optional[Any]:
    """The object of a store chain (walking `inner` / `_inner` wrappers) that carries the change
    signal (`change_token()` + `changed_run_ids(token)`: AbstractRuntime's JsonFileRunStore and
    ObservableLedgerStore), else None."""
    obj = store
    for _ in range(6):
        if obj is None:
            return None
        if callable(getattr(type(obj), "change_token", None)) and callable(getattr(type(obj), "changed_run_ids", None)):
            return obj
        nxt = getattr(obj, "inner", None)
        if nxt is None:
            nxt = getattr(obj, "_inner", None)
        obj = nxt
    return None


# What the last pass saw, per plane (collector file path): the stores' change tokens before it
# read, the switches it ran under, and when the last FULL pass ran. Process-local: a restart
# starts with a full pass.
_COLLECT_MEMO: Dict[str, Dict[str, Any]] = {}
_COLLECT_MEMO_LOCK = threading.Lock()
# A full pass at least this often even when the tokens say nothing changed: what the in-process
# signals cannot see (another process writing the same folder) is read within this delay.
FULL_PASS_S = 300.0
# A finished run's notice is decided once: the full pass re-reads a COMPLETED/FAILED run only when
# its index row (status, updated_at) is new since the last decision. The persisted cursor
# (`jobs.through`) lets the first pass after a restart skip what an earlier process decided;
# it trails the newest decided `updated_at` by this overlap (saves stamped out of order).
FINISHED_OVERLAP_S = 120.0


def _ts(value: Any) -> Optional[float]:
    """An ISO timestamp as epoch seconds; None when it is not one (never skipped then)."""
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _index_rows(run_store: Any, status: Any, limit: int) -> Optional[List[Dict[str, Any]]]:
    """The store's lightweight index rows (`list_run_index`: status, updated_at, wait_reason, no
    vars); None for a store without them (the caller then lists full runs, as before)."""
    fn = getattr(run_store, "list_run_index", None)
    if not callable(fn):
        return None
    try:
        rows = fn(status=status, limit=limit)
    except Exception:  # noqa: BLE001
        return None
    return [r for r in rows if isinstance(r, dict) and r.get("run_id")] if isinstance(rows, list) else None


class NotificationCollector:
    """Turns the plane's ledger / run facts into queued notices (never sends).

    CHANGE-DRIVEN (2026-10-26): every 15 s it used to list every automation, read every
    automation's whole ledger, walk the active automations' occurrence trees and list the
    completed and failed runs (2 more full scans): 7-25% of a core on the operator's idle
    gateway. Now a pass whose run-store and ledger tokens (AbstractRuntime `change_token`) are
    unchanged reads nothing; a pass after a change reads only the runs and ledgers that
    changed (`changed_run_ids`). The full read stays for the first pass, a changed switch
    (preferences, mailbox usable), an unknown change set, and at least every FULL_PASS_S.
    """

    def __init__(self, plane: EmailPlane, svc: Any) -> None:
        self.plane = plane
        self.svc = svc

    def collect(self) -> Dict[str, int]:
        with _plane_lock(self.plane):
            return self._collect_locked()

    # --- one fact each, shared by the full and the incremental pass ---------------------

    def _env(self, ctx: Dict[str, Any]) -> Tuple[str, bool]:
        """The recipient's time zone and whether AbstractCode is installed: read once per pass,
        and only when a notice is about to be queued (a pass that queues nothing reads neither)."""
        if "time_zone" not in ctx:
            ctx["time_zone"] = recipient_time_zone(self.plane)
            ctx["code_app"] = _code_app_installed()
        return str(ctx["time_zone"]), bool(ctx["code_app"])

    def _attention(self, ledger_store: Any, aid: str, title: str, att: Dict[str, int], first: bool, ctx: Dict[str, Any]) -> int:
        from abstractruntime.automations.attention import list_attention

        queued = 0
        prefs, usable, reconnect = ctx["prefs"], ctx["usable"], ctx["reconnect"]
        floor = att.get(aid)
        items = list_attention(ledger_store, aid, after_seq=floor or 0, limit=500)["items"] if ledger_store is not None else []
        if floor is None and first:
            # Baseline: never mail the history that existed before notifications were on.
            att[aid] = max([int(i["seq"]) for i in items] or [0])
            items = [it for it in items if "email_result" in it]
        for it in items:
            seq = int(it["seq"])
            att[aid] = max(att.get(aid, 0), seq)
            kind = "automation_failed" if str(it.get("kind")) == "failure" else "automation_result"
            if kind == "automation_result":
                # "Email result": the automation asks for email delivery
                # (`notify.channels`, schema v2; the runtime stamps the channels on the
                # attention item; v1 = console only). No global preference involved.
                wanted = "email" in (it.get("channels") or [])
            else:
                # A failure after the retries: the user's "Job failed" switch.
                wanted = bool(prefs.get("job_failed"))
            if wanted and (usable or (reconnect and kind == "automation_result")):
                time_zone, _code = self._env(ctx)
                facts = {"title": title, "model_title": it.get("title"), "ref": f"automation {aid}, occurrence {it.get('index')}",
                         "subject_ref": f"automation:{aid}",
                         "context": [["Automation", title], ["Occurrence", str(it.get("index") or "")]],
                         "run_id": str(it.get("run_id") or ""), "event_at": str(it.get("at") or ""), "time_zone": time_zone}
                if kind == "automation_result":
                    facts["model_body"] = it.get("email_result", it.get("body"))
                    facts["recipients"] = it.get("recipients", ["self"])
                else:
                    facts["cause"] = it.get("body")
                    facts["fix"] = "Open the automation's last occurrence in the console to see what failed."
                if queue_notice(self.plane, kind, idempotency_key(kind, aid, seq), facts):
                    queued += 1
        return queued

    def _automation_wait(self, aid: str, title: str, w: Dict[str, Any], ctx: Dict[str, Any], *, run_store: Any, run: Any = None) -> int:
        """A person's wait inside an automation's occurrence tree, named after the automation.
        `run`: the waiting run when the caller already loaded it (the incremental pass)."""
        if w.get("kind") not in ("tool_approval", "ask_user", "event"):
            return 0
        # One email per wait: the key is the wait's identity (node + what it asks), never the
        # runtime's per-issuance wait key.
        key = idempotency_key("approval_needed", w.get("run_id"), wait_identity(w))
        occ = run if run is not None else _load_run(run_store, w.get("run_id"))
        context = [["Automation", title]]
        if w.get("index") is not None:
            context.append(["Occurrence", str(w.get("index"))])
        time_zone, code_app = self._env(ctx)
        facts = _wait_facts(w, occ, run_store, title=title, context=context, ref=f"automation {aid}, run {w.get('run_id')}",
                            time_zone=time_zone, code_app=code_app)
        return 1 if queue_notice(self.plane, "approval_needed", key, facts) else 0

    def _plain_wait(self, run: Any, ctx: Dict[str, Any], *, run_store: Any) -> int:
        """A run of this user waiting on a person outside automations (WAITING, reason USER)."""
        from abstractruntime.automations.attention import typed_wait

        meta = (run.vars or {}).get("_meta") if isinstance(run.vars, dict) else None
        if isinstance(meta, dict) and (meta.get("occurrence") or meta.get("automation")):
            return 0  # automation trees are handled with the automation's title
        w = typed_wait(run)
        if w is None:
            return 0
        key = idempotency_key("approval_needed", run.run_id, wait_identity(w))
        time_zone, code_app = self._env(ctx)
        facts = _wait_facts(w, run, run_store, title=_run_title(run, run_store), context=_run_context(run, run_store),
                            ref=f"run {run.run_id}", time_zone=time_zone, code_app=code_app)
        return 1 if queue_notice(self.plane, "approval_needed", key, facts) else 0

    def _job_notice(self, run: Any, kind: str, baseline_at: str, usable: bool, ctx: Dict[str, Any], *, run_store: Any) -> int:
        """"Email me when done" on a COMPLETED (job_finished) or FAILED (job_failed) run."""
        if str(getattr(run, "updated_at", "") or "") < baseline_at:
            return 0
        notify = _run_notify(run)
        want_on = {str(x) for x in (notify.get("on") or [])}
        channels = {str(x) for x in (notify.get("channels") or [])}
        # "Email me when done": the run's own opt-in suffices (no global preference).
        if "email" not in channels or ("finished" if kind == "job_finished" else "failed") not in want_on:
            return 0
        if not usable:
            return 0
        time_zone, code_app = self._env(ctx)
        facts: Dict[str, Any] = {
            "title": _run_title(run, run_store), "ref": f"run {run.run_id}", "run_id": run.run_id,
            "session_id": str(getattr(run, "session_id", "") or ""), "code_app": code_app,
            "context": _run_context(run, run_store), "started_at": _started_at(run, run_store),
            "event_at": str(getattr(run, "updated_at", "") or ""), "time_zone": time_zone,
        }
        if kind == "job_failed":
            cause, fix = _run_error(run)
            facts["cause"] = cause
            facts["fix"] = fix or "Open the run in the console to see what failed."
        return 1 if queue_notice(self.plane, kind, idempotency_key(kind, run.run_id), facts) else 0

    # --- passes --------------------------------------------------------------------------

    def _collect_locked(self) -> Dict[str, int]:
        out: Dict[str, int] = {"queued": 0}
        host = getattr(self.svc, "host", None)
        run_store = getattr(host, "run_store", None)
        ledger_store = getattr(host, "ledger_store", None)
        if run_store is None:
            return out
        prefs = read_preferences(self.plane)
        usable = email_usable(self.plane)
        # A mailbox sealed with the old keychain key (round 16): an explicit "Email result" is
        # still queued so it fails ONCE, visibly, with the reconnect sentence (never retried).
        reconnect = (not usable) and mailbox_unavailable_reason(self.plane) == REASON_MAILBOX_NEEDS_RECONNECT
        ctx: Dict[str, Any] = {"prefs": prefs, "usable": usable, "reconnect": reconnect}

        run_sig = _change_signal(run_store)
        ledger_sig = _change_signal(ledger_store) if ledger_store is not None else None
        # Captured BEFORE anything is read: a change landing during this pass moves the token
        # again, so the next pass sees it (a change can be read twice, never missed).
        tokens = (
            run_sig.change_token() if run_sig is not None else None,
            ledger_sig.change_token() if ledger_sig is not None else None,
        )
        switches = json.dumps({"prefs": prefs, "usable": bool(usable), "reconnect": bool(reconnect)}, sort_keys=True, default=str)
        key = str(_collector_path(self.plane))
        stores = (id(run_store), id(ledger_store))
        with _COLLECT_MEMO_LOCK:
            memo = dict(_COLLECT_MEMO.get(key) or {})
        now = time.monotonic()
        incremental = bool(
            memo
            and run_sig is not None
            and (ledger_sig is not None or ledger_store is None)
            and memo.get("stores") == stores
            and memo.get("switches") == switches
            and (now - float(memo.get("full_at") or 0.0)) < FULL_PASS_S
        )
        if incremental and memo.get("tokens") == tokens:
            out["skipped"] = 1  # nothing changed: nothing read
            return out
        changed_runs: Optional[set] = None
        changed_ledgers: Optional[set] = set()
        if incremental:
            old_run, old_ledger = memo.get("tokens") or (None, None)
            changed_runs = run_sig.changed_run_ids(old_run) if old_run is not None else None
            if ledger_sig is not None:
                changed_ledgers = ledger_sig.changed_run_ids(old_ledger) if old_ledger is not None else None
        full = changed_runs is None or changed_ledgers is None

        state = _read_json(_collector_path(self.plane))
        # Decisions carry over only under the same switches (a mailbox that becomes usable, a
        # preference that changes: every finished run since the baseline is decided again).
        ctx["seen"] = dict(memo.get("seen") or {}) if memo.get("switches") == switches else {}
        jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        if jobs.get("switches") == switches and jobs.get("through"):
            ctx["jobs_through"] = str(jobs["through"])
        first = not state.get("baseline_at")
        att: Dict[str, int] = {str(k): int(v) for k, v in (state.get("attention") or {}).items()}
        baseline_at = str(state.get("baseline_at") or _now_iso())
        if first or full:
            out["queued"] += self._full_pass(run_store, ledger_store, att, first, baseline_at, ctx)
        else:
            out["queued"] += self._incremental_pass(run_store, ledger_store, changed_runs or set(), changed_ledgers or set(), att, baseline_at, ctx)
            out["incremental"] = 1

        doc: Dict[str, Any] = {"version": 1, "baseline_at": baseline_at, "attention": att, "updated_at": _now_iso()}
        through = ctx.get("jobs_through_out") or ctx.get("jobs_through")
        if through:
            doc["jobs"] = {"switches": switches, "through": str(through)}
        _write_private_json(_collector_path(self.plane), doc)
        with _COLLECT_MEMO_LOCK:
            _COLLECT_MEMO[key] = {
                "tokens": tokens,
                "stores": stores,
                "switches": switches,
                "full_at": now if (first or full) else float(memo.get("full_at") or now),
                "seen": ctx.get("seen") or {},
            }
        return out

    def _full_pass(self, run_store: Any, ledger_store: Any, att: Dict[str, int], first: bool, baseline_at: str, ctx: Dict[str, Any]) -> int:
        queued = 0
        prefs, usable = ctx["prefs"], ctx["usable"]
        # Automations: notify / failure attention items (ledger), human waits (run state).
        try:
            from abstractruntime.automation_queries import list_automations
            from abstractruntime.automations.attention import pending_waits

            cursor = None
            while True:
                page = list_automations(run_store, cursor=cursor, limit=200)
                for item in page.items:
                    aid = str(item.get("automation_id") or "")
                    if not aid:
                        continue
                    title = str(item.get("title") or "Automation")
                    queued += self._attention(ledger_store, aid, title, att, first, ctx)
                    if str(item.get("status") or "") == "active" and usable and prefs.get("approval_needed") and not first:
                        for w in pending_waits(run_store, aid, limit=20):
                            queued += self._automation_wait(aid, title, w, ctx, run_store=run_store)
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
        except Exception:  # noqa: BLE001 - one broken source never blocks the others
            pass

        # Runs of this user waiting on a person (outside automations) and jobs that asked to be notified.
        try:
            from abstractruntime.core.models import RunStatus, WaitReason

            # Index rows first (no vars); a run is loaded only when its row (status, updated_at)
            # is new since this collector last decided it: never 400 full RunStates per pass.
            seen: Dict[str, Tuple[str, str]] = ctx.setdefault("seen", {})
            fresh_seen: Dict[str, Tuple[str, str]] = {}

            def row_sig(row: Dict[str, Any]) -> Tuple[str, str]:
                return (str(row.get("status") or ""), str(row.get("updated_at") or ""))

            if usable and prefs.get("approval_needed") and not first:
                rows = _index_rows(run_store, RunStatus.WAITING, 200)
                if rows is None:
                    for run in run_store.list_runs(status=RunStatus.WAITING, wait_reason=WaitReason.USER, limit=200):
                        queued += self._plain_wait(run, ctx, run_store=run_store)
                else:
                    user = str(getattr(WaitReason.USER, "value", WaitReason.USER))
                    for row in rows:
                        if str(row.get("wait_reason") or "") != user:
                            continue
                        rid, sig = str(row["run_id"]), row_sig(row)
                        fresh_seen[rid] = sig
                        if seen.get(rid) == sig:
                            continue
                        run = self._load(run_store, rid, ctx)
                        if run is None:
                            fresh_seen.pop(rid, None)
                        elif getattr(run, "status", None) == RunStatus.WAITING and getattr(getattr(run, "waiting", None), "reason", None) == WaitReason.USER:
                            queued += self._plain_wait(run, ctx, run_store=run_store)
            if not first:
                through = _ts(ctx.get("jobs_through")) if ctx.get("jobs_through") else None
                base = _ts(baseline_at)
                newest: Optional[float] = None
                for status, kind in ((RunStatus.COMPLETED, "job_finished"), (RunStatus.FAILED, "job_failed")):
                    rows = _index_rows(run_store, status, 200)
                    if rows is None:
                        for run in run_store.list_runs(status=status, limit=200):
                            queued += self._job_notice(run, kind, baseline_at, usable, ctx, run_store=run_store)
                        continue
                    for row in rows:
                        rid, sig = str(row["run_id"]), row_sig(row)
                        fresh_seen[rid] = sig
                        at = _ts(row.get("updated_at"))
                        if at is not None:
                            newest = at if newest is None else max(newest, at)
                        if seen.get(rid) == sig:
                            continue  # decided by an earlier pass of this process
                        if at is not None and ((base is not None and at < base) or (through is not None and at < through)):
                            continue  # before notifications were on / decided before a restart
                        run = self._load(run_store, rid, ctx)
                        if run is None:
                            fresh_seen.pop(rid, None)  # unreadable now: decided on a later pass
                        elif getattr(run, "status", None) == status:
                            queued += self._job_notice(run, kind, baseline_at, usable, ctx, run_store=run_store)
                if newest is not None:
                    mark = newest - FINISHED_OVERLAP_S
                    if through is None or mark > through:
                        ctx["jobs_through_out"] = datetime.datetime.fromtimestamp(mark, datetime.timezone.utc).isoformat()
            seen.clear()
            seen.update(fresh_seen)
        except Exception:  # noqa: BLE001
            pass
        return queued

    @staticmethod
    def _load(run_store: Any, rid: str, ctx: Dict[str, Any]) -> Any:
        try:
            run = run_store.load(rid)
        except Exception:  # noqa: BLE001 - unreadable: not this pass's fact
            return None
        ctx["loads"] = int(ctx.get("loads") or 0) + 1
        return run

    def _incremental_pass(
        self,
        run_store: Any,
        ledger_store: Any,
        changed_runs: set,
        changed_ledgers: set,
        att: Dict[str, int],
        baseline_at: str,
        ctx: Dict[str, Any],
    ) -> int:
        """The full pass's facts, read only from the runs and ledgers that changed: a new
        attention item lands in its automation's ledger; a new wait, a finished or a failed run
        is a run that was saved."""
        from abstractruntime.automation_queries import automation_summary
        from abstractruntime.automations.attention import typed_wait
        from abstractruntime.core.models import RunStatus, WaitReason

        queued = 0
        prefs, usable = ctx["prefs"], ctx["usable"]
        runs: Dict[str, Any] = {}

        def load(rid: str) -> Any:
            if rid not in runs:
                try:
                    runs[rid] = run_store.load(rid)
                except Exception:  # noqa: BLE001 - unreadable: not this pass's fact
                    runs[rid] = None
            return runs[rid]

        summaries: Dict[str, Optional[Dict[str, Any]]] = {}

        def summary(aid: str) -> Optional[Dict[str, Any]]:
            if aid not in summaries:
                run = load(aid)
                try:
                    summaries[aid] = automation_summary(run) if run is not None else None
                except Exception:  # noqa: BLE001 - not an automation controller
                    summaries[aid] = None
            return summaries[aid]

        def controller_of(run: Any) -> Optional[str]:
            """The automation whose occurrence tree holds `run` (pending_waits walks the
            controller's children): the first ancestor that is a controller."""
            seen: set = set()
            parent = getattr(run, "parent_run_id", None)
            while parent and parent not in seen and len(seen) < 64:
                seen.add(parent)
                if summary(str(parent)) is not None:
                    return str(parent)
                up = load(str(parent))
                parent = getattr(up, "parent_run_id", None) if up is not None else None
            return None

        for aid in sorted(changed_ledgers):
            try:
                summ = summary(aid)
                if summ is None:
                    continue
                queued += self._attention(ledger_store, aid, str(summ.get("title") or "Automation"), att, False, ctx)
            except Exception:  # noqa: BLE001 - one broken source never blocks the others
                pass

        for rid in sorted(changed_runs):
            try:
                run = load(rid)
                if run is None:
                    continue
                status = getattr(run, "status", None)
                if status in (RunStatus.WAITING, RunStatus.COMPLETED, RunStatus.FAILED):
                    # Decided here: the next full pass does not load it again for the same row.
                    ctx.setdefault("seen", {})[rid] = (str(getattr(status, "value", status)), str(getattr(run, "updated_at", "") or ""))
                if status == RunStatus.WAITING and usable and prefs.get("approval_needed"):
                    aid = controller_of(run)
                    summ = summary(aid) if aid else None
                    if aid and summ is not None and str(summ.get("status") or "") == "active":
                        w = typed_wait(run)
                        if w is not None:
                            queued += self._automation_wait(aid, str(summ.get("title") or "Automation"), w, ctx, run_store=run_store, run=run)
                    waiting = getattr(run, "waiting", None)
                    if getattr(waiting, "reason", None) == WaitReason.USER:
                        queued += self._plain_wait(run, ctx, run_store=run_store)
                elif status == RunStatus.COMPLETED:
                    queued += self._job_notice(run, "job_finished", baseline_at, usable, ctx, run_store=run_store)
                elif status == RunStatus.FAILED:
                    queued += self._job_notice(run, "job_failed", baseline_at, usable, ctx, run_store=run_store)
            except Exception:  # noqa: BLE001
                pass
        return queued
