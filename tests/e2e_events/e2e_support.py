"""Shared pieces of the events-and-attention end-to-end examples (framework backlog 1048).

Not a test module. Everything is hermetic: the gateway app runs in-process on a scratch data
folder (tests/conftest.py already moved HOME and refuses the operator's live ports, :8080
included); the model is a scripted test double installed where AbstractRuntime builds its
effect handlers, so no provider is ever called; mail servers are AbstractCore's fakes.

The workflow every example binds is the same small VisualFlow, "judge":

    On Flow Start (prompt) -> LLM Call (prompt, response schema {title, body}) -> On Flow End
                                                       data ----------------> notify
                                                       response ------------> response

The model reads the occurrence's prompt (the trigger's untrusted frame carries the event) and
answers `{title, body}`: a title means "tell the person" (the automation's output `notify` raises
an attention item), an empty title and body mean "nothing to say" (quiet). The decision is the
model's — the double answers what the test scripted, it never inspects the text.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

JUDGE_BUNDLE_ID = "events-examples"
JUDGE_FLOW_ID = "judge"
JUDGE_REF = f"{JUDGE_BUNDLE_ID}@1.0.0"
TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

NOTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Empty when nothing needs the person."},
        "body": {"type": "string"},
    },
    "required": ["title", "body"],
}

JUDGE_INSTRUCTIONS = (
    "You read what just arrived. If it needs the person's attention, answer a short title and a "
    "one-paragraph body; otherwise answer an empty title and an empty body."
)


def judge_flow() -> Dict[str, Any]:
    exec_out = {"id": "exec-out", "label": "", "type": "execution"}
    exec_in = {"id": "exec-in", "label": "", "type": "execution"}
    return {
        "id": JUDGE_FLOW_ID,
        "name": "Judge what arrived",
        "description": "An LLM decides whether what arrived needs the person.",
        "interfaces": [],
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [],
                      "outputs": [exec_out, {"id": "prompt", "label": "prompt", "type": "string"}]}},
            {"id": "judge", "type": "llm_call", "position": {"x": 260, "y": 0},
             "data": {"nodeType": "llm_call", "label": "Does it need me?",
                      "inputs": [exec_in, {"id": "prompt", "label": "prompt", "type": "string"},
                                 {"id": "system", "label": "system", "type": "string"},
                                 {"id": "resp_schema", "label": "resp_schema", "type": "object"}],
                      "outputs": [exec_out, {"id": "response", "label": "response", "type": "string"},
                                  {"id": "data", "label": "data", "type": "object"}],
                      "pinDefaults": {"system": JUDGE_INSTRUCTIONS, "resp_schema": NOTIFY_SCHEMA},
                      "effectConfig": {"provider": "test-double", "model": "scripted"}}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 520, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "On Flow End",
                      "inputs": [exec_in, {"id": "response", "label": "response", "type": "string"},
                                 {"id": "notify", "label": "notify", "type": "object"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "judge", "targetHandle": "exec-in"},
            {"id": "e2", "source": "start", "sourceHandle": "prompt", "target": "judge", "targetHandle": "prompt"},
            {"id": "e3", "source": "judge", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"id": "e4", "source": "judge", "sourceHandle": "data", "target": "end", "targetHandle": "notify"},
            {"id": "e5", "source": "judge", "sourceHandle": "response", "target": "end", "targetHandle": "response"},
        ],
        "entryNode": "start",
    }


def write_judge_bundle(bundles_dir: Path) -> str:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": JUDGE_BUNDLE_ID,
        "bundle_version": "1.0.0",
        "created_at": "2026-10-25T00:00:00+00:00",
        "entrypoints": [{"flow_id": JUDGE_FLOW_ID, "name": "Judge what arrived", "description": "", "interfaces": []}],
        "flows": {JUDGE_FLOW_ID: f"flows/{JUDGE_FLOW_ID}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{JUDGE_BUNDLE_ID}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{JUDGE_FLOW_ID}.json", json.dumps(judge_flow(), indent=2))
    return JUDGE_REF


class ScriptedModel:
    """The model test double: answers each structured LLM call with the next scripted
    `{title, body}` (and records the prompt it was given)."""

    def __init__(self) -> None:
        self.script: List[Dict[str, str]] = []
        self.prompts: List[str] = []
        self.lock = threading.Lock()

    def answer(self, *answers: Dict[str, str]) -> None:
        with self.lock:
            self.script.extend(answers)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from abstractruntime.core.models import EffectType
        from abstractruntime.core.runtime import EffectOutcome
        from abstractruntime.integrations.abstractcore import factory as ac_factory
        from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

        model = self

        def llm(run, effect, default_next_node):
            del run, default_next_node
            payload = dict(effect.payload or {})
            with model.lock:
                model.prompts.append(str(payload.get("prompt") or ""))
                data = model.script.pop(0) if model.script else {"title": "", "body": ""}
            return EffectOutcome.completed({"content": json.dumps(data), "data": data, "tool_calls": [],
                                            "model": payload.get("model"), "provider": payload.get("provider")})

        def build(*, llm=None, tools=None, artifact_store=None, run_store=None):  # noqa: ARG001 - factory signature
            return {EffectType.LLM_CALL: llm_handler, EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tools)}

        llm_handler = llm
        monkeypatch.setattr(ac_factory, "build_effect_handlers", build)


def gateway_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "runtime"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "1")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    return data_dir


def wait_until(predicate: Callable[[], Any], *, timeout_s: float = 20.0, poll_s: float = 0.05) -> Any:
    end = time.time() + timeout_s
    last = None
    while time.time() < end:
        last = predicate()
        if last:
            return last
        time.sleep(poll_s)
    raise AssertionError(f"timeout waiting for condition (last={last!r})")


def my_events(client: Any, *, after: int = 0) -> List[Dict[str, Any]]:
    r = client.get(f"/api/gateway/me/events?after={after}&limit=1000", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["events"]


def events_of(client: Any, typ: str, **match: Any) -> List[Dict[str, Any]]:
    return [e for e in my_events(client) if e["type"] == typ and all(e["data"].get(k) == v for k, v in match.items())]


def create_automation(client: Any, *, title: str, trigger: Dict[str, Any], prompt: str, request_id: str) -> str:
    body = {
        "request_id": request_id,
        "title": title,
        "target": {"bundle_ref": JUDGE_REF, "flow_id": JUDGE_FLOW_ID, "input_data": {"prompt": prompt}},
        "trigger": trigger,
        "context": {"mode": "independent"},
    }
    r = client.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    automation_id = r.json()["automation_id"]
    wait_until_armed(automation_id)
    return automation_id


def wait_until_armed(automation_id: str, *, timeout_s: float = 30.0) -> None:
    """Wait until the automation's controller is parked on its wake key: only then does an event
    appended to its trigger wake it (`woken`). Creation answers before the runner's first tick of
    the controller; on a loaded CI runner a webhook call or a feed poll made right after creation
    found it not yet parked (`woken` empty). Same test as abstractruntime's
    `triggers.connections.wake_connection_automations`."""
    from abstractruntime.automations.models import wake_wait_key
    from abstractruntime.core.models import RunStatus

    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().host.run_store

    def armed() -> bool:
        run = store.load(automation_id)
        return (run is not None and run.status == RunStatus.WAITING and run.waiting is not None
                and run.waiting.wait_key == wake_wait_key(automation_id))

    wait_until(armed, timeout_s=timeout_s)


def completed_occurrences(client: Any, automation_id: str) -> List[Dict[str, Any]]:
    r = client.get(f"/api/gateway/automations/{automation_id}/occurrences", headers=HEADERS)
    assert r.status_code == 200, r.text
    return [o for o in r.json()["items"] if o["status"] == "completed"]


def attention_items(client: Any, automation_id: str) -> List[Dict[str, Any]]:
    r = client.get(f"/api/gateway/automations/{automation_id}/attention", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeIcon:
    """The desktop notification surface of the gateway tray, recorded instead of shown."""

    HAS_NOTIFICATION = True

    def __init__(self) -> None:
        self.shown: List[tuple] = []

    def notify(self, message: str, title: str) -> None:
        self.shown.append((title, message))


class TrayOnScratchPort:
    """The gateway's own tray process logic (`abstractgateway.tray`), following the person's event
    stream over real HTTP on a scratch loopback port: the same app, served by uvicorn."""

    def __init__(self) -> None:
        import uvicorn

        from abstractgateway.app import app as asgi
        from abstractgateway.tray.app import TrayApp

        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_until(lambda: self.server.started, timeout_s=10)
        self.tray = TrayApp({"base_url": f"http://127.0.0.1:{self.port}", "token": TOKEN, "parent_pid": 0})
        self.icon = _FakeIcon()
        self.tray._icon = self.icon
        # The tray's sampler (its snapshot names the automations in notifications; the icon drawing
        # it also feeds is not exercised here) and its event follower.
        self.tray.sampler.add_listener(lambda snap: setattr(self.tray, "_snap", snap))
        self.tray.sampler.start()
        self.tray._events.start()
        wait_until(lambda: self.tray._events.state == "live", timeout_s=10)

    def knows_automation(self, automation_id: str) -> bool:
        snap = self.tray._snap
        return any(getattr(r, "automation_id", None) == automation_id for r in (getattr(snap, "automations", None) or ()))

    def close(self) -> None:
        try:
            self.tray._events.stop()
            self.tray.sampler.stop()
        finally:
            self.server.should_exit = True
            self.thread.join(10)


def tray_recorded(client: Any, record_key: str) -> Optional[Dict[str, Any]]:
    for e in my_events(client):
        if e["type"] == "notification.sent" and e["data"].get("channel") == "tray" and e["data"].get("record_key") == record_key:
            return e
    return None
