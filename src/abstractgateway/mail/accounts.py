"""The email account of each user plane, the admin's per-user switch, OAuth clients, legacy import.

Where things live (a "plane" is one principal's data home: `<data_dir>` for the default
runtime -- the single-user gateway and the admin -- else `<data_dir>/users/<tenant>/<runtime>`):

    <plane>/email/account/abstractcore.json     account settings, policy, limits (AbstractCore
                                                `EmailAccountStore`, `email` section)
    <plane>/email/account/email/secret.enc      the password / OAuth tokens, AES-256-GCM; key in
                                                the OS keychain (0600 key file when none)
    <plane>/email/watcher.json                  the mail watcher cursor and state (watcher.py)
    <plane>/email/inbox.sqlite3                 the durable inbox of received-mail events
    <plane>/email/outbox.sqlite3                the durable notification outbox (notifications.py)
    <plane>/email/notifications.json            the user's notification preferences

    <data_dir>/auth/email_capability.json       the admin's per-user email switch (never content)
    <data_dir>/email/oauth_clients/secret.enc   bring-your-own OAuth clients (admin setting)

Everything is resolved from the authenticated principal, never from a path or body id, so a
user reaches only their own plane. Entities have no mailbox.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core_mail import (
    EmailAccount,
    EmailAccountStore,
    EmailAgentToolsOff,
    EmailContext,
    EmailDisabled,
    EmailError,
    EmailInvalidSettings,
    EmailOAuthFailed,
    EmailOAuthPending,
    EmailSecret,
    ImapSettings,
    LoopbackAuthorization,
    OAuthSettings,
    OAuthTokenClient,
    SecretVault,
    SmtpSettings,
    discover_servers,
    provider_preset,
    require_servers,
    resolve_oauth_client,
    tls_context,
)
from .core_mail import legacy as core_legacy

from ..security.principal import GatewayPrincipal, local_admin_principal, safe_principal_component
from ..users import gateway_data_dir_from_env
from .audit import audit_email_event

ACCOUNT_ID = "default"
SETTINGS_LABEL = "Settings → My email"
ADMIN_DISABLED_CAUSE = "Your admin turned mailboxes off for your account."
ADMIN_DISABLED_FIX = "Ask your gateway admin to allow \u201cMailboxes for users\u201d for you; your settings are kept."

# The reasons a user-facing switch is unavailable (DESIGN 2026-09-30 §6), in the order they apply.
REASON_ADMIN_MAILBOXES_OFF = "Your admin turned mailboxes off."
REASON_ADMIN_AGENT_TOOLS_OFF = "Your admin turned agent email tools off."
REASON_CONNECT_MAILBOX = "Connect a mailbox first."
REASON_MAILBOX_NOT_IN_USE = "Switch on \u201cUse this mailbox\u201d (Advanced) first."


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _write_private_json(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


# ---------------------------------------------------------------------------------------
# Planes
# ---------------------------------------------------------------------------------------


class EmailPrincipalRefused(PermissionError):
    """Entities (and unidentified principals) have no mailbox."""


@dataclass(frozen=True)
class EmailPlane:
    tenant_id: str
    user_id: str
    runtime_id: str
    root: Path
    is_default: bool

    @property
    def key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"

    @property
    def account_ref(self) -> str:
        """`<tenant>:<user>:<account_id>` — the run binding's reference (never a secret)."""

        return f"{self.tenant_id}:{self.user_id}:{ACCOUNT_ID}"

    @property
    def email_dir(self) -> Path:
        return self.root / "email"

    @property
    def account_config_file(self) -> Path:
        return self.email_dir / "account" / "abstractcore.json"

    @property
    def runtime_data_dir(self) -> Path:
        return self.root if self.is_default else self.root / "runtime"


def _is_entity(principal: GatewayPrincipal) -> bool:
    return "entity" in {str(r).strip().lower() for r in (principal.roles or ())}


def plane_for_principal(principal: Optional[GatewayPrincipal]) -> EmailPlane:
    """The plane of `principal` — the same rule as the principal's service
    (`service._config_for_principal`), computed without building the service."""

    if principal is None or not str(principal.user_id or "").strip():
        raise EmailPrincipalRefused("Email settings need an identified user.")
    if _is_entity(principal):
        raise EmailPrincipalRefused("Entities have no email account; email is configured per human user.")
    from ..service import _config_for_principal, gateway_multi_user_enabled

    if not gateway_multi_user_enabled():
        # One shared runtime (user accounts off): the operator's plane.
        return default_plane(gateway_data_dir_from_env())
    cfg = _config_for_principal(principal)
    return plane_for_service_config(cfg)


def default_plane(data_dir: Path) -> EmailPlane:
    """The default runtime's plane: the operator's (`default:admin`)."""

    return EmailPlane("default", "admin", "default", Path(data_dir), True)


def plane_for_service_config(cfg: Any) -> EmailPlane:
    """The plane of a service built for `cfg` (`GatewayHostConfig`)."""

    from ..service import gateway_multi_user_enabled

    runtime_id = str(getattr(cfg, "runtime_id", "") or "default")
    if not gateway_multi_user_enabled() or runtime_id == "default":
        # The default runtime keeps the gateway data dir as its data dir.
        return default_plane(Path(cfg.data_dir))
    tenant = safe_principal_component(getattr(cfg, "tenant_id", "") or "default", default="default")
    user = safe_principal_component(getattr(cfg, "user_id", "") or runtime_id, default=runtime_id)
    return EmailPlane(tenant, user, runtime_id, Path(cfg.data_dir).parent, False)


def plane_for_user(user_id: str, *, tenant_id: str = "default") -> EmailPlane:
    """The plane of a registry user (admin views). KeyError when the user does not exist."""

    from ..users import GatewayUserRegistry

    record = GatewayUserRegistry().get_user(user_id, tenant_id=tenant_id)
    if record is None:
        raise KeyError(user_id)
    return plane_for_principal(record.to_principal())


def admin_plane() -> EmailPlane:
    """The plane of the gateway operator (the default runtime)."""

    return plane_for_principal(local_admin_principal())


def account_store(plane: EmailPlane) -> EmailAccountStore:
    return EmailAccountStore(config_file=plane.account_config_file)


def registered_address(plane: EmailPlane) -> str:
    """The user's registered email (users registry record, or the gateway knob for the
    account-less operator): "self" for the recipient policy, notifications and recovery."""

    try:
        from ..runtime_config import resolve_operator_email

        value = resolve_operator_email(gateway_data_dir_from_env(), tenant_id=plane.tenant_id, user_id=plane.user_id).get("value")
    except Exception:
        value = None
    return str(value or "").strip().lower()


def self_address(plane: EmailPlane) -> str:
    """"Self" for runs (the send_email recipient refiner, `_runtime.operator_email`): exactly the
    address the user's email settings show as `registered_address` - the registered email, else
    the connected mailbox's own address (operator D4: "the runtime's registered email account").

    Before 0.8.1 runs read only the registered email, so an administrator without an email on
    their user record got no "self" in runs while /me/email showed one, and every send to their
    own mailbox waited for approval (0.7.0 Linux end-to-end, F1a). The mailbox fallback is the
    user's OWN connected account, never another principal's address.
    """

    reg = registered_address(plane)
    if reg:
        return reg
    try:
        st = account_store(plane).settings()
    except EmailError:
        return ""
    if st.account is None:
        return ""
    return str(st.self_address or "").strip().lower()


def sync_registered_address(plane: EmailPlane, store: Optional[EmailAccountStore] = None) -> EmailAccountStore:
    """Keep the store's registered address equal to the user's registered email."""

    store = store or account_store(plane)
    try:
        st = store.settings()
    except EmailInvalidSettings:
        return store
    if st.account is None:
        return store
    want = registered_address(plane)
    if want != (st.registered_address or "").strip().lower():
        try:
            store.set_registered_address(want)
        except EmailInvalidSettings:
            pass
    return store


