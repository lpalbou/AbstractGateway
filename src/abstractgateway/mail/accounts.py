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
    provider_preset,
    resolve_oauth_client,
    tls_context,
)
from .core_mail import legacy as core_legacy

from ..security.principal import GatewayPrincipal, local_admin_principal, safe_principal_component
from ..users import gateway_data_dir_from_env
from .audit import audit_email_event

ACCOUNT_ID = "default"
SETTINGS_LABEL = "Settings → My email"
ADMIN_DISABLED_CAUSE = "A gateway administrator turned email off for your account."
ADMIN_DISABLED_FIX = "Ask a gateway administrator to turn it back on (Users → Email); your settings are kept."


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

_CAP_LOCK = threading.Lock()


def _capability_path() -> Path:
    return gateway_data_dir_from_env() / "auth" / "email_capability.json"


def admin_email_enabled(plane: EmailPlane) -> bool:
    doc = _read_json(_capability_path())
    row = (doc.get("users") or {}).get(plane.key) if isinstance(doc.get("users"), dict) else None
    if isinstance(row, dict) and row.get("enabled") is False:
        return False
    return True


def set_admin_email_enabled(plane: EmailPlane, enabled: bool, *, actor: str) -> Dict[str, Any]:
    with _CAP_LOCK:
        path = _capability_path()
        doc = _read_json(path)
        users = doc.get("users") if isinstance(doc.get("users"), dict) else {}
        users[plane.key] = {"enabled": bool(enabled), "by": str(actor or ""), "at": _now_iso()}
        _write_private_json(path, {"version": 1, "users": users})
    rebind_live_runtime(plane)
    audit_email_event(
        "email.capability_changed",
        tenant_id=plane.tenant_id,
        user_id=plane.user_id,
        actor=actor,
        enabled=bool(enabled),
    )
    return {"enabled": bool(enabled)}


def email_context(plane: EmailPlane, *, require_enabled: bool = True) -> EmailContext:
    """The user's account ready for use: policy, limits, the user's switch AND the admin's."""

    store = sync_registered_address(plane)
    ctx = store.context(require_enabled=False)
    if not admin_email_enabled(plane):
        ctx.enabled = False
        if require_enabled:
            raise EmailDisabled(ADMIN_DISABLED_CAUSE, ADMIN_DISABLED_FIX)
    elif require_enabled:
        ctx.require_enabled()
    return ctx


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
    try:
        from .watcher import watcher_public_status

        out["watcher"] = watcher_public_status(plane)
    except Exception:  # noqa: BLE001 - the status view never fails on the watcher file
        out["watcher"] = {"state": "unknown"}
    out["source"] = "gateway user settings"
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
        "watcher": watcher,
        "state": _admin_state_label(pub, admin_on, last_error),
    }


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
    imap_s, smtp_s = build_servers(imap, smtp, allow_ca_file=allow_ca_file)
    account = EmailAccount.build(address=address, username=username, imap=imap_s, smtp=smtp_s, display_name=display_name)
    store = account_store(plane)
    reg = registered_address(plane)
    store.connect(account, EmailSecret(password), test=bool(test), registered_address=reg)
    try:
        from .watcher import reset_watcher_cursor

        reset_watcher_cursor(plane)
    except Exception:  # noqa: BLE001
        pass
    rebind_live_runtime(plane)
    audit_email_event(
        "email.connected", tenant_id=plane.tenant_id, user_id=plane.user_id, actor=actor or plane.user_id, auth_kind="password",
        outcome="tested" if test else "untested",
    )
    return public_status(plane)


def test_account(plane: EmailPlane, *, actor: str = "") -> Dict[str, Any]:
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


def choose_oauth_client(provider: str, client_id: str = "", client_secret: str = "") -> Dict[str, str]:
    """Who signs in: the caller's own client, else the gateway's (admin setting), else the
    built-in AbstractFramework client; a typed error naming the ways forward otherwise."""

    prov = str(provider or "").strip().lower()
    if str(client_id or "").strip():
        return resolve_oauth_client(prov, client_id, client_secret)
    gw = oauth_clients_raw().get(prov)
    if gw:
        return {"client_id": gw["client_id"], "client_secret": gw.get("client_secret", ""), "source": "own", "tenant": gw.get("tenant", "")}
    try:
        return resolve_oauth_client(prov, "", "")
    except EmailInvalidSettings as err:
        raise EmailInvalidSettings(
            err.cause,
            "A gateway administrator can add an OAuth client for this provider (Settings → My email → OAuth clients), "
            "or sign in with an app password instead.",
        ) from None


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
    prov = str(provider or "").strip().lower()
    if prov == "custom" and not is_admin:
        raise EmailInvalidSettings(
            "A custom OAuth provider (explicit endpoints) is an administrator setting.",
            "Use google or microsoft, or ask an administrator.",
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


def boot_notices() -> List[str]:
    with _notices_lock:
        return list(_notices)
