from __future__ import annotations

import email
from email.header import decode_header
from email.message import Message
import hashlib
import imaplib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from abstractruntime.integrations.abstractcore.session_attachments import session_memory_owner_run_id


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


def _as_int(raw: Any, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


_MSG_ID_RE = re.compile(r"<[^>]+>")


def _decode_mime_header(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        chunks = decode_header(value)
    except Exception:
        return value.strip()

    out: list[str] = []
    for part, charset in chunks:
        if isinstance(part, bytes):
            enc = charset or "utf-8"
            try:
                out.append(part.decode(enc, errors="replace"))
            except Exception:
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out).strip()


def _parse_message_ids(value: Any) -> list[str]:
    text = _decode_mime_header(value)
    if not text:
        return []
    ids = _MSG_ID_RE.findall(text)
    # Keep original casing; message ids are typically case-sensitive opaque identifiers.
    out = []
    seen: set[str] = set()
    for mid in ids:
        mid2 = str(mid).strip()
        if not mid2 or mid2 in seen:
            continue
        seen.add(mid2)
        out.append(mid2)
    return out


def _safe_id_component(value: str, *, max_len: int = 48) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "default"
    # Keep readable for common cases (emails, simple ids); fallback to hash for very long/odd strings.
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_")
    if not safe:
        safe = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    if len(safe) > max_len:
        safe = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:max_len]
    return safe


