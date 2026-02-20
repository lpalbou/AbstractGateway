from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


class _FakeRunner:
    def __init__(self, *, run_store: InMemoryRunStore, ledger_store: InMemoryLedgerStore, artifact_store: InMemoryArtifactStore) -> None:
        self.run_store = run_store
        self.ledger_store = ledger_store
        self.artifact_store = artifact_store
        self.controls: list[dict[str, Any]] = []

    def apply_run_control(self, typ: str, *, run_id: str, payload: Dict[str, Any], apply_to_tree: bool = False) -> None:
        self.controls.append({"typ": str(typ), "run_id": str(run_id), "payload": dict(payload or {}), "apply_to_tree": bool(apply_to_tree)})


class _SequencedHost:
    def __init__(self) -> None:
        self.starts: List[Dict[str, Any]] = []
        self._i = 0

    def start_run(self, *, flow_id: str, bundle_id: Optional[str], input_data: Dict[str, Any], actor_id: str, session_id: str) -> str:
        self._i += 1
        rid = f"run-{self._i}"
        self.starts.append(
            {
                "flow_id": flow_id,
                "bundle_id": bundle_id,
                "input_data": input_data,
                "actor_id": actor_id,
                "session_id": session_id,
                "run_id": rid,
            }
        )
        return rid


def _make_cfg(*, tmp_path: Path, **overrides: Any) -> Any:
    from abstractgateway.integrations.telegram_bridge import TelegramBridgeConfig

    base: dict[str, Any] = {
        "enabled": True,
        "transport": "bot_api",
        "session_prefix": "telegram:",
        "flow_id": "81795ea9",
        "bundle_id": "basic-agent",
        "state_path": tmp_path / "tg_state.json",
        "store_media": False,
        "typing_max_s": 0.0,
        "dm_policy": "allowlist",
        "allowed_users": frozenset({7}),
    }
    base.update(overrides)
    return TelegramBridgeConfig(**base)


def _make_bridge(*, tmp_path: Path, host: Any, runner: Any, artifacts: InMemoryArtifactStore, **cfg_overrides: Any) -> Any:
    from abstractgateway.integrations.telegram_bridge import TelegramBridge

    cfg = _make_cfg(tmp_path=tmp_path, **cfg_overrides)
    bridge = TelegramBridge(config=cfg, host=host, runner=runner, artifact_store=artifacts)
    bridge._load_state()  # type: ignore[attr-defined]
    return bridge


def test_telegram_bridge_stores_media_and_starts_run(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, store_media=True)

    # Patch download to avoid network.
    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {
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
    )

    assert len(host.starts) == 1
    call = host.starts[0]
    assert call["actor_id"] == "gateway"
    assert call["session_id"] == "telegram:99"

    input_data = call["input_data"]
    assert isinstance(input_data, dict)
    assert input_data.get("prompt") == "hi"
    assert input_data.get("use_context") is True

    tg = input_data.get("telegram")
    assert isinstance(tg, dict)
    assert tg.get("chat_id") == 99
    assert isinstance(tg.get("media"), list)
    assert tg["media"] and isinstance(tg["media"][0], dict)
    assert str(tg["media"][0].get("artifact_id") or "").strip()

    attachments = input_data.get("attachments")
    assert isinstance(attachments, list) and attachments
    assert attachments[0].get("artifact_id") == tg["media"][0].get("artifact_id")

    ctx = input_data.get("context")
    assert isinstance(ctx, dict)
    msgs = ctx.get("messages")
    assert isinstance(msgs, list) and msgs
    assert msgs[-1].get("role") == "user"
    assert msgs[-1].get("content") == "hi"


