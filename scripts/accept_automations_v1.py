#!/usr/bin/env python3
"""Automations v1 end-to-end acceptance (contract H), against a real gateway.

Starts its OWN gateway subprocess on a free loopback port (>= 18900) with an
EMPTY data dir it owns, drives the public HTTP API only, restarts that
subprocess once mid-occurrence, and prints one line per step:

    PASS  step-name  evidence...
    FAIL  step-name  what went wrong

Exit code 0 only when every step passes.

The target workflows are deterministic (no model provider, no tools): an echo
flow that answers with the prompt and the history it was given and sets
`notify` when its input asks for it; an ask flow that waits for a person
first; a writer flow that writes its prompt to `note.txt` in its workspace.
Provider traffic is pointed at a local spy HTTP server, which must receive
zero requests.

    python abstractgateway/scripts/accept_automations_v1.py --data-dir /tmp/automation-acceptance

`--interval` (default 20s) is the schedule of the monitors; real monitors run
every 2 minutes or more, the short interval only makes the run quick.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.server
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BUNDLE_ID = "acceptance-automations"
BUNDLE_VERSION = "1.0.0"
BUNDLE_REF = f"{BUNDLE_ID}@{BUNDLE_VERSION}"

# ----------------------------------------------------------------- fixtures

_ECHO_CODE = """msgs = (context or {}).get('messages') if isinstance(context, dict) else None
msgs = msgs if isinstance(msgs, list) else []
history = [str(m.get('role')) + ':' + str(m.get('content')) for m in msgs if isinstance(m, dict)]
answer = 'ECHO[' + str(len(history)) + '] ' + str(prompt) + ' || ' + ' | '.join(history)
out = {'response': answer, 'success': True}
if notify:
    out['notify'] = {'title': 'Echo notify', 'body': answer[:200]}
