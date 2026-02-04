from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import re
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from abstractruntime.storage.artifacts import InMemoryArtifactStore


@dataclass
class _EmitCall:
    name: str
    session_id: str
    payload: Any


class _FakeRunner:
    def __init__(self) -> None:
        self.emits: List[_EmitCall] = []

    def emit_event(
        self,
        *,
        name: str,
        payload: Any,
        session_id: str,
        scope: str = "session",
        workflow_id: Optional[str] = None,
        run_id: Optional[str] = None,
        event_id: Optional[str] = None,
        emitted_at: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> None:
        del scope, workflow_id, run_id, event_id, emitted_at, client_id
        self.emits.append(_EmitCall(name=name, session_id=session_id, payload=payload))


class _FakeHost:
    def __init__(self) -> None:
        self.starts: List[Dict[str, Any]] = []
        self._count = 0

    def start_run(self, *, flow_id: str, bundle_id: Optional[str], input_data: Dict[str, Any], actor_id: str, session_id: str) -> str:
        self._count += 1
        self.starts.append(
            {
                "flow_id": flow_id,
                "bundle_id": bundle_id,
                "input_data": input_data,
                "actor_id": actor_id,
                "session_id": session_id,
            }
        )
        return f"run-{self._count}"


class _FakeImap:
    def __init__(self, messages_by_uid: Dict[str, bytes]) -> None:
        self._msgs = dict(messages_by_uid)
        self.sock = MagicMock()

    def login(self, *_: object, **__: object) -> tuple[str, list[bytes]]:
        return ("OK", [b""])

    def select(self, *_: object, **__: object) -> tuple[str, list[bytes]]:
        return ("OK", [b""])

    def uid(self, command: str, *_: object) -> tuple[str, list[object]]:
        if command.lower() == "search":
            criteria = str(_[-1]) if _ else ""
            m = re.search(r"UID\\s+(\\d+):\\*", criteria)
            start = int(m.group(1)) if m else 1
            uids = [u for u in sorted(self._msgs.keys(), key=int) if int(u) >= start]
            return ("OK", [" ".join(uids).encode("utf-8") if uids else b""])

        if command.lower() == "fetch":
            uid = str(_[0]) if _ else ""
            raw = self._msgs.get(uid)
            if raw is None:
                return ("OK", [])
            meta = b'1 (FLAGS (\\\\Seen) BODY[] {0}'
            return ("OK", [(meta, raw)])

        raise AssertionError(f"Unexpected command: {command}")

    def logout(self) -> None:
        return None


def _make_msg(*, message_id: str, subject: str, references: Optional[str] = None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@example.com"
    msg["Message-ID"] = message_id
    if references:
        msg["References"] = references
    msg.set_content("Plain body")
    msg.add_attachment(b"hello", maintype="text", subtype="plain", filename="a.txt")
    return msg.as_bytes()


def test_email_bridge_polls_imap_stores_artifacts_and_emits_events(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.integrations.email_bridge import EmailBridge, EmailBridgeConfig

    monkeypatch.setenv("EMAIL_PASSWORD", "pw")

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = EmailBridgeConfig(
        enabled=True,
        event_name="email.message",
        session_prefix="email:",
        account="me@example.com",
        imap_host="imap.example.com",
        imap_username="me@example.com",
        imap_password_env_var="EMAIL_PASSWORD",
        imap_folder="INBOX",
        poll_seconds=60.0,
        autostart_flow_id="bundle:email_agent",
        autostart_bundle_id=None,
        state_dir=tmp_path / "email_bridge",
    )
    bridge = EmailBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    msg1 = _make_msg(message_id="<m1@example.com>", subject="Hello")
    msg2 = _make_msg(message_id="<m2@example.com>", subject="Re: Hello", references="<m1@example.com>")

    fake_imap = _FakeImap({"1": msg1, "2": msg2})
    with patch("imaplib.IMAP4_SSL", return_value=fake_imap):
        n = bridge.poll_once()
        assert n == 2

    assert len(host.starts) == 1  # autostart creates only one binding for the thread
    assert len(runner.emits) == 2
    assert runner.emits[0].name == "email.message"
    assert runner.emits[0].session_id == runner.emits[1].session_id

    payload0 = runner.emits[0].payload
    assert isinstance(payload0, dict)
    email0 = payload0.get("email")
    assert isinstance(email0, dict)
    artifacts0 = email0.get("artifacts")
    assert isinstance(artifacts0, dict)
    raw = artifacts0.get("raw")
    assert raw is None or isinstance(raw, dict)
    atts = artifacts0.get("attachments")
    assert isinstance(atts, list) and atts
    assert isinstance(atts[0], dict) and atts[0].get("artifact_id")

    # Second poll should produce no new events (cursor persisted).
    with patch("imaplib.IMAP4_SSL", return_value=fake_imap):
        n2 = bridge.poll_once()
        assert n2 == 0
        assert len(runner.emits) == 2

