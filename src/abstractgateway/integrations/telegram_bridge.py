from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, Optional

from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from abstractcore.tools.telegram_tdlib import TdlibNotAvailable, get_global_tdlib_client, stop_global_tdlib_client
except Exception:  # pragma: no cover
    # Keep Telegram bridge importable in minimal environments where `abstractcore` (or its deps)
    # are not installed. TDLib transport will surface a clear runtime error when used.
    class TdlibNotAvailable(RuntimeError):
        pass

    def get_global_tdlib_client(*, start: bool = False):  # type: ignore[no-untyped-def]
        raise TdlibNotAvailable(
            "TDLib transport requires `abstractcore` and its Telegram TDLib helper. "
            "Install: pip install \"abstractcore[tools]\" and configure TDLib (tdjson + env vars)."
        )

    def stop_global_tdlib_client() -> None:
        return None
from abstractruntime import Runtime
from abstractruntime.core.models import RunStatus, WaitReason


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_lines_or_json_list(raw: Any) -> list[str]:
    """Parse a newline-separated string or a JSON array of strings (best-effort)."""
    if raw is None or isinstance(raw, bool):
        return []
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()]
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]


def _parse_ints_lines_or_json_list(raw: Any) -> list[int]:
    """Parse a newline/comma-separated string or JSON array into a list of ints (best-effort)."""
    if raw is None or isinstance(raw, bool):
        return []
    text = str(raw or "").strip()
    if not text:
        return []

    items: list[Any] = []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            items = list(parsed)

    if not items:
        parts = [p.strip() for p in re.split(r"[,\n]", text)]
        items = [p for p in parts if p]

    out: list[int] = []
    for x in items:
        if isinstance(x, int) and not isinstance(x, bool):
            out.append(int(x))
            continue
        try:
            s = str(x or "").strip()
        except Exception:
            continue
        if not s:
            continue
        try:
            out.append(int(s))
        except Exception:
            continue

    # Dedup while preserving order.
    seen: set[int] = set()
    deduped: list[int] = []
    for n in out:
        if n in seen:
            continue
        seen.add(n)
        deduped.append(int(n))
    return deduped


def _get_by_path(value: Any, path: str) -> Any:
    """Best-effort dotted-path lookup supporting dicts and numeric list indices."""
    current = value
    for part in str(path or "").split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            idx = int(part)
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
            continue
        return None
    return current


def _command_base(text: str) -> str:
    """Return the normalized bot-command token (e.g. "/reset" from "/reset@botname foo")."""
    s = str(text or "").strip()
    if not s.startswith("/"):
        return ""
    token = s.split(maxsplit=1)[0].strip()
    if "@" in token:
        token = token.split("@", 1)[0].strip()
    return token.lower()


def _command_args(text: str) -> str:
    """Return the raw args string after the bot-command token (best-effort)."""
    s = str(text or "").strip()
    if not s.startswith("/"):
        return ""
    parts = s.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return str(parts[1] or "").strip()


def _approval_token(text: str) -> str:
    """Normalize a user message into an approval token ("approve"/"reject"/"")."""
    s = str(text or "").strip().lower()
    if not s:
        return ""
    # Bot commands
    if s.startswith("/"):
        base = _command_base(s)
        if base in {"/approve", "/ok", "/yes"}:
            return "approve"
        if base in {"/reject", "/deny", "/cancel", "/no"}:
            return "reject"
        return ""
    # Plain text
    if s in {"approve", "approved", "ok", "okay", "yes", "y"}:
        return "approve"
    if s in {"reject", "rejected", "deny", "denied", "cancel", "cancelled", "canceled", "no", "n"}:
        return "reject"
    return ""


_TELEGRAM_PROTECTED_TOOLS: set[str] = {"send_telegram_message", "send_telegram_artifact"}