def test_telegram_bridge_multi_turn_history_and_delivery(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    runner = _FakeRunner(run_store=run_store, ledger_store=ledger_store, artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts)

    sent: list[tuple[int, str]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append((int(chat_id), str(text)))  # type: ignore[attr-defined]

    # Turn 1.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "hi"}}
    )
    assert len(host.starts) == 1
    rid1 = str(host.starts[0].get("run_id") or "")
    assert rid1 == "run-1"

    run_store.save(
        RunState(
            run_id=rid1,
            workflow_id="wf",
            status=RunStatus.COMPLETED,
            current_node="done",
            vars={},
            waiting=None,
            output={"response": "hello from run-1"},
            error=None,
            actor_id="gateway",
            session_id="telegram:99",
        )
    )
    assert bridge._maybe_deliver_thin_run_output(chat_id=99, session_id="telegram:99") is True  # type: ignore[attr-defined]
    assert sent and sent[-1][1] == "hello from run-1"

    # Turn 2: next message starts a new run with prior transcript in context.messages.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 2, "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "again"}}
    )
    assert len(host.starts) == 2
    input2 = host.starts[1]["input_data"]
    ctx2 = input2.get("context")
    assert isinstance(ctx2, dict)
    msgs2 = ctx2.get("messages")
    assert isinstance(msgs2, list)
    assert [m.get("role") for m in msgs2] == ["user", "assistant", "user"]
    assert msgs2[0].get("content") == "hi"
    assert msgs2[1].get("content") == "hello from run-1"
    assert msgs2[2].get("content") == "again"


def test_telegram_bridge_start_bot_api_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_TELEGRAM_BOT_TOKEN", raising=False)

    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(
        tmp_path=tmp_path,
        host=host,
        runner=runner,
        artifacts=artifacts,
        bot_token_env_var="TEST_TELEGRAM_BOT_TOKEN",
    )

    with pytest.raises(ValueError, match="TEST_TELEGRAM_BOT_TOKEN"):
        bridge.start()


def test_telegram_bridge_reset_bumps_session_and_starts_new_run(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts)
    bridge._send_text = lambda *, chat_id, text: None  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "hi"}}
    )
    assert host.starts and host.starts[0]["session_id"] == "telegram:99"

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 2, "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "/reset"}}
    )

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 3, "message": {"message_id": 12, "date": 1700000002, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "who are you?"}}
    )
    assert len(host.starts) == 2
    assert host.starts[1]["session_id"] == "telegram:99:r1"


def test_telegram_bridge_pending_media_only_applies_when_referenced(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, store_media=True)
    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    # Media-only message stashes pending media (no run started).
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
    assert host.starts == []
    assert sent and "attachment" in str(sent[-1]["text"]).lower()

    # Unrelated follow-up text must NOT inherit pending media.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 2, "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "who are you?"}}
    )
    assert len(host.starts) == 1
    assert host.starts[0]["input_data"].get("attachments") in (None, [])

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
        {"update_id": 4, "message": {"message_id": 13, "date": 1700000003, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "what is this?"}}
    )
    assert len(host.starts) == 2
    assert isinstance(host.starts[1]["input_data"].get("attachments"), list)
    assert host.starts[1]["input_data"]["attachments"]


def test_telegram_bridge_pending_media_applies_when_replying_to_media_message(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, store_media=True)
    bridge._bot_download_file = lambda *, file_id, timeout_s=30.0: (b"hello", {"file_id": file_id}, None)  # type: ignore[attr-defined]
    bridge._bot_send_text = lambda *, chat_id, text: None  # type: ignore[attr-defined]

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
    assert len(host.starts) == 1
    assert isinstance(host.starts[0]["input_data"].get("attachments"), list)
    assert host.starts[0]["input_data"]["attachments"]


def test_telegram_bridge_ignores_bot_messages(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts)

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7, "is_bot": True}, "text": "hi"}}
    )
    assert host.starts == []


def test_telegram_bridge_tools_command_is_consumed_and_does_not_start_runs(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts)

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 99, "type": "private"}, "from": {"id": 7}, "text": "/tools allow read_file"}}
    )

    assert host.starts == []
    assert sent and isinstance(sent[0].get("text"), str)


