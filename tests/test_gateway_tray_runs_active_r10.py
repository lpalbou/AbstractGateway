"""R10.2 (operator 2026-10-04): the tray's Workflows submenu said "No runs in
the last 24 hours" while two or three runs were active on the gateway.

THE CAUSES, each pinned below against real run stores (file and SQLite):

1. Automation occurrence runs have their CONTROLLER as parent. The host
   listing kept only parent-less runs, so every automation run vanished. The
   runtime's own definition of "a run a person started" is `is_turn_root`
   (parent-less non-controller runs, plus occurrences).
2. The listing asked the store for the 25 most recently updated runs of ANY
   kind and filtered children/machinery afterwards: a few busy runs (whose
   sub-runs are the rows that keep being updated) filled the page.
3. The 24 h window was applied to the START time of every run, so a run
   started yesterday morning and still going was hidden.
4. A root waiting on its running sub-run read as idle.

Then the menu: running first with the elapsed time, recent finished ones
after, "Open in Observer" per run — read through a FAKE GATEWAY over HTTP
(the real tray client and sampler, no store access).
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _run(run_id: str, workflow_id: str, status: str, *, created_h: float, updated_h: float, parent: Optional[str] = None, meta: Optional[Dict[str, Any]] = None, wait: Optional[str] = None):
    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState

    vars_: Dict[str, Any] = {}
    if meta:
        vars_["_meta"] = meta
    return RunState(
        run_id=run_id,
        workflow_id=workflow_id,
        status=RunStatus(status),
        current_node="n",
        vars=vars_,
        waiting=WaitState(reason=WaitReason(wait)) if wait else None,
        created_at=_iso(created_h),
        updated_at=_iso(updated_h),
        parent_run_id=parent,
    )


def _seed(store: Any, base: Optional[Path]) -> None:
    """The operator's machine, reduced: two automations firing, one chat whose
    sub-run is working, plenty of busy children, machinery, old history."""
    runs = [
        # Automation A: controller (3 days old, waiting for its schedule) and
        # its running occurrence — the run the operator was watching.
        _run("ctl-a", "daily-digest@1.0.0:main", "waiting", created_h=72, updated_h=0.5, meta={"automation": {"id": "a"}}, wait="event"),
        _run("occ-a", "daily-digest@1.0.0:main", "running", created_h=0.2, updated_h=0.01, parent="ctl-a", meta={"occurrence": {"automation_id": "ctl-a", "occurrence_index": 7, "role": "occurrence"}}),
        # Automation B: an occurrence waiting on its sub-run, which runs.
        _run("ctl-b", "inbox-triage@1.0.0:main", "waiting", created_h=50, updated_h=1, meta={"automation": {"id": "b"}}, wait="event"),
        _run("occ-b", "inbox-triage@1.0.0:main", "waiting", created_h=0.1, updated_h=0.09, parent="ctl-b", meta={"occurrence": {"automation_id": "ctl-b", "occurrence_index": 3, "role": "occurrence"}}, wait="subworkflow"),
        _run("occ-b-sub", "visual_react_agent_inbox", "running", created_h=0.09, updated_h=0.001, parent="occ-b", meta={"occurrence": {"automation_id": "ctl-b", "occurrence_index": 3, "role": "descendant"}}),
        # A chat turn started 30 h ago, still going through its sub-run.
        _run("chat-long", "deep-research@0.2.0:main", "waiting", created_h=30, updated_h=29, wait="subworkflow"),
        _run("chat-long-sub", "deep-research@0.2.0:worker", "running", created_h=29, updated_h=0.002, parent="chat-long"),
        # A person was asked something an hour ago (waiting, inside the window)...
        _run("ask-now", "basic-agent@0.0.5:x", "waiting", created_h=1.5, updated_h=1.0, wait="user"),
        # ...and an ask from August is history, not "now".
        _run("ask-august", "basic-agent@0.0.4:x", "waiting", created_h=900, updated_h=880, wait="user"),
        # Finished: inside the window (two of them), and outside it.
        _run("done-2h", "coding-agent@0.2.7:coder", "completed", created_h=2.5, updated_h=2.0),
        _run("failed-5h", "coding-agent@0.2.7:coder", "failed", created_h=5.5, updated_h=5.0),
        _run("done-started-yesterday", "deep-research@0.2.0:main", "completed", created_h=26, updated_h=3.0),
        _run("done-3d", "coding-agent@0.2.7:coder", "completed", created_h=73, updated_h=72),
        # Machinery is never a row.
        _run("mem", "__session_memory__", "running", created_h=0.05, updated_h=0.0005),
    ]
    # 40 finished children, all updated in the last minutes: the rows that
    # filled the old "25 most recent runs" page.
    for i in range(40):
        runs.append(_run(f"child-{i:02d}", "wf_abstractcore_run_facade_tts", "completed", created_h=0.3, updated_h=0.0001 * (i + 1), parent="chat-long-sub"))
    for r in runs:
        store.save(r)
    if base is not None:
        # The file store ranks by mtime (≈ updated_at): make it agree.
        for r in runs:
            ts = datetime.fromisoformat(r.updated_at).timestamp()
            os.utime(base / f"run_{r.run_id}.json", (ts, ts))


def _store(kind: str, tmp_path: Path):
    from abstractgateway.stores import build_file_stores, build_sqlite_stores

    if kind == "file":
        stores = build_file_stores(base_dir=tmp_path)
        return stores.run_store, tmp_path.resolve()
    stores = build_sqlite_stores(base_dir=tmp_path, db_path=tmp_path / "gateway.sqlite3")
    return stores.run_store, None


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_active_runs_lead_and_nothing_a_person_started_is_lost(kind: str, tmp_path: Path) -> None:
    from abstractgateway.admin_runtimes import recent_runs_host_wide

    store, base = _store(kind, tmp_path)
    _seed(store, base)

    out = recent_runs_host_wide(data_dir=tmp_path, limit=25, since_epoch=NOW.timestamp() - 24 * 3600, default_run_store=store)
    ids = [r["run_id"] for r in out["items"]]
    acts = {r["run_id"]: r["activity"] for r in out["items"]}

    # (1) automation occurrences are rows; their controllers are not.
    assert "occ-a" in ids and "occ-b" in ids, ids
    assert "ctl-a" not in ids and "ctl-b" not in ids
    # (4) a root whose sub-run works is RUNNING, not waiting.
    assert acts["occ-b"] == "running" and acts["chat-long"] == "running"
    # (3) a run started 30 h ago and still going is listed.
    assert "chat-long" in ids
    # (2) children never fill the page; machinery is never a row.
    assert not any(i.startswith("child-") or i.endswith("-sub") for i in ids)
    assert "mem" not in ids
    # Waiting for a person: inside the window yes, August no.
    assert acts["ask-now"] == "waiting" and "ask-august" not in ids
    # Finished: windowed on the LAST update.
    assert {"done-2h", "failed-5h", "done-started-yesterday"} <= set(ids)
    assert "done-3d" not in ids

    # Order: running (newest start first), waiting, finished (newest update first).
    assert ids == ["occ-b", "occ-a", "chat-long", "ask-now", "done-2h", "done-started-yesterday", "failed-5h"], ids
    assert out["active_count"] == 4
    assert all(r["observer_path"] == f"/apps/observer/#run/{r['run_id']}" for r in out["items"])


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_limit_cuts_finished_runs_never_active_ones(kind: str, tmp_path: Path) -> None:
    from abstractgateway.admin_runtimes import recent_runs_host_wide

    store, base = _store(kind, tmp_path)
    _seed(store, base)
    out = recent_runs_host_wide(data_dir=tmp_path, limit=2, since_epoch=NOW.timestamp() - 24 * 3600, default_run_store=store)
    assert [r["run_id"] for r in out["items"]] == ["occ-b", "occ-a", "chat-long", "ask-now"]
    assert out["has_more"] is True


# ---------------------------------------------------- the menu, over HTTP


class _FakeGateway:
    """Answers the tray's `GET /api/gateway/host/runs` with active + recent
    runs, and records `POST /apps/observer/open` bodies."""

    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self.items = items
        self.asked: List[str] = []
        self.opened: List[Dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def _send(self, code: int, body: Dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                outer.asked.append(self.path)
                if self.path.startswith("/api/gateway/host/runs"):
                    self._send(200, {"ok": True, "items": outer.items, "count": len(outer.items), "active_count": 3})
                else:
                    self._send(404, {"detail": "not here"})

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/gateway/apps/observer/open":
                    outer.opened.append(body)
                    self._send(200, {"ok": True, "open_url": "/apps/handover/abc"})
                else:
                    self._send(404, {"detail": "not here"})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def _fake_items() -> List[Dict[str, Any]]:
    t = time.time()

    def iso(sec_ago: float) -> str:
        return datetime.fromtimestamp(t - sec_ago, timezone.utc).isoformat()

    # The ORDER a gateway sends is not trusted for the active-first rule.
    return [
        {"run_id": "done-1", "label": "coding-agent:coder", "status": "completed", "activity": "done", "created_at": iso(7200), "updated_at": iso(7000), "ledger_len": 12, "observer_path": "/apps/observer/#run/done-1"},
        {"run_id": "occ-1", "label": "daily-digest:main", "status": "running", "activity": "running", "created_at": iso(754), "updated_at": iso(2), "ledger_len": 4, "observer_path": "/apps/observer/#run/occ-1"},
        {"run_id": "chat-1", "label": "deep-research:main", "status": "waiting", "activity": "running", "created_at": iso(30 * 3600), "updated_at": iso(29 * 3600), "ledger_len": 31, "observer_path": "/apps/observer/#run/chat-1"},
        {"run_id": "ask-1", "label": "basic-agent:x", "status": "waiting", "activity": "waiting", "created_at": iso(3600), "updated_at": iso(3500), "observer_path": "/apps/observer/#run/ask-1"},
        {"run_id": "old-1", "label": "coding-agent:coder", "status": "completed", "activity": "done", "created_at": iso(30 * 3600), "updated_at": iso(29 * 3600)},
    ]


def _render(app: Any) -> List[Any]:
    """The real render path with a pystray stub: returns the item tree."""
    import sys
    import types

    class _MenuItem:
        def __init__(self, text, action=None, **kw):
            self.text, self.action, self.kw = str(text), action, kw

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    stub = types.ModuleType("pystray")
    stub.MenuItem = _MenuItem  # type: ignore[attr-defined]
    stub.Menu = _Menu  # type: ignore[attr-defined]
    saved = sys.modules.get("pystray")
    sys.modules["pystray"] = stub
    try:
        return list(app._menu_items())
    finally:
        if saved is None:
            sys.modules.pop("pystray", None)
        else:
            sys.modules["pystray"] = saved


def _sub(items: List[Any], label: str) -> List[Any]:
    for it in items:
        if getattr(it, "text", None) == label:
            return list(it.action.items)
    raise AssertionError(f"no {label!r} in {[getattr(i, 'text', '') for i in items]}")


def test_the_menu_reads_a_fake_gateway_running_first_with_elapsed_time_and_observer(tmp_path: Path) -> None:
    from abstractgateway.tray import app as tray_app
    from abstractgateway.tray.apps import AppEntry
    from abstractgateway.tray.client import GatewayClient
    from abstractgateway.tray.sampler import Sampler

    gw = _FakeGateway(_fake_items())
    try:
        client = GatewayClient(gw.url, "t")
        sampler = Sampler(client)
        sampler._sample_runs()  # the slow lane's run sample, over HTTP
        assert any(p.startswith("/api/gateway/host/runs") for p in gw.asked)
        snap = sampler.snapshot()
        assert [r.run_id for r in snap.runs] == ["occ-1", "chat-1", "ask-1", "done-1"]

        app = tray_app.TrayApp({"base_url": gw.url, "token": "t", "data_dir": str(tmp_path)})
        app.client = client
        app.sampler = sampler
        app._snap = dataclasses.replace(snap, gateway_state="running", reachable=True)
        app._tk = False
        app._apps = (AppEntry(id="observer", name="Observer", status="running", source="gateway"),)
        app._apps_fetched = True
        items = _render(app)
        wf = _sub(items, "Workflows")
        labels = [getattr(i, "text", "") for i in wf if hasattr(i, "text")]

        assert labels[0] == "Now: 2 running · 1 waiting — last 24 hours: 1 done", labels
        runs = [lbl for lbl in labels if lbl[:1] in {"🟢", "🟡", "✅", "❌", "⚪"}]
        assert runs[0].startswith("🟢 daily-digest:main · 4 steps · 12m ") and runs[0].endswith(" so far"), runs
        assert runs[1].startswith("🟢 deep-research:main · 31 steps · 30h ") and runs[1].endswith(" so far"), runs
        assert runs[2].startswith("🟡 basic-agent:x · 1h 00m so far") or runs[2].startswith("🟡 basic-agent:x · 59m"), runs
        assert runs[3] == "✅ coding-agent:coder · 12 steps · 3m 20s", runs
        assert "No runs in the last 24 hours" not in labels

        first = _sub(wf, runs[0])
        assert first[0].text.startswith("Running · started ")
        open_item = next(i for i in first if i.text == "Open in Observer")
        assert open_item.kw.get("enabled", True) is True

        # Clicking it asks the gateway's app door for the run's page inside Observer.
        done = threading.Event()
        opened: List[str] = []
        app._open_signed_in = lambda url: (opened.append(url), done.set())  # type: ignore[method-assign]
        app._bg = lambda fn, name: fn()  # type: ignore[method-assign]
        app.dispatch(("open_run_observer", "occ-1"))
        assert gw.opened == [{"remember": True, "path": "/#run/occ-1"}]
        assert opened == ["/apps/handover/abc"]
    finally:
        gw.close()


def test_open_in_observer_is_greyed_when_observer_cannot_open(tmp_path: Path) -> None:
    from abstractgateway.tray.menu_model import observer_open_available, workflow_items
    from abstractgateway.tray.apps import AppEntry
    from abstractgateway.tray.sampler import RunRow
    from test_gateway_tray_helper import _snap

    snap = _snap(runs=(RunRow("r", "w", "running", 1, 5.0, time.time() - 5, observer_path="/apps/observer/#run/r"),))
    assert observer_open_available(()) is False
    assert observer_open_available((AppEntry(id="observer", name="Observer", status="not_installed", source=""),)) is False
    assert observer_open_available((AppEntry(id="observer", name="Observer", status="stopped", source="gateway"),)) is True

    def open_node(observer: bool):
        row = [n for n in workflow_items(snap, observer=observer) if n.children][0]
        return next(c for c in row.children if c.label == "Open in Observer")

    assert open_node(False).enabled is False
    assert open_node(True).enabled is True
