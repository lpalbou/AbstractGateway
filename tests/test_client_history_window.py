"""Client-sent chat histories go through the runtime's ONE history window.

ADR-0026 + operator ruling 2026-09-28: no message-count or character caps;
models use their full context; replayed history is the newest WHOLE messages
up to 50,000 tokens, explicit and recorded. Routes that take a client-sent
`messages` list (run chat, backlog assist / maintain / advisor) bound it with
`abstractruntime.session_history.fold_history_window` and return the window's
receipt as `history`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractruntime.session_history import HISTORY_REPLAY_MAX_TOKENS

from test_backlog_advisor_and_maintain_readonly_scope import _Config, _Host, _make_app, _Runner, _Service
from test_gateway_http_api import _write_test_bundle


def _conversation(turns: int, *, answer_chars: int = 40) -> List[Dict[str, str]]:
    """`turns` user/assistant pairs, then the question being asked."""
    out: List[Dict[str, str]] = []
    for i in range(turns):
        out.append({"role": "user", "content": f"question-{i}"})
        out.append({"role": "assistant", "content": f"answer-{i} " + "x" * answer_chars})
    out.append({"role": "user", "content": "the-newest-question"})
    return out


# ---------------------------------------------------------------- the helper


@pytest.mark.basic
def test_short_history_is_replayed_whole_with_no_message_or_char_cap() -> None:
    from abstractgateway.routes.gateway import _client_history_window

    history = _conversation(100)  # 201 messages: far above the old 20/40 caps
    history[3]["content"] = "long-answer " + "y" * 40_000  # far above the old 8k/12k clamps
    messages, report = _client_history_window(history)

    assert messages == history
    assert report["replayed_messages"] == 201
    assert report["dropped_messages"] == 0
    assert report["max_tokens"] == HISTORY_REPLAY_MAX_TOKENS


@pytest.mark.basic
def test_history_above_the_window_drops_oldest_whole_turns_and_says_so() -> None:
    from abstractgateway.routes.gateway import _client_history_window

    history = _conversation(40, answer_chars=8_000)  # ~80k estimated tokens
    messages, report = _client_history_window(history)

    assert report["dropped_messages"] > 0
    assert report["replayed_tokens"] <= HISTORY_REPLAY_MAX_TOKENS
    assert report["replayed_messages"] + report["dropped_messages"] == len(history)
    assert len(messages) == report["replayed_messages"]
    # The question being asked is always kept, whole and last.
    assert messages[-1] == {"role": "user", "content": "the-newest-question"}
    # Whole turns: the window starts on a user message (a reply never loses its question) ...
    assert messages[0]["role"] == "user"
    # ... carrying the runtime's labeled notice, and the kept messages are uncut.
    assert messages[0]["content"].startswith("[#TRUNCATION: ")
    first_kept = len(history) - len(messages)
    assert messages[0]["content"].endswith("\n" + history[first_kept]["content"])
    assert messages[1:] == history[first_kept + 1 :]


@pytest.mark.basic
def test_an_oversize_newest_question_is_kept_whole() -> None:
    from abstractgateway.routes.gateway import _client_history_window

    big = "z" * (HISTORY_REPLAY_MAX_TOKENS * 6)
    messages, report = _client_history_window([{"role": "user", "content": "old"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": big}])

    assert report["oversize_turn_kept"] is True
    assert report["dropped_messages"] == 2
    assert messages[-1]["content"].endswith(big)


@pytest.mark.basic
def test_generate_chat_text_sends_every_message_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-message 12,000-char clamp on the run-chat prompt is gone."""
    import abstractgateway.routes.gateway as gateway_routes

    seen: Dict[str, Any] = {}

    class _Llm:
        def generate(self, **kwargs: Any) -> Dict[str, Any]:
            seen.update(kwargs)
            return {"content": "ok"}

    monkeypatch.setattr(gateway_routes, "_gateway_llm_client", lambda *_a, **_k: (_Llm(), None, None))
    history = _conversation(30)
    history[1]["content"] = "w" * 40_000
    gateway_routes._generate_chat_text(provider="p", model="m", context={"run_id": "r"}, messages=history)

    assert seen["messages"][1:] == history