def _extract_text_bodies(msg: Message) -> tuple[str, str]:
    """Return (text/plain, text/html) bodies, best-effort decoded."""
    if msg is None:
        return "", ""

    text_parts: list[str] = []
    html_parts: list[str] = []

    def _decode_part(part: Message) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            return payload.decode("utf-8", errors="replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = part.get_content_disposition()
            if disp == "attachment":
                continue
            ctype = str(part.get_content_type() or "")
            if ctype == "text/plain":
                text = _decode_part(part).strip()
                if text:
                    text_parts.append(text)
            elif ctype == "text/html":
                html = _decode_part(part).strip()
                if html:
                    html_parts.append(html)
    else:
        ctype = str(msg.get_content_type() or "")
        if ctype == "text/plain":
            t = _decode_part(msg).strip()
            if t:
                text_parts.append(t)
        elif ctype == "text/html":
            h = _decode_part(msg).strip()
            if h:
                html_parts.append(h)

    return ("\n\n".join(text_parts).strip(), "\n\n".join(html_parts).strip())


def _clamp_text(text: str, *, max_chars: int) -> str:
    t = str(text or "")
    try:
        limit = int(max_chars)
    except Exception:
        limit = 0
    if limit <= 0:
        return t
    if len(t) <= limit:
        return t
    #[WARNING:TRUNCATION] inbound email body text is bounded before entering durable state
    return t[:limit] + "…"


@dataclass(frozen=True)
class EmailBridgeConfig:
    enabled: bool

    event_name: str
    session_prefix: str
    account: str

    imap_host: str
    imap_username: str
    imap_password_env_var: str
    imap_folder: str
    imap_port: int = 993
    imap_timeout_s: float = 30.0

    poll_seconds: float = 60.0
    max_messages_per_poll: int = 50

    # Optional: start a workflow per email thread/session (Telegram-bridge-style).
    autostart_flow_id: Optional[str] = None
    autostart_bundle_id: Optional[str] = None

    # Storage and normalization caps (treat email as untrusted input).
    store_raw_message: bool = True
    store_attachments: bool = True
    max_raw_bytes: int = 2 * 1024 * 1024
    max_body_chars: int = 20_000
    max_html_chars: int = 20_000
    max_attachments: int = 20
    max_attachment_bytes: int = 5 * 1024 * 1024
    max_total_attachment_bytes: int = 15 * 1024 * 1024

    state_dir: Path = Path("./runtime/email_bridge")

    @staticmethod
    def from_env(*, base_dir: Path) -> "EmailBridgeConfig":
        enabled = _as_bool(os.getenv("ABSTRACT_EMAIL_BRIDGE"), False)

        # Defaults from AbstractCore config system (best-effort).
        cfg_smtp_host = ""
        cfg_smtp_username = ""
        cfg_smtp_password_env_var = "EMAIL_PASSWORD"
        cfg_imap_host = ""
        cfg_imap_username = ""
        cfg_imap_password_env_var = "EMAIL_PASSWORD"
        cfg_imap_folder = "INBOX"
        try:
            from abstractcore.config.manager import get_config_manager  # type: ignore

            core_cfg = get_config_manager().config
            email_cfg = getattr(core_cfg, "email", None)
            if email_cfg is not None:
                cfg_smtp_host = str(getattr(email_cfg, "smtp_host", "") or "")
                cfg_smtp_username = str(getattr(email_cfg, "smtp_username", "") or "")
                cfg_smtp_password_env_var = str(getattr(email_cfg, "smtp_password_env_var", "") or "") or "EMAIL_PASSWORD"
                cfg_imap_host = str(getattr(email_cfg, "imap_host", "") or "")
                cfg_imap_username = str(getattr(email_cfg, "imap_username", "") or "")
                cfg_imap_password_env_var = str(getattr(email_cfg, "imap_password_env_var", "") or "") or "EMAIL_PASSWORD"
                cfg_imap_folder = str(getattr(email_cfg, "imap_folder", "") or "") or "INBOX"
        except Exception:
            pass

        event_name = str(os.getenv("ABSTRACT_EMAIL_EVENT_NAME", "") or "").strip() or "email.message"
        session_prefix = str(os.getenv("ABSTRACT_EMAIL_SESSION_PREFIX", "") or "").strip() or "email:"

        account = str(os.getenv("ABSTRACT_EMAIL_ACCOUNT", "") or "").strip()
        imap_host = str(os.getenv("ABSTRACT_EMAIL_IMAP_HOST", "") or cfg_imap_host or "").strip()
        imap_username = str(os.getenv("ABSTRACT_EMAIL_IMAP_USERNAME", "") or cfg_imap_username or "").strip()
        imap_password_env_var = str(os.getenv("ABSTRACT_EMAIL_IMAP_PASSWORD_ENV_VAR", "") or cfg_imap_password_env_var or "").strip() or "EMAIL_PASSWORD"
        imap_folder = str(os.getenv("ABSTRACT_EMAIL_IMAP_FOLDER", "") or cfg_imap_folder or "").strip() or "INBOX"

        poll_seconds = float(os.getenv("ABSTRACT_EMAIL_POLL_SECONDS", "60") or "60")
        imap_port = _as_int(os.getenv("ABSTRACT_EMAIL_IMAP_PORT"), 993)
        imap_timeout_s = float(os.getenv("ABSTRACT_EMAIL_IMAP_TIMEOUT_S", "30") or "30")
        max_messages_per_poll = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_MESSAGES_PER_POLL"), 50)

        autostart_flow_id = str(os.getenv("ABSTRACT_EMAIL_FLOW_ID", "") or "").strip() or None
        autostart_bundle_id = str(os.getenv("ABSTRACT_EMAIL_BUNDLE_ID", "") or "").strip() or None

        store_raw_message = _as_bool(os.getenv("ABSTRACT_EMAIL_STORE_RAW_MESSAGE"), True)
        store_attachments = _as_bool(os.getenv("ABSTRACT_EMAIL_STORE_ATTACHMENTS"), True)

        max_raw_bytes = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_RAW_BYTES"), 2 * 1024 * 1024)
        max_body_chars = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_BODY_CHARS"), 20_000)
        max_html_chars = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_HTML_CHARS"), 20_000)
        max_attachments = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_ATTACHMENTS"), 20)
        max_attachment_bytes = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_ATTACHMENT_BYTES"), 5 * 1024 * 1024)
        max_total_attachment_bytes = _as_int(os.getenv("ABSTRACT_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES"), 15 * 1024 * 1024)

        state_dir = Path(base_dir) / "email_bridge"

        # Use outbound defaults as a fallback account label if no explicit account is set.
        if not account:
            account = str(os.getenv("ABSTRACT_EMAIL_FROM", "") or cfg_smtp_username or imap_username or "default").strip()

        return EmailBridgeConfig(
            enabled=bool(enabled),
            event_name=event_name,
            session_prefix=session_prefix,
            account=account,
            imap_host=imap_host,
            imap_username=imap_username,
            imap_password_env_var=imap_password_env_var,
            imap_folder=imap_folder,
            imap_port=max(1, int(imap_port)),
            imap_timeout_s=max(1.0, float(imap_timeout_s)),
            poll_seconds=max(1.0, float(poll_seconds)),
            max_messages_per_poll=max(1, int(max_messages_per_poll)),
            autostart_flow_id=autostart_flow_id,
            autostart_bundle_id=autostart_bundle_id,
            store_raw_message=bool(store_raw_message),
            store_attachments=bool(store_attachments),
            max_raw_bytes=max(1, int(max_raw_bytes)),
            max_body_chars=max(0, int(max_body_chars)),
            max_html_chars=max(0, int(max_html_chars)),
            max_attachments=max(0, int(max_attachments)),
            max_attachment_bytes=max(1, int(max_attachment_bytes)),
            max_total_attachment_bytes=max(1, int(max_total_attachment_bytes)),
            state_dir=state_dir,
        )


class EmailBridge:
    """Bridge inbound IMAP email messages to AbstractGateway events."""

    def __init__(self, *, config: EmailBridgeConfig, host: Any, runner: Any, artifact_store: Any) -> None:
        self._cfg = config
        self._host = host
        self._runner = runner
        self._artifact_store = artifact_store

        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def state_path(self) -> Path:
        return Path(self._cfg.state_dir) / "state.json"

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        if not self._cfg.imap_host or not self._cfg.imap_username:
            raise ValueError("Email bridge is enabled but IMAP host/username are missing (ABSTRACT_EMAIL_IMAP_HOST/USERNAME)")

        self._load_state()

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="email-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=3.0)
            except Exception:
                pass
        self._thread = None

    # ---------------------------------------------------------------------
    # State (cursor + optional bindings)
    # ---------------------------------------------------------------------

    def _load_state(self) -> None:
        path = self.state_path
        try:
            if path.exists():
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    self._state = obj
        except Exception:
            self._state = {}
        self._state.setdefault("version", 1)
        self._state.setdefault("cursors", {})
        self._state.setdefault("bindings", {})  # session_id -> {run_id, ...}

    def _save_state(self) -> None:
        path = self.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def _cursor_key(self) -> str:
        # Keyed by account+folder so multiple bridges/accounts can share a state dir safely.
        account = _safe_id_component(self._cfg.account)
        folder = _safe_id_component(self._cfg.imap_folder, max_len=64)
        return f"{account}:{folder}"

    def _get_last_uid(self) -> int:
        cursors = self._state.get("cursors")
        if not isinstance(cursors, dict):
            return 0
        entry = cursors.get(self._cursor_key())
        if isinstance(entry, dict):
            raw = entry.get("last_uid")
        else:
            raw = entry
        try:
            return int(raw or 0)
        except Exception:
            return 0

    def _set_last_uid(self, uid: int) -> None:
        cursors = self._state.setdefault("cursors", {})
        if not isinstance(cursors, dict):
            self._state["cursors"] = {}
            cursors = self._state["cursors"]
        cursors[self._cursor_key()] = {"last_uid": int(uid), "updated_at": _utc_now_iso()}

    def _binding_for_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        b = self._state.get("bindings")
        if not isinstance(b, dict):
            return None
        entry = b.get(str(session_id))
        return entry if isinstance(entry, dict) else None

    def _ensure_binding(self, *, session_id: str, thread_key: str) -> Optional[Dict[str, Any]]:
        if not self._cfg.autostart_flow_id:
            return None
        with self._lock:
            existing = self._binding_for_session(session_id)
            if isinstance(existing, dict):
                return existing

            try:
                run_id = self._host.start_run(
                    flow_id=self._cfg.autostart_flow_id,
                    bundle_id=self._cfg.autostart_bundle_id,
                    input_data={"email": {"thread_key": thread_key, "session_id": session_id}},
                    actor_id="email",
                    session_id=session_id,
                )
            except Exception:
                return None

            binding = {
                "session_id": session_id,
                "thread_key": thread_key,
                "run_id": str(run_id),
                "flow_id": self._cfg.autostart_flow_id,
                "bundle_id": self._cfg.autostart_bundle_id,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }
            bindings = self._state.setdefault("bindings", {})
            if isinstance(bindings, dict):
                bindings[str(session_id)] = binding
            self._save_state()
            return binding

    # ---------------------------------------------------------------------
    # Polling loop
    # ---------------------------------------------------------------------

    def _resolve_password(self) -> tuple[Optional[str], Optional[str]]:
        name = str(self._cfg.imap_password_env_var or "").strip() or "EMAIL_PASSWORD"
        v = os.getenv(name)
        if v is None or not str(v).strip():
            return None, f"Missing IMAP password env var {name}"
        return str(v).strip(), None

    def _connect_imap(self) -> tuple[Optional[imaplib.IMAP4_SSL], Optional[str]]:
        password, err = self._resolve_password()
        if err is not None:
            return None, err
        try:
            client = imaplib.IMAP4_SSL(self._cfg.imap_host, int(self._cfg.imap_port))
            try:
                if getattr(client, "sock", None) is not None:
                    client.sock.settimeout(float(self._cfg.imap_timeout_s))  # type: ignore[attr-defined]
            except Exception:
                pass
            client.login(self._cfg.imap_username, password)
            typ, _ = client.select(self._cfg.imap_folder, readonly=True)
            if typ != "OK":
                try:
                    client.logout()
                except Exception:
                    pass
                return None, f"Failed to select mailbox: {self._cfg.imap_folder}"
            return client, None
        except Exception as e:
            return None, str(e)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # Best-effort: never crash the runner thread.
                pass
            self._stop.wait(timeout=float(self._cfg.poll_seconds))

    def poll_once(self) -> int:
        """Poll IMAP once and process new messages (deterministic; used by tests)."""
        if not self._cfg.enabled:
            return 0

        with self._lock:
            last_uid = self._get_last_uid()

        client, err = self._connect_imap()
        if err is not None or client is None:
            return 0

        processed = 0
        try:
            # Search new UIDs.
            search_query = f"UID {int(last_uid) + 1}:*"
            typ, data = client.uid("search", None, search_query)
            if typ != "OK" or not data:
                return 0
            raw_uids = data[0] if isinstance(data, list) and data else b""
            if not isinstance(raw_uids, (bytes, bytearray)):
                raw_uids = str(raw_uids).encode("utf-8", errors="replace")
            uids = [u.decode("utf-8", errors="replace") for u in bytes(raw_uids).split() if u]

            def _as_uid(u: str) -> int:
                try:
                    return int(str(u).strip())
                except Exception:
                    return -1

            uids_sorted = sorted({u for u in uids if _as_uid(u) > int(last_uid)}, key=_as_uid)
            if not uids_sorted:
                return 0

            limit = max(1, int(self._cfg.max_messages_per_poll))
            uids_sorted = uids_sorted[:limit]

            for uid in uids_sorted:
                uid_i = _as_uid(uid)
                if uid_i <= int(last_uid):
                    continue
                ok = self._process_uid(client, uid=uid, uid_i=uid_i)
                if ok:
                    processed += 1
                    last_uid = uid_i
                    with self._lock:
                        self._set_last_uid(uid_i)
                        self._save_state()
        finally:
            try:
                client.logout()
            except Exception:
                pass

        return processed

    def _process_uid(self, client: imaplib.IMAP4_SSL, *, uid: str, uid_i: int) -> bool:
        # Fetch full message bytes.
        typ, fetched = client.uid("fetch", str(uid), "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not fetched:
            return False

        raw_bytes: Optional[bytes] = None
        flags: list[str] = []
        for item in fetched:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta, payload = item[0], item[1]
            if isinstance(payload, (bytes, bytearray)) and payload:
                raw_bytes = bytes(payload)
            if isinstance(meta, (bytes, bytearray)):
                try:
                    flags_bytes = imaplib.ParseFlags(meta)
                    flags = [fb.decode("utf-8", errors="replace") for fb in flags_bytes]
                except Exception:
                    flags = []

        if raw_bytes is None:
            return False

        event_payload, session_id, thread_key = self._normalize_message(uid=str(uid), raw_bytes=raw_bytes, flags=flags)

        # Optional: start the per-thread workflow run.
        binding = self._ensure_binding(session_id=session_id, thread_key=thread_key)
        if isinstance(binding, dict):
            event_payload["binding"] = {"run_id": str(binding.get("run_id") or "")}

        self._runner.emit_event(
            name=self._cfg.event_name,
            session_id=session_id,
            scope="session",
            payload={"email": event_payload},
            client_id="email",
        )

        return True

    def _normalize_message(self, *, uid: str, raw_bytes: bytes, flags: list[str]) -> tuple[Dict[str, Any], str, str]:
        msg = email.message_from_bytes(raw_bytes)
        subject_v = _decode_mime_header(msg.get("Subject"))
        from_v = _decode_mime_header(msg.get("From"))
        to_v = _decode_mime_header(msg.get("To"))
        cc_v = _decode_mime_header(msg.get("Cc"))
        date_v = _decode_mime_header(msg.get("Date"))
        message_id = _decode_mime_header(msg.get("Message-ID"))

        in_reply_to = _parse_message_ids(msg.get("In-Reply-To"))
        references = _parse_message_ids(msg.get("References"))

        thread_root = ""
        if references:
            thread_root = references[0]
        elif in_reply_to:
            thread_root = in_reply_to[0]
        elif message_id:
            thread_root = message_id
        else:
            thread_root = f"{from_v}\n{subject_v}".strip() or uid

        thread_key = hashlib.sha256(str(thread_root).encode("utf-8")).hexdigest()[:16]

        account_key = _safe_id_component(self._cfg.account)
        prefix = str(self._cfg.session_prefix or "email:").strip() or "email:"
        if not prefix.endswith(":"):
            prefix = prefix + ":"
        session_id = f"{prefix}{account_key}:{thread_key}"

        rid = session_memory_owner_run_id(session_id)

        raw_meta: Optional[Dict[str, Any]] = None
        if self._cfg.store_raw_message:
            raw_cap = max(1, int(self._cfg.max_raw_bytes))
            raw_truncated = len(raw_bytes) > raw_cap
            raw_to_store = raw_bytes[:raw_cap] if raw_truncated else raw_bytes
            tags = {
                "kind": "email_raw",
                "source": "email_bridge",
                "session_id": session_id,
                "account": account_key,
                "mailbox": str(self._cfg.imap_folder or ""),
                "uid": str(uid),
                "thread_key": thread_key,
                "message_id": str(message_id or ""),
            }
            try:
                meta = self._artifact_store.store(bytes(raw_to_store), content_type="message/rfc822", run_id=str(rid), tags=tags)
                raw_meta = {
                    "artifact_id": str(getattr(meta, "artifact_id", "") or ""),
                    "blob_id": str(getattr(meta, "blob_id", "") or ""),
                    "size_bytes": int(getattr(meta, "size_bytes", 0) or 0),
                    "truncated": bool(raw_truncated),
                    "total_bytes": int(len(raw_bytes)),
                }
            except Exception:
                raw_meta = None

        body_text, body_html = _extract_text_bodies(msg)
        body_text = _clamp_text(body_text, max_chars=int(self._cfg.max_body_chars))
        body_html = _clamp_text(body_html, max_chars=int(self._cfg.max_html_chars))

        attachments: list[Dict[str, Any]] = []
        skipped_attachments: list[Dict[str, Any]] = []

        if self._cfg.store_attachments and msg.is_multipart():
            # Dedupe by (handle, sha256) within the session attachment registry.
            existing: list[Any]
            try:
                existing = self._artifact_store.list_by_run(str(rid)) or []
            except Exception:
                existing = []
            existing_index: Dict[Tuple[str, str], Any] = {}
            for m in existing:
                tags = getattr(m, "tags", None)
                if not isinstance(tags, dict):
                    continue
                if str(tags.get("kind") or "") != "attachment":
                    continue
                handle = str(tags.get("path") or "").strip()
                sha256 = str(tags.get("sha256") or "").strip().lower()
                if handle and sha256:
                    existing_index[(handle, sha256)] = m

            max_count = max(0, int(self._cfg.max_attachments))
            max_each = max(1, int(self._cfg.max_attachment_bytes))
            max_total = max(1, int(self._cfg.max_total_attachment_bytes))
            total = 0
            idx = 0

            for part in msg.walk():
                if part.is_multipart():
                    continue
                disp = part.get_content_disposition()
                filename = _decode_mime_header(part.get_filename())
                if disp != "attachment" and not filename:
                    continue

                idx += 1
                if max_count and len(attachments) >= max_count:
                    skipped_attachments.append({"reason": "max_attachments", "filename": filename or "", "content_type": str(part.get_content_type() or "")})
                    continue

                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                content = bytes(payload)
                if not content:
                    continue

                if len(content) > max_each:
                    skipped_attachments.append(
                        {
                            "reason": "max_attachment_bytes",
                            "filename": filename or "",
                            "content_type": str(part.get_content_type() or ""),
                            "size_bytes": int(len(content)),
                        }
                    )
                    continue

                if total + len(content) > max_total:
                    skipped_attachments.append(
                        {
                            "reason": "max_total_attachment_bytes",
                            "filename": filename or "",
                            "content_type": str(part.get_content_type() or ""),
                            "size_bytes": int(len(content)),
                        }
                    )
                    continue

                filename2 = filename or f"attachment_{idx}"
                handle = f"email/{account_key}/{thread_key}/{filename2}"
                sha256 = hashlib.sha256(content).hexdigest()

                existing_meta = existing_index.get((handle, sha256))
                if existing_meta is not None:
                    attachments.append(
                        {
                            "artifact_id": str(getattr(existing_meta, "artifact_id", "") or ""),
                            "blob_id": str(getattr(existing_meta, "blob_id", "") or ""),
                            "sha256": sha256,
                            "handle": handle,
                            "filename": filename2,
                            "content_type": str(getattr(existing_meta, "content_type", "") or str(part.get_content_type() or "")),
                            "size_bytes": int(getattr(existing_meta, "size_bytes", 0) or len(content)),
                            "deduped": True,
                        }
                    )
                    total += len(content)
                    continue

                tags2 = {
                    "kind": "attachment",
                    "source": "email",
                    "path": handle,
                    "filename": filename2,
                    "session_id": session_id,
                    "sha256": sha256,
                    "email_uid": str(uid),
                    "thread_key": thread_key,
                }
                try:
                    meta2 = self._artifact_store.store(content, content_type=str(part.get_content_type() or "application/octet-stream"), run_id=str(rid), tags=tags2)
                except Exception:
                    skipped_attachments.append(
                        {"reason": "store_failed", "filename": filename2, "content_type": str(part.get_content_type() or ""), "size_bytes": int(len(content))}
                    )
                    continue

                attachments.append(
                    {
                        "artifact_id": str(getattr(meta2, "artifact_id", "") or ""),
                        "blob_id": str(getattr(meta2, "blob_id", "") or ""),
                        "sha256": sha256,
                        "handle": handle,
                        "filename": filename2,
                        "content_type": str(getattr(meta2, "content_type", "") or ""),
                        "size_bytes": int(getattr(meta2, "size_bytes", 0) or 0),
                        "deduped": False,
                    }
                )
                total += len(content)

        payload: Dict[str, Any] = {
            "bridge": {"version": 1, "received_at": _utc_now_iso()},
            "account": account_key,
            "mailbox": str(self._cfg.imap_folder or ""),
            "uid": str(uid),
            "message_id": message_id,
            "thread_key": thread_key,
            "thread_root_message_id": thread_root,
            "in_reply_to": in_reply_to,
            "references": references,
            "from": from_v,
            "to": to_v,
            "cc": cc_v,
            "subject": subject_v,
            "date": date_v,
            "flags": list(flags),
            "seen": any(str(f).lstrip("\\").lower() == "seen" for f in flags or []),
            "body_text": body_text,
            "body_html": body_html,
            "artifacts": {"raw": raw_meta, "attachments": attachments, "skipped_attachments": skipped_attachments},
        }

        return payload, session_id, thread_key

