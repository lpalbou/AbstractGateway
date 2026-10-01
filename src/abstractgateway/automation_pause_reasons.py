"""Why the GATEWAY paused an automation (DESIGN-v3 §13.4; reusable for §13.9).

The runtime's `automation.pause` command records THAT an automation is paused, not why.
When the gateway pauses a user's automation on its own initiative — an admin turned a
workflow's "Available to users" off — the user must read the reason on the automation
row. The reason lives here, in the principal's runtime data dir
(`<runtime data_dir>/automation_pause_reasons.json`), `{automation_id: {reason, by, at}}`;
`GET /automations` adds it as `paused_reason` while the automation is paused, and a
resume clears it. Nothing is auto-resumed: turning availability back on leaves the
automation paused for its owner to resume.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
FILENAME = "automation_pause_reasons.json"


def _path(data_dir: Any) -> Path:
    return Path(data_dir) / FILENAME


def _load(data_dir: Any) -> Dict[str, Any]:
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt file only loses the explanation, never the pause
        logger.warning("unreadable %s; pause reasons are not shown", _path(data_dir))
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(data_dir: Any, data: Dict[str, Any]) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))


def record_pause_reason(data_dir: Any, automation_id: str, reason: str, *, by: str) -> None:
    with _LOCK:
        data = _load(data_dir)
        data[str(automation_id)] = {
            "reason": str(reason),
            "by": str(by),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _save(data_dir, data)


def pause_reason(data_dir: Any, automation_id: str) -> Optional[str]:
    rec = _load(data_dir).get(str(automation_id))
    return str(rec.get("reason") or "") or None if isinstance(rec, dict) else None


def clear_pause_reason(data_dir: Any, automation_id: str) -> None:
    with _LOCK:
        data = _load(data_dir)
        if data.pop(str(automation_id), None) is not None:
            _save(data_dir, data)


def pause_automation_with_reason(svc: Any, automation_id: str, reason: str, *, by: str) -> Dict[str, Any]:
    """Queue the runtime's own `automation.pause` (the durable command store, like a
    user's Pause) and record why. Returns the command receipt."""
    from .routes.automations import _append_command

    receipt = _append_command(
        svc,
        automation_id=str(automation_id),
        command_id=f"gateway-pause:{uuid.uuid4().hex}",
        type_="automation.pause",
        payload={"reason": str(reason)},
        client_id=str(by),
    )
    record_pause_reason(svc.config.data_dir, str(automation_id), reason, by=by)
    return receipt


def _automation_bundle_id(controller: Any) -> str:
    from .routes.gateway import _split_bundle_ref

    definition = ((getattr(controller, "vars", None) or {}).get("_meta") or {}).get("automation") or {}
    target = definition.get("target") if isinstance(definition, dict) else None
    ref = str((target or {}).get("bundle_ref") or "") if isinstance(target, dict) else ""
    return _split_bundle_ref(ref)[0] if ref else ""


def pause_active_automations_on_bundle(svc: Any, bundle_id: str, reason: str, *, by: str) -> List[str]:
    """Pause every ACTIVE automation of one principal's store that targets `bundle_id`."""
    from abstractruntime.automation_queries import list_automations
    from abstractruntime.automations.models import automation_status

    run_store = svc.host.run_store
    paused: List[str] = []
    cursor: Optional[str] = None
    while True:
        page = list_automations(run_store, status=None, cursor=cursor, limit=200)
        for row in page.items:
            aid = str(row.get("automation_id") or "")
            controller = run_store.load(aid)
            if controller is None or _automation_bundle_id(controller) != str(bundle_id):
                continue
            if automation_status(controller) != "active":
                continue
            pause_automation_with_reason(svc, aid, reason, by=by)
            paused.append(aid)
        cursor = page.next_cursor
        if not cursor:
            return paused


def pause_users_automations_on_bundle(bundle_id: str, reason: str) -> List[Dict[str, Any]]:
    """Every NON-ADMIN user's active automations on `bundle_id` -> paused with `reason`.

    Only multi-user gateways give users their own runtime (single-user: every automation
    is the operator's, and the operator is an admin). Users who never ran anything have no
    runtime dir and therefore no automations; no tree is created for them."""
    from .service import _principal_runtime_dir_exists, gateway_multi_user_enabled, get_gateway_service_for_principal
    from .users import GatewayUserRegistry

    if not gateway_multi_user_enabled():
        return []
    out: List[Dict[str, Any]] = []
    for rec in GatewayUserRegistry().list_users():
        roles = {str(r).strip().lower() for r in (getattr(rec, "roles", ()) or ())}
        if "entity" in roles:
            continue  # entities run on their own per-home plane (§13.9 is the accounts lane's)
        principal = rec.to_principal(token_fingerprint_value="")
        if principal.is_admin() or not _principal_runtime_dir_exists(principal):
            continue
        svc = get_gateway_service_for_principal(principal)
        for aid in pause_active_automations_on_bundle(svc, bundle_id, reason, by="gateway:workflow-availability"):
            out.append({"tenant_id": principal.tenant_id, "user_id": principal.user_id, "automation_id": aid})
    return out