# ---------------------------------------------------------------- run chat


def _run_chat_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: List[Any]):
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    def _fake(**kwargs: Any) -> str:
        captured.append(kwargs["messages"])
        return "answer"

    monkeypatch.setattr(gateway_routes, "_generate_chat_text", _fake)
    return app, bundle_id, flow_id


def _chat(client: TestClient, bundle_id: str, flow_id: str, messages: List[Dict[str, str]]) -> tuple[str, Dict[str, Any]]:
    headers = {"Authorization": "Bearer t"}
    r = client.post(
        "/api/gateway/runs/start",
        json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": "do the thing"}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    chat = client.post(
        f"/api/gateway/runs/{run_id}/chat",
        json={"provider": "openai", "model": "gpt-test", "messages": messages, "persist": True},
        headers=headers,
    )
    assert chat.status_code == 200, chat.text
    return run_id, chat.json()


@pytest.mark.basic
def test_run_chat_replays_the_whole_short_history_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Any] = []
    app, bundle_id, flow_id = _run_chat_client(tmp_path, monkeypatch, captured)
    history = _conversation(30)  # 61 messages (the Observer used to send only the last 20)
    with TestClient(app) as client:
        run_id, body = _chat(client, bundle_id, flow_id, history)
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers={"Authorization": "Bearer t"}).json()

    assert captured[-1] == history
    assert body["history"]["replayed_messages"] == 61
    assert body["history"]["dropped_messages"] == 0
    chats = [
        i["effect"]["payload"]["payload"]
        for i in ledger.get("items") or []
        if (i.get("effect") or {}).get("type") == "emit_event" and (i["effect"].get("payload") or {}).get("name") == "abstract.chat"
    ]
    assert chats and chats[-1]["history"] == body["history"]
    assert chats[-1]["question"] == "the-newest-question"


@pytest.mark.basic
def test_run_chat_bounds_a_long_history_with_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: List[Any] = []
    app, bundle_id, flow_id = _run_chat_client(tmp_path, monkeypatch, captured)
    history = _conversation(40, answer_chars=8_000)  # ~80k estimated tokens
    with TestClient(app) as client:
        _run_id, body = _chat(client, bundle_id, flow_id, history)

    sent = captured[-1]
    report = body["history"]
    assert report["dropped_messages"] > 0
    assert report["replayed_tokens"] <= HISTORY_REPLAY_MAX_TOKENS
    assert len(sent) == report["replayed_messages"] < len(history)
    assert sent[-1]["content"] == "the-newest-question"
    assert sent[0]["content"].startswith("[#TRUNCATION: ")


# ---------------------------------------------------------------- backlog chat routes


