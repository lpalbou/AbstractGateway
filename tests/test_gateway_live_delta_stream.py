"""Live token deltas on `GET /runs/{id}/ledger/stream` (routes/gateway.py + live_deltas.py).

End to end through the real gateway: `POST /runs/start` with
`_runtime.stream: true` -> the runner ticks the run -> the runtime's per-call
emitter -> the sink the bundle host registered -> the hub -> the SSE route.
The model is a stub client that streams through the `_on_delta` callback the
runtime hands it, and can hold the call open mid-stream.

Pins:
* deltas arrive as `event: llm.delta` / `llm.delta_end`, with NO `id:` line;
  the delta_end comes after the call's durable record; `done` comes last;
* a client that connects mid-call gets ONE snapshot (the text so far), and
  the `Last-Event-ID` cursor is untouched by deltas;
* a cancelled run's open call is closed with a synthetic delta_end;
* another user's run is a 404 and none of its deltas reach anyone else;
* `_runtime.stream` must be a boolean (400); `agents.streaming_default`
  applies to interactive starts only (never /runs/schedule), and scheduled
  runs now carry the built-in tool deny list;
* the setting has its three doors and is advertised in discovery.
"""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
CHUNKS = ["The ", "quick ", "brown ", "fox"]


def _write_llm_bundle(bundles_dir: Path, *, bundle_id: str = "live", flow_id: str = "root") -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "llm",
        "description": "",
        "interfaces": [],
        "nodes": [
            {"id": "node-1", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "node-2", "type": "llm_call", "position": {"x": 200, "y": 0},
             "data": {"nodeType": "llm_call", "label": "LLM Call",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "prompt", "label": "prompt", "type": "string"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "response", "label": "response", "type": "string"}],
                      "pinDefaults": {"prompt": "hello"},
                      "effectConfig": {"provider": "stub", "model": "stub-model"}}},
            {"id": "node-3", "type": "on_flow_end", "position": {"x": 400, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "End",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1", "bundle_id": bundle_id, "bundle_version": "0.0.0",
        "created_at": "2026-09-26T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


class _Ctl:
    """Shared control of the stub model (class-level: the factory builds the client)."""

    gate = threading.Event()        # the call streams CHUNKS[:2], then waits here
    midway = threading.Event()      # set once the first half was emitted
    hold = True
    calls: List[Dict[str, Any]] = []

    @classmethod
    def reset(cls, *, hold: bool) -> None:
        cls.gate = threading.Event()
        cls.midway = threading.Event()
        cls.hold = hold
        cls.calls = []


class _StreamingStubClient:
    def __init__(self, provider: str, model: str, llm_kwargs: Optional[Dict[str, Any]] = None, artifact_store: Any = None, **kwargs: Any) -> None:
        self._model = model

    def generate(self, *, prompt: str, messages: Any = None, system_prompt: Any = None, tools: Any = None, media: Any = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        on_delta = params.pop("_on_delta", None)
        _Ctl.calls.append({"stream": params.get("stream"), "has_on_delta": callable(on_delta)})
        if callable(on_delta):
            for i, chunk in enumerate(CHUNKS):
                on_delta(chunk)
                if i == 1:
                    on_delta.flush()
                    _Ctl.midway.set()
                    if _Ctl.hold:
                        _Ctl.gate.wait(20)
        return {
            "content": "".join(CHUNKS),
            "tool_calls": None,
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            "model": self._model,
            "finish_reason": "stop",
        }


@pytest.fixture
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_llm_bundle(bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractruntime.integrations.abstractcore import factory as ac_factory

    monkeypatch.setattr(ac_factory, "MultiLocalAbstractCoreLLMClient", _StreamingStubClient)
    _Ctl.reset(hold=True)

    from abstractgateway.app import app

    with TestClient(app) as client:
        yield client, runtime_dir
    _Ctl.gate.set()


def _parse_sse(lines: List[str]) -> List[Dict[str, Any]]:
    """[{event, id, data}] from raw SSE lines (comments dropped)."""
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in lines:
        if line == "":
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(": ")
        if k == "data":
            cur["data"] = json.loads(v)
        else:
            cur[k] = v
    if cur:
        events.append(cur)
    return events


def _read_stream(client: TestClient, url: str, *, headers: Dict[str, str], on_event=None) -> List[Dict[str, Any]]:
    lines: List[str] = []
    with client.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200, resp.read()
        for line in resp.iter_lines():
            lines.append(line)
            if line == "" and on_event is not None:
                on_event(_parse_sse(lines))
            if line.startswith("event: done"):
                # read the data line and the blank separator
                pass
            if lines[-2:-1] and lines[-2].startswith("event: done") and line.startswith("data:"):
                break
    return _parse_sse(lines)


def _start(client: TestClient, **input_data: Any) -> str:
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": "live", "flow_id": "root", "input_data": input_data})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _wait(pred, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("timeout")


def _llm_record_index(events: List[Dict[str, Any]]) -> int:
    for i, e in enumerate(events):
        if e.get("event") == "step":
            rec = e["data"]["record"]
            if (rec.get("effect") or {}).get("type") == "llm_call" and rec.get("status") == "completed":
                return i
    raise AssertionError("no completed llm_call record on the stream")


def test_a_streamed_run_delivers_deltas_then_the_durable_record_then_done(gw) -> None:
    client, _ = gw
    run_id = _start(client, prompt="hi", _runtime={"stream": True})
    assert _Ctl.midway.wait(10), "the stub call never started streaming"
    # Hold the call until the stream is connected (the test client hands the
    # body over when the stream ends), then let it finish.
    threading.Timer(1.0, _Ctl.gate.set).start()
    events = _read_stream(client, f"/api/gateway/runs/{run_id}/ledger/stream?after=0&heartbeat_s=1", headers=HEADERS)

    assert _Ctl.calls and _Ctl.calls[0]["has_on_delta"] is True and _Ctl.calls[0]["stream"] is True
    deltas = [e for e in events if e.get("event") == "llm.delta"]
    ends = [e for e in events if e.get("event") == "llm.delta_end"]
    assert deltas, events
    assert all("id" not in e for e in deltas + ends), "delta frames never carry an id line"
    live_text = "".join(e["data"]["text"] for e in deltas)
    assert live_text == "".join(CHUNKS), "snapshot + live deltas = the whole answer, nothing repeated or lost"
    assert len(ends) == 1 and ends[0]["data"]["reason"] == "completed"
    d0 = deltas[0]["data"]
    assert d0["run_id"] == run_id and d0["root_run_id"] == run_id and d0["channel"] == "content"
    assert ends[0]["data"]["call_id"] == d0["call_id"]

    idx_end = events.index(ends[0])
    idx_rec = _llm_record_index(events)
    assert idx_rec < idx_end, "the durable record precedes its delta_end on the wire"
    rec = events[idx_rec]["data"]["record"]
    assert rec["step_id"] == d0["call_id"], "call_id is the LLM_CALL step id"
    assert events[-1]["event"] == "done" and events.index(ends[0]) < len(events) - 1


def test_reconnect_mid_call_gets_one_snapshot_and_an_untouched_cursor(gw) -> None:
    client, runtime_dir = gw
    run_id = _start(client, prompt="hi", _runtime={"stream": True})
    assert _Ctl.midway.wait(10), "the stub call never started streaming"
    # The test client delivers the response body when the stream ends, so the
    # call is released on a timer after the stream has connected.
    threading.Timer(1.0, _Ctl.gate.set).start()

    # The client had consumed N ledger records before it dropped.
    from abstractgateway.service import get_gateway_service

    n = len(get_gateway_service().host.ledger_store.list(run_id))
    seen: Dict[str, Any] = {}

    def on_event(evs: List[Dict[str, Any]]) -> None:
        if not seen and any(e.get("event") == "llm.delta" for e in evs):
            seen["first"] = next(e for e in evs if e.get("event") == "llm.delta")

    events = _read_stream(
        client,
        f"/api/gateway/runs/{run_id}/ledger/stream?heartbeat_s=1",
        headers={**HEADERS, "Last-Event-ID": str(n)},
        on_event=on_event,
    )
    first = seen["first"]["data"]
    assert first["snapshot"] is True and first["text"] == "The quick ", first
    snaps = [e for e in events if e.get("event") == "llm.delta" and e["data"]["snapshot"]]
    assert len(snaps) == 1, "one snapshot per open call and channel"
    later = [e["data"]["text"] for e in events if e.get("event") == "llm.delta" and not e["data"]["snapshot"]]
    assert "".join(later) == "brown fox", "live deltas continue after the snapshot, nothing repeated"
    # Delivered LIVE, in causal order: the text streamed while the call ran
    # precedes the call's durable record, and its delta_end follows it. (The
    # runtime flushes its last <=40 ms batch at the call's end, after the
    # record: clients ignore deltas of a call whose record they hold.)
    idx_rec = _llm_record_index(events)
    idx_live = [i for i, e in enumerate(events) if e.get("event") == "llm.delta" and not e["data"]["snapshot"]]
    idx_end = [i for i, e in enumerate(events) if e.get("event") == "llm.delta_end"]
    assert idx_live[0] < idx_rec < idx_end[0], [e.get("event") for e in events]

    step_ids = [int(e["id"]) for e in events if e.get("event") == "step"]
    assert step_ids and step_ids[0] == n + 1, "Last-Event-ID resumes the LEDGER cursor; deltas never move it"
    assert step_ids == list(range(n + 1, step_ids[-1] + 1)), "consecutive ledger ids, none skipped"
    assert events[-1]["event"] == "done" and events[-1]["data"]["cursor"] == step_ids[-1]


def test_a_cancel_mid_stream_closes_the_call_with_a_synthetic_end(gw) -> None:
    client, _ = gw
    run_id = _start(client, prompt="hi", _runtime={"stream": True})
    assert _Ctl.midway.wait(10)

    from abstractgateway.live_deltas import get_hub, scope_key
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    scope = scope_key(svc.host.data_dir)
    sub = get_hub().subscribe(scope, (run_id,), loop=__import__("asyncio").new_event_loop())
    assert [f["text"] for f in sub.snapshot] == ["The quick "]

    # The runner applies a cancel on its own Runtime object: its terminal hook
    # must close the live call even though the model call is still blocked.
    svc.runner._apply_run_control("cancel", run_id=run_id, payload={"reason": "stop"}, apply_to_tree=True)
    frames = sub.drain()
    ends = [f for f in frames if f["kind"] == "llm.delta_end"]
    assert [(f["reason"], f.get("synthetic")) for f in ends] == [("cancelled", True)]
    assert get_hub().open_calls(scope, run_id) == []
    _Ctl.gate.set()
    # The runtime's own late delta_end (and any late delta) never reopens it.
    time.sleep(0.3)
    assert get_hub().open_calls(scope, run_id) == []
    sub.close()


def test_stream_must_be_a_boolean(gw) -> None:
    client, _ = gw
    for bad in ("true", 1, None, "yes"):
        r = client.post("/api/gateway/runs/start", headers=HEADERS,
                        json={"bundle_id": "live", "flow_id": "root", "input_data": {"_runtime": {"stream": bad}}})
        assert r.status_code == 400, (bad, r.text)
        assert "_runtime.stream must be true or false" in r.json()["detail"]
    r = client.post("/api/gateway/runs/schedule", headers=HEADERS,
                    json={"bundle_id": "live", "flow_id": "root", "input_data": {"_runtime": {"stream": "true"}}})
    assert r.status_code == 400, r.text


def _run_vars(run_id: str) -> Dict[str, Any]:
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store.load(run_id).vars


def test_streaming_default_applies_to_interactive_starts_only(gw) -> None:
    client, _ = gw
    _Ctl.reset(hold=False)
    off = _start(client, prompt="a")
    assert "stream" not in (_run_vars(off).get("_runtime") or {}), "default off: nothing is added"

    r = client.post("/api/gateway/admin/runtime-config", headers=HEADERS, json={"agents": {"streaming_default": True}})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == {"agents.streaming_default": True}

    on = _start(client, prompt="b")
    assert _run_vars(on)["_runtime"]["stream"] is True
    explicit = _start(client, prompt="c", _runtime={"stream": False})
    assert _run_vars(explicit)["_runtime"]["stream"] is False, "an explicit false beats the default"

    sched = client.post("/api/gateway/runs/schedule", headers=HEADERS,
                        json={"bundle_id": "live", "flow_id": "root", "input_data": {"prompt": "d"}, "start_at": "now"})
    assert sched.status_code == 200, sched.text
    wrapper_vars = _run_vars(sched.json()["run_id"])
    assert "stream" not in (wrapper_vars.get("_runtime") or {})
    assert "stream" not in ((wrapper_vars.get("vars") or {}).get("_runtime") or {}), "never on a schedule"


def test_scheduled_runs_carry_the_builtin_tool_deny_list(gw) -> None:
    """The wrapper run gets a workspace + the deny rule at the host; every
    scheduled execution inherits both (and a client cannot slip keys in)."""
    client, runtime_dir = gw
    _Ctl.reset(hold=False)
    sched = client.post("/api/gateway/runs/schedule", headers=HEADERS,
                        json={"bundle_id": "live", "flow_id": "root", "start_at": "now",
                              "input_data": {"prompt": "d", "workspace_builtin_allow": ["/"]}})
    assert sched.status_code == 200, sched.text
    parent = sched.json()["run_id"]
    wrapper = _run_vars(parent)
    data = str(runtime_dir.resolve())
    assert data in wrapper["workspace_builtin_deny_prefixes"]
    assert any(p.endswith("/.ssh") for p in wrapper["workspace_builtin_deny_prefixes"])
    assert wrapper["workspace_builtin_allow"] == [wrapper["workspace_root"]]
    assert "workspace_builtin_allow" not in wrapper["vars"], "the client's allow entry was dropped"

    from abstractgateway.service import get_gateway_service

    rs = get_gateway_service().host.run_store

    def _child():
        items = client.get(f"/api/gateway/runs?limit=50&session_id={parent}", headers=HEADERS).json().get("items") or []
        kids = [x for x in items if x.get("parent_run_id") == parent]
        return kids[0]["run_id"] if kids else None

    _wait(lambda: _child() is not None)
    child_vars = rs.load(_child()).vars
    assert data in child_vars["workspace_builtin_deny_prefixes"]
    assert child_vars["workspace_builtin_allow"] == [child_vars["workspace_root"]] == [wrapper["workspace_root"]]


def test_a_client_cannot_reach_the_data_folder_by_scheduling(gw) -> None:
    """Same workspace policy as /runs/start at /runs/schedule."""
    client, runtime_dir = gw
    for bad in (runtime_dir.resolve(), runtime_dir.resolve() / "auth"):
        r = client.post("/api/gateway/runs/schedule", headers=HEADERS,
                        json={"bundle_id": "live", "flow_id": "root", "start_at": "now",
                              "input_data": {"prompt": "d", "workspace_root": str(bad)}})
        assert r.status_code == 400 and "data folder" in r.json()["detail"], (bad, r.text)


def test_every_host_run_start_gets_a_workspace_and_the_deny_rule(gw) -> None:
    """Bridges (Telegram, email, agora), sandbox routes and entity summons all
    start runs through host.start_run: a bare bridge-style start is confined."""
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope, rewrite_tool_arguments

    client, runtime_dir = gw
    _Ctl.reset(hold=False)
    from abstractgateway.service import get_gateway_service

    host = get_gateway_service().host
    data = runtime_dir.resolve()
    rid = host.start_run(flow_id="root", bundle_id="live", input_data={"prompt": "from a bridge",
                         "workspace_builtin_allow": ["/"]}, actor_id="gateway", session_id="telegram:42")
    v = host.run_store.load(rid).vars
    own = Path(v["workspace_root"])
    assert own.parent == data / "workspaces" and v["_gateway_workspace"]["kind"] == "session"
    assert v["workspace_builtin_deny_prefixes"][0] == str(data)
    assert v["workspace_builtin_allow"] == [str(own)], "the bridge caller's allow entry is replaced"
    scope = WorkspaceScope.from_input_data(v)
    (data / "run_secret.json").write_text("{}")
    with pytest.raises(ValueError):
        rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(data / "run_secret.json")}, scope=scope)


def test_streaming_setting_three_doors_and_discovery(gw, capsys) -> None:
    client, runtime_dir = gw
    caps = client.get("/api/gateway/discovery/capabilities", headers=HEADERS).json()["capabilities"]
    assert caps["streaming"]["deltas"] is True and caps["streaming"]["default"] is False

    cfg = client.get("/api/gateway/admin/runtime-config", headers=HEADERS).json()
    assert cfg["agents"]["streaming_default"]["value"] is False
    assert cfg["agents"]["streaming_default"]["source"] == "default"

    bad = client.post("/api/gateway/admin/runtime-config", headers=HEADERS, json={"agents.streaming_default": "maybe"})
    assert bad.status_code == 400
    ok = client.post("/api/gateway/admin/runtime-config", headers=HEADERS, json={"agents.streaming_default": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["agents"]["streaming_default"] == {**ok.json()["agents"]["streaming_default"], "value": True, "source": "stored"}
    assert client.get("/api/gateway/discovery/capabilities", headers=HEADERS).json()["capabilities"]["streaming"]["default"] is True

    from abstractgateway.config_cli import main as config_main

    with pytest.raises(SystemExit) as ex:
        config_main(["unset", "agents.streaming_default", "--data-dir", str(runtime_dir)])
    assert ex.value.code == 0
    capsys.readouterr()
    config_main(["get", "agents.streaming_default", "--data-dir", str(runtime_dir), "--json"])
    row = json.loads(capsys.readouterr().out)
    assert row["value"] is False and row["source"] == "default"
    with pytest.raises(SystemExit) as ex:
        config_main(["set", "agents.streaming_default", "on", "--data-dir", str(runtime_dir)])
    assert ex.value.code == 0
    from abstractgateway.runtime_config import resolve_streaming_default

    assert resolve_streaming_default(runtime_dir) is True


def test_the_bundle_host_registers_the_sink_on_its_runtime(gw) -> None:
    """Break check for the seam: without the registration no run can stream."""
    from abstractgateway.service import get_gateway_service

    rt = get_gateway_service().host.runtime
    assert callable(getattr(rt, "_live_delta_sink", None))


def _save_run(rs: Any, run_id: str, *, parent: Optional[str] = None, status: str = "running") -> Any:
    import datetime

    from abstractruntime import RunState, RunStatus

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run = RunState(
        run_id=run_id, workflow_id="test", status=RunStatus(status), current_node="n", vars={},
        waiting=None, output=None, error=None, created_at=now, updated_at=now, actor_id=None,
        session_id="s", parent_run_id=parent,
    )
    rs.save(run)
    return run


def _ev(kind: str, run_id: str, call_id: str, seq: int, parent: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out = {"kind": kind, "run_id": run_id, "parent_run_id": parent, "node_id": "n", "call_id": call_id, "seq": seq}
    out.update(extra)
    return out


def test_a_child_stream_carries_its_subtree_and_the_root_stream_everything(gw) -> None:
    """The Assistant follows a CHILD run's stream while the root waits on it."""
    client, _ = gw
    from abstractruntime import RunStatus

    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    rs = svc.host.run_store
    sink = svc.host.runtime._live_delta_sink  # the sink the bundle host registered
    # WAITING with no wait: live, but inert to the runner's tick loop. (These
    # hand-made runs have no workflow; a RUNNING one would be failed by the
    # runner, which rightly closes its calls, racing the streams below.)
    _save_run(rs, "root-a", status="waiting")
    _save_run(rs, "child-a", parent="root-a", status="waiting")
    _save_run(rs, "grand-a", parent="child-a", status="waiting")
    _save_run(rs, "sib-a", parent="root-a", status="waiting")
    sink(_ev("llm.delta", "root-a", "r1", 0, text="root says", channel="content"))
    sink(_ev("llm.delta", "child-a", "k1", 0, parent="root-a", text="child says", channel="content"))
    sink(_ev("llm.delta", "grand-a", "g1", 0, parent="child-a", text="grandchild says", channel="content"))
    sink(_ev("llm.delta", "sib-a", "s1", 0, parent="root-a", text="sibling says", channel="content"))

    from abstractgateway.live_deltas import get_hub, scope_key

    _scope = scope_key(svc.host.data_dir)
    assert get_hub().open_calls(_scope, "root-a") == ["g1", "k1", "r1", "s1"], get_hub().tracked_roots()
    # The runs end behind the hub's back (no runtime terminal hook reached it):
    # the stream's own terminal check closes the open calls before `done`.
    for rid in ("root-a", "child-a", "grand-a", "sib-a"):
        run = rs.load(rid)
        run.status = RunStatus.CANCELLED
        rs.save(run)

    child = _read_stream(client, "/api/gateway/runs/child-a/ledger/stream?after=0&heartbeat_s=1", headers=HEADERS)
    snaps = [e["data"] for e in child if e.get("event") == "llm.delta"]
    assert sorted(d["text"] for d in snaps) == ["child says", "grandchild says"]
    assert all(d["root_run_id"] == "root-a" and d["snapshot"] for d in snaps)
    ends = [e["data"] for e in child if e.get("event") == "llm.delta_end"]
    assert sorted((d["call_id"], d["reason"], d["synthetic"]) for d in ends) == [
        ("g1", "cancelled", True), ("k1", "cancelled", True),
    ]
    assert child[-1]["event"] == "done"

    _diag = {"open_before_root_stream": get_hub().open_calls(_scope, "root-a"), "child_events": [e.get("event") for e in child]}
    root = _read_stream(client, "/api/gateway/runs/root-a/ledger/stream?after=0&heartbeat_s=1", headers=HEADERS)
    _diag["root_events"] = [(e.get("event"), (e.get("data") or {}).get("call_id")) for e in root]
    assert sorted(e["data"]["text"] for e in root if e.get("event") == "llm.delta") == ["root says", "sibling says"], (
        "the child's calls were closed by its own stream; the root still had its own and the sibling's open", _diag
    )
    assert root[-1]["event"] == "done"


def test_another_users_run_is_404_and_leaks_no_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_llm_bundle(bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractruntime import RunStatus
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    monkeypatch.setattr(ac_factory, "MultiLocalAbstractCoreLLMClient", _StreamingStubClient)
    from abstractgateway.app import app
    from abstractgateway.live_deltas import get_hub

    def _user(client: TestClient, uid: str) -> Dict[str, str]:
        r = client.post("/api/gateway/admin/users", headers={"Authorization": "Bearer admin-token"},
                        json={"user_id": uid, "tenant_id": "default", "roles": ["user"], "runtime_id": uid})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    with TestClient(app) as client:
        alice, bob = _user(client, "alice"), _user(client, "bob")
        _write_llm_bundle(runtime_dir / "users" / "default" / "alice" / "flows")
        _write_llm_bundle(runtime_dir / "users" / "default" / "bob" / "flows")
        # Materialise both users' services (their hosts register the sinks).
        for h in (alice, bob):
            assert client.get("/api/gateway/runs?limit=1", headers=h).status_code == 200

        from abstractgateway import service as service_mod

        svcs = {str(getattr(s.config, "user_id", "")): s for s in service_mod._services_by_principal.values()}
        a_svc, b_svc = svcs["alice"], svcs["bob"]
        assert a_svc.host.data_dir != b_svc.host.data_dir
        _save_run(a_svc.host.run_store, "shared-id")
        _save_run(b_svc.host.run_store, "bob-run")
        a_svc.host.runtime._live_delta_sink(_ev("llm.delta", "shared-id", "c1", 0, text="alice secret", channel="content"))

        # Bob asking for Alice's run: 404, before any hub state is touched.
        assert client.get("/api/gateway/runs/shared-id/ledger/stream?after=0", headers=bob).status_code == 404

        # Even a run of Bob's with the SAME id sees none of Alice's text: the hub
        # is keyed by the owner's data folder.
        _save_run(b_svc.host.run_store, "shared-id", status="completed")
        bob_stream = _read_stream(client, "/api/gateway/runs/shared-id/ledger/stream?after=0&heartbeat_s=1", headers=bob)
        assert not [e for e in bob_stream if str(e.get("event", "")).startswith("llm.")], bob_stream

        # Positive control: Alice's own stream shows it.
        run = a_svc.host.run_store.load("shared-id")
        run.status = RunStatus.COMPLETED
        a_svc.host.run_store.save(run)
        alice_stream = _read_stream(client, "/api/gateway/runs/shared-id/ledger/stream?after=0&heartbeat_s=1", headers=alice)
        assert [e["data"]["text"] for e in alice_stream if e.get("event") == "llm.delta"] == ["alice secret"]
        assert get_hub().open_calls(str(Path(a_svc.host.data_dir).resolve()), "shared-id") == []


def _open_call_for(svc: Any, run_id: str, *, status: str = "waiting") -> str:
    from abstractgateway.live_deltas import scope_key

    _save_run(svc.host.run_store, run_id, status=status)
    svc.host.runtime._live_delta_sink(_ev("llm.delta", run_id, f"call-{run_id}", 0, text="partial", channel="content"))
    return scope_key(svc.host.data_dir)


def test_a_kill_switched_run_with_no_subscriber_leaves_no_live_state(gw) -> None:
    """REVIEW/19 G1: the kill switch marks runs CANCELLED straight in the
    store (no Runtime, no hooks); nobody watches; the hub must still let go."""
    from abstractgateway.live_deltas import get_hub
    from abstractgateway.service import get_gateway_service
    from abstractgateway.stop_kill_switch import StopKillSwitch

    svc = get_gateway_service()
    scope = _open_call_for(svc, "killed-run")
    assert get_hub().open_calls(scope, "killed-run") == ["call-killed-run"]

    now = [1000.0]
    stuck = [{"run_id": "killed-run", "node_id": "llm", "step_id": "call-killed-run", "effect_type": "llm_call",
              "attempt": 1, "provider": "mlx", "model": "m", "elapsed_s": 30.0, "thread": "tick-1"}]
    switch = StopKillSwitch(
        run_store=svc.host.run_store, ledger_store=svc.host.ledger_store,
        settings=lambda: {"deadline_s": 1.0, "source": "stored"},
        kill=lambda step_id, **kw: (stuck.clear(), {"injected": True, "step_id": step_id})[1],
        inflight=lambda ids: list(stuck), clock=lambda: now[0], sleep=lambda s: None, watch=False,
        on_terminal=svc.runner._close_live_state,
    )
    incident = switch.arm(root_run_id="killed-run", run_ids=["killed-run"])
    now[0] += 2.0
    assert switch.check(incident) == "fired"
    assert svc.host.run_store.load("killed-run").status.value == "cancelled"
    assert (scope, "killed-run") not in get_hub().tracked_roots(), "the hub let go of the killed run"


def test_runs_the_runner_fails_in_the_store_leave_no_live_state(gw) -> None:
    """REVIEW/19 G1: the unresolvable-workflow and tick-exception promotions."""
    from abstractgateway.live_deltas import get_hub
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    runner = svc.runner

    # These paths promote RUNNING runs only.
    scope = _open_call_for(svc, "unresolvable-run", status="running")
    for _ in range(50):
        runner._note_resolution_failure("unresolvable-run", KeyError("Workflow 'test' not registered"))
        if svc.host.run_store.load("unresolvable-run").status.value == "failed":
            break
    assert svc.host.run_store.load("unresolvable-run").status.value == "failed"
    assert (scope, "unresolvable-run") not in get_hub().tracked_roots()

    _open_call_for(svc, "exploding-run", status="running")

    class _Boom:
        run_store = svc.host.run_store

        def tick(self, **kw: Any) -> Any:
            raise RuntimeError("tick exploded")

    original = svc.host.runtime_and_workflow_for_run
    try:
        svc.host.runtime_and_workflow_for_run = lambda rid: (_Boom(), object())  # type: ignore[assignment]
        runner._tick_run("exploding-run")
    finally:
        svc.host.runtime_and_workflow_for_run = original  # type: ignore[assignment]
    assert svc.host.run_store.load("exploding-run").status.value == "failed"
    assert (scope, "exploding-run") not in get_hub().tracked_roots()


def test_split_runner_kill_switch_closes_and_deletes_the_live_file(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import live_deltas as ld
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    monkeypatch.setattr(ld, "_process_role", ld.ROLE_RUNNER)
    scope = ld.scope_key(svc.host.data_dir)
    _save_run(svc.host.run_store, "split-run", status="waiting")
    ld._file_sink_for(scope).publish(scope, _ev("llm.delta", "split-run", "c1", 0, text="x", channel="content"), ("split-run",))
    path = ld.live_file_path(scope, "split-run")
    assert path.exists()
    run = svc.host.run_store.load("split-run")
    run.status = __import__("abstractruntime").RunStatus.CANCELLED
    svc.host.run_store.save(run)
    svc.runner._close_live_state(run)
    assert not path.exists(), "the runner's live file is deleted when the run ends"
    assert "split-run" not in ld._file_sink_for(scope)._fds, "and its descriptor closed"


def test_the_runtime_floor_is_declared_once_and_checked_at_host_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REVIEW/19 G2: the deny prefixes and the live sink need a recent runtime;
    an older one must stop the host from building, loudly."""
    import dataclasses
    import re

    from abstractgateway import live_deltas as ld

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    m = re.search(r'"AbstractRuntime>=([0-9.]+)"', pyproject)
    assert m and m.group(1) == ld.ABSTRACTRUNTIME_FLOOR

    from abstractruntime import InMemoryLedgerStore, InMemoryRunStore
    from abstractruntime.integrations.abstractcore import workspace_scoped_tools as wst

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    @dataclasses.dataclass(frozen=True)
    class _OldScope:  # the WorkspaceScope of a runtime before 6567ed4
        root: Path

    real_scope = wst.WorkspaceScope
    monkeypatch.setattr(wst, "WorkspaceScope", _OldScope)
    bundles = tmp_path / "bundles"
    _write_llm_bundle(bundles)
    with pytest.raises(ld.RuntimeTooOld, match=r"abstractruntime>=" + re.escape(ld.ABSTRACTRUNTIME_FLOOR) + r".*builtin_deny_prefixes"):
        WorkflowBundleGatewayHost.load_from_dir(
            bundles_dir=bundles, data_dir=tmp_path / "data", run_store=InMemoryRunStore(),
            ledger_store=InMemoryLedgerStore(), artifact_store=None,
        )

    class _NoSinkRuntime:
        pass

    monkeypatch.setattr(wst, "WorkspaceScope", real_scope)
    with pytest.raises(ld.RuntimeTooOld, match="set_live_delta_sink"):
        ld.require_runtime_features(_NoSinkRuntime())
