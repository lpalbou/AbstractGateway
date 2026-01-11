from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional

from urllib.parse import urlencode
from urllib.request import urlopen

from abstractcore.tools.telegram_tdlib import TdlibNotAvailable, get_global_tdlib_client, stop_global_tdlib_client


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

    # Storage behavior
    store_media: bool = True

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
            store_media=bool(store_media),
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

    def _save_state(self) -> None:
        path = self._cfg.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def _binding_for_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        b = self._state.get("bindings")
        if not isinstance(b, dict):
            return None
        return b.get(str(chat_id))

    def _ensure_binding(self, *, chat_id: int, from_user_id: Optional[int]) -> Optional[Dict[str, Any]]:
        with self._lock:
            existing = self._binding_for_chat(chat_id)
            if isinstance(existing, dict):
                return existing

            session_id = f"{self._cfg.session_prefix}{chat_id}"
            try:
                run_id = self._host.start_run(
                    flow_id=self._cfg.flow_id,
                    bundle_id=self._cfg.bundle_id,
                    input_data={"telegram": {"chat_id": chat_id, "from_user_id": from_user_id}},
                    actor_id="telegram",
                    session_id=session_id,
                )
            except Exception:
                return None

            binding = {
                "chat_id": chat_id,
                "session_id": session_id,
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
        from_user_id = from_obj.get("id") if isinstance(from_obj, dict) else None
        from_uid = int(from_user_id) if isinstance(from_user_id, int) else None

        binding = self._ensure_binding(chat_id=chat_id, from_user_id=from_uid)
        if binding is None:
            return

        payload: Dict[str, Any] = {
            "transport": "bot_api",
            "chat_id": chat_id,
            "from_user_id": from_uid,
            "message_id": msg.get("message_id"),
            "date": msg.get("date"),
            "text": msg.get("text") if isinstance(msg.get("text"), str) else "",
            "media": [],
        }

        if self._cfg.store_media:
            media = self._extract_bot_media(msg, run_id=str(binding.get("run_id") or ""))
            if media:
                payload["media"] = media

        self._runner.emit_event(
            name=self._cfg.event_name,
            session_id=str(binding.get("session_id") or ""),
            scope="session",
            payload={"telegram": payload},
            client_id="telegram",
        )

        with self._lock:
            binding["updated_at"] = _utc_now_iso()
            self._save_state()

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

        binding = self._ensure_binding(chat_id=chat_id, from_user_id=from_uid)
        if binding is None:
            return

        content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
        text = ""
        media: list[Dict[str, Any]] = []
        if isinstance(content, dict):
            ctype = content.get("@type")
            if ctype == "messageText":
                t = content.get("text")
                if isinstance(t, dict):
                    tt = t.get("text")
                    if isinstance(tt, str):
                        text = tt
            elif self._cfg.store_media:
                media = self._extract_tdlib_media(content, run_id=str(binding.get("run_id") or ""))

        payload = {
            "transport": "tdlib",
            "chat_id": chat_id,
            "from_user_id": from_uid,
            "message_id": msg.get("id"),
            "date": msg.get("date"),
            "text": text,
            "media": media,
        }

        self._runner.emit_event(
            name=self._cfg.event_name,
            session_id=str(binding.get("session_id") or ""),
            scope="session",
            payload={"telegram": payload},
            client_id="telegram",
        )

        with self._lock:
            binding["updated_at"] = _utc_now_iso()
            self._save_state()

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

        if ctype == "messageDocument":
            doc = content.get("document")
            file_name = doc.get("file_name") if isinstance(doc, dict) else ""
            mime = doc.get("mime_type") if isinstance(doc, dict) else ""
            file = doc.get("document") if isinstance(doc, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("document", file_id=fid, filename=str(file_name or ""), mime_type=str(mime or "application/octet-stream"))

        if ctype == "messageVoiceNote":
            vn = content.get("voice_note")
            file = vn.get("voice") if isinstance(vn, dict) else None
            fid = file.get("id") if isinstance(file, dict) else None
            _download_and_store("voice", file_id=fid, mime_type="audio/ogg")

        return out