def set_email_address(principal: GatewayPrincipal, address: str, *, actor: str = "") -> Dict[str, Any]:
    """Set the user's EMAIL ADDRESS (not a mailbox): where sign-in codes and notifications go and
    "self" for the recipient policy. The users registry stays the source of truth (the record's
    `email`); the account-less operator (static admin token, no registry record) keeps the
    gateway knob `operator_email`. Empty clears it."""

    plane = plane_for_principal(principal)
    from ..users import GatewayUserRegistry, _normalize_email

    try:
        value = _normalize_email(address)
    except ValueError:
        raise EmailInvalidSettings(
            f"{str(address or '').strip()!r} is not a valid email address.", "Give one address, like name@example.com, or leave it empty."
        ) from None
    registry = GatewayUserRegistry()
    record = registry.get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default"))
    if record is not None:
        registry.update_user(user_id=record.user_id, tenant_id=record.tenant_id, email=value)
        source = "account"
    else:
        from ..runtime_config import write_runtime_config

        write_runtime_config(gateway_data_dir_from_env(), {"operator_email": value or None}, actor=actor or f"person:{principal.user_id}")
        source = "stored"
    sync_registered_address(plane)
    rebind_live_runtime(plane)
    audit_email_event(
        "email.address_changed", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id,
        outcome="set" if value else "cleared", source=source,
    )
    return public_status(plane)


def rebind_live_runtime(plane: EmailPlane) -> None:
    """After a settings change, re-bind the plane's live runtime (if its service is built)."""

    try:
        from .runtime_wiring import refresh_cached_service_binding

        refresh_cached_service_binding(plane)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------
# The admin's per-user switch (D3: admins enable/disable, never read)
# ---------------------------------------------------------------------------------------

_CAP_LOCK = threading.RLock()

# What the administrator makes AVAILABLE (operator decisions 2026-09-30): a gateway-wide default
# per capability plus per-user overrides. A user can switch on, on their own runtime, only what
# is available to them. The admin's simple view is ONE switch, "Mailboxes for users" (`email`);
# `email_agent_tools` and `email_recovery` sit under its Advanced disclosure (`advanced: True`).
CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "email": {
        "default": True,
        "label": "Mailboxes for users",
        "description": "Users may connect their own mailbox for their agents, automations and notifications. "
        "You never see anyone's mail.",
        "per_user": True,
        "advanced": False,
    },
    "email_agent_tools": {
        # ON since capabilities.json v3: each user still switches the tools on for themselves
        # (their own switch is off by default), so availability no longer needs a second admin step.
        "default": True,
        "label": "Agent email tools for users",
        "description": "Users may let their agents and workflows list, search, read, send and reply to their mail. "
        "Each user still switches this on for themselves.",
        "per_user": True,
        "advanced": True,
    },
    "email_recovery": {
        "default": True,
        "label": "Sign-in by email",
        "description": "Shows \u201cForgot your token?\u201d on the sign-in page. Whoever controls a user's mailbox can "
        "then sign in as that user.",
        "per_user": False,
        "advanced": True,
    },
}

CAPABILITIES_VERSION = 3
# The built-in defaults before v3: the migration judges what each user could use under them.
_V2_BUILT_IN_DEFAULTS = {"email": True, "email_agent_tools": False, "email_recovery": True}


def _capability_path() -> Path:
    return gateway_data_dir_from_env() / "auth" / "capabilities.json"


def _capability_doc() -> Dict[str, Any]:
    path = _capability_path()
    doc = _read_json(path)
    if int(doc.get("version") or 0) < CAPABILITIES_VERSION:
        doc = _migrate_capabilities(path, doc)
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    users = doc.get("users") if isinstance(doc.get("users"), dict) else {}
    return {"defaults": defaults, "users": users}


def _all_planes() -> List[EmailPlane]:
    """Every human plane of this gateway (the default runtime's plus each registry user's)."""

    planes: Dict[str, EmailPlane] = {}
    try:
        p = default_plane(gateway_data_dir_from_env())
        planes[p.key] = p
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..users import GatewayUserRegistry

        for rec in GatewayUserRegistry().list_users():
            if rec.principal_kind == "entity":
                continue
            try:
                p = plane_for_principal(rec.to_principal())
            except Exception:  # noqa: BLE001 - one unreadable record never blocks the others
                continue
            planes.setdefault(p.key, p)
    except Exception:  # noqa: BLE001
        pass
    return list(planes.values())


def _migrate_capabilities(path: Path, doc: Dict[str, Any]) -> Dict[str, Any]:
    """capabilities.json v2 -> v3 (`email_agent_tools` built-in default OFF -> ON).

    Nobody gains tools they could not use before: a user whose agent-tools availability was OFF
    under the v2 rules (their override, else the stored gateway default, else the v2 built-in
    OFF) and whose own switch is stored ON gets a per-user `email_agent_tools: false` override.
    Everyone else keeps their own switch (default OFF), so the new default only lets them opt in.
    Recorded in the audit log (`email.capabilities_migrated`)."""

    with _CAP_LOCK:
        current = _read_json(path)
        if int(current.get("version") or 0) >= CAPABILITIES_VERSION:
            return current
        existed = path.exists()
        defaults = dict(current.get("defaults") or {}) if isinstance(current.get("defaults"), dict) else {}
        users = dict(current.get("users") or {}) if isinstance(current.get("users"), dict) else {}
        pinned: List[str] = []
        for plane in _all_planes():
            if not agent_tools_switch(plane):
                continue
            row = users.get(plane.key) if isinstance(users.get(plane.key), dict) else {}
            if isinstance(row.get("email_agent_tools"), bool):
                continue  # an explicit per-user choice already decides, under v2 and v3 alike
            stored_default = defaults.get("email_agent_tools")
            was_available = stored_default if isinstance(stored_default, bool) else _V2_BUILT_IN_DEFAULTS["email_agent_tools"]
            if was_available:
                continue
            row = dict(row)
            row["email_agent_tools"] = False
            row["by"], row["at"] = "migration:capabilities-v3", _now_iso()
            users[plane.key] = row
            pinned.append(plane.key)
        migrated = {"version": CAPABILITIES_VERSION, "defaults": defaults, "users": users}
        try:
            _write_private_json(path, migrated)
        except OSError:
            return migrated  # read-only data dir: the migrated view still applies in memory
    if existed or pinned:
        audit_email_event(
            "email.capabilities_migrated",
            actor="gateway",
            from_version=int(current.get("version") or 0),
            to_version=CAPABILITIES_VERSION,
            pinned_off=pinned,
            reason="email_agent_tools built-in default is now ON; users who could not use the tools keep them off",
        )
    return migrated


def capability_default(cap: str) -> bool:
    stored = _capability_doc()["defaults"].get(cap)
    return bool(CAPABILITIES[cap]["default"]) if not isinstance(stored, bool) else stored


def capability_for(plane: EmailPlane, cap: str) -> Dict[str, Any]:
    """`{value, source: "user" | "gateway" | "built-in"}` for one user."""

    doc = _capability_doc()
    row = doc["users"].get(plane.key) if CAPABILITIES[cap]["per_user"] else None
    if isinstance(row, dict) and isinstance(row.get(cap), bool):
        return {"value": row[cap], "source": "user"}
    if isinstance(doc["defaults"].get(cap), bool):
        return {"value": doc["defaults"][cap], "source": "gateway"}
    return {"value": bool(CAPABILITIES[cap]["default"]), "source": "built-in"}


def capabilities_public() -> Dict[str, Any]:
    return {
        "capabilities": [
            {"id": cid, "label": spec["label"], "description": spec["description"], "per_user": spec["per_user"],
             "advanced": spec["advanced"], "default": capability_default(cid), "built_in_default": spec["default"]}
            for cid, spec in CAPABILITIES.items()
        ]
    }


