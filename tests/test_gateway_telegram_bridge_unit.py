from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

    def start_run(self, *, flow_id: str, bundle_id: Optional[str], input_data: Dict[str, Any], actor_id: str, session_id: str) -> str:
        self.starts.append(
            {
                "flow_id": flow_id,
                "bundle_id": bundle_id,
                "input_data": input_data,
                "actor_id": actor_id,
                "session_id": session_id,
            }
        )
        return "run-1"


def test_telegram_bridge_creates_binding_stores_media_and_emits_event(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=True,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # best-effort: avoid starting the polling thread

    # Patch download to avoid network.
    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]

    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1700000000,
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 7},
            "text": "hi",
            "document": {"file_id": "file123", "file_name": "a.txt", "mime_type": "text/plain"},
        },
    }
    bridge._handle_bot_update(update)  # type: ignore[attr-defined]

    assert len(host.starts) == 1
    assert host.starts[0]["session_id"] == "telegram:99"
    assert host.starts[0]["actor_id"] == "gateway"
    assert len(runner.emits) == 1
    assert runner.emits[0].name == "telegram.message"
    assert runner.emits[0].session_id == "telegram:99"

    payload = runner.emits[0].payload
    assert isinstance(payload, dict)
    assert "attachments" in payload
    tg = payload.get("telegram")
    assert isinstance(tg, dict)
    media = tg.get("media")
    assert isinstance(media, list)
    assert media and isinstance(media[0], dict)
    assert "artifact_id" in media[0]

    # Second message in same chat must not start a new run.
    update2 = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "date": 1700000001,
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 7},
            "text": "hi again",
        },
    }
    bridge._handle_bot_update(update2)  # type: ignore[attr-defined]
    assert len(host.starts) == 1
    assert len(runner.emits) == 2


def test_telegram_bridge_start_bot_api_requires_token(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    monkeypatch.delenv("TEST_TELEGRAM_BOT_TOKEN", raising=False)

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        bot_token_env_var="TEST_TELEGRAM_BOT_TOKEN",
        store_media=False,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)

    with pytest.raises(ValueError, match="TEST_TELEGRAM_BOT_TOKEN"):
        bridge.start()


def test_telegram_bridge_reset_bumps_session_and_starts_new_run(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "hi"},
        }
    )
    assert host.starts[0]["session_id"] == "telegram:99"

    # Reset clears the binding and advances the session revision.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "/reset"},
        }
    )

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 3,
            "message": {"message_id": 12, "date": 1700000002, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "who are you?"},
        }
    )
    assert len(host.starts) == 2
    assert host.starts[1]["session_id"] == "telegram:99:r1"
    assert runner.emits[-1].session_id == "telegram:99:r1"


def test_telegram_bridge_pending_media_only_applies_when_referenced(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=True,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]

    # Media-only message stashes pending media for a follow-up.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1700000000,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 7},
                "document": {"file_id": "file123", "file_name": "a.txt", "mime_type": "text/plain"},
            },
        }
    )
    assert "attachments" in runner.emits[-1].payload

    # Unrelated follow-up text must NOT inherit the pending media.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "who are you?"},
        }
    )
    assert runner.emits[-1].payload.get("attachments") == []

    # Stash media again, then reference it explicitly.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "date": 1700000002,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 7},
                "document": {"file_id": "file456", "file_name": "b.txt", "mime_type": "text/plain"},
            },
        }
    )
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 4,
            "message": {"message_id": 13, "date": 1700000003, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "what is this?"},
        }
    )
    assert "attachments" in runner.emits[-1].payload


def test_telegram_bridge_pending_media_applies_when_replying_to_media_message(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=True,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1700000000,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 7},
                "document": {"file_id": "file123", "file_name": "a.txt", "mime_type": "text/plain"},
            },
        }
    )

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "date": 1700000001,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 7},
                "text": "describe",
                "reply_to_message": {"message_id": 10},
            },
        }
    )
    assert "attachments" in runner.emits[-1].payload


def test_telegram_bridge_ignores_bot_messages(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "is_bot": True}, "text": "hi"},
        }
    )
    assert host.starts == []
    assert runner.emits == []


def test_telegram_bridge_tools_command_is_consumed_and_persists_policy(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1700000000,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 7},
                "text": "/tools allow read_file",
            },
        }
    )

    assert host.starts == []
    assert runner.emits == []
    assert sent and isinstance(sent[0].get("text"), str)

    pol = bridge._state.get("tool_policies", {}).get("99")  # type: ignore[attr-defined]
    assert isinstance(pol, dict)
    allowed = pol.get("allowed_tools")
    assert isinstance(allowed, list)
    assert "read_file" in allowed