return out"""

_ASKED_CODE = """return {'response': 'APPROVED:' + str(response), 'success': True}"""


def _pin(pid: str, typ: str) -> Dict[str, str]:
    return {"id": pid, "label": pid, "type": typ}


_EXEC = {"id": "exec-in", "label": "", "type": "execution"}
_EXEC_OUT = {"id": "exec-out", "label": "", "type": "execution"}


def _echo_flow() -> Dict[str, Any]:
    return {
        "id": "echo",
        "name": "Echo",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [
                _EXEC_OUT, _pin("prompt", "string"), _pin("context", "object"), _pin("notify", "boolean")]}},
            {"id": "code", "type": "code", "data": {"nodeType": "code", "codeBody": _ECHO_CODE, "inputs": [
                _EXEC, _pin("prompt", "string"), _pin("context", "object"), _pin("notify", "boolean")]}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [_EXEC, _pin("result", "object")]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "code", "targetHandle": "exec-in"},
            {"source": "code", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "start", "sourceHandle": "prompt", "target": "code", "targetHandle": "prompt"},
            {"source": "start", "sourceHandle": "context", "target": "code", "targetHandle": "context"},
            {"source": "start", "sourceHandle": "notify", "target": "code", "targetHandle": "notify"},
            {"source": "code", "sourceHandle": "output", "target": "end", "targetHandle": "result"},
        ],
        "entryNode": "start",
    }


def _ask_flow() -> Dict[str, Any]:
    return {
        "id": "ask",
        "name": "Ask then answer",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [_EXEC_OUT, _pin("prompt", "string")]}},
            {"id": "ask", "type": "ask_user", "data": {"nodeType": "ask_user", "inputs": [_EXEC, _pin("prompt", "string"), _pin("choices", "array")],
                                                      "outputs": [_EXEC_OUT, _pin("response", "string")]}},
            {"id": "code", "type": "code", "data": {"nodeType": "code", "codeBody": _ASKED_CODE, "inputs": [_EXEC, _pin("response", "string")]}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [_EXEC, _pin("result", "object")]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "ask", "targetHandle": "exec-in"},
            {"source": "ask", "sourceHandle": "exec-out", "target": "code", "targetHandle": "exec-in"},
            {"source": "code", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "start", "sourceHandle": "prompt", "target": "ask", "targetHandle": "prompt"},
            {"source": "ask", "sourceHandle": "response", "target": "code", "targetHandle": "response"},
            {"source": "code", "sourceHandle": "output", "target": "end", "targetHandle": "result"},
        ],
        "entryNode": "start",
    }


def _writer_flow() -> Dict[str, Any]:
    return {
        "id": "writer",
        "name": "Write a note",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [_EXEC_OUT, _pin("prompt", "string")]}},
            {"id": "write", "type": "write_file", "data": {"nodeType": "write_file", "pinDefaults": {"file_path": "note.txt"}}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [_EXEC, _pin("file_path", "string")]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "write", "targetHandle": "exec-in"},
            {"source": "write", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "start", "sourceHandle": "prompt", "target": "write", "targetHandle": "content"},
            {"source": "write", "sourceHandle": "file_path", "target": "end", "targetHandle": "file_path"},
        ],
        "entryNode": "start",
    }


def _shell_agent_flow() -> Dict[str, Any]:
    """Stand-in for the default agent (`abstractcode.agent.v1`): its run calls
    `execute_command` deterministically, so the tool approval path is real
    while no model provider is involved."""
    return {
        "id": "agent",
        "name": "Shell agent stand-in",
        "interfaces": ["abstractcode.agent.v1"],
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [_EXEC_OUT, _pin("prompt", "string")]}},
            {"id": "tools", "type": "tool_calls", "data": {"nodeType": "tool_calls", "inputs": [_EXEC, _pin("tool_calls", "array")],
                                                           "outputs": [_EXEC_OUT, _pin("results", "array")],
                                                           "pinDefaults": {"tool_calls": [{"name": "execute_command", "arguments": {"command": "echo acceptance-shell-ok"}}]}}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [_EXEC, _pin("results", "array")]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "tools", "targetHandle": "exec-in"},
            {"source": "tools", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "tools", "sourceHandle": "results", "target": "end", "targetHandle": "results"},
        ],
        "entryNode": "start",
    }


def write_shell_agent_bundle(bundles_dir: Path) -> None:
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "basic-agent",   # the built-in default bundle id for abstractcode.agent.v1
        "bundle_version": "9.9.9",
        "created_at": "2026-09-27T00:00:00+00:00",
        "default_entrypoint": "agent",
        "entrypoints": [{"flow_id": "agent", "name": "agent", "description": "", "interfaces": ["abstractcode.agent.v1"]}],
        "flows": {"agent": "flows/agent.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / "basic-agent.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("flows/agent.json", json.dumps(_shell_agent_flow(), indent=2))


def write_fixture_bundle(bundles_dir: Path) -> None:
    flows = {"echo": _echo_flow(), "ask": _ask_flow(), "writer": _writer_flow()}
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": BUNDLE_ID,
        "bundle_version": BUNDLE_VERSION,
        "created_at": "2026-09-27T00:00:00+00:00",
        "entrypoints": [{"flow_id": fid, "name": fid, "description": "", "interfaces": []} for fid in flows],
        "flows": {fid: f"flows/{fid}.json" for fid in flows},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundles_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundles_dir / f"{BUNDLE_ID}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))


# ------------------------------------------------------------ provider spy


class _Spy(http.server.BaseHTTPRequestHandler):
    hits: List[str] = []

    def _any(self) -> None:
        _Spy.hits.append(f"{self.command} {self.path}")
        self.send_response(503)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = _any  # noqa: N815

    def log_message(self, *_a: Any) -> None:  # silence
        pass


def _free_port(start: int) -> int:
    for port in range(start, start + 500):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port from {start}")


# ----------------------------------------------------------------- gateway


class Gateway:
    def __init__(self, data_dir: Path, port: int, token: str, spy_url: str) -> None:
        self.data_dir = data_dir
        self.port = port
        self.token = token
        self.spy_url = spy_url
        self.proc: Optional[subprocess.Popen] = None
        self.log = open(data_dir / "gateway.log", "ab")

    def env(self) -> Dict[str, str]:
        home = self.data_dir / "home"
        env = {k: v for k, v in os.environ.items() if not (k.startswith("ABSTRACT") or k.startswith("HF_") or k.startswith("XDG_"))}
        env.update({
            "HOME": str(home),
            "ABSTRACTGATEWAY_DATA_DIR": str(self.data_dir / "runtime"),
            "ABSTRACTGATEWAY_FLOWS_DIR": str(self.data_dir / "bundles"),
            "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle",
            "ABSTRACTGATEWAY_AUTH_TOKEN": self.token,
            "ABSTRACTGATEWAY_USER_AUTH": "0",
            "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*",
            "ABSTRACTGATEWAY_POLL_S": "0.2",
            "ABSTRACTFRAMEWORK_DATA_REGISTRY": str(self.data_dir / "data_registry.json"),
            "ABSTRACTCORE_CONFIG_FILE": str(self.data_dir / "abstractcore" / "abstractcore.json"),
            "ABSTRACTCORE_CONFIG_DIR": str(self.data_dir / "abstractcore"),
            "ABSTRACTCORE_JOBS_PERSIST": "0",
            "HF_HOME": str(home / ".cache" / "huggingface"),
            # Every provider endpoint points at the spy: any model call is counted.
            "OPENAI_BASE_URL": self.spy_url,
            "ANTHROPIC_BASE_URL": self.spy_url,
            "LMSTUDIO_BASE_URL": self.spy_url,
            "OLLAMA_BASE_URL": self.spy_url,
            "OLLAMA_HOST": self.spy_url,
        })
        return env

    def start(self) -> None:
        cmd = [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(self.port), "--no-tray", "--no-print-token"]
        self.proc = subprocess.Popen(cmd, env=self.env(), stdout=self.log, stderr=self.log, start_new_session=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"gateway exited with {self.proc.returncode}; see {self.data_dir / 'gateway.log'}")
            try:
                if self.request("GET", "/api/gateway/ping")[0] == 200:
                    return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.5)
        raise RuntimeError("gateway did not come up within 120 s")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw.decode("utf-8", "replace")

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, out = self.request(method, path, body)
        if status != 200:
            raise AssertionError(f"{method} {path} -> {status}: {out}")
        return out


# ------------------------------------------------------------------- steps


class StepFailed(AssertionError):
    pass


def wait_for(what: str, fn: Callable[[], Any], timeout_s: float) -> Any:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.5)
    raise StepFailed(f"timed out after {timeout_s:.0f}s waiting for {what} (last: {last!r})")


def check(cond: bool, message: str) -> None:
    if not cond:
        raise StepFailed(message)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, type=Path, help="EMPTY (or absent) folder this script owns")
    ap.add_argument("--interval", default="20s", help="Monitor schedule (default 20s; real monitors use 2m or more)")
    ap.add_argument("--port", type=int, default=0, help="Gateway port (default: first free port >= 18900)")
    args = ap.parse_args()

    data_dir: Path = args.data_dir.expanduser().resolve()
    if data_dir.exists() and any(data_dir.iterdir()):
        print(f"FAIL  preflight  {data_dir} is not empty; give an empty or new folder")
        return 2
    every = str(args.interval)
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[every[-1]]
    interval_s = int(every[:-1]) * unit
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "home").mkdir()
    write_fixture_bundle(data_dir / "bundles")
    write_shell_agent_bundle(data_dir / "bundles")

    spy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Spy)
    threading.Thread(target=spy.serve_forever, daemon=True).start()
    spy_url = f"http://127.0.0.1:{spy.server_address[1]}"

    port = args.port or _free_port(18900)
    check(port >= 18900, "port must be >= 18900")
    gw = Gateway(data_dir, port, secrets.token_urlsafe(24), spy_url)
    results: List[tuple[str, bool, str]] = []
    state: Dict[str, Any] = {}

    def step(name: str, fn: Callable[[], str]) -> None:
        try:
            evidence = fn()
            results.append((name, True, evidence))
            print(f"PASS  {name}  {evidence}", flush=True)
        except Exception as e:  # noqa: BLE001 - one step's failure never hides the others
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {name}  {type(e).__name__}: {e}", flush=True)

    def create(request_id: str, flow_id: str, *, prompt: str, mode: str = "independent", notify: bool = False,
               trigger: Optional[Dict[str, Any]] = None) -> str:
        body = {
            "request_id": request_id,
            "title": f"Acceptance {request_id}",
            "target": {"bundle_ref": BUNDLE_REF, "flow_id": flow_id, "input_data": {"prompt": prompt, "notify": notify}},
            "trigger": trigger or {"source_id": "schedule", "source_version": 1, "config": {"every": every}},
            "context": {"mode": mode},
        }
        return gw.ok("POST", "/api/gateway/automations", body)["automation_id"]

    def occurrences(aid: str) -> List[Dict[str, Any]]:
        return gw.ok("GET", f"/api/gateway/automations/{aid}/occurrences?limit=200")["items"]

    def summary(aid: str) -> Dict[str, Any]:
        return gw.ok("GET", f"/api/gateway/automations/{aid}")["summary"]

    def command(aid: str, typ: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return gw.ok("POST", f"/api/gateway/automations/{aid}/commands", {"command_id": f"acc-{uuid.uuid4()}", "type": typ, "payload": payload or {}})

    def done_rows(aid: str, n: int) -> Optional[List[Dict[str, Any]]]:
        rows = [o for o in occurrences(aid) if o["status"] in ("completed", "failed", "cancelled")]
        return rows if len(rows) >= n else None

    def run_record(run_id: str) -> Dict[str, Any]:
        return gw.ok("GET", f"/api/gateway/runs/{run_id}")

    try:
        step("gateway-start", lambda: (gw.start(), f"port {port}, data {data_dir}")[1])

        def s_create() -> str:
            state["ind"] = create("independent", "echo", prompt="Report the memory usage.")
            state["gro"] = create("growing", "echo", prompt="Report the memory usage.", mode="growing", notify=True)
            caps = gw.ok("GET", "/api/gateway/discovery/capabilities")["capabilities"]["contracts"]["common"]["automations"]
            check(caps["available"] is True, "capability automations.available is not true")
            return f"independent {state['ind']}, growing {state['gro']}, every {every}"

        step("create-monitors", s_create)

        def s_first_ticks() -> str:
            wait_for("a first scheduled occurrence of each monitor",
                     lambda: done_rows(state["ind"], 1) and done_rows(state["gro"], 1), interval_s * 2 + 30)
            row = occurrences(state["ind"])[-1]
            check(row["trigger"]["summary"].startswith("schedule: every"), f"trigger summary {row['trigger']['summary']!r}")
            return f"first ticks done; summary {row['trigger']['summary']!r}"

        step("scheduled-ticks", s_first_ticks)

        def s_quiet_vs_notable() -> str:
            quiet = summary(state["ind"])["attention"]
            loud = summary(state["gro"])["attention"]
            check(quiet["unread"] is False and quiet["unseen_count"] == 0, f"quiet monitor has attention {quiet}")
            check(loud["unread"] is True and loud["items"] and loud["items"][0]["kind"] == "notify", f"notable monitor attention {loud}")
            return f"quiet unread=False; notable unread=True ({loud['unseen_count']} unseen)"

        step("quiet-vs-notable", s_quiet_vs_notable)

        def s_pause() -> str:
            command(state["ind"], "automation.pause")
            wait_for("paused status", lambda: summary(state["ind"])["status"] == "paused", 30)
            before = len(occurrences(state["ind"]))
            time.sleep(interval_s + 5)
            after = len(occurrences(state["ind"]))
            check(after == before, f"{after - before} occurrence(s) admitted while paused")
            state["paused_count"] = after
            return f"no scheduled admission in {interval_s + 5}s while paused ({after} occurrences)"

        step("pause", s_pause)

        def s_run_now_paused() -> str:
            command(state["ind"], "automation.run_now")
            rows = wait_for("the manual occurrence", lambda: done_rows(state["ind"], state["paused_count"] + 1), 60)
            check(rows[0]["trigger"]["source_id"] == "manual", f"newest occurrence trigger {rows[0]['trigger']}")
            check(summary(state["ind"])["status"] == "paused", "run_now un-paused the automation")
            state["paused_count"] += 1
            return f"one manual occurrence ({rows[0]['trigger']['summary']}); still paused"

        step("run-now-while-paused", s_run_now_paused)

        def s_resume() -> str:
            command(state["ind"], "automation.resume")
            wait_for("active status", lambda: summary(state["ind"])["status"] == "active", 30)
            time.sleep(3)
            check(len(occurrences(state["ind"])) == state["paused_count"], "resume fired an occurrence")
            nxt = summary(state["ind"]).get("next_fire_at")
            check(bool(nxt), "no next_fire_at after resume")
            return f"resumed without firing; next_fire_at {nxt}"

        step("resume-without-firing", s_resume)

        def s_revise() -> str:
            detail = gw.ok("GET", f"/api/gateway/automations/{state['ind']}")
            rev, binding = detail["definition"]["revision"], detail["definition"]["trigger"]["binding_id"]
            doubled = f"{interval_s * 2}s"
            status, out = gw.request("PATCH", f"/api/gateway/automations/{state['ind']}",
                                     {"command_id": "acc-stale", "expected_revision": rev + 5, "changes": {"title": "x"}})
            check(status == 409 and out["detail"]["reason_code"] == "revision_conflict", f"stale revise -> {status} {out}")
            gw.ok("PATCH", f"/api/gateway/automations/{state['ind']}",
                  {"command_id": "acc-revise", "expected_revision": rev,
                   "changes": {"trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": doubled}}}})
            new = wait_for("revision +1", lambda: (lambda d: d if d["definition"]["revision"] == rev + 1 else None)(
                gw.ok("GET", f"/api/gateway/automations/{state['ind']}")), 30)
            check(new["definition"]["trigger"]["config"]["every"] == doubled, "interval not revised")
            check(new["definition"]["trigger"]["binding_id"] != binding, "binding_id unchanged after a trigger revision")
            return f"revision {rev} -> {rev + 1}, every {doubled}, new binding_id"

        step("revise-interval", s_revise)

        def s_context_modes() -> str:
            wait_for("a second growing occurrence", lambda: done_rows(state["gro"], 2), interval_s * 2 + 30)
            gro_rows = occurrences(state["gro"])
            ind_rows = [o for o in occurrences(state["ind"]) if o["status"] == "completed"]
            check(all(o["answer"].startswith("ECHO[0]") for o in ind_rows), "an independent occurrence received history")
            newest = gro_rows[0]
            check(not newest["answer"].startswith("ECHO[0]"), f"growing occurrence saw no history: {newest['answer'][:80]}")
            check("assistant:ECHO[" in newest["answer"], "growing history lacks the previous answer")
            return f"independent answers start ECHO[0]; growing #{newest['index']} starts {newest['answer'][:8]}"

        step("independent-vs-growing", s_context_modes)

        def s_restart_mid_occurrence() -> str:
            start_at = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=3)).replace(microsecond=0).isoformat()
            aid = create("restart", "ask", prompt="Approve the report?",
                         trigger={"source_id": "schedule", "source_version": 1, "config": {"start_at": start_at, "every": every, "count": 1}})
            att = wait_for("the occurrence waiting on a person", lambda: (lambda a: a if a["pending_waits"] == 1 else None)(summary(aid)["attention"]), 60)
            wait = att["waits"][0]
            gw.stop()
            gw.start()
            gw.ok("POST", "/api/gateway/commands", {"command_id": f"acc-{uuid.uuid4()}", "run_id": wait["run_id"], "type": "resume",
                                                    "payload": {"wait_key": wait["wait_key"], "payload": {"response": "yes"}}})
            rows = wait_for("the answered occurrence to complete", lambda: done_rows(aid, 1), 60)
            children = gw.ok("GET", f"/api/gateway/runs?parent_run_id={aid}&limit=50&include_ledger_len=false")["items"]
            tick_children = [c for c in children if c.get("role") == "occurrence" and c.get("occurrence_index") == 1]
            check(len(tick_children) == 1, f"{len(tick_children)} children for occurrence 1")
            check(rows[0]["answer"] == "APPROVED:yes", f"answer {rows[0]['answer']!r}")
            state["restart"] = aid
            return f"waited on a person, restarted, answered: exactly 1 child ({tick_children[0]['run_id']})"

        step("restart-mid-occurrence", s_restart_mid_occurrence)

        def s_discussion() -> str:
            gro = state["gro"]
            out = gw.ok("POST", f"/api/gateway/automations/{gro}/discuss", {"request_id": f"acc-{uuid.uuid4()}", "occurrence_index": 1, "prompt": "Why this value?"})
            check(out["session_kind"] == "discussion", f"session_kind {out['session_kind']}")
            first = wait_for("discussion turn 1", lambda: (lambda r: r if r["status"] == "completed" else None)(run_record(out["run_id"])), 60)
            start = gw.ok("POST", "/api/gateway/runs/start", {"bundle_id": BUNDLE_REF, "flow_id": "echo", "session_id": out["session_id"],
                                                               "input_data": {"prompt": "And now?"}})
            second = wait_for("discussion turn 2", lambda: (lambda r: r if r["status"] == "completed" else None)(run_record(start["run_id"])), 60)
            answer2 = json.dumps(second.get("output"))
            check("Why this value?" in answer2, "turn 2 did not see turn 1")
            session_turns = gw.ok("GET", f"/api/gateway/runs?root_only=true&session_kind=chat,discussion&limit=200&include_ledger_len=false")["items"]
            ids = {r["run_id"] for r in session_turns}
            check(out["run_id"] in ids and start["run_id"] in ids, "discussion turns missing from the chat list")
            state["discussion"] = out
            state["discussion_runs"] = [out["run_id"], start["run_id"]]
            return f"two turns in {out['session_id']}; turn 2 saw turn 1; listed as chats"

        step("discussion-two-turns", s_discussion)

        def s_blocked_writes() -> str:
            aid = create("writer", "writer", prompt="written by the occurrence", trigger={"source_id": "manual", "source_version": 1, "config": {}})
            command(aid, "automation.run_now")
            rows = wait_for("the writer occurrence", lambda: done_rows(aid, 1), 60)
            check(rows[0]["status"] == "completed", f"writer occurrence {rows[0]['status']}")
            ws = Path(gw.ok("GET", f"/api/gateway/automations/{aid}")["definition"]["workspace_root"])
            note = ws / "note.txt"
            written = note.read_text() if note.exists() else ""
            # The occurrence's prompt is its user turn: the trigger line, then the prompt.
            check(written.endswith("\nwritten by the occurrence"), f"occurrence did not write its note ({written!r})")
            out = gw.ok("POST", f"/api/gateway/automations/{aid}/discuss", {"request_id": f"acc-{uuid.uuid4()}", "occurrence_index": 1, "prompt": "overwritten by the discussion"})
            run = wait_for("the discussion to end", lambda: (lambda r: r if r["status"] in ("completed", "failed") else None)(run_record(out["run_id"])), 60)
            check(note.read_text() == written, "the discussion wrote into the automation's workspace")
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            # The write node reports the refusal in its output (success: false)
            # or the run fails; either way nothing was written.
            refused = run["status"] == "failed" or output.get("success") is False
            check(refused, f"the discussion's write was not refused (run {run['status']}, output {output})")
            reason = run.get("error") or output.get("error") or ""
            return f"discussion write refused ({str(reason)[:80]}); note.txt unchanged"

        step("discussion-blocked-writes", s_blocked_writes)

        def s_attention() -> str:
            gro = state["gro"]
            page = gw.ok("GET", f"/api/gateway/automations/{gro}/attention?limit=1")
            check(page["items"] and page["items"][0]["kind"] == "notify", f"attention page {page}")
            last = page["items"][0]["cursor"]
            before = summary(gro)["attention"]["unseen_count"]
            gw.ok("POST", f"/api/gateway/automations/{gro}/seen", {"attention_cursor": last})
            after = summary(gro)["attention"]["unseen_count"]
            check(after == before - 1, f"acknowledging one item moved unseen {before} -> {after}")
            status, out = gw.request("GET", f"/api/gateway/automations?changed_since=2026-01-01T00:00:00Z")
            check(status == 422 and out["detail"]["reason_code"] == "unsupported_feature", f"changed_since -> {status}")
            return f"acknowledged {last}: unseen {before} -> {after}; changed_since -> 422"

        step("attention-items", s_attention)

        def _default_agent_automation(request_id: str, policy: Optional[Dict[str, Any]]) -> str:
            body: Dict[str, Any] = {
                "request_id": request_id,
                "title": f"Acceptance {request_id}",
                "target": {"flow_id": "@default", "interface": "abstractcode.agent.v1", "input_data": {"prompt": "check the machine"}},
                "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
            }
            if policy is not None:
                body["policy"] = policy
            aid = gw.ok("POST", "/api/gateway/automations", body)["automation_id"]
            command(aid, "automation.run_now")
            return aid

        def s_unattended_shell() -> str:
            aid = _default_agent_automation("shell-auto", None)
            check(gw.ok("GET", f"/api/gateway/automations/{aid}")["definition"]["policy"]["tool_approval"] == "auto", "default policy is not auto")
            rows = wait_for("the unattended shell tick", lambda: done_rows(aid, 1), 60)
            check(rows[0]["status"] == "completed" and rows[0]["waits"] == [], f"shell tick {rows[0]['status']} waits {rows[0]['waits']}")
            ledger = gw.ok("GET", f"/api/gateway/runs/{rows[0]['run_id']}/ledger?after=0&limit=1000")["items"]
            out = [str((r.get("result") or {}).get("results")) for r in ledger if ((r or {}).get("effect") or {}).get("type") == "tool_calls" and r.get("status") == "completed"]
            check(any("acceptance-shell-ok" in o for o in out), "execute_command did not run")
            return "@default target ran execute_command with no approval wait (policy auto, no client tool_policy)"

        step("unattended-shell-tick", s_unattended_shell)

        def s_tool_approval_by_kind() -> str:
            aid = _default_agent_automation("shell-ask", {"tool_approval": "ask"})
            att = wait_for("the tool approval wait", lambda: (lambda a: a if a["pending_waits"] == 1 else None)(summary(aid)["attention"]), 60)
            wait = att["waits"][0]
            check(wait["kind"] == "tool_approval" and [c["name"] for c in wait.get("details") or []] == ["execute_command"], f"wait {wait}")
            status, out = gw.request("POST", "/api/gateway/commands", {"command_id": f"acc-{uuid.uuid4()}", "run_id": wait["run_id"], "type": "resume",
                                                                        "payload": {"wait_key": wait["wait_key"], "payload": {"response": "approve"}}})
            check(status == 422 and out["detail"]["field"] == "payload", f"a {{response}} answer to a tool approval -> {status} {out}")
            gw.ok("POST", "/api/gateway/commands", {"command_id": f"acc-{uuid.uuid4()}", "run_id": wait["run_id"], "type": "resume",
                                                    "payload": {"wait_key": wait["wait_key"], "payload": {"approved": True}}})
            rows = wait_for("the approved tick", lambda: done_rows(aid, 1), 60)
            check(rows[0]["status"] == "completed", f"approved tick {rows[0]['status']}")
            return "ask: parked as tool_approval (execute_command); {response} -> 422; {approved: true} -> completed"

        step("tool-approval-by-kind", s_tool_approval_by_kind)

        def s_replay() -> str:
            runs = [occurrences(state["gro"])[0]["run_id"], *state["discussion_runs"]]
            lengths = {r: len(gw.ok("GET", f"/api/gateway/runs/{r}/ledger?after=0&limit=10000")["items"]) for r in runs}
            spy_before = len(_Spy.hits)
            for r in runs:
                gw.ok("GET", f"/api/gateway/runs/{r}/history_bundle?detail=replay")
            gw.ok("GET", f"/api/gateway/sessions/{state['discussion']['session_id']}/history/bloc?limit=10")
            after = {r: len(gw.ok("GET", f"/api/gateway/runs/{r}/ledger?after=0&limit=10000")["items"]) for r in runs}
            check(after == lengths, f"replay appended ledger records: {lengths} -> {after}")
            check(len(_Spy.hits) == spy_before == 0, f"provider calls: {_Spy.hits}")
            effects: List[str] = []
            for r in runs:
                for rec in gw.ok("GET", f"/api/gateway/runs/{r}/ledger?after=0&limit=10000")["items"]:
                    t = ((rec or {}).get("effect") or {}).get("type")
                    if t in ("llm_call", "tool_calls"):
                        effects.append(f"{r}:{t}")
            check(not effects, f"provider/tool effects recorded: {effects}")
            return f"replayed {len(runs)} runs: 0 provider calls, 0 tool calls, ledgers unchanged"

        step("replay-zero-calls", s_replay)
    finally:
        gw.stop()
        spy.shutdown()

    failed = [name for name, ok, _ in results if not ok]
    print(f"{'PASS' if not failed else 'FAIL'}  summary  {len(results) - len(failed)}/{len(results)} steps passed"
          + (f"; failed: {', '.join(failed)}" if failed else "") + f"; gateway log {data_dir / 'gateway.log'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
