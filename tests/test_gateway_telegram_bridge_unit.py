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
            "chat": {"id": 99},
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
            "chat": {"id": 99},
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
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99}, "from": {"id": 7}, "text": "hi"},
        }
    )
    assert host.starts[0]["session_id"] == "telegram:99"

    # Reset clears the binding and advances the session revision.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 2,
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99}, "from": {"id": 7}, "text": "/reset"},
        }
    )

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 3,
            "message": {"message_id": 12, "date": 1700000002, "chat": {"id": 99}, "from": {"id": 7}, "text": "who are you?"},
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
                "chat": {"id": 99},
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
            "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99}, "from": {"id": 7}, "text": "who are you?"},
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
                "chat": {"id": 99},
                "from": {"id": 7},
                "document": {"file_id": "file456", "file_name": "b.txt", "mime_type": "text/plain"},
            },
        }
    )
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 4,
            "message": {"message_id": 13, "date": 1700000003, "chat": {"id": 99}, "from": {"id": 7}, "text": "what is this?"},
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
                "chat": {"id": 99},
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
                "chat": {"id": 99},
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
    )
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
            "update_id": 1,
            "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99}, "from": {"id": 7, "is_bot": True}, "text": "hi"},
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
                "chat": {"id": 99},
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