@dataclass(frozen=True)
class TelegramBridgeConfig:
    enabled: bool
    transport: str  # "tdlib" | "bot_api"
    event_name: str
    session_prefix: str
    flow_id: str
    bundle_id: Optional[str]
    state_path: Path

    # Bot API settings (used only when transport == bot_api)
    bot_token_env_var: str = "ABSTRACT_TELEGRAM_BOT_TOKEN"
    poll_timeout_s: int = 25
    poll_sleep_s: float = 0.25

    # Typing indicator ("...")
    typing_interval_s: float = 4.0
    typing_max_s: float = 600.0

    # Storage behavior
    store_media: bool = True
    pending_media_max_s: float = 300.0

    # Optional routing overrides (Telegram-only)
    provider_override: Optional[str] = None
    model_override: Optional[str] = None

    # Conversation context limits (applied via `_limits.max_history_messages`)
    max_history_messages: int = 30

    # /reset behavior
    reset_delete_messages: bool = True
    reset_delete_max: int = 200
    reset_message: str = "Conversation reset. Send a new message to start fresh."

    # Access control
    #
    # Telegram bots (and user accounts) are discoverable; without access control, anyone can message
    # the bridge and trigger durable runs + LLM calls. These policies are enforced in-process in the
    # bridge before creating bindings or emitting events.
    #
    # DM policy: "disabled" | "open" | "allowlist" | "pairing"
    # Group policy: "disabled" | "open" | "allowlist"
    dm_policy: str = "pairing"
    group_policy: str = "allowlist"
    require_mention_in_groups: bool = True
    pairing_ttl_s: float = 3600.0
    allowed_users: frozenset[int] = frozenset()
    allowed_chats: frozenset[int] = frozenset()
    group_allowed_users: Optional[frozenset[int]] = None  # None -> fallback to allowed_users; empty -> allow any sender
    admin_users: frozenset[int] = frozenset()

    # Tool permissions (Telegram UX)
    #
    # These defaults apply per-chat unless overridden via `/tools ...` commands.
    tool_approve_all: bool = False
    tool_allowed_tools: Optional[list[str]] = None  # None means "all"
    tool_auto_approve_tools: Optional[list[str]] = None  # None means runtime defaults
    tool_require_approval_tools: Optional[list[str]] = None  # None means runtime defaults
    tool_blocked_tools: Optional[list[str]] = None

    @staticmethod
    def from_env(*, base_dir: Path) -> "TelegramBridgeConfig":
        enabled = _as_bool(os.getenv("ABSTRACT_TELEGRAM_BRIDGE"), False)
        transport_raw = str(os.getenv("ABSTRACT_TELEGRAM_TRANSPORT", "") or "").strip().lower()
        transport = "tdlib" if transport_raw in {"", "tdlib"} else "bot_api" if transport_raw in {"bot", "bot_api", "botapi"} else "tdlib"

        event_name = str(os.getenv("ABSTRACT_TELEGRAM_EVENT_NAME", "") or "").strip() or "telegram.message"
        session_prefix = str(os.getenv("ABSTRACT_TELEGRAM_SESSION_PREFIX", "") or "").strip() or "telegram:"

        flow_id = str(os.getenv("ABSTRACT_TELEGRAM_FLOW_ID", "") or os.getenv("ABSTRACT_TELEGRAM_DEFAULT_FLOW_ID", "") or "").strip()
        bundle_id = str(os.getenv("ABSTRACT_TELEGRAM_BUNDLE_ID", "") or "").strip() or None

        state_path = Path(os.getenv("ABSTRACT_TELEGRAM_STATE_PATH", "") or "").expanduser().resolve() if os.getenv("ABSTRACT_TELEGRAM_STATE_PATH") else (Path(base_dir) / "telegram_bridge_state.json")

        poll_timeout_s = int(float(os.getenv("ABSTRACT_TELEGRAM_POLL_TIMEOUT_S", "25") or "25"))
        poll_sleep_s = float(os.getenv("ABSTRACT_TELEGRAM_POLL_SLEEP_S", "0.25") or "0.25")
        store_media = _as_bool(os.getenv("ABSTRACT_TELEGRAM_STORE_MEDIA"), True)
        typing_interval_s = float(os.getenv("ABSTRACT_TELEGRAM_TYPING_INTERVAL_S", "4.0") or "4.0")
        typing_max_s = float(os.getenv("ABSTRACT_TELEGRAM_TYPING_MAX_S", "600.0") or "600.0")
        pending_media_max_s = float(os.getenv("ABSTRACT_TELEGRAM_PENDING_MEDIA_MAX_S", "300.0") or "300.0")

        provider_override = str(os.getenv("ABSTRACT_TELEGRAM_PROVIDER", "") or "").strip().lower() or None
        model_override = str(os.getenv("ABSTRACT_TELEGRAM_MODEL", "") or "").strip() or None
        # Provider-only overrides are ambiguous (we can't infer the right model). Ignore them.
        if provider_override and not model_override:
            provider_override = None

        try:
            max_history_messages = int(float(os.getenv("ABSTRACT_TELEGRAM_MAX_HISTORY_MESSAGES", "30") or "30"))
        except Exception:
            max_history_messages = 30
        if max_history_messages < 0:
            max_history_messages = 0

        reset_delete_messages = _as_bool(os.getenv("ABSTRACT_TELEGRAM_RESET_DELETE_MESSAGES"), True)
        try:
            reset_delete_max = int(float(os.getenv("ABSTRACT_TELEGRAM_RESET_DELETE_MAX", "200") or "200"))
        except Exception:
            reset_delete_max = 200
        if reset_delete_max < 0:
            reset_delete_max = 0
        reset_message = (
            str(os.getenv("ABSTRACT_TELEGRAM_RESET_MESSAGE", "") or "").strip()
            or "Conversation reset. Send a new message to start fresh."
        )

        dm_policy_raw = str(os.getenv("ABSTRACT_TELEGRAM_DM_POLICY", "") or "").strip().lower()
        if dm_policy_raw in {"allow_list", "allow-list", "whitelist"}:
            dm_policy_raw = "allowlist"
        if dm_policy_raw in {"pair", "pairing"}:
            dm_policy = "pairing"
        elif dm_policy_raw in {"disabled", "off", "none"}:
            dm_policy = "disabled"
        elif dm_policy_raw in {"open", "public"}:
            dm_policy = "open"
        elif dm_policy_raw in {"allowlist"}:
            dm_policy = "allowlist"
        else:
            dm_policy = "pairing"

        group_policy_raw = str(os.getenv("ABSTRACT_TELEGRAM_GROUP_POLICY", "") or "").strip().lower()
        if group_policy_raw in {"allow_list", "allow-list", "whitelist"}:
            group_policy_raw = "allowlist"
        if group_policy_raw in {"disabled", "off", "none"}:
            group_policy = "disabled"
        elif group_policy_raw in {"open", "public"}:
            group_policy = "open"
        elif group_policy_raw in {"allowlist"}:
            group_policy = "allowlist"
        else:
            group_policy = "allowlist"

        require_mention_in_groups = _as_bool(os.getenv("ABSTRACT_TELEGRAM_REQUIRE_MENTION_IN_GROUPS"), True)
        try:
            pairing_ttl_s = float(os.getenv("ABSTRACT_TELEGRAM_PAIRING_TTL_S", "3600") or "3600")
        except Exception:
            pairing_ttl_s = 3600.0
        if pairing_ttl_s < 0:
            pairing_ttl_s = 0.0

        allowed_users = frozenset(_parse_ints_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_ALLOWED_USERS")))
        allowed_chats = frozenset(_parse_ints_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_ALLOWED_CHATS")))

        group_allowed_users_raw = os.getenv("ABSTRACT_TELEGRAM_GROUP_ALLOWED_USERS")
        group_allowed_users_list = _parse_ints_lines_or_json_list(group_allowed_users_raw)
        group_allowed_users = None if group_allowed_users_raw is None else frozenset(group_allowed_users_list)

        admin_users = frozenset(_parse_ints_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_ADMIN_USERS")))

        tool_approve_all = _as_bool(os.getenv("ABSTRACT_TELEGRAM_APPROVE_ALL_TOOLS"), False)
        tool_allowed_tools = _parse_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_ALLOWED_TOOLS"))
        tool_auto_approve_tools = _parse_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_AUTO_APPROVE_TOOLS"))
        tool_require_approval_tools = _parse_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_REQUIRE_APPROVAL_TOOLS"))
        tool_blocked_tools = _parse_lines_or_json_list(os.getenv("ABSTRACT_TELEGRAM_BLOCKED_TOOLS"))

        # Interpret empty lists as "unset" to keep defaults predictable.
        if not tool_allowed_tools:
            tool_allowed_tools = None
        if not tool_auto_approve_tools:
            tool_auto_approve_tools = None
        if not tool_require_approval_tools:
            tool_require_approval_tools = None
        if not tool_blocked_tools:
            tool_blocked_tools = None

        return TelegramBridgeConfig(
            enabled=bool(enabled),
            transport=transport,
            event_name=event_name,
            session_prefix=session_prefix,
            flow_id=flow_id,
            bundle_id=bundle_id,
            state_path=state_path,
            poll_timeout_s=max(0, int(poll_timeout_s)),
            poll_sleep_s=max(0.0, float(poll_sleep_s)),
            typing_interval_s=max(0.5, float(typing_interval_s)),
            typing_max_s=max(0.0, float(typing_max_s)),
            store_media=bool(store_media),
            pending_media_max_s=max(0.0, float(pending_media_max_s)),
            provider_override=provider_override,
            model_override=model_override,
            max_history_messages=int(max_history_messages),
            reset_delete_messages=bool(reset_delete_messages),
            reset_delete_max=int(reset_delete_max),
            reset_message=str(reset_message),
            dm_policy=str(dm_policy),
            group_policy=str(group_policy),
            require_mention_in_groups=bool(require_mention_in_groups),
            pairing_ttl_s=float(pairing_ttl_s),
            allowed_users=frozenset(int(x) for x in allowed_users if isinstance(x, int) and not isinstance(x, bool)),
            allowed_chats=frozenset(int(x) for x in allowed_chats if isinstance(x, int) and not isinstance(x, bool)),
            group_allowed_users=frozenset(int(x) for x in group_allowed_users if isinstance(x, int) and not isinstance(x, bool)) if isinstance(group_allowed_users, frozenset) else None,
            admin_users=frozenset(int(x) for x in admin_users if isinstance(x, int) and not isinstance(x, bool)),
            tool_approve_all=bool(tool_approve_all),
            tool_allowed_tools=list(tool_allowed_tools) if isinstance(tool_allowed_tools, list) else None,
            tool_auto_approve_tools=list(tool_auto_approve_tools) if isinstance(tool_auto_approve_tools, list) else None,
            tool_require_approval_tools=list(tool_require_approval_tools) if isinstance(tool_require_approval_tools, list) else None,
            tool_blocked_tools=list(tool_blocked_tools) if isinstance(tool_blocked_tools, list) else None,
        )


class TelegramBridge:
    """Bridge inbound Telegram messages to AbstractGateway events."""

    def __init__(self, *, config: TelegramBridgeConfig, host: Any, runner: Any, artifact_store: Any):
        self._cfg = config
        self._host = host
        self._runner = runner
        self._artifact_store = artifact_store

        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Bot API polling cursor
        self._bot_offset: int = 0

        # TDLib handler toggle
        self._tdlib_handler_installed = False

        # Typing indicator loops (per chat_id)
        self._typing_lock = threading.Lock()
        self._typing_until: Dict[int, float] = {}
        self._typing_threads: Dict[int, threading.Thread] = {}

        # Access-control caches (best-effort)
        self._bot_username: Optional[str] = None
        self._tdlib_username: Optional[str] = None
        self._tdlib_chat_kind_cache: Dict[int, tuple[str, float]] = {}
        self._unauthorized_log_until: Dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if not self._cfg.flow_id:
            raise ValueError("ABSTRACT_TELEGRAM_FLOW_ID is required when ABSTRACT_TELEGRAM_BRIDGE=1")

        self._load_state()
        # Operator footgun: local tool mode bypasses the Telegram approval UX.
        try:
            import logging

            tm = str(os.getenv("ABSTRACTGATEWAY_TOOL_MODE") or "passthrough").strip().lower()
            if tm in {"local", "local_all", "local-all"}:
                logging.getLogger(__name__).warning(
                    "Telegram bridge: ABSTRACTGATEWAY_TOOL_MODE=%s bypasses tool approvals; "
                    "set ABSTRACTGATEWAY_TOOL_MODE=passthrough (recommended) or approval.",
                    tm,
                )
        except Exception:
            pass

        # Access control warnings (fail-closed defaults).
        try:
            import logging

            logger = logging.getLogger(__name__)
            dm_pol = str(self._cfg.dm_policy or "").strip().lower() or "pairing"
            grp_pol = str(self._cfg.group_policy or "").strip().lower() or "allowlist"

            if dm_pol == "open":
                logger.warning("Telegram bridge: dm_policy=open allows any Telegram user to trigger runs (dangerous).")
            if grp_pol == "open":
                logger.warning("Telegram bridge: group_policy=open allows any Telegram group to trigger runs (dangerous).")
            if dm_pol == "pairing" and not set(self._cfg.admin_users or frozenset()):
                logger.warning(
                    "Telegram bridge: dm_policy=pairing but ABSTRACT_TELEGRAM_ADMIN_USERS is empty; "
                    "pairing approvals require an admin to send /pair approve <code>. "
                    "Tip: use /whoami to discover your numeric user_id."
                )
            if dm_pol == "allowlist" and not (self._effective_allowed_users() or self._effective_allowed_chats()):
                logger.warning(
                    "Telegram bridge: dm_policy=allowlist but no allowed users/chats are configured; "
                    "all DMs will be ignored except /whoami. Set ABSTRACT_TELEGRAM_ALLOWED_USERS or use dm_policy=pairing."
                )
            if grp_pol == "allowlist" and not self._effective_allowed_chats():
                logger.warning(
                    "Telegram bridge: group_policy=allowlist but ABSTRACT_TELEGRAM_ALLOWED_CHATS is empty; "
                    "all group messages will be ignored (expected for secure defaults)."
                )
        except Exception:
            pass

        if self._cfg.transport == "bot_api":
            if not self._bot_token():
                name = str(self._cfg.bot_token_env_var or "").strip() or "ABSTRACT_TELEGRAM_BOT_TOKEN"
                raise ValueError(f"Missing Telegram bot token env var {name} (required when ABSTRACT_TELEGRAM_TRANSPORT=bot_api)")
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._bot_loop, name="telegram-bot-bridge", daemon=True)
            self._thread.start()
            return

        # TDLib: reuse the global TDLib receive loop and install an update handler.
        if self._tdlib_handler_installed:
            return
        try:
            client = get_global_tdlib_client(start=True)
        except (TdlibNotAvailable, ValueError) as e:
            raise RuntimeError(
                "Telegram bridge is enabled with TDLib transport, but TDLib could not be initialized. "
                "Configure TDLib (tdjson + required env vars) and run `abstractgateway telegram-auth` once."
            ) from e
        client.add_update_handler(self._handle_tdlib_update)
        self._tdlib_handler_installed = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=3.0)
            except Exception:
                pass
        self._thread = None
        if self._cfg.transport == "tdlib" and self._tdlib_handler_installed:
            # Best-effort cleanup; TDLib is single-instance per database dir.
            try:
                stop_global_tdlib_client()
            except Exception:
                pass
            self._tdlib_handler_installed = False

    # ---------------------------------------------------------------------
    # State (chat_id -> binding)
    # ---------------------------------------------------------------------

    def _load_state(self) -> None:
        path = self._cfg.state_path
        try:
            if path.exists():
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    self._state = obj
        except Exception:
            self._state = {}
        self._state.setdefault("version", 1)
        self._state.setdefault("access", {})
        self._state.setdefault("bindings", {})
        self._state.setdefault("session_revs", {})
        self._state.setdefault("approval_prompts", {})
        self._state.setdefault("tool_policies", {})

    def _save_state(self) -> None:
        path = self._cfg.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Access control (Telegram)
    # ---------------------------------------------------------------------

    def _access_state(self) -> Dict[str, Any]:
        access = self._state.get("access")
        if not isinstance(access, dict):
            access = {}
            self._state["access"] = access
        access.setdefault("version", 1)
        access.setdefault("authorized_users", [])
        access.setdefault("authorized_chats", [])
        access.setdefault("pairing_requests", {})  # code -> record
        access.setdefault("pairing_user_to_code", {})  # str(user_id) -> code
        return access

    def _authorized_users(self) -> set[int]:
        access = self._access_state()
        raw = access.get("authorized_users")
        out: set[int] = set()
        if isinstance(raw, list):
            for x in raw:
                if isinstance(x, int) and not isinstance(x, bool):
                    out.add(int(x))
        return out

    def _authorized_chats(self) -> set[int]:
        access = self._access_state()
        raw = access.get("authorized_chats")
        out: set[int] = set()
        if isinstance(raw, list):
            for x in raw:
                if isinstance(x, int) and not isinstance(x, bool):
                    out.add(int(x))
        return out

    def _is_admin_user(self, user_id: Optional[int]) -> bool:
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return False
        return int(user_id) in set(self._cfg.admin_users or frozenset())

    def _effective_allowed_users(self) -> set[int]:
        return set(self._cfg.allowed_users or frozenset()) | self._authorized_users() | set(self._cfg.admin_users or frozenset())

    def _effective_allowed_chats(self) -> set[int]:
        return set(self._cfg.allowed_chats or frozenset()) | self._authorized_chats()

    def _effective_group_allowed_users(self) -> set[int]:
        raw = self._cfg.group_allowed_users
        if raw is None:
            return set(self._cfg.allowed_users or frozenset()) | self._authorized_users()
        # Explicit empty -> no sender restriction.
        if not raw:
            return set()
        return set(raw)

    def _cleanup_expired_pairings_locked(self, access: Dict[str, Any]) -> None:
        ttl = float(self._cfg.pairing_ttl_s or 0.0)
        if ttl <= 0:
            # Pairing effectively disabled; drop requests to avoid confusing operators.
            access["pairing_requests"] = {}
            access["pairing_user_to_code"] = {}
            return

        now = time.time()
        reqs = access.get("pairing_requests")
        if not isinstance(reqs, dict):
            reqs = {}
            access["pairing_requests"] = reqs
        u2c = access.get("pairing_user_to_code")
        if not isinstance(u2c, dict):
            u2c = {}
            access["pairing_user_to_code"] = u2c

        expired_codes: list[str] = []
        for code, rec in list(reqs.items()):
            if not isinstance(code, str) or not isinstance(rec, dict):
                expired_codes.append(str(code))
                continue
            exp = rec.get("expires_at_s")
            if not isinstance(exp, (int, float)) or isinstance(exp, bool) or float(exp) <= now:
                expired_codes.append(str(code))
        for code in expired_codes:
            rec = reqs.pop(str(code), None)
            if isinstance(rec, dict):
                uid = rec.get("user_id")
                if isinstance(uid, int) and not isinstance(uid, bool):
                    u2c.pop(str(uid), None)

    def _new_pairing_code(self, *, existing: set[str]) -> str:
        import secrets
        import string

        alphabet = string.ascii_uppercase + string.digits
        for _ in range(64):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if code not in existing:
                return code
        return secrets.token_hex(4).upper()

    def _ensure_pairing_request(self, *, user_id: int, chat_id: int) -> tuple[str, Dict[str, Any], bool]:
        """Return (code, record, is_new) for a user pairing request."""
        uid = int(user_id)
        now = time.time()
        ttl = float(self._cfg.pairing_ttl_s or 0.0)
        with self._lock:
            access = self._access_state()
            self._cleanup_expired_pairings_locked(access)

            reqs = access.get("pairing_requests")
            if not isinstance(reqs, dict):
                reqs = {}
                access["pairing_requests"] = reqs
            u2c = access.get("pairing_user_to_code")
            if not isinstance(u2c, dict):
                u2c = {}
                access["pairing_user_to_code"] = u2c

            code0 = u2c.get(str(uid))
            if isinstance(code0, str) and code0.strip():
                rec0 = reqs.get(code0.strip())
                if isinstance(rec0, dict):
                    exp0 = rec0.get("expires_at_s")
                    if isinstance(exp0, (int, float)) and not isinstance(exp0, bool) and float(exp0) > now:
                        return code0.strip(), dict(rec0), False

            existing = {str(k).strip() for k in reqs.keys() if isinstance(k, str) and str(k).strip()}
            code = self._new_pairing_code(existing=existing)
            rec = {
                "code": str(code),
                "user_id": int(uid),
                "chat_id": int(chat_id),
                "requested_at": _utc_now_iso(),
                "expires_at_s": float(now + max(0.0, ttl)),
            }
            reqs[str(code)] = rec
            u2c[str(uid)] = str(code)
            self._save_state()
            return str(code), dict(rec), True

    def _format_pairing_request_message(self, *, code: str) -> str:
        ttl = float(self._cfg.pairing_ttl_s or 0.0)
        mins = int(ttl // 60) if ttl > 0 else 0
        lines = [
            "This Telegram contact is private.",
            "",
            f"Pairing code: {str(code).strip()}",
            "",
            "Ask the operator to approve with:",
            f"/pair approve {str(code).strip()}",
        ]
        if mins > 0:
            lines.append(f"(expires in ~{mins} min)")
        lines.extend(["", "Send /whoami to share your IDs with the operator."])
        return "\n".join(lines)

    def _approve_pairing_code(self, *, code: str) -> tuple[Optional[Dict[str, Any]], str]:
        c = str(code or "").strip().upper()
        if not c:
            return None, "Usage: /pair approve <code>"
        with self._lock:
            access = self._access_state()
            self._cleanup_expired_pairings_locked(access)
            reqs = access.get("pairing_requests")
            if not isinstance(reqs, dict):
                return None, "No pending pairing requests."
            rec = reqs.get(c)
            if not isinstance(rec, dict):
                return None, "Unknown or expired pairing code."

            uid = rec.get("user_id")
            if not isinstance(uid, int) or isinstance(uid, bool):
                return None, "Invalid pairing request (missing user_id)."
            user_id = int(uid)

            users = self._authorized_users()
            users.add(int(user_id))
            access["authorized_users"] = sorted(users)

            reqs.pop(c, None)
            u2c = access.get("pairing_user_to_code")
            if isinstance(u2c, dict):
                u2c.pop(str(user_id), None)

            self._save_state()
            return dict(rec), f"Approved user_id={user_id}."

    def _deny_pairing_code(self, *, code: str) -> str:
        c = str(code or "").strip().upper()
        if not c:
            return "Usage: /pair deny <code>"
        with self._lock:
            access = self._access_state()
            self._cleanup_expired_pairings_locked(access)
            reqs = access.get("pairing_requests")
            if not isinstance(reqs, dict):
                return "No pending pairing requests."
            rec = reqs.pop(c, None)
            if not isinstance(rec, dict):
                return "Unknown or expired pairing code."
            uid = rec.get("user_id")
            if isinstance(uid, int) and not isinstance(uid, bool):
                u2c = access.get("pairing_user_to_code")
                if isinstance(u2c, dict):
                    u2c.pop(str(int(uid)), None)
            self._save_state()
        return "Pairing request denied."

    def _pairing_list(self) -> list[Dict[str, Any]]:
        with self._lock:
            access = self._access_state()
            self._cleanup_expired_pairings_locked(access)
            reqs = access.get("pairing_requests")
            if not isinstance(reqs, dict):
                return []
            rows: list[Dict[str, Any]] = []
            for code, rec in reqs.items():
                if not isinstance(code, str) or not isinstance(rec, dict):
                    continue
                uid = rec.get("user_id")
                cid = rec.get("chat_id")
                exp = rec.get("expires_at_s")
                if not isinstance(uid, int) or isinstance(uid, bool):
                    continue
                if not isinstance(cid, int) or isinstance(cid, bool):
                    continue
                expires_at = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else 0.0
                rows.append({"code": code, "user_id": int(uid), "chat_id": int(cid), "expires_at_s": expires_at})
            rows.sort(key=lambda r: float(r.get("expires_at_s") or 0.0))
            return rows

    def _my_username(self) -> Optional[str]:
        if self._cfg.transport == "bot_api":
            return self._bot_username_cached()
        return self._tdlib_username_cached()

    def _bot_username_cached(self) -> Optional[str]:
        if isinstance(self._bot_username, str) and self._bot_username.strip():
            return self._bot_username.strip()
        url = self._bot_api("getMe")
        if not url:
            return None
        try:
            j = self._http_get_json(url, timeout_s=10.0)
        except Exception:
            return None
        if not isinstance(j, dict) or j.get("ok") is not True:
            return None
        res = j.get("result")
        if not isinstance(res, dict):
            return None
        u = res.get("username")
        if isinstance(u, str) and u.strip():
            self._bot_username = u.strip()
            return self._bot_username
        return None

    def _tdlib_username_cached(self) -> Optional[str]:
        if isinstance(self._tdlib_username, str) and self._tdlib_username.strip():
            return self._tdlib_username.strip()
        try:
            client = get_global_tdlib_client(start=True)
        except Exception:
            return None
        try:
            me = client.request({"@type": "getMe"}, timeout_s=10.0)
        except Exception:
            return None
        if not isinstance(me, dict) or me.get("@type") == "error":
            return None
        u = me.get("username")
        if isinstance(u, str) and u.strip():
            self._tdlib_username = u.strip()
            return self._tdlib_username
        usernames = me.get("usernames")
        if isinstance(usernames, dict):
            active = usernames.get("active_usernames")
            if isinstance(active, list):
                for x in active:
                    if isinstance(x, str) and x.strip():
                        self._tdlib_username = x.strip()
                        return self._tdlib_username
        return None

    def _text_mentions_me(self, text: str) -> bool:
        uname = self._my_username()
        if not isinstance(uname, str) or not uname.strip():
            return False
        t = str(text or "")
        if not t:
            return False
        pat = r"(?i)(?:^|\s)@" + re.escape(uname.strip()) + r"(?:\b|$)"
        try:
            return re.search(pat, t) is not None
        except Exception:
            return ("@" + uname.strip().lower()) in t.lower()

    def _bot_chat_kind(self, *, chat_type: str) -> str:
        t = str(chat_type or "").strip().lower()
        if t == "private":
            return "dm"
        if t in {"group", "supergroup"}:
            return "group"
        if t == "channel":
            return "channel"
        return "group"

    def _tdlib_chat_kind(self, *, chat_id: int) -> str:
        cid = int(chat_id)
        now = time.time()
        cached = self._tdlib_chat_kind_cache.get(cid)
        if isinstance(cached, tuple) and len(cached) == 2:
            kind0, until0 = cached
            if isinstance(kind0, str) and isinstance(until0, (int, float)) and float(until0) > now:
                return str(kind0)

        kind = "group"
        try:
            client = get_global_tdlib_client(start=True)
            chat = client.request({"@type": "getChat", "chat_id": int(cid)}, timeout_s=10.0)
        except Exception:
            return kind

        t = chat.get("type") if isinstance(chat, dict) else None
        if isinstance(t, dict):
            tt = str(t.get("@type") or "")
            if tt in {"chatTypePrivate", "chatTypeSecret"}:
                kind = "dm"
            elif tt == "chatTypeBasicGroup":
                kind = "group"
            elif tt == "chatTypeSupergroup":
                kind = "channel" if bool(t.get("is_channel")) else "group"

        # Cache for a while to avoid repeated getChat calls.
        self._tdlib_chat_kind_cache[int(cid)] = (str(kind), float(now + 600.0))
        return kind

    def _log_unauthorized(self, *, chat_id: int, from_user_id: Optional[int], reason: str) -> None:
        import logging

        key = f"{chat_id}:{from_user_id}:{reason}"
        now = time.time()
        until = self._unauthorized_log_until.get(key, 0.0)
        if isinstance(until, (int, float)) and float(until) > now:
            return
        self._unauthorized_log_until[key] = float(now + 30.0)
        logging.getLogger(__name__).warning(
            "Telegram bridge: ignoring unauthorized message (reason=%s) user_id=%s chat_id=%s",
            str(reason or ""),
            from_user_id,
            chat_id,
        )

    def _is_authorized_incoming(
        self,
        *,
        chat_id: int,
        chat_kind: str,
        from_user_id: Optional[int],
        text: str,
    ) -> bool:
        """Return True when this inbound message should be processed by the bridge."""
        kind = str(chat_kind or "").strip().lower() or "group"
        uid = int(from_user_id) if isinstance(from_user_id, int) and not isinstance(from_user_id, bool) else None

        if self._is_admin_user(uid):
            return True

        allowed_users = self._effective_allowed_users()
        allowed_chats = self._effective_allowed_chats()

        if kind == "dm":
            pol = str(self._cfg.dm_policy or "pairing").strip().lower()
            if pol == "disabled":
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="dm_disabled")
                return False
            if pol == "open":
                return True
            if (uid is not None and uid in allowed_users) or (int(chat_id) in allowed_chats):
                return True
            if pol == "pairing":
                if uid is None:
                    self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pairing_missing_user_id")
                    return False
                code, _rec, is_new = self._ensure_pairing_request(user_id=uid, chat_id=int(chat_id))
                if bool(is_new):
                    self._send_text(chat_id=int(chat_id), text=self._format_pairing_request_message(code=code))
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pairing_requested")
                return False
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="dm_allowlist")
            return False

        # Groups/channels
        pol = str(self._cfg.group_policy or "allowlist").strip().lower()
        if pol == "disabled":
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="group_disabled")
            return False

        if pol == "allowlist":
            if int(chat_id) not in allowed_chats:
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="group_chat_allowlist")
                return False

        # Mention requirement: skip only for commands (including /reset, /tools, /pair, /whoami).
        if bool(self._cfg.require_mention_in_groups):
            if not str(text or "").lstrip().startswith("/"):
                if not self._text_mentions_me(str(text or "")):
                    return False

        allowed_senders = self._effective_group_allowed_users()
        if allowed_senders and (uid is None or uid not in allowed_senders):
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="group_sender_allowlist")
            return False

        return True

    def _handle_whoami_command(self, *, chat_id: int, chat_kind: str, from_user_id: Optional[int]) -> None:
        uid = int(from_user_id) if isinstance(from_user_id, int) and not isinstance(from_user_id, bool) else None
        lines = [
            f"transport: {self._cfg.transport}",
            f"chat_id: {int(chat_id)}",
            f"chat_kind: {str(chat_kind or '') or 'unknown'}",
            f"user_id: {uid if uid is not None else 'unknown'}",
        ]
        self._send_text(chat_id=int(chat_id), text="\n".join(lines))

    def _handle_pair_command(self, *, chat_id: int, from_user_id: Optional[int], text: str, chat_kind: str) -> None:
        uid = int(from_user_id) if isinstance(from_user_id, int) and not isinstance(from_user_id, bool) else None
        args = _command_args(text)
        parts = [p for p in str(args or "").split() if p.strip()]
        sub = parts[0].strip().lower() if parts else ""

        # Admin subcommands
        if sub in {"approve", "allow", "grant"}:
            if not self._is_admin_user(uid):
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pair_admin_only")
                return
            code = parts[1].strip() if len(parts) >= 2 else ""
            rec, msg = self._approve_pairing_code(code=code)
            self._send_text(chat_id=int(chat_id), text=str(msg))
            if isinstance(rec, dict):
                # Notify the paired user in the original chat.
                req_chat_id = rec.get("chat_id")
                if isinstance(req_chat_id, int) and not isinstance(req_chat_id, bool):
                    self._send_text(chat_id=int(req_chat_id), text="✅ Approved. You can now send messages here.")
            return

        if sub in {"deny", "reject", "cancel"}:
            if not self._is_admin_user(uid):
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pair_admin_only")
                return
            code = parts[1].strip() if len(parts) >= 2 else ""
            msg = self._deny_pairing_code(code=code)
            self._send_text(chat_id=int(chat_id), text=str(msg))
            return

        if sub in {"list", "ls"}:
            if not self._is_admin_user(uid):
                self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pair_admin_only")
                return
            rows = self._pairing_list()
            if not rows:
                self._send_text(chat_id=int(chat_id), text="No pending pairing requests.")
                return
            lines = ["Pending pairing requests:"]
            for r in rows[:20]:
                code = str(r.get("code") or "")
                user_id = r.get("user_id")
                lines.append(f"- {code} (user_id={user_id})")
            if len(rows) > 20:
                lines.append(f"…and {len(rows) - 20} more")
            self._send_text(chat_id=int(chat_id), text="\n".join(lines))
            return

        # Pairing request (DM-only)
        if str(chat_kind or "").lower() != "dm":
            # In groups, pairing requests are silently ignored (avoid spam).
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pair_dm_only")
            return
        if str(self._cfg.dm_policy or "").strip().lower() != "pairing" and not self._is_admin_user(uid):
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pairing_disabled")
            return
        if uid is None:
            self._log_unauthorized(chat_id=chat_id, from_user_id=uid, reason="pairing_missing_user_id")
            return
        code, _rec, _is_new = self._ensure_pairing_request(user_id=uid, chat_id=int(chat_id))
        self._send_text(chat_id=int(chat_id), text=self._format_pairing_request_message(code=code))

    # ---------------------------------------------------------------------
    # Tool approvals (Telegram UX)
    # ---------------------------------------------------------------------

    def _tool_policies(self) -> Dict[str, Any]:
        pol = self._state.get("tool_policies")
        if not isinstance(pol, dict):
            pol = {}
            self._state["tool_policies"] = pol
        return pol

    def _default_tool_policy(self) -> Dict[str, Any]:
        """Default per-chat tool policy (operator-controlled via env)."""
        auto: set[str] = set()
        require: set[str] = set()
        try:
            from abstractruntime.integrations.abstractcore.tool_executor import ToolApprovalPolicy as _ToolApprovalPolicy

            base = _ToolApprovalPolicy()
            auto = set(getattr(base, "auto_approve_tools", set()) or set())
            require = set(getattr(base, "require_approval_tools", set()) or set())
        except Exception:
            auto = set()
            require = {"write_file", "edit_file", "execute_command"}

        if isinstance(self._cfg.tool_auto_approve_tools, list):
            auto = {str(t).strip() for t in self._cfg.tool_auto_approve_tools if isinstance(t, str) and str(t).strip()}
        if isinstance(self._cfg.tool_require_approval_tools, list):
            require = {str(t).strip() for t in self._cfg.tool_require_approval_tools if isinstance(t, str) and str(t).strip()}

        allowed: Optional[set[str]] = None
        if isinstance(self._cfg.tool_allowed_tools, list):
            allowed = {str(t).strip() for t in self._cfg.tool_allowed_tools if isinstance(t, str) and str(t).strip()}
            if not allowed:
                allowed = None

        blocked: set[str] = set()
        if isinstance(self._cfg.tool_blocked_tools, list):
            blocked = {str(t).strip() for t in self._cfg.tool_blocked_tools if isinstance(t, str) and str(t).strip()}

        # Always allow + auto-approve the delivery tools so the bridge can keep responding.
        if allowed is not None:
            allowed |= set(_TELEGRAM_PROTECTED_TOOLS)
        blocked -= set(_TELEGRAM_PROTECTED_TOOLS)
        auto |= set(_TELEGRAM_PROTECTED_TOOLS)
        require -= set(_TELEGRAM_PROTECTED_TOOLS)

        return {
            "version": 1,
            "approve_all": bool(self._cfg.tool_approve_all),
            "allowed_tools": sorted(allowed) if isinstance(allowed, set) else None,
            "blocked_tools": sorted(blocked),
            "auto_approve_tools": sorted(auto),
            "require_approval_tools": sorted(require),
        }

    def _effective_tool_policy(self, *, chat_id: int) -> Dict[str, Any]:
        default = self._default_tool_policy()
        raw: Any = None
        with self._lock:
            raw = dict(self._tool_policies().get(str(chat_id)) or {}) if isinstance(self._tool_policies().get(str(chat_id)), dict) else None

        if not isinstance(raw, dict):
            return dict(default)

        merged = dict(default)
        # Shallow merge is enough; all leaves are JSON primitives / lists.
        merged.update(raw)

        approve_all = _as_bool(merged.get("approve_all"), default=bool(default.get("approve_all")))

        allowed_tools_raw = merged.get("allowed_tools")
        allowed: Optional[set[str]] = None
        if allowed_tools_raw is None:
            allowed = None
        elif isinstance(allowed_tools_raw, list):
            allowed = {str(t).strip() for t in allowed_tools_raw if isinstance(t, str) and str(t).strip()}
            if not allowed:
                allowed = None

        blocked_raw = merged.get("blocked_tools")
        blocked = {str(t).strip() for t in blocked_raw if isinstance(t, str) and str(t).strip()} if isinstance(blocked_raw, list) else set()

        auto_raw = merged.get("auto_approve_tools")
        auto = {str(t).strip() for t in auto_raw if isinstance(t, str) and str(t).strip()} if isinstance(auto_raw, list) else set()

        req_raw = merged.get("require_approval_tools")
        require = {str(t).strip() for t in req_raw if isinstance(t, str) and str(t).strip()} if isinstance(req_raw, list) else set()

        if allowed is not None:
            allowed |= set(_TELEGRAM_PROTECTED_TOOLS)
        blocked -= set(_TELEGRAM_PROTECTED_TOOLS)
        auto |= set(_TELEGRAM_PROTECTED_TOOLS)
        require -= set(_TELEGRAM_PROTECTED_TOOLS)

        return {
            "version": 1,
            "approve_all": bool(approve_all),
            "allowed_tools": sorted(allowed) if isinstance(allowed, set) else None,
            "blocked_tools": sorted(blocked),
            "auto_approve_tools": sorted(auto),
            "require_approval_tools": sorted(require),
        }

    def _save_tool_policy(self, *, chat_id: int, policy: Dict[str, Any]) -> None:
        snap = dict(policy or {})
        with self._lock:
            self._tool_policies()[str(chat_id)] = snap
            self._save_state()

    def _clear_tool_policy(self, *, chat_id: int) -> None:
        with self._lock:
            self._tool_policies().pop(str(chat_id), None)
            self._save_state()

    def _tool_names_available(self) -> list[str]:
        """Best-effort list of tool names (for `/tools list`)."""
        names: set[str] = set()
        try:
            from abstractruntime.integrations.abstractcore.default_tools import build_default_tool_map

            names |= {str(k).strip() for k in (build_default_tool_map() or {}).keys() if isinstance(k, str) and str(k).strip()}
        except Exception:
            pass
        # Runtime-owned tool (executed inside the runtime effect handler).
        names.add("open_attachment")
        names |= set(_TELEGRAM_PROTECTED_TOOLS)
        return sorted(n for n in names if n)

    def _approval_prompts(self) -> Dict[str, Any]:
        prompts = self._state.get("approval_prompts")
        if not isinstance(prompts, dict):
            prompts = {}
            self._state["approval_prompts"] = prompts
        return prompts

    def _format_tool_approval_prompt(self, *, tool_calls: list[Dict[str, Any]]) -> str:
        def _compact_json(v: Any, *, max_chars: int) -> str:
            try:
                s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                s = str(v)
            if len(s) <= max_chars:
                return s
            return s[: max(0, max_chars - 16)] + "…(truncated)…"

        lines: list[str] = [
            "Tool approval required to continue.",
            "",
            "Requested tool calls:",
        ]
        for i, tc in enumerate(tool_calls[:8], start=1):
            name = str(tc.get("name") or "").strip() or "unknown"
            args = tc.get("arguments")
            args_obj = dict(args) if isinstance(args, dict) else args
            lines.append(f"{i}) {name} { _compact_json(args_obj, max_chars=800) }")
        if len(tool_calls) > 8:
            lines.append(f"…and {len(tool_calls) - 8} more")
        lines.extend(
            [
                "",
                "Reply with /approve (or 'approve') to run.",
                "Reply with anything else to cancel.",
                "Send /tools to view or change tool permissions.",
            ]
        )
        # Telegram message hard cap ~4096 bytes; keep margin.
        text = "\n".join(lines).strip()
        return text[:3800] + "…" if len(text) > 3800 else text

    def _format_tools_policy_message(self, *, chat_id: int) -> str:
        pol = self._effective_tool_policy(chat_id=chat_id)
        allowed = pol.get("allowed_tools")
        allowed_s = "all" if allowed is None else ", ".join(list(allowed)[:40]) + ("…" if isinstance(allowed, list) and len(allowed) > 40 else "")
        blocked = pol.get("blocked_tools") or []
        auto = pol.get("auto_approve_tools") or []
        req = pol.get("require_approval_tools") or []
        approve_all = bool(pol.get("approve_all"))
        tool_mode = str(os.getenv("ABSTRACTGATEWAY_TOOL_MODE") or "passthrough").strip().lower() or "passthrough"

        def _fmt_list(items: Any, *, max_items: int) -> str:
            if not isinstance(items, list) or not items:
                return "(none)"
            s = ", ".join(items[:max_items])
            return s + ("…" if len(items) > max_items else "")

        lines = [
            "Tool permissions (this chat):",
            f"- gateway_tool_mode: {tool_mode}",
            f"- approve_all: {str(approve_all).lower()}",
            f"- allowed: {allowed_s or '(none)'}",
            f"- blocked: {_fmt_list(blocked, max_items=24)}",
            f"- auto-run: {_fmt_list(auto, max_items=24)}",
            f"- require approval: {_fmt_list(req, max_items=24)}",
        ]
        if tool_mode in {"local", "local_all", "local-all"}:
            lines.append("")
            lines.append("Note: approvals are bypassed when gateway_tool_mode is `local` (dangerous).")
            lines.append("Set `ABSTRACTGATEWAY_TOOL_MODE=passthrough` (recommended) or `approval`.")
        lines.extend(
            [
            "",
            "Commands:",
            "- /tools safe | open | strict | reset",
            "- /tools approve_all on|off",
            "- /tools allow all | <tool...>",
            "- /tools block <tool...> | /tools unblock <tool...>",
            "- /tools auto <tool...> | /tools ask <tool...>",
            "- /tools list",
            ]
        )
        text = "\n".join(lines).strip()
        return text[:3800] + "…" if len(text) > 3800 else text

    def _handle_tools_command(self, *, chat_id: int, from_user_id: Optional[int], text: str) -> bool:
        """Handle `/tools ...` commands. Returns True when the message was consumed."""
        base = _command_base(text)
        if base not in {"/tools", "/tool"}:
            return False

        args = _command_args(text)
        sub = args.split(maxsplit=1)[0].strip().lower() if args else ""
        rest = args.split(maxsplit=1)[1].strip() if args and " " in args else ""

        # Owner-only mutations (best-effort): allow viewing for anyone.
        binding = self._binding_for_chat(chat_id)
        owner = binding.get("owner_user_id") if isinstance(binding, dict) else None
        is_owner = True
        if isinstance(owner, int) and from_user_id is not None and int(from_user_id) != int(owner):
            is_owner = False

        if not sub or sub in {"help", "?"}:
            self._send_text(chat_id=int(chat_id), text=self._format_tools_policy_message(chat_id=int(chat_id)))
            return True

        if sub == "list":
            names = self._tool_names_available()
            msg = "Available tools:\n" + "\n".join([f"- {n}" for n in names][:200])
            self._send_text(chat_id=int(chat_id), text=msg[:3800] + "…" if len(msg) > 3800 else msg)
            return True

        if sub == "reset":
            if not is_owner:
                self._send_text(chat_id=int(chat_id), text="Only the chat owner can change tool permissions.")
                return True
            self._clear_tool_policy(chat_id=int(chat_id))
            self._send_text(chat_id=int(chat_id), text="Tool permissions reset to defaults.")
            return True

        if sub in {"safe", "open", "strict"}:
            if not is_owner:
                self._send_text(chat_id=int(chat_id), text="Only the chat owner can change tool permissions.")
                return True

            default = self._default_tool_policy()
            if sub == "safe":
                pol = dict(default)
            elif sub == "open":
                pol = dict(default)
                pol["approve_all"] = True
                pol["allowed_tools"] = None
                pol["blocked_tools"] = []
            else:  # strict
                pol = dict(default)
                pol["approve_all"] = False
                allow = set(pol.get("auto_approve_tools") or [])
                allow |= set(_TELEGRAM_PROTECTED_TOOLS)
                pol["allowed_tools"] = sorted(allow)
                pol["blocked_tools"] = []
                pol["require_approval_tools"] = []
                pol["auto_approve_tools"] = sorted(allow)

            self._save_tool_policy(chat_id=int(chat_id), policy=pol)
            self._send_text(chat_id=int(chat_id), text=self._format_tools_policy_message(chat_id=int(chat_id)))
            return True

        if sub in {"approve_all", "approve-all"}:
            if not is_owner:
                self._send_text(chat_id=int(chat_id), text="Only the chat owner can change tool permissions.")
                return True
            token = str(rest or "").strip().lower()
            val = True if token in {"1", "true", "yes", "on"} else False if token in {"0", "false", "no", "off"} else None
            if val is None:
                self._send_text(chat_id=int(chat_id), text="Usage: /tools approve_all on|off")
                return True
            pol = self._effective_tool_policy(chat_id=int(chat_id))
            pol["approve_all"] = bool(val)
            self._save_tool_policy(chat_id=int(chat_id), policy=pol)
            self._send_text(chat_id=int(chat_id), text=self._format_tools_policy_message(chat_id=int(chat_id)))
            return True

        def _parse_tool_names(raw: str) -> list[str]:
            if not raw:
                return []
            parts = re.split(r"[\\s,]+", raw.strip())
            return [p for p in parts if p]

        if sub in {"allow", "block", "unblock", "auto", "ask", "require"}:
            if not is_owner:
                self._send_text(chat_id=int(chat_id), text="Only the chat owner can change tool permissions.")
                return True

            pol = self._effective_tool_policy(chat_id=int(chat_id))
            names = _parse_tool_names(rest)
            if not names:
                self._send_text(chat_id=int(chat_id), text=f"Usage: /tools {sub} <tool...>")
                return True

            allowed_tools = pol.get("allowed_tools")
            allowed_set: Optional[set[str]] = None
            if allowed_tools is None:
                allowed_set = None
            elif isinstance(allowed_tools, list):
                allowed_set = {str(t).strip() for t in allowed_tools if isinstance(t, str) and str(t).strip()}
                if not allowed_set:
                    allowed_set = None

            blocked_set = {str(t).strip() for t in (pol.get("blocked_tools") or []) if isinstance(t, str) and str(t).strip()}
            auto_set = {str(t).strip() for t in (pol.get("auto_approve_tools") or []) if isinstance(t, str) and str(t).strip()}
            req_set = {str(t).strip() for t in (pol.get("require_approval_tools") or []) if isinstance(t, str) and str(t).strip()}

            if sub == "allow":
                if any(n in {"*", "all"} for n in names):
                    allowed_set = None
                else:
                    allowed_set = set(names)
            elif sub == "block":
                blocked_set |= set(names)
            elif sub == "unblock":
                blocked_set -= set(names)
            elif sub == "auto":
                auto_set |= set(names)
                req_set -= set(names)
            else:  # ask/require
                req_set |= set(names)
                auto_set -= set(names)

            if allowed_set is not None:
                allowed_set |= set(_TELEGRAM_PROTECTED_TOOLS)
            blocked_set -= set(_TELEGRAM_PROTECTED_TOOLS)
            auto_set |= set(_TELEGRAM_PROTECTED_TOOLS)
            req_set -= set(_TELEGRAM_PROTECTED_TOOLS)

            pol["allowed_tools"] = sorted(allowed_set) if isinstance(allowed_set, set) else None
            pol["blocked_tools"] = sorted(blocked_set)
            pol["auto_approve_tools"] = sorted(auto_set)
            pol["require_approval_tools"] = sorted(req_set)
            pol["approve_all"] = bool(pol.get("approve_all"))
            self._save_tool_policy(chat_id=int(chat_id), policy=pol)
            self._send_text(chat_id=int(chat_id), text=self._format_tools_policy_message(chat_id=int(chat_id)))
            return True

        self._send_text(chat_id=int(chat_id), text="Unknown /tools command. Send /tools for help.")
        return True


    def _send_text(self, *, chat_id: int, text: str) -> None:
        """Best-effort: send a simple text message using the active transport."""
        msg = str(text or "").strip()
        if not msg:
            return
        if self._cfg.transport == "bot_api":
            self._bot_send_text(chat_id=chat_id, text=msg)
            return
        try:
            client = get_global_tdlib_client(start=True)
            client.send(
                {
                    "@type": "sendMessage",
                    "chat_id": int(chat_id),
                    "input_message_content": {
                        "@type": "inputMessageText",
                        "text": {"@type": "formattedText", "text": msg},
                        "disable_web_page_preview": True,
                    },
                }
            )
        except Exception:
            pass

    def _find_pending_tool_approval(self, *, session_id: str) -> Optional[Dict[str, Any]]:
        """Return {run_id, wait_key, tool_calls} for the newest pending tool approval in a session."""
        sid = str(session_id or "").strip()
        if not sid:
            return None

        try:
            runtime = Runtime(
                run_store=self._runner.run_store,
                ledger_store=self._runner.ledger_store,
                artifact_store=self._runner.artifact_store,
            )
        except Exception:
            return None

        store = getattr(runtime, "run_store", None)
        load_run = getattr(store, "load", None)
        if not callable(load_run):
            return None

        candidate_ids: list[str] = []
        list_index = getattr(store, "list_run_index", None)
        if callable(list_index):
            try:
                rows = list_index(status=RunStatus.WAITING, session_id=sid, limit=200)
            except TypeError:
                rows = list_index(status=RunStatus.WAITING, limit=500)
                if isinstance(rows, list):
                    rows = [r for r in rows if isinstance(r, dict) and str(r.get("session_id") or "").strip() == sid]
            except Exception:
                rows = []
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, dict):
                        rid = r.get("run_id")
                        if isinstance(rid, str) and rid.strip():
                            candidate_ids.append(rid.strip())

        if not candidate_ids:
            list_runs = getattr(store, "list_runs", None)
            if not callable(list_runs):
                return None
            try:
                runs = list_runs(session_id=sid, limit=2000)
            except TypeError:
                runs = list_runs(limit=5000)
                runs = [r for r in runs if str(getattr(r, "session_id", "") or "").strip() == sid]
            except Exception:
                runs = []
            for r in runs or []:
                if getattr(r, "status", None) != RunStatus.WAITING:
                    continue
                rid = getattr(r, "run_id", None)
                if isinstance(rid, str) and rid.strip():
                    candidate_ids.append(rid.strip())

        seen: set[str] = set()
        for rid in candidate_ids:
            if rid in seen:
                continue
            seen.add(rid)
            try:
                run = load_run(str(rid))
            except Exception:
                run = None
            if run is None:
                continue
            if getattr(run, "status", None) != RunStatus.WAITING:
                continue
            waiting = getattr(run, "waiting", None)
            if waiting is None:
                continue
            details = getattr(waiting, "details", None)
            if not isinstance(details, dict):
                continue
            mode = str(details.get("mode") or "").strip().lower()
            if mode != "approval_required":
                continue
            tool_calls = details.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                continue
            wait_key = getattr(waiting, "wait_key", None)
            wait_key_s = str(wait_key or "").strip()
            if not wait_key_s:
                continue
            return {
                "run_id": str(getattr(run, "run_id", "") or ""),
                "wait_key": wait_key_s,
                "tool_calls": list(tool_calls),
                "details": dict(details),
            }

        return None

    def _resume_tool_wait(self, *, run_id: str, wait_key: str, payload: Dict[str, Any]) -> None:
        rid = str(run_id or "").strip()
        wk = str(wait_key or "").strip()
        if not rid or not wk:
            return
        try:
            rt, wf = self._host.runtime_and_workflow_for_run(rid)
            rt.resume(workflow=wf, run_id=rid, wait_key=wk, payload=dict(payload), max_steps=0)
        except Exception:
            return
        with self._lock:
            self._approval_prompts().pop(wk, None)
            self._save_state()

    def _tool_allowed_by_policy(self, *, tool_name: str, policy: Dict[str, Any]) -> bool:
        name = str(tool_name or "").strip()
        if not name:
            return False
        if name in _TELEGRAM_PROTECTED_TOOLS:
            return True
        blocked = policy.get("blocked_tools")
        if isinstance(blocked, list) and name in blocked:
            return False
        allowed = policy.get("allowed_tools")
        if isinstance(allowed, list) and allowed and name not in allowed:
            return False
        return True

    def _execute_tool_calls_with_policy(
        self,
        *,
        tool_calls: list[Dict[str, Any]],
        policy: Dict[str, Any],
        approved: bool,
        reject_error: str,
    ) -> Dict[str, Any]:
        results_by_index: Dict[int, Dict[str, Any]] = {}

        def _err(tc: Dict[str, Any], message: str) -> Dict[str, Any]:
            call_id = str(tc.get("call_id") or "")
            runtime_call_id = tc.get("runtime_call_id")
            name = str(tc.get("name") or "")
            return {
                "call_id": call_id,
                "runtime_call_id": runtime_call_id,
                "name": name,
                "success": False,
                "output": None,
                "error": str(message or "Tool call failed"),
            }

        calls_to_exec: list[Dict[str, Any]] = []
        exec_indexes: list[int] = []

        for idx, tc in enumerate(list(tool_calls or [])):
            if not isinstance(tc, dict):
                results_by_index[idx] = {
                    "call_id": "",
                    "runtime_call_id": None,
                    "name": "",
                    "success": False,
                    "output": None,
                    "error": "Invalid tool call (expected an object)",
                }
                continue

            name = str(tc.get("name") or "").strip()
            if not approved:
                results_by_index[idx] = _err(tc, reject_error)
                continue

            if not name:
                results_by_index[idx] = _err(tc, "Tool call missing a valid name")
                continue

            if not self._tool_allowed_by_policy(tool_name=name, policy=policy):
                results_by_index[idx] = _err(tc, f"Tool '{name}' is not allowed in this chat (send /tools to change).")
                continue

            calls_to_exec.append(dict(tc))
            exec_indexes.append(int(idx))

        if approved and calls_to_exec:
            try:
                from abstractruntime.integrations.abstractcore.default_tools import build_default_tool_map
                from abstractruntime.integrations.abstractcore.tool_executor import MappingToolExecutor

                exec2 = MappingToolExecutor(build_default_tool_map())
                out = exec2.execute(tool_calls=calls_to_exec)
                exec_results = out.get("results") if isinstance(out, dict) else None
                if isinstance(exec_results, list):
                    for i2, r in enumerate(exec_results):
                        idx = exec_indexes[i2] if i2 < len(exec_indexes) else None
                        if idx is None:
                            continue
                        if isinstance(r, dict):
                            results_by_index[int(idx)] = dict(r)
                        else:
                            results_by_index[int(idx)] = _err(calls_to_exec[i2], "Tool executor returned invalid result")
                else:
                    for idx, tc in zip(exec_indexes, calls_to_exec):
                        results_by_index[int(idx)] = _err(tc, "Tool executor returned invalid results")
            except Exception as e:
                for idx, tc in zip(exec_indexes, calls_to_exec):
                    results_by_index[int(idx)] = _err(tc, f"Tool execution failed: {e}")

        merged: list[Dict[str, Any]] = []
        for idx in range(len(tool_calls or [])):
            r = results_by_index.get(idx)
            if r is None:
                r = {
                    "call_id": "",
                    "runtime_call_id": None,
                    "name": "",
                    "success": False,
                    "output": None,
                    "error": "Missing tool result",
                }
            merged.append(r)

        return {"mode": "executed", "results": merged}

    def _merge_tool_wait_results(self, *, pending: Dict[str, Any], host_payload: Dict[str, Any]) -> Dict[str, Any]:
        details = pending.get("details")
        if not isinstance(details, dict):
            return dict(host_payload)

        host_results = host_payload.get("results") if isinstance(host_payload, dict) else None
        if not isinstance(host_results, list):
            host_results = []

        evidence = details.get("tool_calls_for_evidence")
        if not isinstance(evidence, list) or not evidence:
            # No mapping information: best-effort fall back to host results.
            return {"mode": "executed", "results": list(host_results)}

        # Determine the expected result length (original tool call count).
        final_len: Optional[int] = None
        raw_count = details.get("original_call_count")
        if raw_count is not None and not isinstance(raw_count, bool):
            try:
                final_len = int(raw_count)
            except Exception:
                final_len = None
        if final_len is None or final_len <= 0:
            final_len = len(evidence)

        results_by_index: Dict[int, Dict[str, Any]] = {}

        def _int_key_map(obj: Any) -> Dict[int, Dict[str, Any]]:
            out: Dict[int, Dict[str, Any]] = {}
            if not isinstance(obj, dict):
                return out
            for k, v in obj.items():
                try:
                    idx = int(k)
                except Exception:
                    continue
                if isinstance(v, dict):
                    out[idx] = dict(v)
            return out

        results_by_index.update(_int_key_map(details.get("blocked_by_index")))
        results_by_index.update(_int_key_map(details.get("pre_results_by_index")))

        # Build mapping (call_id/runtime_call_id -> original index).
        call_id_to_idx: Dict[str, int] = {}
        runtime_call_id_to_idx: Dict[str, int] = {}
        for idx, tc in enumerate(evidence):
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("call_id") or "")
            if cid:
                call_id_to_idx.setdefault(cid, int(idx))
            rcid = tc.get("runtime_call_id")
            if rcid is not None:
                rcid_s = str(rcid).strip()
                if rcid_s:
                    runtime_call_id_to_idx.setdefault(rcid_s, int(idx))

        extras: list[Dict[str, Any]] = []
        for r in host_results:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("call_id") or "")
            rcid = r.get("runtime_call_id")
            rcid_s = str(rcid).strip() if rcid is not None else ""
            idx = call_id_to_idx.get(cid) if cid else None
            if idx is None and rcid_s:
                idx = runtime_call_id_to_idx.get(rcid_s)
            if idx is None:
                extras.append(dict(r))
                continue
            if idx in results_by_index:
                continue
            results_by_index[int(idx)] = dict(r)

        merged: list[Dict[str, Any]] = []
        for idx in range(int(final_len)):
            fixed = results_by_index.get(idx)
            if fixed is not None:
                merged.append(fixed)
                continue
            if extras:
                merged.append(extras.pop(0))
                continue
            merged.append(
                {
                    "call_id": "",
                    "runtime_call_id": None,
                    "name": "",
                    "success": False,
                    "output": None,
                    "error": "Missing tool result",
                }
            )

        return {"mode": "executed", "results": merged}

    def _maybe_handle_tool_wait(self, *, chat_id: int, session_id: str) -> bool:
        """Return True when waiting on user approval (prompted)."""
        pending = self._find_pending_tool_approval(session_id=session_id)
        if pending is None:
            return False
        wait_key = str(pending.get("wait_key") or "").strip()
        run_id = str(pending.get("run_id") or "").strip()
        tool_calls_any = pending.get("tool_calls")
        tool_calls: list[Dict[str, Any]] = [dict(x) for x in tool_calls_any if isinstance(x, dict)] if isinstance(tool_calls_any, list) else []
        if not wait_key or not run_id:
            return False

        policy = self._effective_tool_policy(chat_id=int(chat_id))
        approve_all = bool(policy.get("approve_all"))

        allowed_calls: list[Dict[str, Any]] = []
        for tc in tool_calls:
            name = str(tc.get("name") or "").strip()
            if name and self._tool_allowed_by_policy(tool_name=name, policy=policy):
                allowed_calls.append(tc)

        requires_approval = True
        if not allowed_calls:
            requires_approval = False
        elif approve_all:
            requires_approval = False
        else:
            try:
                from abstractruntime.integrations.abstractcore.tool_executor import ToolApprovalPolicy as _ToolApprovalPolicy

                auto = set(policy.get("auto_approve_tools") or [])
                req = set(policy.get("require_approval_tools") or [])
                tap = _ToolApprovalPolicy(auto_approve_tools=auto, require_approval_tools=req)
                requires_approval = bool(tap.requires_approval(allowed_calls))
            except Exception:
                requires_approval = True

        if not requires_approval:
            host_payload = self._execute_tool_calls_with_policy(
                tool_calls=tool_calls,
                policy=policy,
                approved=True,
                reject_error="",
            )
            payload = self._merge_tool_wait_results(pending=pending, host_payload=host_payload)
            self._resume_tool_wait(run_id=run_id, wait_key=wait_key, payload=payload)
            return False

        with self._lock:
            prompts = self._approval_prompts()
            if wait_key not in prompts:
                prompts[wait_key] = {
                    "chat_id": int(chat_id),
                    "session_id": str(session_id),
                    "run_id": str(run_id),
                    "prompted_at": _utc_now_iso(),
                }
                self._save_state()
                self._send_text(chat_id=int(chat_id), text=self._format_tool_approval_prompt(tool_calls=tool_calls))
        return True

    def _handle_tool_approval_response(
        self,
        *,
        chat_id: int,
        session_id: str,
        from_user_id: Optional[int],
        text: str,
    ) -> bool:
        """Handle an approval reply. Returns True when the message was consumed."""
        pending = self._find_pending_tool_approval(session_id=session_id)
        if pending is None:
            return False

        # Best-effort owner check: prevent other chat participants from approving tools.
        binding = self._binding_for_chat(chat_id)
        owner = binding.get("owner_user_id") if isinstance(binding, dict) else None
        if isinstance(owner, int) and from_user_id is not None and int(from_user_id) != int(owner):
            return False

        token = _approval_token(text)
        approve = token == "approve"

        run_id = str(pending.get("run_id") or "").strip()
        wait_key = str(pending.get("wait_key") or "").strip()
        tool_calls_any = pending.get("tool_calls")
        tool_calls: list[Dict[str, Any]] = [dict(x) for x in tool_calls_any if isinstance(x, dict)] if isinstance(tool_calls_any, list) else []

        if not run_id or not wait_key:
            return False

        policy = self._effective_tool_policy(chat_id=int(chat_id))
        if approve:
            host_payload = self._execute_tool_calls_with_policy(
                tool_calls=tool_calls,
                policy=policy,
                approved=True,
                reject_error="",
            )
            payload = self._merge_tool_wait_results(pending=pending, host_payload=host_payload)
            self._resume_tool_wait(run_id=run_id, wait_key=wait_key, payload=payload)
            self._send_text(chat_id=int(chat_id), text="Approved. Continuing…")
            # The typing loop is stopped while waiting for approval. Restart it so we can:
            # - keep the "typing…" indicator alive if the workflow keeps running
            # - detect and auto-run any subsequent auto-approved waits (including delivery tools)
            try:
                if self._cfg.transport == "bot_api":
                    self._start_typing_loop(chat_id, session_id=str(session_id or ""), send_fn=lambda: self._send_bot_typing(chat_id))
                else:
                    self._start_typing_loop(chat_id, session_id=str(session_id or ""), send_fn=lambda: self._send_tdlib_typing(chat_id))
            except Exception:
                pass
            # Best-effort: immediately re-check for a follow-up tool wait (common: send_telegram_message).
            try:
                nxt = self._find_pending_tool_approval(session_id=session_id)
                if isinstance(nxt, dict):
                    wk2 = str(nxt.get("wait_key") or "").strip()
                    if wk2 and wk2 != wait_key:
                        self._maybe_handle_tool_wait(chat_id=int(chat_id), session_id=str(session_id or ""))
            except Exception:
                pass
            return True

        host_payload = self._execute_tool_calls_with_policy(
            tool_calls=tool_calls,
            policy=policy,
            approved=False,
            reject_error="User rejected tool call",
        )
        payload = self._merge_tool_wait_results(pending=pending, host_payload=host_payload)
        self._resume_tool_wait(run_id=run_id, wait_key=wait_key, payload=payload)

        self._send_text(chat_id=int(chat_id), text="Cancelled.")
        try:
            if self._cfg.transport == "bot_api":
                self._start_typing_loop(chat_id, session_id=str(session_id or ""), send_fn=lambda: self._send_bot_typing(chat_id))
            else:
                self._start_typing_loop(chat_id, session_id=str(session_id or ""), send_fn=lambda: self._send_tdlib_typing(chat_id))
        except Exception:
            pass
        return True

    def _stash_pending_media(
        self,
        *,
        binding: Dict[str, Any],
        media: list[Dict[str, Any]],
        message_id: Optional[int] = None,
    ) -> None:
        """Persist pending media so a follow-up text message can reference it."""
        if not media:
            return
        with self._lock:
            binding["pending_media"] = list(media)
            binding["pending_media_at"] = float(time.time())
            if isinstance(message_id, int):
                binding["pending_media_message_id"] = int(message_id)
            self._save_state()

    def _peek_pending_media(self, *, binding: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Return pending media if still fresh (does not consume)."""
        with self._lock:
            raw = binding.get("pending_media")
            ts = binding.get("pending_media_at")
            if not isinstance(raw, list) or not raw:
                return []
            max_age = float(self._cfg.pending_media_max_s or 0.0)
            if isinstance(ts, (int, float)) and max_age > 0:
                if (time.time() - float(ts)) > max_age:
                    return []
            return list(raw)

    def _clear_pending_media(self, *, binding: Dict[str, Any]) -> None:
        with self._lock:
            binding.pop("pending_media", None)
            binding.pop("pending_media_at", None)
            binding.pop("pending_media_message_id", None)
            self._save_state()

    def _consume_pending_media(self, *, binding: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Consume and clear pending media if still fresh; otherwise clear it."""
        media = self._peek_pending_media(binding=binding)
        if not media:
            self._clear_pending_media(binding=binding)
            return []
        self._clear_pending_media(binding=binding)
        return media

    def _pending_media_message_id(self, *, binding: Dict[str, Any]) -> Optional[int]:
        raw = binding.get("pending_media_message_id")
        return int(raw) if isinstance(raw, int) else None

    def _should_apply_pending_media(self, *, text: str) -> bool:
        """Heuristic: only reuse pending media when the text likely refers to it."""
        s = str(text or "").strip()
        if not s:
            return False
        if s.startswith("/"):
            return False
        lowered = s.lower()
        # Token-based match to avoid accidental substring hits.
        tokens = set(re.findall(r"[a-z0-9]+", lowered))
        # Intentionally exclude ambiguous tokens like "it".
        keywords = {
            "this",
            "that",
            "these",
            "those",
            "above",
            "attached",
            "attachment",
            "image",
            "photo",
            "picture",
            "screenshot",
            "video",
            "audio",
            "voice",
            "sound",
            "music",
            "file",
            "document",
            "pdf",
            "sticker",
            "gif",
            "animation",
        }
        return bool(tokens & keywords)

    def _maybe_apply_pending_media(
        self,
        *,
        binding: Dict[str, Any],
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        pending = self._peek_pending_media(binding=binding)
        if not pending:
            self._clear_pending_media(binding=binding)
            return []
        pending_mid = self._pending_media_message_id(binding=binding)
        if isinstance(reply_to_message_id, int) and pending_mid is not None and int(reply_to_message_id) == int(pending_mid):
            return self._consume_pending_media(binding=binding)
        if self._should_apply_pending_media(text=text):
            return self._consume_pending_media(binding=binding)
        # Clear to avoid surprising carry-over to unrelated future messages.
        self._clear_pending_media(binding=binding)
        return []

    def _cancel_session_runs(self, *, session_id: str) -> None:
        """Best-effort: cancel all runs for a Telegram session to avoid duplicate listeners."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            runtime = Runtime(
                run_store=self._runner.run_store,
                ledger_store=self._runner.ledger_store,
                artifact_store=self._runner.artifact_store,
            )
        except Exception:
            return
        list_runs = getattr(runtime.run_store, "list_runs", None)
        if not callable(list_runs):
            return
        try:
            runs = list_runs(session_id=sid, limit=2000)
        except TypeError:
            # Older stores may not support session_id filtering.
            runs = list_runs(limit=2000)
            runs = [r for r in runs if getattr(r, "session_id", None) == sid]
        except Exception:
            runs = []
        for r in runs or []:
            rid = getattr(r, "run_id", None)
            if not isinstance(rid, str) or not rid:
                continue
            try:
                runtime.cancel_run(rid, reason="Telegram session reset")
            except Exception:
                continue

    def _cancel_chat_runs(self, *, chat_id: int) -> None:
        """Best-effort: cancel all runs for a chat, across session revisions."""
        base_prefix = f"{self._cfg.session_prefix}{chat_id}"
        if not base_prefix:
            return
        try:
            runtime = Runtime(
                run_store=self._runner.run_store,
                ledger_store=self._runner.ledger_store,
                artifact_store=self._runner.artifact_store,
            )
        except Exception:
            return

        list_runs = getattr(runtime.run_store, "list_runs", None)
        if not callable(list_runs):
            # Fall back to exact-session cancellation for the common legacy id.
            self._cancel_session_runs(session_id=base_prefix)
            return

        try:
            runs = list_runs(limit=5000)
        except Exception:
            runs = []
        for r in runs or []:
            try:
                sid = str(getattr(r, "session_id", "") or "").strip()
            except Exception:
                continue
            if not sid.startswith(base_prefix):
                continue
            rid = getattr(r, "run_id", None)
            if not isinstance(rid, str) or not rid:
                continue
            try:
                runtime.cancel_run(rid, reason="Telegram session reset")
            except Exception:
                continue

    def _collect_tracked_bot_message_ids(self, *, chat_id: int) -> list[int]:
        """Best-effort: collect sent Telegram message_ids tracked in run vars for this chat.

        Shipped `telegram-agent` workflows store sent message ids in:
          `run.vars._runtime.telegram.sent_message_ids`
        """
        base_prefix = f"{self._cfg.session_prefix}{chat_id}"
        if not base_prefix:
            return []
        try:
            runtime = Runtime(
                run_store=self._runner.run_store,
                ledger_store=self._runner.ledger_store,
                artifact_store=self._runner.artifact_store,
            )
        except Exception:
            return []

        store = getattr(runtime, "run_store", None)
        list_runs = getattr(store, "list_runs", None)
        load = getattr(store, "load", None)
        if not callable(list_runs):
            return []

        try:
            runs = list_runs(limit=5000)
        except Exception:
            runs = []

        out: list[int] = []

        def _collect(v: Any) -> None:
            if isinstance(v, int) and not isinstance(v, bool):
                out.append(int(v))
                return
            if isinstance(v, float) and not isinstance(v, bool):
                # Best-effort: tolerate floats that represent ints.
                try:
                    if float(v).is_integer():
                        out.append(int(v))
                except Exception:
                    pass
                return
            if isinstance(v, (list, tuple)):
                for x in v:
                    _collect(x)
                return

        for r in runs or []:
            try:
                sid = str(getattr(r, "session_id", "") or "").strip()
            except Exception:
                continue
            if not sid.startswith(base_prefix):
                continue

            run_obj = r
            rid = getattr(r, "run_id", None)
            if callable(load) and isinstance(rid, str) and rid:
                try:
                    run_obj = load(str(rid))
                except Exception:
                    run_obj = r

            vars_obj = getattr(run_obj, "vars", None)
            if not isinstance(vars_obj, dict):
                continue
            v = _get_by_path(vars_obj, "_runtime.telegram.sent_message_ids")
            _collect(v)

        return _dedup_ints(out)

    def _binding_for_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        b = self._state.get("bindings")
        if not isinstance(b, dict):
            return None
        return b.get(str(chat_id))

    def _session_rev(self, chat_id: int) -> int:
        revs = self._state.get("session_revs")
        if not isinstance(revs, dict):
            revs = {}
            self._state["session_revs"] = revs
        raw = revs.get(str(chat_id), 0)
        try:
            if raw is None or isinstance(raw, bool):
                return 0
            return max(0, int(raw))
        except Exception:
            return 0

    def _bump_session_rev(self, chat_id: int) -> int:
        with self._lock:
            revs = self._state.get("session_revs")
            if not isinstance(revs, dict):
                revs = {}
                self._state["session_revs"] = revs
            cur = self._session_rev(chat_id)
            nxt = cur + 1
            revs[str(chat_id)] = int(nxt)
            self._save_state()
            return int(nxt)

    def _session_id_for_chat(self, *, chat_id: int, rev: int) -> str:
        base = f"{self._cfg.session_prefix}{chat_id}"
        r = int(rev) if isinstance(rev, int) else 0
        if r > 0:
            return f"{base}:r{r}"
        return base

    def _ensure_binding(self, *, chat_id: int, from_user_id: Optional[int]) -> Optional[Dict[str, Any]]:
        with self._lock:
            existing = self._binding_for_chat(chat_id)
            if isinstance(existing, dict):
                return existing

            rev = self._session_rev(chat_id)
            session_id = self._session_id_for_chat(chat_id=chat_id, rev=rev)
            # Best-effort: cancel stale runs for this session to avoid duplicate listeners.
            self._cancel_session_runs(session_id=session_id)
            try:
                # NOTE: actor_id MUST be "gateway" so the GatewayRunner tick loop
                # picks up these runs. The Telegram origin is recorded in session_id
                # (telegram:<chat_id>), the binding state, and every event payload.
                input_data: Dict[str, Any] = {"telegram": {"chat_id": chat_id, "from_user_id": from_user_id}}
                rt_overrides: Dict[str, Any] = {}
                if isinstance(self._cfg.provider_override, str) and self._cfg.provider_override.strip():
                    rt_overrides["provider"] = self._cfg.provider_override.strip().lower()
                if isinstance(self._cfg.model_override, str) and self._cfg.model_override.strip():
                    rt_overrides["model"] = self._cfg.model_override.strip()
                if rt_overrides:
                    input_data["_runtime"] = rt_overrides
                if isinstance(self._cfg.max_history_messages, int) and self._cfg.max_history_messages > 0:
                    input_data["_limits"] = {"max_history_messages": int(self._cfg.max_history_messages)}
                run_id = self._host.start_run(
                    flow_id=self._cfg.flow_id,
                    bundle_id=self._cfg.bundle_id,
                    input_data=input_data,
                    actor_id="gateway",
                    session_id=session_id,
                )
            except Exception:
                return None

            binding = {
                "chat_id": chat_id,
                "owner_user_id": int(from_user_id) if isinstance(from_user_id, int) and not isinstance(from_user_id, bool) else None,
                "session_id": session_id,
                "session_rev": int(rev),
                "run_id": str(run_id),
                "flow_id": self._cfg.flow_id,
                "bundle_id": self._cfg.bundle_id,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }
            bindings = self._state.setdefault("bindings", {})
            if isinstance(bindings, dict):
                bindings[str(chat_id)] = binding
            self._save_state()
            return binding

    # ---------------------------------------------------------------------
    # Bot API mode (non-E2EE; dev fallback)
    # ---------------------------------------------------------------------

    def _bot_token(self) -> Optional[str]:
        name = str(self._cfg.bot_token_env_var or "").strip() or "ABSTRACT_TELEGRAM_BOT_TOKEN"
        v = os.getenv(name)
        return str(v).strip() if v is not None and str(v).strip() else None

    def _bot_api(self, method: str) -> Optional[str]:
        token = self._bot_token()
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/{method}"

    def _bot_loop(self) -> None:
        while not self._stop.is_set():
            base = self._bot_api("getUpdates")
            if not base:
                time.sleep(1.0)
                continue
            params = {
                "timeout": int(self._cfg.poll_timeout_s),
                "offset": int(self._bot_offset) if self._bot_offset else None,
            }
            try:
                data = self._http_get_json(base, params={k: v for k, v in params.items() if v is not None}, timeout_s=max(1, self._cfg.poll_timeout_s + 5))
            except Exception:
                time.sleep(max(0.1, float(self._cfg.poll_sleep_s)))
                continue

            if not isinstance(data, dict) or data.get("ok") is not True:
                time.sleep(max(0.1, float(self._cfg.poll_sleep_s)))
                continue

            results = data.get("result")
            if not isinstance(results, list):
                time.sleep(max(0.1, float(self._cfg.poll_sleep_s)))
                continue

            for upd in results:
                if not isinstance(upd, dict):
                    continue
                upd_id = upd.get("update_id")
                if isinstance(upd_id, int):
                    self._bot_offset = max(self._bot_offset, upd_id + 1)
                self._handle_bot_update(upd)

            time.sleep(max(0.0, float(self._cfg.poll_sleep_s)))

    def _bot_download_file(self, *, file_id: str, timeout_s: float = 30.0) -> tuple[Optional[bytes], Optional[Dict[str, Any]], Optional[str]]:
        get_file_url = self._bot_api("getFile")
        if not get_file_url:
            return None, None, "Missing bot token"
        try:
            j1 = self._http_get_json(get_file_url, params={"file_id": file_id}, timeout_s=float(timeout_s))
        except Exception as e:
            return None, None, str(e)
        if not isinstance(j1, dict) or j1.get("ok") is not True:
            return None, j1 if isinstance(j1, dict) else None, str((j1 or {}).get("description") or "getFile failed")
        result = j1.get("result")
        if not isinstance(result, dict):
            return None, j1, "Invalid getFile result"
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return None, j1, "Missing file_path"
        token = self._bot_token()
        if not token:
            return None, j1, "Missing bot token"
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        try:
            content = self._http_get_bytes(url, timeout_s=float(timeout_s))
            return content, result, None
        except Exception as e:
            return None, j1, str(e)

    # -----------------------------------------------------------------
    # Typing indicator ("..." bubble in Telegram while agent processes)
    # -----------------------------------------------------------------

    def _send_bot_typing(self, chat_id: int) -> None:
        """Best-effort: show 'typing...' indicator in the chat via Bot API."""
        url = self._bot_api("sendChatAction")
        if not url:
            return
        try:
            body = json.dumps({"chat_id": chat_id, "action": "typing"}).encode("utf-8")
            req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urlopen(req, timeout=5.0)  # nosec - controlled URL (Telegram API)
        except Exception:
            pass  # Best-effort; never break the message flow

    def _send_tdlib_typing(self, chat_id: int) -> None:
        """Best-effort: show 'typing...' indicator in the chat via TDLib."""
        try:
            client = get_global_tdlib_client(start=True)
            client.send({"@type": "sendChatAction", "chat_id": int(chat_id), "action": {"@type": "chatActionTyping"}})
        except Exception:
            pass  # Best-effort

    def _session_has_active_runs(self, *, session_id: str) -> Optional[bool]:
        """Return True when the session has active work (RUNNING or WAITING != EVENT)."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        try:
            runtime = Runtime(
                run_store=self._runner.run_store,
                ledger_store=self._runner.ledger_store,
                artifact_store=self._runner.artifact_store,
            )
        except Exception:
            return None

        store = getattr(runtime, "run_store", None)
        list_index = getattr(store, "list_run_index", None)
        if callable(list_index):
            try:
                rows = list_index(status=RunStatus.RUNNING, session_id=sid, limit=50)
            except TypeError:
                # Older stores may not accept session_id filtering in the fast-path.
                try:
                    rows = list_index(status=RunStatus.RUNNING, limit=200)
                    if isinstance(rows, list):
                        rows = [r for r in rows if isinstance(r, dict) and str(r.get("session_id") or "").strip() == sid]
                except Exception:
                    rows = None
            except Exception:
                rows = None

            if isinstance(rows, list):
                if rows:
                    return True

        list_runs = getattr(store, "list_runs", None)
        if callable(list_runs):
            runs = None
            try:
                runs = list_runs(limit=2000)
            except Exception:
                runs = None

            if not isinstance(runs, list):
                return None

            for r in runs:
                try:
                    if str(getattr(r, "session_id", "") or "").strip() != sid:
                        continue
                    status = getattr(r, "status", None)
                    if status == RunStatus.RUNNING or str(status or "") == str(RunStatus.RUNNING):
                        return True
                    if status == RunStatus.WAITING or str(status or "") == str(RunStatus.WAITING):
                        waiting = getattr(r, "waiting", None)
                        reason = getattr(waiting, "reason", None) if waiting is not None else None
                        if reason is None:
                            continue
                        if reason == WaitReason.EVENT:
                            continue
                        reason_s = str(getattr(reason, "value", reason) or "").strip().lower()
                        if not reason_s:
                            continue
                        if reason_s == str(getattr(WaitReason.EVENT, "value", "event") or "event").strip().lower():
                            continue
                        return True
                except Exception:
                    continue
            return False

        return None

    def _start_typing_loop(self, chat_id: int, *, session_id: str, send_fn: Callable[[], None]) -> None:
        """Keep the typing indicator alive while the agent processes the current event."""
        if self._cfg.typing_max_s <= 0:
            return
        now = time.time()
        started_at = now
        until = now + float(self._cfg.typing_max_s)
        with self._typing_lock:
            self._typing_until[int(chat_id)] = until
            existing = self._typing_threads.get(int(chat_id))
            if existing is not None and existing.is_alive():
                return

            def _loop() -> None:
                observed_running = False
                while not self._stop.is_set():
                    with self._typing_lock:
                        deadline = self._typing_until.get(int(chat_id), 0.0)
                    if time.time() >= float(deadline):
                        break
                    send_fn()
                    active = self._session_has_active_runs(session_id=str(session_id or ""))
                    # If the workflow is blocked on a tool approval, prompt the user and stop typing.
                    try:
                        if self._maybe_handle_tool_wait(chat_id=int(chat_id), session_id=str(session_id or "")):
                            break
                    except Exception:
                        pass
                    if active is True:
                        observed_running = True
                    if active is False and not observed_running and (time.time() - float(started_at)) >= 30.0:
                        break
                    if active is None and not observed_running and (time.time() - float(started_at)) >= 30.0:
                        break
                    if active is False and observed_running:
                        break
                    self._stop.wait(timeout=float(self._cfg.typing_interval_s))
                with self._typing_lock:
                    self._typing_until.pop(int(chat_id), None)
                    self._typing_threads.pop(int(chat_id), None)

            t = threading.Thread(target=_loop, name=f"telegram-typing-{chat_id}", daemon=True)
            self._typing_threads[int(chat_id)] = t
            t.start()

    # -----------------------------------------------------------------
    # Bot API mode (non-E2EE; dev fallback)
    # -----------------------------------------------------------------

    def _handle_bot_update(self, upd: Dict[str, Any]) -> None:
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        chat_kind = self._bot_chat_kind(chat_type=str(chat.get("type") or ""))

        from_obj = msg.get("from")
        if isinstance(from_obj, dict) and from_obj.get("is_bot") is True:
            # Avoid loops: ignore bot's own messages.
            return
        from_user_id = from_obj.get("id") if isinstance(from_obj, dict) else None
        from_uid = int(from_user_id) if isinstance(from_user_id, int) else None

        # Handle /reset command: cancel runs + clear binding.
        text_raw = msg.get("text") if isinstance(msg.get("text"), str) else ""
        if not text_raw:
            caption = msg.get("caption") if isinstance(msg.get("caption"), str) else ""
            text_raw = caption

        cmd = _command_base(text_raw)
        if cmd == "/whoami":
            self._handle_whoami_command(chat_id=chat_id, chat_kind=chat_kind, from_user_id=from_uid)
            return
        if cmd in {"/pair", "/start"}:
            self._handle_pair_command(chat_id=chat_id, from_user_id=from_uid, text=("/pair " + _command_args(text_raw)) if cmd == "/start" else text_raw, chat_kind=chat_kind)
            return

        if not self._is_authorized_incoming(chat_id=chat_id, chat_kind=chat_kind, from_user_id=from_uid, text=text_raw):
            return

        if _command_base(text_raw) in {"/reset", "/clear", "/new"}:
            self._handle_bot_reset(chat_id=chat_id, message_id=msg.get("message_id"))
            return
        if _command_base(text_raw) in {"/tools", "/tool"}:
            self._handle_tools_command(chat_id=chat_id, from_user_id=from_uid, text=text_raw)
            return

        existing = self._binding_for_chat(chat_id)
        if isinstance(existing, dict):
            sid0 = str(existing.get("session_id") or "").strip()
            if sid0 and self._handle_tool_approval_response(chat_id=chat_id, session_id=sid0, from_user_id=from_uid, text=text_raw):
                return

        binding = self._ensure_binding(chat_id=chat_id, from_user_id=from_uid)
        if binding is None:
            return

        # Keep "typing..." indicator alive while the agent processes.
        self._start_typing_loop(chat_id, session_id=str(binding.get("session_id") or ""), send_fn=lambda: self._send_bot_typing(chat_id))

        tg_payload: Dict[str, Any] = {
            "transport": "bot_api",
            "chat_id": chat_id,
            "from_user_id": from_uid,
            "message_id": msg.get("message_id"),
            "date": msg.get("date"),
            "text": text_raw,
            "media": [],
        }

        # Download and store media attachments (photos, documents, audio, video, stickers, etc.).
        media_refs: list[Dict[str, Any]] = []
        if self._cfg.store_media:
            media_refs = self._extract_bot_media(msg, run_id=str(binding.get("run_id") or ""))
            if media_refs:
                tg_payload["media"] = media_refs

        # If user sent text without media, reuse recent pending media (sent just before).
        if (not media_refs) and text_raw.strip():
            reply_to = msg.get("reply_to_message")
            reply_to_mid = reply_to.get("message_id") if isinstance(reply_to, dict) else None
            pending = self._maybe_apply_pending_media(
                binding=binding,
                text=text_raw,
                reply_to_message_id=reply_to_mid if isinstance(reply_to_mid, int) else None,
            )
            if pending:
                media_refs = pending
                tg_payload["media"] = media_refs

        # If user sent media without text, stash it for the next message.
        if media_refs and not text_raw.strip():
            self._stash_pending_media(binding=binding, media=media_refs, message_id=msg.get("message_id") if isinstance(msg.get("message_id"), int) else None)

        # Build event envelope: promote attachments to top-level so the agent adapter's
        # extract_media_from_context() can find them (enables VLM image analysis).
        event_payload: Dict[str, Any] = {"telegram": tg_payload, "attachments": list(media_refs)}

        self._runner.emit_event(
            name=self._cfg.event_name,
            session_id=str(binding.get("session_id") or ""),
            scope="session",
            payload=event_payload,
            client_id="telegram",
        )

        with self._lock:
            binding["updated_at"] = _utc_now_iso()
            self._save_state()

    # -----------------------------------------------------------------
    # /reset command (Bot API): clear binding
    # -----------------------------------------------------------------

    def _handle_bot_reset(self, *, chat_id: int, message_id: Any = None) -> None:
        """Handle /reset: cancel runs, advance session rev, delete binding, send confirmation."""
        import logging
        logger = logging.getLogger(__name__)

        # Cancel existing runs for this chat (across session revisions).
        self._cancel_chat_runs(chat_id=chat_id)

        # Remove the binding so the next message creates a fresh run.
        with self._lock:
            bindings = self._state.get("bindings")
            if isinstance(bindings, dict):
                bindings.pop(str(chat_id), None)
            self._save_state()

        # Advance the session revision so the next message starts a brand-new session_id.
        new_rev = self._bump_session_rev(chat_id)
        logger.info("Telegram bridge: reset chat_id=%s (new_rev=%s)", chat_id, new_rev)

        # Send a confirmation message immediately; message deletion can be slow.
        self._send_text(chat_id=int(chat_id), text=str(self._cfg.reset_message or "").strip())

        # Best-effort: delete prior messages by deleting a recent message-id window (async).
        if self._cfg.reset_delete_messages:
            try:
                anchor = int(message_id) if isinstance(message_id, int) and not isinstance(message_id, bool) else None
                max_delete = int(self._cfg.reset_delete_max or 0)
            except Exception:
                anchor = None
                max_delete = 0
            if anchor is not None and max_delete > 0:
                def _delete_window() -> None:
                    try:
                        for i in range(int(max_delete)):
                            mid = int(anchor) - int(i)
                            if mid <= 0:
                                break
                            self._bot_delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass

                threading.Thread(target=_delete_window, daemon=True).start()

    def _bot_send_text(self, *, chat_id: int, text: str) -> None:
        """Best-effort: send a simple text message via Bot API."""
        url = self._bot_api("sendMessage")
        if not url:
            return
        try:
            body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urlopen(req, timeout=10.0)  # nosec - controlled URL
        except Exception:
            pass

    def _bot_delete_message(self, *, chat_id: int, message_id: int) -> None:
        """Best-effort: delete a message via Bot API (may fail depending on chat permissions)."""
        url = self._bot_api("deleteMessage")
        if not url:
            return
        try:
            body = json.dumps({"chat_id": int(chat_id), "message_id": int(message_id)}).encode("utf-8")
            req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urlopen(req, timeout=10.0)  # nosec - controlled URL (Telegram API)
        except Exception:
            pass

    def _extract_bot_media(self, msg: Dict[str, Any], *, run_id: str) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []

        def _store(kind: str, *, file_id: str, filename: str = "", mime_type: str = "application/octet-stream") -> None:
            content, file_meta, err = self._bot_download_file(file_id=file_id)
            if err or content is None:
                return
            tags = {"source": "telegram", "kind": kind, "file_id": file_id}
            meta = self._artifact_store.store(content, content_type=mime_type, run_id=run_id, tags=tags)
            out.append(
                {
                    "kind": kind,
                    "artifact_id": meta.artifact_id,
                    "content_type": mime_type,
                    "filename": filename or "",
                    "size_bytes": meta.size_bytes,
                    "telegram": {"file_id": file_id, "file": file_meta},
                }
            )

        doc = msg.get("document")
        if isinstance(doc, dict):
            file_id = doc.get("file_id")
            if isinstance(file_id, str) and file_id:
                _store(
                    "document",
                    file_id=file_id,
                    filename=str(doc.get("file_name") or ""),
                    mime_type=str(doc.get("mime_type") or "application/octet-stream"),
                )

        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            best = None
            for p in photos:
                if isinstance(p, dict) and isinstance(p.get("file_id"), str):
                    best = p
            if isinstance(best, dict):
                fid = best.get("file_id")
                if isinstance(fid, str) and fid:
                    _store("photo", file_id=fid, filename="", mime_type="image/jpeg")

        voice = msg.get("voice")
        if isinstance(voice, dict):
            fid = voice.get("file_id")
            if isinstance(fid, str) and fid:
                _store("voice", file_id=fid, filename=str(voice.get("file_name") or ""), mime_type="audio/ogg")

        audio = msg.get("audio")
        if isinstance(audio, dict):
            fid = audio.get("file_id")
            if isinstance(fid, str) and fid:
                mime = str(audio.get("mime_type") or "audio/mpeg")
                filename = str(audio.get("file_name") or audio.get("title") or "")
                _store("audio", file_id=fid, filename=filename, mime_type=mime)

        video = msg.get("video")
        if isinstance(video, dict):
            fid = video.get("file_id")
            if isinstance(fid, str) and fid:
                mime = str(video.get("mime_type") or "video/mp4")
                filename = str(video.get("file_name") or "video.mp4")
                _store("video", file_id=fid, filename=filename, mime_type=mime)

        video_note = msg.get("video_note")
        if isinstance(video_note, dict):
            fid = video_note.get("file_id")
            if isinstance(fid, str) and fid:
                _store("video_note", file_id=fid, filename="video_note.mp4", mime_type="video/mp4")

        animation = msg.get("animation")
        if isinstance(animation, dict):
            fid = animation.get("file_id")
            if isinstance(fid, str) and fid:
                mime = str(animation.get("mime_type") or "video/mp4")
                filename = str(animation.get("file_name") or "animation")
                _store("animation", file_id=fid, filename=filename, mime_type=mime)

        sticker = msg.get("sticker")
        if isinstance(sticker, dict):
            fid = sticker.get("file_id")
            if isinstance(fid, str) and fid:
                is_video = bool(sticker.get("is_video"))
                is_animated = bool(sticker.get("is_animated"))
                if is_video:
                    mime = "video/webm"
                    filename = "sticker.webm"
                elif is_animated:
                    mime = "application/x-tgsticker"
                    filename = "sticker.tgs"
                else:
                    mime = "image/webp"
                    filename = "sticker.webp"
                _store("sticker", file_id=fid, filename=filename, mime_type=mime)

        return out

    def _http_get_json(self, url: str, *, params: Optional[Dict[str, Any]] = None, timeout_s: float = 30.0) -> Any:
        q = urlencode({k: str(v) for k, v in (params or {}).items() if v is not None})
        full = f"{url}?{q}" if q else url
        with urlopen(full, timeout=float(timeout_s)) as r:  # nosec - controlled URL (Telegram API)
            raw = r.read()
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _http_get_bytes(self, url: str, *, timeout_s: float = 30.0) -> bytes:
        with urlopen(url, timeout=float(timeout_s)) as r:  # nosec - controlled URL (Telegram API)
            return bytes(r.read())

    # ---------------------------------------------------------------------
    # TDLib mode (Secret Chats)
    # ---------------------------------------------------------------------

    def _tdlib_caption_text(self, content: Dict[str, Any]) -> str:
        cap = content.get("caption")
        if isinstance(cap, dict):
            txt = cap.get("text")
            if isinstance(txt, str) and txt.strip():
                return txt
        return ""

    def _handle_tdlib_update(self, upd: Dict[str, Any]) -> None:
        # Expected: {"@type":"updateNewMessage","message":{...}}
        if not isinstance(upd, dict):
            return
        if upd.get("@type") != "updateNewMessage":
            return
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return

        # Ignore outgoing messages (sent by the AI account itself).
        if msg.get("is_outgoing") is True:
            return

        chat_id = msg.get("chat_id")
        if not isinstance(chat_id, int):
            return
        chat_kind = self._tdlib_chat_kind(chat_id=int(chat_id))

        sender_id = msg.get("sender_id")
        from_uid = None
        if isinstance(sender_id, dict):
            if sender_id.get("@type") == "messageSenderUser" and isinstance(sender_id.get("user_id"), int):
                from_uid = int(sender_id.get("user_id"))

        content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
        text = ""
        ctype = ""
        if isinstance(content, dict):
            ctype = str(content.get("@type") or "")
            if ctype == "messageText":
                t = content.get("text")
                if isinstance(t, dict):
                    tt = t.get("text")
                    if isinstance(tt, str):
                        text = tt
            if not text:
                text = self._tdlib_caption_text(content)

        cmd = _command_base(text)
        if cmd == "/whoami":
            self._handle_whoami_command(chat_id=int(chat_id), chat_kind=chat_kind, from_user_id=from_uid)
            return
        if cmd in {"/pair", "/start"}:
            self._handle_pair_command(chat_id=int(chat_id), from_user_id=from_uid, text=("/pair " + _command_args(text)) if cmd == "/start" else text, chat_kind=chat_kind)
            return

        if not self._is_authorized_incoming(chat_id=int(chat_id), chat_kind=chat_kind, from_user_id=from_uid, text=text):
            return

        # Handle /reset command: clear binding and cancel runs.
        if _command_base(text) in {"/reset", "/clear", "/new"}:
            self._handle_tdlib_reset(chat_id=chat_id, message_id=msg.get("id"))
            return
        if _command_base(text) in {"/tools", "/tool"}:
            self._handle_tools_command(chat_id=chat_id, from_user_id=from_uid, text=text)
            return

        existing = self._binding_for_chat(chat_id)
        if isinstance(existing, dict):
            sid0 = str(existing.get("session_id") or "").strip()
            if sid0 and self._handle_tool_approval_response(chat_id=chat_id, session_id=sid0, from_user_id=from_uid, text=text):
                return

        binding = self._ensure_binding(chat_id=chat_id, from_user_id=from_uid)
        if binding is None:
            return

        media: list[Dict[str, Any]] = []
        if ctype and ctype != "messageText" and self._cfg.store_media and isinstance(content, dict):
            media = self._extract_tdlib_media(content, run_id=str(binding.get("run_id") or ""))

        # Keep "typing..." indicator alive while the agent processes.
        self._start_typing_loop(chat_id, session_id=str(binding.get("session_id") or ""), send_fn=lambda: self._send_tdlib_typing(chat_id))

        # If user sent media without text, stash it for the next message.
        if media and not text.strip():
            self._stash_pending_media(binding=binding, media=media, message_id=msg.get("id") if isinstance(msg.get("id"), int) else None)

        # If user sent text without media, reuse recent pending media (when it likely refers to the media).
        if (not media) and text.strip():
            reply_to_mid = msg.get("reply_to_message_id")
            pending = self._maybe_apply_pending_media(
                binding=binding,
                text=text,
                reply_to_message_id=reply_to_mid if isinstance(reply_to_mid, int) else None,
            )
            if pending:
                media = pending

        tg_payload = {
            "transport": "tdlib",
            "chat_id": chat_id,
            "from_user_id": from_uid,
            "message_id": msg.get("id"),
            "date": msg.get("date"),
            "text": text,
            "media": media,
        }

        # Build event envelope: promote attachments to top-level so the agent adapter's
        # extract_media_from_context() can find them (enables VLM image analysis).
        event_payload: Dict[str, Any] = {"telegram": tg_payload, "attachments": list(media)}

        self._runner.emit_event(
            name=self._cfg.event_name,
            session_id=str(binding.get("session_id") or ""),
            scope="session",
            payload=event_payload,
            client_id="telegram",
        )

        with self._lock:
            binding["updated_at"] = _utc_now_iso()
            self._save_state()

    def _handle_tdlib_reset(self, *, chat_id: int, message_id: Any = None) -> None:
        """Handle /reset for TDLib transport (Secret Chats): clear binding + cancel runs."""
        import logging

        logger = logging.getLogger(__name__)
        self._cancel_chat_runs(chat_id=chat_id)

        with self._lock:
            bindings = self._state.get("bindings")
            if isinstance(bindings, dict):
                bindings.pop(str(chat_id), None)
            self._save_state()

        new_rev = self._bump_session_rev(chat_id)
        logger.info("Telegram bridge: reset chat_id=%s (new_rev=%s)", chat_id, new_rev)

        # Send a confirmation message immediately; message deletion can be slow.
        self._send_text(chat_id=int(chat_id), text=str(self._cfg.reset_message or "").strip())

        if self._cfg.reset_delete_messages:
            try:
                tracked: list[int] = []

                anchor = int(message_id) if isinstance(message_id, int) and not isinstance(message_id, bool) else None
                max_delete = int(self._cfg.reset_delete_max or 0)
                if anchor is not None and max_delete > 0:
                    for i in range(int(max_delete)):
                        mid = int(anchor) - int(i)
                        if mid <= 0:
                            break
                        tracked.append(int(mid))

                tracked.extend(self._collect_tracked_bot_message_ids(chat_id=chat_id))
                tracked = _dedup_ints(tracked)
                if max_delete > 0 and len(tracked) > max_delete:
                    tracked = sorted(tracked)[-int(max_delete) :]
                self._tdlib_delete_messages(chat_id=chat_id, message_ids=tracked)
            except Exception:
                pass

    def _tdlib_delete_messages(self, *, chat_id: int, message_ids: list[int]) -> None:
        """Best-effort: delete messages via TDLib (may fail depending on chat permissions)."""
        ids = [int(x) for x in message_ids if isinstance(x, int) and not isinstance(x, bool)]
        if not ids:
            return
        try:
            client = get_global_tdlib_client(start=True)
            client.send({"@type": "deleteMessages", "chat_id": int(chat_id), "message_ids": ids, "revoke": True})
        except Exception:
            pass

    def _extract_tdlib_media(self, content: Dict[str, Any], *, run_id: str) -> list[Dict[str, Any]]:
        # Best-effort only: TDLib JSON shapes vary by content type; we try common paths.
        out: list[Dict[str, Any]] = []

        try:
            client = get_global_tdlib_client(start=True)
        except Exception:
            return out

        def _download_and_store(kind: str, *, file_id: Optional[int], filename: str = "", mime_type: str = "application/octet-stream") -> None:
            if not isinstance(file_id, int):
                return
            try:
                fobj = client.request(
                    {"@type": "downloadFile", "file_id": int(file_id), "priority": 32, "offset": 0, "limit": 0, "synchronous": True},
                    timeout_s=60.0,
                )
            except Exception:
                return
            if not isinstance(fobj, dict) or fobj.get("@type") == "error":
                return
            local = fobj.get("local")
            path = local.get("path") if isinstance(local, dict) else None
            if not isinstance(path, str) or not path:
                return
            try:
                data = Path(path).read_bytes()
            except Exception:
                return
            tags = {"source": "telegram", "kind": kind, "tdlib_file_id": str(file_id)}
            meta = self._artifact_store.store(data, content_type=mime_type, run_id=run_id, tags=tags)
            out.append({"kind": kind, "artifact_id": meta.artifact_id, "content_type": mime_type, "filename": filename, "size_bytes": meta.size_bytes})

        ctype = content.get("@type")
        if ctype == "messagePhoto":
            photo = content.get("photo")
            sizes = photo.get("sizes") if isinstance(photo, dict) else None
            file_id = None
            if isinstance(sizes, list) and sizes:
                best = sizes[-1]
                file = best.get("photo") if isinstance(best, dict) else None
                file_id = file.get("id") if isinstance(file, dict) else None
            _download_and_store("photo", file_id=file_id, mime_type="image/jpeg")

        elif ctype == "messageDocument":
            doc = content.get("document")
            file_name = doc.get("file_name") if isinstance(doc, dict) else ""
            mime = doc.get("mime_type") if isinstance(doc, dict) else ""
            file = doc.get("document") if isinstance(doc, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("document", file_id=fid, filename=str(file_name or ""), mime_type=str(mime or "application/octet-stream"))

        elif ctype == "messageVoiceNote":
            vn = content.get("voice_note")
            file = vn.get("voice") if isinstance(vn, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("voice", file_id=fid, mime_type="audio/ogg")

        elif ctype == "messageAudio":
            audio = content.get("audio")
            file_name = audio.get("file_name") if isinstance(audio, dict) else ""
            mime = audio.get("mime_type") if isinstance(audio, dict) else ""
            file = audio.get("audio") if isinstance(audio, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("audio", file_id=fid, filename=str(file_name or ""), mime_type=str(mime or "audio/mpeg"))

        elif ctype == "messageVideo":
            video = content.get("video")
            file_name = video.get("file_name") if isinstance(video, dict) else ""
            mime = video.get("mime_type") if isinstance(video, dict) else ""
            file = video.get("video") if isinstance(video, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("video", file_id=fid, filename=str(file_name or ""), mime_type=str(mime or "video/mp4"))

        elif ctype == "messageVideoNote":
            vn = content.get("video_note")
            file = vn.get("video") if isinstance(vn, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("video_note", file_id=fid, filename="video_note.mp4", mime_type="video/mp4")

        elif ctype == "messageAnimation":
            anim = content.get("animation")
            file_name = anim.get("file_name") if isinstance(anim, dict) else ""
            mime = anim.get("mime_type") if isinstance(anim, dict) else ""
            file = anim.get("animation") if isinstance(anim, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("animation", file_id=fid, filename=str(file_name or ""), mime_type=str(mime or "video/mp4"))

        elif ctype == "messageSticker":
            st = content.get("sticker")
            mime = st.get("mime_type") if isinstance(st, dict) else ""
            file = st.get("sticker") if isinstance(st, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("sticker", file_id=fid, filename="sticker", mime_type=str(mime or "application/octet-stream"))

        return out


def _dedup_ints(values: list[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for v in values or []:
        if not isinstance(v, int) or isinstance(v, bool):
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(int(v))
    return out