def test_telegram_bridge_tool_approval_response_resumes_wait(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts)
    bridge._state.setdefault("bindings", {})["99"] = {"owner_user_id": 7, "session_id": "telegram:99"}  # type: ignore[attr-defined]

    # Avoid network; capture resume.
    calls: list[tuple[str, str, dict[str, Any]]] = []
    bridge._send_text = lambda *, chat_id, text: None  # type: ignore[attr-defined]
    bridge._start_typing_loop = lambda *a, **k: None  # type: ignore[attr-defined]

    def _resume_wait(*, run_id: str, wait_key: str, payload: Dict[str, Any]) -> bool:
        calls.append((str(run_id), str(wait_key), dict(payload)))
        return True

    bridge._resume_wait = _resume_wait  # type: ignore[attr-defined]
    bridge._find_pending_tool_approval = lambda *a, **k: {"run_id": "run-1", "wait_key": "wk1", "tool_calls": [{"name": "write_file", "arguments": {}}], "details": {"mode": "approval_required"}}  # type: ignore[attr-defined]

    ok = bridge._handle_tool_approval_response(chat_id=99, session_id="telegram:99", from_user_id=7, text="/approve")  # type: ignore[attr-defined]
    assert ok is True
    assert calls and calls[-1][2].get("approved") is True


def test_telegram_bridge_access_control_whoami_bypasses_allowlist(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, allowed_users=frozenset())

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/whoami"}}
    )
    assert host.starts == []
    assert sent and sent[-1].get("chat_id") == 7


def test_telegram_bridge_access_control_pairing_flow(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(
        tmp_path=tmp_path,
        host=host,
        runner=runner,
        artifacts=artifacts,
        dm_policy="pairing",
        allowed_users=frozenset(),
        admin_users=frozenset({1}),
    )

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    # Unknown user triggers pairing request (no run started).
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "hello"}}
    )
    assert host.starts == []
    assert sent and sent[-1]["chat_id"] == 7
    m = re.search(r"Pairing code:\s*([A-Z0-9]+)", str(sent[-1]["text"]))
    assert m, f"expected pairing code in: {sent[-1]['text']}"
    code = m.group(1)

    sent.clear()
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 2, "message": {"message_id": 11, "date": 1700000001, "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": f"/pair approve {code}"}}
    )
    assert any(d.get("chat_id") == 1 and "Approved user_id=7" in str(d.get("text")) for d in sent)
    assert any(d.get("chat_id") == 7 and "Approved" in str(d.get("text")) for d in sent)

    # Now the paired user can start a run.
    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 3, "message": {"message_id": 12, "date": 1700000002, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "hi again"}}
    )
    assert len(host.starts) == 1


def test_telegram_bridge_group_requires_mention_by_default(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(
        tmp_path=tmp_path,
        host=host,
        runner=runner,
        artifacts=artifacts,
        dm_policy="disabled",
        group_policy="allowlist",
        allowed_chats=frozenset({-100}),
        group_allowed_users=frozenset(),  # no sender restriction
        require_mention_in_groups=True,
    )

    # Avoid network calls: seed the cached bot username used by mention checks.
    bridge._bot_username = "mybot"  # type: ignore[attr-defined]

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 1, "message": {"message_id": 10, "date": 1700000000, "chat": {"id": -100, "type": "group"}, "from": {"id": 7}, "text": "hello"}}
    )
    assert host.starts == []

    bridge._handle_bot_update(  # type: ignore[attr-defined]
        {"update_id": 2, "message": {"message_id": 11, "date": 1700000001, "chat": {"id": -100, "type": "group"}, "from": {"id": 7}, "text": "@mybot hello"}}
    )
    assert len(host.starts) == 1
    assert host.starts[0]["session_id"] == "telegram:-100"


def test_telegram_bridge_tdlib_reset_accepts_message_id(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, transport="bot_api", reset_delete_messages=False)

    sent: list[dict[str, object]] = []
    bridge._bot_send_text = lambda *, chat_id, text: sent.append({"chat_id": chat_id, "text": text})  # type: ignore[attr-defined]

    bridge._handle_tdlib_reset(chat_id=1, message_id=10)  # type: ignore[attr-defined]
    assert sent and sent[-1]["chat_id"] == 1


def test_telegram_bridge_tdlib_extract_media_is_method(tmp_path: Path) -> None:
    host = _SequencedHost()
    artifacts = InMemoryArtifactStore()
    runner = _FakeRunner(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=artifacts)

    bridge = _make_bridge(tmp_path=tmp_path, host=host, runner=runner, artifacts=artifacts, transport="bot_api")
    assert callable(getattr(bridge, "_extract_tdlib_media", None))
