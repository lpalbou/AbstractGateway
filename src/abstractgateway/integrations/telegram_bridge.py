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

from abstractcore.tools.telegram_tdlib import TdlibNotAvailable, get_global_tdlib_client, stop_global_tdlib_client
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


def _command_base(text: str) -> str:
    """Return the normalized bot-command token (e.g. "/reset" from "/reset@botname foo")."""
    s = str(text or "").strip()
    if not s.startswith("/"):
        return ""
    token = s.split(maxsplit=1)[0].strip()
    if "@" in token:
        token = token.split("@", 1)[0].strip()
    return token.lower()


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

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if not self._cfg.flow_id:
            raise ValueError("ABSTRACT_TELEGRAM_FLOW_ID is required when ABSTRACT_TELEGRAM_BRIDGE=1")

        self._load_state()

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
        self._state.setdefault("bindings", {})
        self._state.setdefault("session_revs", {})

    def _save_state(self) -> None:
        path = self._cfg.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

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
        if _command_base(text_raw) in {"/reset", "/clear", "/new"}:
            self._handle_bot_reset(chat_id=chat_id, message_id=msg.get("message_id"))
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

        # Best-effort: delete prior messages by deleting a recent message-id window.
        if self._cfg.reset_delete_messages:
            try:
                anchor = int(message_id) if isinstance(message_id, int) and not isinstance(message_id, bool) else None
                max_delete = int(self._cfg.reset_delete_max or 0)
                if anchor is not None and max_delete > 0:
                    for i in range(max_delete):
                        mid = int(anchor) - int(i)
                        if mid <= 0:
                            break
                        self._bot_delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

        # Advance the session revision so the next message starts a brand-new session_id.
        new_rev = self._bump_session_rev(chat_id)
        logger.info("Telegram bridge: reset chat_id=%s (new_rev=%s)", chat_id, new_rev)

        # Send a confirmation message (this starts a new "first message" in the chat).
        self._bot_send_text(chat_id=chat_id, text="Conversation reset. Send a new message to start fresh.")

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

        # Handle /reset command: clear binding and cancel runs.
        if _command_base(text) in {"/reset", "/clear", "/new"}:
            self._handle_tdlib_reset(chat_id=chat_id, message_id=msg.get("id"))
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

    def _handle_tdlib_reset(self, *, chat_id: int) -> None:
        """Handle /reset for TDLib transport (Secret Chats): clear binding + cancel runs."""
        import logging

        logger = logging.getLogger(__name__)
        self._cancel_chat_runs(chat_id=chat_id)

        removed_binding: Optional[Dict[str, Any]] = None
        with self._lock:
            bindings = self._state.get("bindings")
            if isinstance(bindings, dict):
                removed = bindings.pop(str(chat_id), None)
                if isinstance(removed, dict):
                    removed_binding = dict(removed)
            self._save_state()

        if self._cfg.reset_delete_messages:
            try:
                tracked: list[int] = []
                if isinstance(removed_binding, dict):
                    raw_user_ids = removed_binding.get("user_message_ids")
                    if isinstance(raw_user_ids, list):
                        for x in raw_user_ids:
                            if isinstance(x, int) and not isinstance(x, bool):
                                tracked.append(int(x))
                tracked.extend(self._collect_tracked_bot_message_ids(chat_id=chat_id))
                tracked = _dedup_ints(tracked)[-int(self._cfg.reset_delete_max or 0) :] if self._cfg.reset_delete_max else _dedup_ints(tracked)
                self._tdlib_delete_messages(chat_id=chat_id, message_ids=tracked)
            except Exception:
                pass

        new_rev = self._bump_session_rev(chat_id)
        logger.info("Telegram bridge: reset chat_id=%s (new_rev=%s)", chat_id, new_rev)

        # Best-effort: send confirmation via TDLib (async).
        try:
            client = get_global_tdlib_client(start=True)
            client.send(
                {
                    "@type": "sendMessage",
                    "chat_id": int(chat_id),
                    "input_message_content": {
                        "@type": "inputMessageText",
                        "text": {"@type": "formattedText", "text": "Conversation reset. Send a new message to start fresh."},
                        "disable_web_page_preview": True,
                    },
                }
            )
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
