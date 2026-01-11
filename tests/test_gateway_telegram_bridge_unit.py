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
    assert host.starts[0]["actor_id"] == "telegram"
    assert len(runner.emits) == 1
    assert runner.emits[0].name == "telegram.message"
    assert runner.emits[0].session_id == "telegram:99"

    payload = runner.emits[0].payload
    assert isinstance(payload, dict)
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