def test_telegram_bridge_tool_approval_response_restarts_typing_and_checks_next_wait(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset({7}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    # Fake binding so owner checks pass.
    bridge._state.setdefault("bindings", {})["99"] = {"owner_user_id": 7}  # type: ignore[attr-defined]

    calls: list[str] = []
    bridge._start_typing_loop = lambda *a, **k: calls.append("start_typing")  # type: ignore[attr-defined]
    bridge._maybe_handle_tool_wait = lambda *a, **k: calls.append("maybe_tool_wait") or False  # type: ignore[attr-defined]
    bridge._execute_tool_calls_with_policy = lambda *a, **k: {"mode": "executed", "results": []}  # type: ignore[attr-defined]
    bridge._merge_tool_wait_results = lambda *, pending, host_payload: host_payload  # type: ignore[attr-defined]
    bridge._resume_tool_wait = lambda *a, **k: calls.append("resume")  # type: ignore[attr-defined]
    bridge._send_text = lambda *a, **k: None  # type: ignore[attr-defined]
    bridge._effective_tool_policy = lambda *a, **k: {"approve_all": False, "allowed_tools": None, "blocked_tools": [], "auto_approve_tools": [], "require_approval_tools": []}  # type: ignore[attr-defined]

    pending1 = {"run_id": "run-1", "wait_key": "wk1", "tool_calls": [{"name": "write_file", "arguments": {}}], "details": {"mode": "approval_required", "tool_calls": []}}
    pending2 = {"run_id": "run-2", "wait_key": "wk2", "tool_calls": [{"name": "send_telegram_message", "arguments": {"chat_id": 99, "text": "hi"}}], "details": {"mode": "approval_required", "tool_calls": []}}
    pending_seq = [pending1, pending2]
    bridge._find_pending_tool_approval = lambda *a, **k: pending_seq.pop(0) if pending_seq else None  # type: ignore[attr-defined]

    ok = bridge._handle_tool_approval_response(chat_id=99, session_id="telegram:99", from_user_id=7, text="/approve")  # type: ignore[attr-defined]
    assert ok is True
    assert "resume" in calls
    assert "start_typing" in calls
    assert "maybe_tool_wait" in calls


def test_telegram_bridge_access_control_whoami_bypasses_allowlist(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="allowlist",
        allowed_users=frozenset(),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/whoami"},
        }
    )
    assert host.starts == []
    assert runner.emits == []
    assert sent and sent[-1]["chat_id"] == 7

    sent.clear()
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "hi"},
        }
    )
    assert host.starts == []
    assert runner.emits == []
    assert sent == []


def test_telegram_bridge_access_control_pairing_flow(tmp_path) -> None:
    import re

    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="pairing",
        pairing_ttl_s=3600.0,
        admin_users=frozenset({1}),
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    # Unknown user triggers pairing request (no run/event).
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "hello"},
        }
    )
    assert host.starts == []
    assert runner.emits == []
    assert sent and sent[-1]["chat_id"] == 7
    m = re.search(r"Pairing code:\s*([A-Z0-9]+)", str(sent[-1]["text"]))
    assert m, f"expected pairing code in: {sent[-1]['text']}"
    code = m.group(1)

    sent.clear()
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": f"/pair approve {code}"},
        }
    )
    assert host.starts == []
    assert runner.emits == []
    assert any(d.get("chat_id") == 1 and "Approved user_id=7" in str(d.get("text")) for d in sent)
    assert any(d.get("chat_id") == 7 and "Approved" in str(d.get("text")) for d in sent)

    # Now the paired user can start a run.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 3,
            "message": {"message_id": 12, "date": 1700000002, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "hi again"},
        }
    )
    assert len(host.starts) == 1
    assert len(runner.emits) == 1


def test_telegram_bridge_group_requires_mention_by_default(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        dm_policy="disabled",
        group_policy="allowlist",
        allowed_chats=frozenset({-100}),
        group_allowed_users=frozenset(),  # no sender restriction
        require_mention_in_groups=True,
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    # Avoid network calls: seed the cached bot username used by mention checks.
    bridge._bot_username = "mybot"  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": -100, "type": "group"}, "from": {"id": 7}, "text": "hello"},
        }
    )
    assert host.starts == []
    assert runner.emits == []

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": -100, "type": "group"}, "from": {"id": 7}, "text": "@mybot hello"},
        }
    )
    assert len(host.starts) == 1
    assert len(runner.emits) == 1


def test_telegram_bridge_tdlib_reset_accepts_message_id(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",  # avoid TDLib dependency for this unit test
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
        typing_max_s=0.0,
        reset_delete_messages=False,
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_tdlib_reset(chat_id=1, message_id=10)  # type: ignore[attr-defined]
    assert sent and sent[-1]["chat_id"] == 1


def test_telegram_bridge_tdlib_extract_media_is_method(tmp_path) -> None:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    host = _FakeHost()
    runner = _FakeRunner()
    artifacts = InMemoryArtifactStore()

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        event_name="telegram.message",
        session_prefix="telegram:",
        flow_id="bundle:telegram_agent",
        bundle_id=None,
        state_path=tmp_path / "tg_state.json",
        store_media=False,
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    assert callable(getattr(bridge, "_extract_tdlib_media", None))