def set_capability_defaults(changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    unknown = sorted(k for k in changes if k not in CAPABILITIES)
    if unknown:
        raise EmailInvalidSettings(f"Unknown capability: {', '.join(unknown)}.", f"Use one of: {', '.join(CAPABILITIES)}.")
    with _CAP_LOCK:
        doc = _capability_doc()
        for k, v in changes.items():
            if v is None:
                doc["defaults"].pop(k, None)
            elif isinstance(v, bool):
                doc["defaults"][k] = v
            else:
                raise EmailInvalidSettings(f"{k} must be true, false or null.", "Send true, false, or null for the built-in default.")
        _write_private_json(_capability_path(), {"version": CAPABILITIES_VERSION, **doc})
    for k, v in changes.items():
        audit_email_event("email.capability_changed", actor=actor, kind=k, enabled=v, reason="gateway default")
    rebind_all_live_runtimes()
    return capabilities_public()


def set_user_capabilities(plane: EmailPlane, changes: Dict[str, Any], *, actor: str) -> None:
    """Per-user overrides: true / false, or None to inherit the gateway default."""

    for k in changes:
        if k not in CAPABILITIES or not CAPABILITIES[k]["per_user"]:
            raise EmailInvalidSettings(f"{k} is not a per-user capability.", "Use email or email_agent_tools.")
    with _CAP_LOCK:
        doc = _capability_doc()
        row = dict(doc["users"].get(plane.key) or {})
        for k, v in changes.items():
            if v is None:
                row.pop(k, None)
            elif isinstance(v, bool):
                row[k] = v
            else:
                raise EmailInvalidSettings(f"{k} must be true, false or null.", "Send true, false, or null to inherit.")
        row["by"], row["at"] = str(actor or ""), _now_iso()
        doc["users"][plane.key] = row
        _write_private_json(_capability_path(), {"version": CAPABILITIES_VERSION, **doc})
    rebind_live_runtime(plane)
    for k, v in changes.items():
        audit_email_event("email.capability_changed", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor, kind=k, enabled=v)


def admin_email_enabled(plane: EmailPlane) -> bool:
    return bool(capability_for(plane, "email")["value"])


def agent_tools_available(plane: EmailPlane) -> bool:
    return bool(capability_for(plane, "email_agent_tools")["value"])


def recovery_enabled() -> bool:
    return capability_default("email_recovery")


def set_admin_email_enabled(plane: EmailPlane, enabled: bool, *, actor: str) -> Dict[str, Any]:
    set_user_capabilities(plane, {"email": bool(enabled)}, actor=actor)
    return {"enabled": bool(enabled)}


def rebind_all_live_runtimes() -> None:
    try:
        from .runtime_wiring import refresh_all_cached_bindings

        refresh_all_cached_bindings()
    except Exception:  # noqa: BLE001
        pass


def _sent_recorder(plane: EmailPlane, previous: Any = None):
    """`EmailContext.on_sent` for the plane: every AUTOMATIC send through the account (one that
    carries the framework marker: an automation occurrence, a notification, a sign-in code) has
    its Message-ID recorded in the plane's outbox, which the mail watcher consults so the
    account's own automatic mail never triggers an automation. A person's sends are not recorded.
    """

    def on_sent(info: Dict[str, Any]) -> None:
        if callable(previous):
            try:
                previous(info)
            except Exception:  # noqa: BLE001 - core ignores on_sent failures too
                pass
        marker = str((info or {}).get("automation_marker") or "")
        if not marker:
            return
        from .notifications import NotificationOutbox

        NotificationOutbox(plane).record_sent(str(info.get("message_id") or ""), kind="automatic", marker=marker)

    return on_sent


def email_context(plane: EmailPlane, *, require_enabled: bool = True) -> EmailContext:
    """The user's account ready for use: policy, limits, the user's switch AND the admin's."""

    store = sync_registered_address(plane)
    ctx = store.context(require_enabled=False)
    ctx.on_sent = _sent_recorder(plane, ctx.on_sent)
    if not admin_email_enabled(plane):
        ctx.enabled = False
        if require_enabled:
            raise EmailDisabled(ADMIN_DISABLED_CAUSE, ADMIN_DISABLED_FIX)
    elif require_enabled:
        ctx.require_enabled()
    return ctx


def require_admin_email_on(plane: EmailPlane) -> None:
    """Connect, Test and OAuth sign-in open connections to hosts the user chose; while an
    administrator has email off for this user, none of them runs (settings are kept)."""

    if not admin_email_enabled(plane):
        raise EmailDisabled(ADMIN_DISABLED_CAUSE, ADMIN_DISABLED_FIX)


def email_usable(plane: EmailPlane) -> bool:
    """Connected, turned on by the user, not turned off by the admin."""

    if not admin_email_enabled(plane):
        return False
    try:
        st = account_store(plane).settings()
    except EmailError:
        return False
    return bool(st.account is not None and st.enabled and account_store(plane).vault.exists())


# ---------------------------------------------------------------------------------------
# Agent email tools (per user, default OFF)
# ---------------------------------------------------------------------------------------

AGENT_TOOLS_OFF_CAUSE = "Agent email tools are off for your account."
AGENT_TOOLS_OFF_FIX = "Switch on \u201cAgent email tools\u201d on your account page (a mailbox must be connected)."


def _agent_tools_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "agent_tools.json"


def agent_tools_switch(plane: EmailPlane) -> bool:
    """The user's "Agent email tools" choice (default OFF)."""

    return _read_json(_agent_tools_path(plane)).get("enabled") is True


def set_agent_tools_switch(plane: EmailPlane, enabled: bool, *, actor: str = "") -> bool:
    if enabled and not agent_tools_available(plane):
        raise EmailDisabled(AGENT_TOOLS_UNAVAILABLE_CAUSE, AGENT_TOOLS_UNAVAILABLE_FIX)
    _write_private_json(_agent_tools_path(plane), {"version": 1, "enabled": bool(enabled), "at": _now_iso()})
    rebind_live_runtime(plane)
    audit_email_event("email.agent_tools_changed", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id, enabled=bool(enabled))
    return bool(enabled)


AGENT_TOOLS_UNAVAILABLE_CAUSE = "Your admin turned agent email tools off."
AGENT_TOOLS_UNAVAILABLE_FIX = "Ask your gateway admin to allow \u201cAgent email tools for users\u201d (Users \u2192 Advanced)."


def agent_tools_active(plane: EmailPlane) -> bool:
    """Agents and workflows get the email tools only when ALL hold: the administrator made them
    available, the account is connected and turned on (and email is allowed), and the user's
    own toggle is on."""

    return agent_tools_available(plane) and agent_tools_switch(plane) and email_usable(plane)


def mailbox_unavailable_reason(plane: EmailPlane) -> Optional[str]:
    """Why the mailbox cannot be used right now (None = usable): the admin's switch, no
    connected mailbox, or the user's own "Use this mailbox" switch."""

    if not admin_email_enabled(plane):
        return REASON_ADMIN_MAILBOXES_OFF
    try:
        st = account_store(plane).settings()
        configured = st.account is not None and account_store(plane).vault.exists()
        in_use = bool(st.enabled)
    except EmailError:
        configured, in_use = False, False
    if not configured:
        return REASON_CONNECT_MAILBOX
    if not in_use:
        return REASON_MAILBOX_NOT_IN_USE
    return None


def agent_tools_status(plane: EmailPlane) -> Dict[str, Any]:
    """The user's "Agent email tools" switch: `on` (their choice), `available` (can it be
    switched on now), `unavailable_reason` (DESIGN §6 wording), `active` (agents have the tools).
    `enabled` / `reason` / `admin_available` keep the pre-0.10 shape readable."""

    switch = agent_tools_switch(plane)
    admin_available = agent_tools_available(plane)
    usable = email_usable(plane)
    unavailable = REASON_ADMIN_MAILBOXES_OFF if not admin_email_enabled(plane) else (
        REASON_ADMIN_AGENT_TOOLS_OFF if not admin_available else mailbox_unavailable_reason(plane)
    )
    reason = unavailable or ("" if switch else "Off (your choice; the default).")
    return {
        "on": switch,
        "available": unavailable is None,
        "unavailable_reason": unavailable,
        "active": bool(admin_available and switch and usable),
        "enabled": switch,
        "admin_available": admin_available,
        "reason": reason or "",
    }


def agent_tools_off_reason(plane: EmailPlane) -> Optional[str]:
    """AbstractRuntime's typed off-reason for the email toolset of this plane (None = active):
    "not_available" (the administrator has not made agent email tools available to this user),
    "admin_disabled" (email turned off for this user by an administrator), "not_connected" (no
    connected, turned-on account), "agent_tools_off" (the user's choice). Same order as
    `agent_tools_status` and `require_agent_tools`."""

    if agent_tools_active(plane):
        return None
    if not agent_tools_available(plane):
        return "not_available"
    if not admin_email_enabled(plane):
        return "admin_disabled"
    if not email_usable(plane):
        return "not_connected"
    return "agent_tools_off"


def principal_email_tools(principal: Any) -> tuple:
    """`(email_enabled, email_off_reason)` for a principal's toolset listings."""

    try:
        plane = plane_for_principal(principal)
    except EmailPrincipalRefused:
        return False, "not_connected"
    reason = agent_tools_off_reason(plane)
    return reason is None, reason


def require_agent_tools(plane: EmailPlane) -> None:
    if agent_tools_active(plane):
        return
    if not agent_tools_available(plane):
        raise EmailDisabled(AGENT_TOOLS_UNAVAILABLE_CAUSE, AGENT_TOOLS_UNAVAILABLE_FIX)
    if not admin_email_enabled(plane):
        raise EmailDisabled(ADMIN_DISABLED_CAUSE, ADMIN_DISABLED_FIX)
    if not email_usable(plane):
        # Not connected / turned off by the user: the store's own typed error says which.
        email_context(plane)
    raise EmailAgentToolsOff(AGENT_TOOLS_OFF_CAUSE, AGENT_TOOLS_OFF_FIX)


# ---------------------------------------------------------------------------------------
# Read shapes
# ---------------------------------------------------------------------------------------


def public_status(plane: EmailPlane) -> Dict[str, Any]:
    """The user's own view (`GET /me/email`). Never a secret."""

    store = sync_registered_address(plane)
    out = store.public()
    out.pop("config_file", None)
    admin_on = admin_email_enabled(plane)
    out["admin_enabled"] = admin_on
    out["effective_enabled"] = bool(out.get("configured") and out.get("enabled") and admin_on)
    if not admin_on:
        out["admin_disabled"] = {"cause": ADMIN_DISABLED_CAUSE, "fix": ADMIN_DISABLED_FIX}
    out["account_ref"] = plane.account_ref
    out["agent_tools"] = agent_tools_status(plane)
    # Additive fields for the account page (CONTRACT 2026-09-30 §5.4, DESIGN §6).
    out["email_available"] = admin_on
    # `email_address`: the user's email address as stored (users registry; "" when none) — the
    # account page's field. `registered_address` stays "self" for runs: that address, else the
    # connected mailbox's own (filled here too when no mailbox exists yet).
    out["email_address"] = registered_address(plane)
    # The shared mailbox shape (DESIGN-v2 §2.5): the same function /admin/users and
    # /admin/accounts read, so the admin's row and card never disagree.
    out["mailbox"] = mailbox_view(plane)
    out["registered_address"] = str(out.get("registered_address") or "") or self_address(plane)
    out["oauth_providers"] = oauth_providers_for_users()
    from .notifications import read_preferences

    prefs = read_preferences(plane)
    out["notifications"] = {"job_failed": bool(prefs["job_failed"]), "approval_needed": bool(prefs["approval_needed"])}
    out["notifications_unavailable_reason"] = mailbox_unavailable_reason(plane)
    try:
        from .watcher import watcher_public_status

        out["watcher"] = watcher_public_status(plane)
    except Exception:  # noqa: BLE001 - the status view never fails on the watcher file
        out["watcher"] = {"state": "unknown"}
    out["source"] = "gateway user settings"
    out["store"] = {
        "kind": "gateway",
        "label": (
            "The gateway's email account for the administrator (the default runtime); AbstractCore's own local "
            "account (`abstractcore email`) is configured separately."
            if plane.is_default
            else "Your email account on this gateway, stored in your own data home."
        ),
    }
    return out


def admin_status(plane: EmailPlane) -> Dict[str, Any]:
    """What an administrator may see about a user's email: never content, policy entries
    (the user's correspondents) or secrets."""

    store = account_store(plane)
    pub = store.public()
    status = pub.get("status") or {}
    last_error = status.get("last_error") if isinstance(status.get("last_error"), dict) else None
    try:
        from .watcher import watcher_public_status

        watcher = watcher_public_status(plane)
        watcher = {k: watcher.get(k) for k in ("state", "last_poll", "last_error") if k in watcher}
    except Exception:  # noqa: BLE001
        watcher = {"state": "unknown"}
    admin_on = admin_email_enabled(plane)
    return {
        "tenant_id": plane.tenant_id,
        "user_id": plane.user_id,
        "configured": bool(pub.get("configured")),
        "address": pub.get("address") or "",
        "auth_kind": pub.get("auth_kind") or "",
        "user_enabled": bool(pub.get("enabled")),
        "admin_enabled": admin_on,
        "effective_enabled": bool(pub.get("configured") and pub.get("enabled") and admin_on),
        "status": {
            "last_test": status.get("last_test") or "",
            "last_ok": status.get("last_ok") or "",
            "last_error": (
                {"code": last_error.get("code"), "cause": last_error.get("cause"), "fix": last_error.get("fix"), "at": last_error.get("at")}
                if last_error
                else None
            ),
        },
        "capabilities": {"email": capability_for(plane, "email"), "email_agent_tools": capability_for(plane, "email_agent_tools")},
        "agent_tools": {"available": agent_tools_available(plane), "user_enabled": agent_tools_switch(plane), "active": agent_tools_active(plane)},
        "watcher": watcher,
        "state": _admin_state_label(pub, admin_on, last_error),
    }


# ---------------------------------------------------------------------------------------
# One resolver for "what is this account's email address and mailbox" (DESIGN-v2 §2.5)
# ---------------------------------------------------------------------------------------

REASON_ENTITY_NO_MAILBOX = "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime."
REASON_SHARED_RUNTIME_NO_MAILBOX = (
    "User accounts are off on this gateway, so only the operator's mailbox exists; this account can't sign in."
)
REASON_MAILBOXES_OFF_FOR_USER = "Mailboxes are turned off for this account (Email for everyone)."
REASON_MAILBOX_PAUSED = "The mailbox is paused: its owner switched Active off; the settings are kept."


def mailbox_view(plane: EmailPlane) -> Dict[str, Any]:
    """`{state, address, provider, reason}` of the plane's mailbox — the one shape the account
    page, `/admin/users` and `/admin/accounts` show. state: connected | not_connected | paused |
    unavailable."""

    pub = account_store(plane).public()
    configured = bool(pub.get("configured"))
    oauth = pub.get("oauth") if isinstance(pub.get("oauth"), dict) else None
    provider = (str(oauth.get("provider") or "") or None) if oauth else ("imap" if configured else None)
    address = str(pub.get("address") or "") or None
    if not admin_email_enabled(plane):
        return {"state": "unavailable", "address": address, "provider": provider, "reason": REASON_MAILBOXES_OFF_FOR_USER}
    if not configured:
        return {"state": "not_connected", "address": None, "provider": None, "reason": None}
    if not pub.get("enabled"):
        return {"state": "paused", "address": address, "provider": provider, "reason": REASON_MAILBOX_PAUSED}
    return {"state": "connected", "address": address, "provider": provider, "reason": None}


def account_email_view(principal: GatewayPrincipal) -> Dict[str, Any]:
    """`{email_address, mailbox}` for the account `principal` names, through exactly the code
    path `GET /me/email` uses: the principal's plane (`plane_for_principal`, the service's own
    rule — the admin's default runtime included), `registered_address(plane)` for the address
    (the user record, else the operator knob for the admin) and `mailbox_view(plane)`.
    `/me/email`, `/admin/users` and `/admin/accounts` all call this, so the admin's own row and
    the admin's own card can never disagree (item 4)."""

    if _is_entity(principal):
        from ..users import GatewayUserRegistry

        rec = GatewayUserRegistry().get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default"))
        address = str(getattr(rec, "email", "") or "").strip().lower() or None
        return {
            "email_address": address,
            "mailbox": {"state": "unavailable", "address": None, "provider": None, "reason": REASON_ENTITY_NO_MAILBOX},
        }
    from ..service import gateway_multi_user_enabled

    if not gateway_multi_user_enabled() and not principal.is_admin():
        # One shared runtime: this account's plane would be the operator's; never show it as theirs.
        from ..runtime_config import resolve_operator_email

        value = resolve_operator_email(gateway_data_dir_from_env(), tenant_id=principal.tenant_id, user_id=principal.user_id).get("value")
        return {
            "email_address": str(value or "").strip().lower() or None,
            "mailbox": {"state": "unavailable", "address": None, "provider": None, "reason": REASON_SHARED_RUNTIME_NO_MAILBOX},
        }
    plane = plane_for_principal(principal)
    return {"email_address": registered_address(plane) or None, "mailbox": mailbox_view(plane)}


def _admin_state_label(pub: Dict[str, Any], admin_on: bool, last_error: Optional[Dict[str, Any]]) -> str:
    if not admin_on:
        return "turned off by an administrator"
    if not pub.get("configured"):
        return "not connected"
    if not pub.get("enabled"):
        return "turned off by the user"
    if last_error:
        return "needs action"
    return "connected"


# ---------------------------------------------------------------------------------------
# Connect / OAuth request shapes
# ---------------------------------------------------------------------------------------


def build_servers(imap: Optional[Dict[str, Any]], smtp: Optional[Dict[str, Any]], preset: Optional[Dict[str, Any]] = None, *, allow_ca_file: bool):
    """IMAP / SMTP settings from request dicts (or a provider preset). `ca_file` names a file
    on the gateway host, so only an administrator may set it."""

    preset = preset or {}
    imap_raw = dict(imap) if isinstance(imap, dict) and str(imap.get("host") or "").strip() else dict(preset.get("imap") or {})
    smtp_raw = dict(smtp) if isinstance(smtp, dict) and str(smtp.get("host") or "").strip() else dict(preset.get("smtp") or {})
    for raw in (imap_raw, smtp_raw):
        if str(raw.get("ca_file") or "").strip() and not allow_ca_file:
            raise EmailInvalidSettings(
                "A CA file is a path on the gateway host, which only an administrator may set.",
                "Leave the CA file empty (the system trust store verifies public mail servers), or ask an administrator.",
            )
    imap_s = (
        ImapSettings.build(
            str(imap_raw["host"]),
            port=imap_raw.get("port"),
            security=str(imap_raw.get("security") or "ssl"),
            folder=str(imap_raw.get("folder") or "INBOX"),
            ca_file=str(imap_raw.get("ca_file") or ""),
        )
        if str(imap_raw.get("host") or "").strip()
        else None
    )
    smtp_s = (
        SmtpSettings.build(
            str(smtp_raw["host"]),
            port=smtp_raw.get("port"),
            security=str(smtp_raw.get("security") or "ssl"),
            ca_file=str(smtp_raw.get("ca_file") or ""),
        )
        if str(smtp_raw.get("host") or "").strip()
        else None
    )
    return imap_s, smtp_s


def discover(address: str, **kwargs: Any) -> Dict[str, Any]:
    """The mailbox's IMAP / SMTP servers from its address (AbstractCore's deterministic
    discovery: known providers, autoconfig, ISPDB, SRV, MX). A non-address is a typed 400."""

    try:
        return discover_servers(str(address or "").strip(), **kwargs)
    except ValueError:
        raise EmailInvalidSettings(
            f"{str(address or '').strip()!r} is not a valid email address.", "Give the mailbox's address as name@example.com."
        ) from None


def _no_host(leg: Optional[Dict[str, Any]]) -> bool:
    return not (isinstance(leg, dict) and str(leg.get("host") or "").strip())


def _named_step_error(err: EmailError) -> EmailError:
    """A failed connect names the step that failed (`details.step`: imap | smtp) and says it the
    way the Connect form shows it (`details.step_message`)."""

    d = err.details or {}
    step = str(d.get("protocol") or "")
    if step not in ("imap", "smtp"):
        return err
    host, port = str(d.get("host") or ""), d.get("port")
    where = f"{host}:{port}" if port else host
    if err.code == "email_auth_failed":
        msg = f"Sign-in refused by {host} \u2014 check the password."
    elif err.code == "email_tls_failed":
        msg = f"Couldn't set up a secure connection to {where}."
    elif err.code in ("email_unreachable", "email_transient"):
        msg = f"Couldn't reach {where}."
    else:
        msg = f"The {step.upper()} check at {where} failed: {err.cause}"
    err.details = {**d, "step": step, "step_message": msg}
    return err


def connect_password(
    plane: EmailPlane,
    *,
    address: str,
    password: str,
    username: str = "",
    display_name: str = "",
    imap: Optional[Dict[str, Any]] = None,
    smtp: Optional[Dict[str, Any]] = None,
    test: bool = True,
    allow_ca_file: bool = False,
    actor: str = "",
) -> Dict[str, Any]:
    require_admin_email_on(plane)
    discovery: Optional[Dict[str, Any]] = None
    if _no_host(imap) and _no_host(smtp):
        # No servers given: discover them from the address (400 email_discovery_failed + tried).
        try:
            found = require_servers(str(address or "").strip())
        except ValueError:
            raise EmailInvalidSettings(
                f"{str(address or '').strip()!r} is not a valid email address.", "Give the mailbox's address as name@example.com."
            ) from None
        imap = {**found["imap"], "folder": (imap or {}).get("folder") or "INBOX"} if isinstance(found.get("imap"), dict) else None
        smtp = dict(found["smtp"]) if isinstance(found.get("smtp"), dict) else None
        username = username or str(found.get("username") or "")
        discovery = {"source": found.get("source"), "provider": found.get("provider"), "tried": found.get("tried") or []}
    imap_s, smtp_s = build_servers(imap, smtp, allow_ca_file=allow_ca_file)
    account = EmailAccount.build(address=address, username=username, imap=imap_s, smtp=smtp_s, display_name=display_name)
    store = account_store(plane)
    reg = registered_address(plane)
    try:
        store.connect(account, EmailSecret(password), test=bool(test), registered_address=reg)
    except EmailError as err:
        raise _named_step_error(err) from None
    try:
        from .watcher import reset_watcher_cursor

        reset_watcher_cursor(plane)
    except Exception:  # noqa: BLE001
        pass
    rebind_live_runtime(plane)
    audit_email_event(
        "email.connected", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id, auth_kind="password",
        outcome="tested" if test else "untested", discovered=discovery is not None,
    )
    out = public_status(plane)
    if discovery is not None:
        out["discovery"] = discovery
    return out


def test_account(plane: EmailPlane, *, actor: str = "") -> Dict[str, Any]:
    require_admin_email_on(plane)
    store = sync_registered_address(plane)
    result = store.test()
    audit_email_event(
        "email.tested",
        tenant_id=plane.tenant_id,
        user_id=plane.user_id,
        actor=actor or plane.user_id,
        outcome="ok" if result.get("ok") else "failed",
        code=next(
            (str((result.get(leg) or {}).get("code")) for leg in ("imap", "smtp") if (result.get(leg) or {}).get("ok") is False),
            None,
        ),
    )
    return result


def set_folder(plane: EmailPlane, folder: str, *, actor: str = "") -> Dict[str, Any]:
    """The IMAP folder the mailbox is read from (agents' list/search, the mail watcher); empty =
    INBOX. The connection is kept and nothing is tested; the watcher starts the new folder from a
    fresh baseline (only mail that arrives after). No mailbox: `email_not_configured` (404)."""

    account_store(plane).set_folder(folder)
    try:
        from .watcher import reset_watcher_cursor

        reset_watcher_cursor(plane)
    except Exception:  # noqa: BLE001
        pass
    rebind_live_runtime(plane)
    out = public_status(plane)
    audit_email_event(
        "email.folder_changed", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id,
        folder=str(((out.get("imap") or {}).get("folder")) or ""),
    )
    return out


def disconnect(plane: EmailPlane, *, actor: str = "") -> Dict[str, Any]:
    account_store(plane).disconnect()
    try:
        from .watcher import reset_watcher_cursor

        reset_watcher_cursor(plane)
    except Exception:  # noqa: BLE001
        pass
    rebind_live_runtime(plane)
    audit_email_event("email.disconnected", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id)
    return public_status(plane)


# ---------------------------------------------------------------------------------------
# Bring-your-own OAuth clients (admin setting; never env vars)
# ---------------------------------------------------------------------------------------

OAUTH_PROVIDERS = ("google", "microsoft")
_OAUTH_LOCK = threading.Lock()


def _oauth_vault() -> SecretVault:
    return SecretVault(gateway_data_dir_from_env() / "email" / "oauth_clients")


def oauth_clients_raw() -> Dict[str, Dict[str, str]]:
    try:
        payload = _oauth_vault().load() or {}
    except EmailError:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for prov in OAUTH_PROVIDERS:
        row = payload.get(prov) if isinstance(payload, dict) else None
        if isinstance(row, dict) and str(row.get("client_id") or "").strip():
            out[prov] = {
                "client_id": str(row.get("client_id") or "").strip(),
                "client_secret": str(row.get("client_secret") or ""),
                "tenant": str(row.get("tenant") or "").strip(),
            }
    return out


def oauth_clients_public() -> Dict[str, Any]:
    raw = oauth_clients_raw()
    out: Dict[str, Any] = {}
    for prov in OAUTH_PROVIDERS:
        row = raw.get(prov)
        builtin = None
        try:
            from .core_mail import builtin_client

            builtin = builtin_client(prov)
        except Exception:  # noqa: BLE001
            builtin = None
        out[prov] = {
            "configured": row is not None,
            "client_id": row["client_id"] if row else "",
            "client_secret_set": bool(row and row.get("client_secret")),
            "tenant": row["tenant"] if row else "",
            "builtin_available": builtin is not None,
        }
    return {"providers": out}


_PROVIDER_LABELS = {"google": "Google", "microsoft": "Microsoft"}


def oauth_providers_for_users() -> List[Dict[str, Any]]:
    """`[{id, available, reason}]` for the account page's "Sign in with Google / Microsoft"
    buttons: available with the gateway's own client (admin setting) or a built-in one."""

    pub = oauth_clients_public()["providers"]
    out: List[Dict[str, Any]] = []
    for prov in OAUTH_PROVIDERS:
        row = pub.get(prov) or {}
        available = bool(row.get("configured") or row.get("builtin_available"))
        label = _PROVIDER_LABELS[prov]
        out.append({
            "id": prov,
            "available": available,
            "reason": None if available else f"No {label} sign-in client on this gateway: add one under Advanced, or ask your admin.",
        })
    return out


def set_oauth_client(provider: str, *, client_id: str, client_secret: Optional[str], tenant: str = "", actor: str = "") -> Dict[str, Any]:
    prov = str(provider or "").strip().lower()
    if prov not in OAUTH_PROVIDERS:
        raise EmailInvalidSettings(f"The OAuth provider {provider!r} is not one of: google, microsoft.", "Use google or microsoft.")
    cid = str(client_id or "").strip()
    with _OAUTH_LOCK:
        raw = oauth_clients_raw()
        if not cid:
            raw.pop(prov, None)
        else:
            previous = raw.get(prov) or {}
            secret = previous.get("client_secret", "") if client_secret is None and previous.get("client_id") == cid else str(client_secret or "")
            raw[prov] = {"client_id": cid, "client_secret": secret, "tenant": str(tenant or "").strip()}
        vault = _oauth_vault()
        if raw:
            vault.store(raw, reuse_key=vault.exists())
        else:
            vault.delete()
    audit_email_event("email.oauth_client_changed", actor=actor, provider=prov, outcome="set" if cid else "cleared")
    return oauth_clients_public()


OAUTH_CLIENTS_API = "PUT /api/gateway/admin/email/oauth-clients/<provider>"


def choose_oauth_client(provider: str, client_id: str = "", client_secret: str = "") -> Dict[str, str]:
    """Who signs in: the caller's own client, else the gateway's (admin setting), else the
    built-in AbstractFramework client; a typed error naming the ways forward otherwise.

    `held` says whose secret it is: `"caller"` (the caller brought it) or `"gateway"` (the
    admin's client or the built-in one). A gateway-held secret only ever goes to the
    provider preset's endpoints (`_gateway_secret_stays_with_preset`)."""

    prov = str(provider or "").strip().lower()
    if str(client_id or "").strip():
        return {**resolve_oauth_client(prov, client_id, client_secret), "held": "caller"}
    gw = oauth_clients_raw().get(prov)
    if gw:
        return {
            "client_id": gw["client_id"],
            "client_secret": gw.get("client_secret", ""),
            "source": "own",
            "tenant": gw.get("tenant", ""),
            "held": "gateway",
        }
    try:
        return {**resolve_oauth_client(prov, "", ""), "held": "gateway"}
    except EmailInvalidSettings as err:
        raise EmailInvalidSettings(
            err.cause,
            f"A gateway administrator can add an OAuth client for this provider ({OAUTH_CLIENTS_API}), "
            "or sign in with an app password instead.",
        ) from None


OAUTH_OVERRIDE_REFUSED = "email_oauth_override_refused"
# A Microsoft tenant is a GUID, a domain name, or common / organizations / consumers: letters,
# digits, dots and hyphens only, so it can never change the preset endpoint's host or path.
_TENANT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def _refuse_oauth_overrides(
    prov: str,
    *,
    is_admin: bool,
    tenant: str,
    token_endpoint: str,
    authorization_endpoint: str,
    device_authorization_endpoint: str,
    scopes: Optional[List[str]],
) -> None:
    """Endpoints and scopes come from the provider preset. Only an administrator, and only
    for provider `custom`, may give them (a custom provider brings its own client, never the
    gateway's). Otherwise any user could send the admin's OAuth client secret, or make the
    gateway POST, to a host of their choosing (framework backlog 0992, gate review G1)."""

    if prov == "custom":
        if not is_admin:
            raise EmailError(
                "A custom OAuth provider (explicit endpoints) is an administrator setting.",
                "Use google or microsoft (their endpoints are built in), or ask an administrator.",
                code=OAUTH_OVERRIDE_REFUSED,
            )
        return
    given = [
        name
        for name, value in (
            ("token_endpoint", token_endpoint),
            ("authorization_endpoint", authorization_endpoint),
            ("device_authorization_endpoint", device_authorization_endpoint),
        )
        if str(value or "").strip()
    ]
    if any(str(s or "").strip() for s in (scopes or [])):
        given.append("scopes")
    if given:
        raise EmailError(
            f"The {prov or 'OAuth'} sign-in uses the provider's built-in endpoints and scopes; "
            f"these fields cannot be overridden: {', '.join(given)}.",
            "Leave them empty. Explicit endpoints and scopes are for provider custom, which an administrator sets up.",
            code=OAUTH_OVERRIDE_REFUSED,
            details={"fields": given},
        )
    t = str(tenant or "").strip()
    if t and not set(t) <= _TENANT_CHARS:
        raise EmailError(
            f"The tenant {t!r} is not a tenant id or domain.",
            "Give a tenant GUID, a domain (contoso.onmicrosoft.com), or leave it empty (common).",
            code=OAUTH_OVERRIDE_REFUSED,
        )


def _gateway_secret_stays_with_preset(chosen: Dict[str, str], oauth: Any, preset: Dict[str, Any]) -> None:
    """A gateway-held client (the admin's or the built-in one) is used only against the
    provider preset's endpoints; anything else is refused before any request is made."""

    if chosen.get("held") == "caller":
        return
    for key in ("token_endpoint", "authorization_endpoint", "device_authorization_endpoint"):
        if str(getattr(oauth, key, "") or "") != str(preset.get(key) or ""):
            raise EmailError(
                "The gateway's OAuth client may only be used with the provider's own endpoints.",
                "Leave the endpoints empty, or bring your own OAuth client (client id and secret).",
                code=OAUTH_OVERRIDE_REFUSED,
                details={"field": key},
            )


# ---------------------------------------------------------------------------------------
# OAuth sign-in flows (in memory, per principal, 15 minutes)
# ---------------------------------------------------------------------------------------

_FLOWS: Dict[str, Dict[str, Any]] = {}
_FLOWS_LOCK = threading.Lock()
FLOW_TTL_S = 900.0


def _prune_flows() -> None:
    now = time.time()
    with _FLOWS_LOCK:
        for fid in [k for k, v in _FLOWS.items() if now - v["created"] > FLOW_TTL_S]:
            flow = _FLOWS.pop(fid)
            loop = flow.get("loopback")
            if loop is not None:
                loop.close()


def oauth_start(
    plane: EmailPlane,
    *,
    address: str,
    provider: str,
    client_id: str = "",
    client_secret: str = "",
    tenant: str = "",
    flow: str = "",
    display_name: str = "",
    imap: Optional[Dict[str, Any]] = None,
    smtp: Optional[Dict[str, Any]] = None,
    token_endpoint: str = "",
    authorization_endpoint: str = "",
    device_authorization_endpoint: str = "",
    scopes: Optional[List[str]] = None,
    ca_file: str = "",
    is_admin: bool = False,
) -> Dict[str, Any]:
    _prune_flows()
    require_admin_email_on(plane)
    prov = str(provider or "").strip().lower()
    _refuse_oauth_overrides(
        prov,
        is_admin=is_admin,
        tenant=tenant,
        token_endpoint=token_endpoint,
        authorization_endpoint=authorization_endpoint,
        device_authorization_endpoint=device_authorization_endpoint,
        scopes=scopes,
    )
    if ca_file and not is_admin:
        raise EmailInvalidSettings(
            "A CA file is a path on the gateway host, which only an administrator may set.",
            "Leave the CA file empty, or ask an administrator.",
        )
    chosen = choose_oauth_client(prov, client_id, client_secret)
    preset = provider_preset(prov, tenant=tenant or chosen.get("tenant", "")) if prov != "custom" else {}
    oauth = OAuthSettings.build(
        prov,
        chosen["client_id"],
        client_source=chosen["source"],
        token_endpoint=token_endpoint,
        authorization_endpoint=authorization_endpoint,
        device_authorization_endpoint=device_authorization_endpoint,
        scopes=list(scopes) if scopes else None,
        tenant=tenant or chosen.get("tenant", ""),
    )
    _gateway_secret_stays_with_preset(chosen, oauth, preset)
    imap_s, smtp_s = build_servers(imap, smtp, preset, allow_ca_file=is_admin)
    if ca_file:
        imap_s = replace(imap_s, ca_file=imap_s.ca_file or ca_file) if imap_s else None
        smtp_s = replace(smtp_s, ca_file=smtp_s.ca_file or ca_file) if smtp_s else None
    account = EmailAccount.build(
        address=address, username=address, imap=imap_s, smtp=smtp_s, display_name=display_name, auth_kind="oauth2", oauth=oauth
    )
    try:
        verify = tls_context(ca_file) if ca_file else None
    except (OSError, ValueError):
        raise EmailInvalidSettings(
            f"The CA file {ca_file!r} could not be loaded.",
            "Give the path of a readable PEM file, or leave it empty to use the system trust store.",
        ) from None
    client = OAuthTokenClient(oauth, client_secret=chosen["client_secret"], verify=verify)
    kind = str(flow or "").strip().lower() or ("device" if oauth.provider == "microsoft" else "loopback")
    if kind not in ("device", "loopback"):
        raise EmailInvalidSettings(f"The sign-in flow {flow!r} is not one of: device, loopback.", "Use device or loopback.")
    fid = secrets.token_urlsafe(18)
    entry: Dict[str, Any] = {
        "created": time.time(),
        "plane_key": plane.key,
        "account": account,
        "client": client,
        "kind": kind,
        "client_secret": chosen["client_secret"],
    }
    if kind == "device":
        device = client.start_device_authorization()
        entry["device"] = device
        out: Dict[str, Any] = {"flow_id": fid, "flow": "device", **device.public()}
    else:
        loop = LoopbackAuthorization(client, login_hint=address)
        url = loop.start()
        entry["loopback"] = loop
        out = {
            "flow_id": fid,
            "flow": "loopback",
            "authorization_url": url,
            "note": "Open this link in a browser on the gateway's own computer: the provider redirects to a one-shot "
            "listener on 127.0.0.1 there. On a remote gateway, use the device flow (Microsoft) or an app password.",
        }
    out["client_source"] = chosen["source"]
    with _FLOWS_LOCK:
        _FLOWS[fid] = entry
    return out


def _flow_for(plane: EmailPlane, flow_id: str) -> Dict[str, Any]:
    with _FLOWS_LOCK:
        entry = _FLOWS.get(str(flow_id or ""))
    if entry is None or entry.get("plane_key") != plane.key:
        # Another user's flow reads exactly like an unknown one.
        raise EmailOAuthFailed("This sign-in is unknown or expired.", "Start the sign-in again.")
    return entry


def oauth_finish(plane: EmailPlane, flow_id: str, *, wait_s: float = 0.0, actor: str = "") -> Dict[str, Any]:
    entry = _flow_for(plane, flow_id)
    wait = max(0.0, min(float(wait_s or 0.0), 60.0))
    client = entry["client"]
    try:
        if entry["kind"] == "device":
            device = entry["device"]
            deadline = time.time() + wait
            tokens = None
            while tokens is None:
                if time.time() >= device.expires_at:
                    raise EmailOAuthFailed("The sign-in code expired before it was approved.", "Start the sign-in again.")
                try:
                    tokens = client.poll_device_once(device)
                except EmailOAuthPending:
                    if time.time() + device.interval > deadline:
                        return {"ok": False, "pending": True, "flow_id": flow_id}
                    time.sleep(device.interval)
        else:
            loop = entry["loopback"]
            if not loop.wait(wait):
                return {"ok": False, "pending": True, "flow_id": flow_id}
            tokens = loop.finish(timeout_s=0.1)
    except EmailError:
        with _FLOWS_LOCK:
            _FLOWS.pop(flow_id, None)
        raise
    with _FLOWS_LOCK:
        _FLOWS.pop(flow_id, None)
    secret = EmailSecret(
        refresh_token=tokens.refresh_token,
        access_token=tokens.access_token,
        expires_at=tokens.expires_at,
        client_secret=entry["client_secret"],
    )
    store = account_store(plane)
    store.connect(entry["account"], secret, test=True, registered_address=registered_address(plane))
    try:
        from .watcher import reset_watcher_cursor

        reset_watcher_cursor(plane)
    except Exception:  # noqa: BLE001
        pass
    rebind_live_runtime(plane)
    audit_email_event(
        "email.connected", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id,
        auth_kind="oauth2", provider=entry["account"].oauth.provider if entry["account"].oauth else None, outcome="tested",
    )
    return {"ok": True, **public_status(plane)}


def oauth_cancel(plane: EmailPlane, flow_id: str) -> Dict[str, Any]:
    with _FLOWS_LOCK:
        entry = _FLOWS.get(str(flow_id or ""))
        if entry is None or entry.get("plane_key") != plane.key:
            return {"ok": True, "cancelled": False}
        _FLOWS.pop(str(flow_id), None)
    loop = entry.get("loopback")
    if loop is not None:
        loop.close()
    return {"ok": True, "cancelled": True}


# ---------------------------------------------------------------------------------------
# The retired ABSTRACT_EMAIL_* configuration (D10: import once into the admin's account)
# ---------------------------------------------------------------------------------------

# Variables that configured the retired gateway email bridge (not account settings).
_BRIDGE_ONLY = {
    "ABSTRACT_EMAIL_BRIDGE": "the mail watcher starts by itself for a user with a connected account and an email-triggered automation",
    "ABSTRACT_EMAIL_POLL_SECONDS": "the watcher checks every 60 s (automations that run a model on new mail: hourly by default, per automation)",
    "ABSTRACT_EMAIL_EVENT_NAME": "new mail feeds the email.received automation trigger",
    "ABSTRACT_EMAIL_SESSION_PREFIX": "new mail feeds the email.received automation trigger",
    "ABSTRACT_EMAIL_FLOW_ID": "new mail feeds the email.received automation trigger (create an automation)",
    "ABSTRACT_EMAIL_BUNDLE_ID": "new mail feeds the email.received automation trigger (create an automation)",
    "ABSTRACT_EMAIL_ACCOUNT": f"one account per user ({SETTINGS_LABEL})",
}

_notices: List[str] = []
_notices_lock = threading.Lock()


def legacy_env_notices(environ: Optional[Dict[str, str]] = None) -> List[str]:
    env = dict(os.environ if environ is None else environ)
    out = []
    for name in core_legacy.legacy_env_names(env):
        why = _BRIDGE_ONLY.get(name)
        if why:
            out.append(f"{name} is set but ignored: {why}.")
        else:
            out.append(
                f"{name} is set but ignored: the gateway's email is configured per user in {SETTINGS_LABEL} "
                "(PUT /api/gateway/me/email), stored encrypted in that user's data home."
            )
    return out


def _persisted_process_manager_email_env() -> Dict[str, str]:
    """Email variables an operator once saved through the process manager's env overrides
    (`<data_dir>/process_manager/env_overrides.json`, no longer applied): read for the
    one-time import only."""

    doc = _read_json(gateway_data_dir_from_env() / "process_manager" / "env_overrides.json")
    out: Dict[str, str] = {}
    rows = doc.get("vars") if isinstance(doc.get("vars"), dict) else {}
    for key, rec in rows.items():
        k = str(key)
        if not (k.startswith(core_legacy.LEGACY_ENV_PREFIX) or k in {"EMAIL_PASSWORD", "DEFAULT_EMAIL_PASSWORD"}):
            continue
        if isinstance(rec, dict) and rec.get("enabled", True) is not False and str(rec.get("value") or "").strip():
            out[k] = str(rec.get("value"))
    return out


def import_legacy_env_once(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """First boot with `ABSTRACT_EMAIL_*`: import once into the admin's account, then those
    variables are ignored and each one still set is named with the setting that replaced it."""

    env = dict(os.environ if environ is None else environ)
    try:
        for key, value in _persisted_process_manager_email_env().items():
            env.setdefault(key, value)
    except Exception:  # noqa: BLE001
        pass
    notes: List[str] = []
    try:
        plane = admin_plane()
        store = EmailAccountStore(config_file=plane.account_config_file, environ=env)
        before = store.settings().legacy_import
        core_notes = store.ensure_legacy_imported()
        after = store.settings().legacy_import
        if not before.get("done") and after.get("source") not in (None, "", "none"):
            if after.get("error"):
                notes.append(
                    f"The retired ABSTRACT_EMAIL_* configuration could not be imported ({after.get('error')}); "
                    f"connect the admin's account in {SETTINGS_LABEL}."
                )
            else:
                notes.append(
                    "Imported the retired ABSTRACT_EMAIL_* email account once into the admin's gateway email settings "
                    f"({SETTINGS_LABEL}); the variables are ignored from now on. The recipient policy starts as an "
                    "allowlist holding the account's own address."
                )
                if not after.get("secret_imported"):
                    notes.append(
                        f"The password was not imported (its variable was not set); set it in {SETTINGS_LABEL}."
                    )
                audit_email_event("email.legacy_imported", tenant_id=plane.tenant_id, user_id=plane.user_id, actor="gateway", outcome="imported")
        del core_notes
    except Exception as exc:  # noqa: BLE001 - boot never dies on the import
        notes.append(f"The retired ABSTRACT_EMAIL_* configuration could not be read ({type(exc).__name__}).")
    notes.extend(legacy_env_notices(env))
    with _notices_lock:
        _notices[:] = notes
    return list(notes)


def import_core_account_once() -> List[str]:
    """The administrator's gateway account starts from AbstractCore's own local account
    (`abstractcore email connect`, the core store) ONCE, when the gateway plane has none: the
    same person, the same machine. Later changes to either are independent."""

    notes: List[str] = []
    try:
        plane = admin_plane()
        marker = plane.email_dir / "core_import.json"
        if marker.exists():
            return notes
        gw = account_store(plane)
        if gw.settings().account is not None:
            _write_private_json(marker, {"done": True, "at": _now_iso(), "source": "none (gateway account exists)"})
            return notes
        from ..core_config import core_store_path

        core_path = core_store_path()
        if core_path is None or not Path(core_path).is_file():
            return notes
        core = EmailAccountStore(config_file=core_path)
        st = core.settings()
        if st.account is None or not core.vault.exists():
            return notes
        ctx = core.context(require_enabled=False)
        gw.connect(st.account, ctx.secret, test=False, registered_address=registered_address(plane) or None)
        if not st.policy_is_default:
            gw.set_policy(mode=st.policy.mode, add=list(st.policy.entries), clear=True)
        gw.set_limits(per_hour=st.limits.per_hour, per_day=st.limits.per_day)
        _write_private_json(marker, {"done": True, "at": _now_iso(), "source": "abstractcore"})
        audit_email_event("email.legacy_imported", tenant_id=plane.tenant_id, user_id=plane.user_id, actor="gateway", outcome="imported", reason="abstractcore local account")
        notes.append(
            "Imported AbstractCore's local email account once into the administrator's gateway email settings "
            f"({SETTINGS_LABEL}); from now on the two are configured separately."
        )
    except Exception as exc:  # noqa: BLE001 - boot never dies on the import
        notes.append(f"AbstractCore's local email account could not be imported ({type(exc).__name__}); connect it in {SETTINGS_LABEL}.")
    with _notices_lock:
        _notices.extend(notes)
    return notes


def boot_notices() -> List[str]:
    with _notices_lock:
        return list(_notices)