def _backlog_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo_root = tmp_path / "repo"
    backlog_root = repo_root / "docs" / "backlog"
    (backlog_root / "proposed").mkdir(parents=True, exist_ok=True)
    (backlog_root / "template.md").write_text("# {ID}-{Package}: {Title}\n", encoding="utf-8")
    (backlog_root / "proposed" / "672-framework-test.md").write_text("# 672-framework: [TASK] Test\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from abstractcore.config.manager import ConfigurationManager

    assert ConfigurationManager().set_capability_default("output.text", provider="stub", model="stub-model")
    gateway_dir = tmp_path / "gateway_data"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    host = _Host()
    svc = _Service(config=_Config(data_dir=gateway_dir), runner=_Runner(), host=host)
    return _make_app(monkeypatch=monkeypatch, svc=svc), host


def _prompt_messages(prompt: str) -> List[Dict[str, str]]:
    """The `messages` list of the JSON CONTEXT embedded in an agent prompt."""
    start = prompt.index("CONTEXT (JSON):\n") + len("CONTEXT (JSON):\n")
    ctx, _end = json.JSONDecoder().raw_decode(prompt[start:])
    return ctx["messages"]


@pytest.mark.basic
def test_backlog_advisor_sends_the_newest_question_past_forty_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The advisor used to keep the FIRST 40 messages: the question being asked vanished."""
    app, host = _backlog_app(tmp_path, monkeypatch)
    history = _conversation(30)
    history[1]["content"] = "v" * 20_000
    with TestClient(app) as client:
        r = client.post("/api/gateway/backlog/advisor", json={"messages": history})
        assert r.status_code == 200, r.text
        report = r.json()["history"]

    assert _prompt_messages(str(host.starts[-1]["input_data"]["prompt"])) == history
    assert report["replayed_messages"] == len(history) and report["dropped_messages"] == 0


@pytest.mark.basic
def test_backlog_maintain_sends_messages_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, host = _backlog_app(tmp_path, monkeypatch)
    history = _conversation(3)
    history[1]["content"] = "u" * 20_000  # the old per-message clamp was 8,000 chars
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/backlog/maintain",
            json={
                "kind": "proposed",
                "filename": "672-framework-test.md",
                "package": "framework",
                "title": "Test",
                "summary": "Test",
                "draft_markdown": "# 672-framework: [TASK] Test\n",
                "messages": history,
            },
        )
        assert r.status_code == 200, r.text
        report = r.json()["history"]

    assert _prompt_messages(str(host.starts[-1]["input_data"]["prompt"])) == history
    assert report["replayed_messages"] == len(history)


@pytest.mark.basic
def test_backlog_assist_passes_the_windowed_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    app, _host = _backlog_app(tmp_path, monkeypatch)
    captured: List[Any] = []

    def _fake(**kwargs: Any) -> Dict[str, str]:
        captured.append(kwargs["messages"])
        return {"reply": "ok", "draft_markdown": ""}

    monkeypatch.setattr(gateway_routes, "_generate_backlog_assist_json", _fake)
    history = _conversation(40, answer_chars=8_000)
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/backlog/assist",
            json={"kind": "proposed", "package": "framework", "title": "T", "messages": history},
        )
        assert r.status_code == 200, r.text
        report = r.json()["history"]

    assert report["dropped_messages"] > 0
    assert len(captured[-1]) == report["replayed_messages"]
    assert captured[-1][-1]["content"] == "the-newest-question"


@pytest.mark.basic
def test_backlog_assist_sends_template_and_draft_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 500k draft cut and the 120k/180k context clamps on assist are gone."""
    import abstractgateway.routes.gateway as gateway_routes

    seen: Dict[str, Any] = {}

    class _Llm:
        def generate(self, **kwargs: Any) -> Dict[str, Any]:
            seen.update(kwargs)
            return {"content": json.dumps({"reply": "ok", "draft_markdown": ""})}

    monkeypatch.setattr(gateway_routes, "_gateway_llm_client", lambda *_a, **_k: (_Llm(), None, None))
    draft = "# draft\n" + "d" * 600_000
    template = "# {ID}-{Package}: {Title}\n" + "t" * 150_000
    gateway_routes._generate_backlog_assist_json(
        provider="p", model="m", thinking=None, template_md=template, kind="proposed",
        package="framework", title="T", summary="S", draft_md=draft, messages=[],
    )
    ctx = json.loads(seen["messages"][0]["content"][len("CONTEXT:\n"):])
    assert ctx["current_draft_markdown"] == draft
    assert ctx["backlog_template"] == template


@pytest.mark.basic
def test_backlog_maintain_sends_the_draft_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 600k draft cut and the 220k clamp on maintain are gone."""
    app, host = _backlog_app(tmp_path, monkeypatch)
    draft = "# 672-framework: [TASK] Test\n" + "d" * 700_000
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/backlog/maintain",
            json={"kind": "proposed", "filename": "672-framework-test.md", "package": "framework",
                  "title": "Test", "summary": "Test", "draft_markdown": draft,
                  "messages": [{"role": "user", "content": "please improve"}]},
        )
        assert r.status_code == 200, r.text
    prompt = str(host.starts[-1]["input_data"]["prompt"])
    start = prompt.index("CONTEXT (JSON):\n") + len("CONTEXT (JSON):\n")
    ctx, _end = json.JSONDecoder().raw_decode(prompt[start:])
    assert ctx["current_draft_markdown"] == draft


@pytest.mark.basic
def test_console_sandbox_history_goes_through_the_window() -> None:
    from abstractgateway.routes.gateway import _GatewaySandboxGenerateRequest, _sandbox_messages

    short = _conversation(30)
    req = _GatewaySandboxGenerateRequest(provider="p", model="m", prompt="the-prompt", messages=short)
    messages, report = _sandbox_messages(req)
    assert messages == [*short, {"role": "user", "content": "the-prompt"}]
    assert report["dropped_messages"] == 0

    long = _conversation(40, answer_chars=8_000)
    messages, report = _sandbox_messages(_GatewaySandboxGenerateRequest(provider="p", model="m", prompt="the-prompt", messages=long))
    assert report["dropped_messages"] > 0 and report["replayed_tokens"] <= HISTORY_REPLAY_MAX_TOKENS
    assert messages[-1] == {"role": "user", "content": "the-prompt"}
    assert messages[0]["content"].startswith("[#TRUNCATION: ")


# ---------------------------------------------------------------- /runs/start client context (older clients)


def _start_with_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: Dict[str, Any], extra: Dict[str, Any] | None = None):
    app, bundle_id, flow_id = _run_chat_client(tmp_path, monkeypatch, [])
    from abstractgateway.service import get_gateway_service

    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": "now?", "context": context, **(extra or {})}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        run = get_gateway_service().host.run_store.load(run_id)
        summary = client.get(f"/api/gateway/runs/{run_id}", headers=headers).json()
    return run, summary


@pytest.mark.basic
def test_runs_start_bounds_a_client_transcript_with_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = [{"role": "system", "content": "be brief"}] + _conversation(40, answer_chars=8_000)
    run, summary = _start_with_context(tmp_path, monkeypatch, {"messages": history, "task": "t"})

    sent = run.vars["context"]["messages"]
    receipt = run.vars["_runtime"]["session_history"]
    assert receipt["source"] == "client_context" and receipt["seeded"] == 0
    assert receipt["dropped_messages"] > 0 and receipt["replayed_tokens"] <= HISTORY_REPLAY_MAX_TOKENS
    assert receipt["system_messages_kept"] == 1
    assert sent[0] == {"role": "system", "content": "be brief"}             # instructions always kept
    assert sent[1]["role"] == "user" and sent[1]["content"].startswith("[#TRUNCATION: ")
    assert sent[-1]["content"] == "the-newest-question"
    assert len(sent) == 1 + receipt["replayed_messages"]
    assert run.vars["context"]["task"] == "t"                               # the rest of the context untouched
    assert summary["session_history"] == receipt


@pytest.mark.basic
def test_runs_start_keeps_a_short_client_transcript_whole_and_shaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _conversation(30)
    history[1] = {"role": "assistant", "content": "w" * 40_000, "tool_calls": [{"id": "c1", "name": "read_file"}]}
    history.insert(2, {"role": "tool", "content": "file body", "tool_call_id": "c1"})
    run, _summary = _start_with_context(tmp_path, monkeypatch, {"messages": history})
    assert run.vars["context"]["messages"] == history                       # nothing cut, nothing reshaped
    assert run.vars["_runtime"]["session_history"]["dropped_messages"] == 0


@pytest.mark.basic
def test_runs_start_never_trusts_a_client_sent_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _start_with_context(
        tmp_path, monkeypatch, {"task": "t"}, extra={"_runtime": {"session_history": {"dropped_messages": 0, "forged": True}}}
    )
    assert "session_history" not in (run.vars.get("_runtime") or {}) or "forged" not in run.vars["_runtime"]["session_history"]


@pytest.mark.basic
def test_runs_start_keeps_the_client_receipt_when_session_replay_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, bundle_id, flow_id = _run_chat_client(tmp_path, monkeypatch, [])
    from abstractgateway.service import get_gateway_service

    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "session_id": "s-legacy",
                  "input_data": {"prompt": "now?", "use_session_history": True,
                                 "context": {"messages": _conversation(40, answer_chars=8_000)}}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run = get_gateway_service().host.run_store.load(r.json()["run_id"])
    receipt = run.vars["_runtime"]["session_history"]
    assert receipt["skipped"] == "client context.messages present"
    assert receipt["source"] == "client_context" and receipt["dropped_messages"] > 0
