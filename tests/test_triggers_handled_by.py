"""Triggers page: Handled by + Use in a flow (operator 2026-10-25), at the gateway.

Every automation summary carries the workflow it runs (`target` {workflow_id, bundle_ref, flow_id},
never its inputs) and that entrypoint's name (`flow_name`), so the Triggers page lists
"<automation> · <flow>" per connection from ONE list read. A pick in Use in a flow is the
automations create route with the body the console builds (the flow's published defaults, this
trigger): the automation exists afterwards with this trigger and that target; a refused trigger
comes back as the route's 4xx envelope the console words. The name on Handled by's face opens the
flow in Flow (`/apps/flow/?flow=<id>`): the id is the library flow a published bundle keeps in
`metadata.source.root_flow_id`, read here off the real GET /bundles answer by the console's own
`trgLibraryIds` (operator 2026-10-26)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_BUNDLE_ID, ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle
from test_gateway_console_offline import _console_script, _node, _slice_function

pytestmark = pytest.mark.basic

DEFAULTS = {"schema_version": 1, "title": "Echo digest",
            "trigger": {"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": "author_conn"},
            "context": {"mode": "growing", "growing": {"max_tokens": 9000}}, "input_data": {"prompt": "from the defaults"}}


def _body(request_id: str, ref: str, trigger: dict) -> dict:
    """What the console sends for a pick (console_triggers.py trgUseFlowBody): the flow's name as
    the title, its defaults' inputs and context, this trigger."""
    return {"request_id": request_id, "title": ECHO_FLOW_ID,
            "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID, "input_data": dict(DEFAULTS["input_data"])},
            "trigger": trigger, "context": DEFAULTS["context"]}


def test_two_automations_on_one_connection_are_listed_with_their_flow_one_paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles", automation_defaults=DEFAULTS)
    from abstractgateway.app import app

    with TestClient(app) as c:
        # The menu's flows: triggerable entrypoints of GET /bundles, with their defaults.
        row = next(i for i in c.get("/api/gateway/bundles", headers=HEADERS).json()["items"] if i["bundle_ref"] == ref)
        assert [ep["flow_id"] for ep in row["entrypoints"] if ep["triggerable"]] == [ECHO_FLOW_ID]
        assert row["automation_defaults"][ECHO_FLOW_ID]["input_data"] == {"prompt": "from the defaults"}

        feed = c.post("/api/gateway/triggers/connections", headers=HEADERS, json={
            "source_id": "feed.item", "source_version": 1, "label": "Python news", "config": {"url": "http://127.0.0.1:9/feed.xml"}})
        assert feed.status_code == 201, feed.text
        cid = feed.json()["connection_id"]
        trigger = {"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": cid}
        first = c.post("/api/gateway/automations", headers=HEADERS, json=_body("pick-1", ref, trigger))
        assert first.status_code == 200, first.text
        second = c.post("/api/gateway/automations", headers=HEADERS, json={**_body("pick-2", ref, trigger), "title": "Morning brief"})
        assert second.status_code == 200, second.text
        a1, a2 = first.json()["automation_id"], second.json()["automation_id"]

        # The create path: the automation exists with THIS trigger and that target, turned on.
        detail = c.get(f"/api/gateway/automations/{a1}", headers=HEADERS).json()
        assert detail["definition"]["trigger"]["source_id"] == "feed.item"
        assert detail["definition"]["trigger"]["config"]["connection_id"] == cid
        assert detail["definition"]["target"]["bundle_ref"] == ref and detail["definition"]["target"]["flow_id"] == ECHO_FLOW_ID
        assert detail["definition"]["target"]["input_data"]["prompt"] == "from the defaults"
        assert detail["definition"]["context"]["mode"] == "growing"
        assert detail["summary"]["status"] == "active" and detail["summary"]["title"] == ECHO_FLOW_ID

        r = c.post(f"/api/gateway/automations/{a2}/commands", headers=HEADERS, json={"command_id": "p1", "type": "automation.pause", "payload": {}})
        assert r.status_code == 200, r.text
        wait_until(lambda: c.get(f"/api/gateway/automations/{a2}", headers=HEADERS).json()["summary"]["status"] == "paused")

        listed = c.get("/api/gateway/automations?status=active,paused&limit=200", headers=HEADERS).json()["items"]
        on_feed = {a["automation_id"]: a for a in listed if (a["trigger"].get("config") or {}).get("connection_id") == cid}
        assert set(on_feed) == {a1, a2}
        assert on_feed[a1]["status"] == "active" and on_feed[a2]["status"] == "paused"
        for a in on_feed.values():
            assert a["target"] == {"workflow_id": f"{ref}:{ECHO_FLOW_ID}", "bundle_ref": ref, "flow_id": ECHO_FLOW_ID}
            assert a["flow_name"] == ECHO_FLOW_ID  # the entrypoint's name in the bundle (write_echo_bundle names it by id)
            assert "input_data" not in a["target"]


def test_a_refused_trigger_comes_back_as_the_routes_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    ref = write_echo_bundle(tmp_path / "bundles", automation_defaults=DEFAULTS)
    from abstractgateway.app import app

    with TestClient(app) as c:
        mail = {"source_id": "email.received", "source_version": 1, "config": {"account": "nosuchbox"}}
        r = c.post("/api/gateway/automations", headers=HEADERS, json=_body("pick-mail", ref, mail))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["reason_code"] == "email_account_not_found"
        gone = {"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": "conn_not_mine"}
        r = c.post("/api/gateway/automations", headers=HEADERS, json=_body("pick-gone", ref, gone))
        assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "connection_not_found", r.text
        assert c.get("/api/gateway/automations", headers=HEADERS).json()["items"] == []


def test_flow_name_reads_the_entrypoint_name_of_the_version_it_runs() -> None:
    from types import SimpleNamespace

    from abstractgateway.routes.automations import _flow_name, _target_projection

    ep = lambda fid, name: SimpleNamespace(flow_id=fid, name=name)  # noqa: E731
    bundle = SimpleNamespace(manifest=SimpleNamespace(entrypoints=[ep("main", "Release digest"), ep("other", "")]))
    svc = SimpleNamespace(host=SimpleNamespace(bundles={"digest": {"1.2.0": bundle}}))
    assert _flow_name(svc, {"bundle_ref": "digest@1.2.0", "flow_id": "main"}) == "Release digest"
    assert _flow_name(svc, {"bundle_ref": "digest@1.2.0", "flow_id": "other"}) is None
    assert _flow_name(svc, {"bundle_ref": "digest@9.9.9", "flow_id": "main"}) is None
    assert _flow_name(svc, None) is None
    assert _target_projection({"workflow_id": "w", "bundle_ref": "b@1", "flow_id": "f", "input_data": {"secret": 1}}) == {"workflow_id": "w", "bundle_ref": "b@1", "flow_id": "f"}


def _library_flow(name: str) -> dict:
    """A triggered library flow (its automation defaults carry a trigger): start → end."""
    return {
        "name": name,
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 200, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [{"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
        "automation_defaults": {"schema_version": 1, "title": name, "trigger": {"source_id": "manual", "source_version": 1, "config": {}}},
    }


def test_the_face_links_a_published_flow_to_its_library_flow_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    write_echo_bundle(tmp_path / "bundles", automation_defaults=DEFAULTS)  # on disk, not published from Flow
    from abstractgateway.app import app

    with TestClient(app) as c:
        created = c.post("/api/gateway/visualflows", headers=HEADERS, json=_library_flow("Judge what arrived"))
        assert created.status_code == 200, created.text
        flow_id = created.json()["id"]
        pub = c.post(f"/api/gateway/visualflows/{flow_id}/publish", headers=HEADERS,
                     json={"bundle_id": "judge", "bundle_version": "0.0.1", "overwrite": True, "reload_gateway": True})
        assert pub.status_code == 200, pub.text
        items = c.get("/api/gateway/bundles", headers=HEADERS).json()["items"]
        judge = next(i for i in items if i["bundle_id"] == "judge")
        # What the console reads: published from Flow, the library flow's id, a triggerable entrypoint.
        assert judge["source"] == "published" and judge["metadata"]["source"]["root_flow_id"] == flow_id
        assert [ep["triggerable"] for ep in judge["entrypoints"]] == [True]
        echo = next(i for i in items if i["bundle_id"] == ECHO_BUNDLE_ID)
        assert echo["source"] != "published"
        # The console's own mapping, run on this answer: only the published bundle has a library flow.
        source = _console_script()
        out = _node(_slice_function(source, "trgLibraryIds") + _slice_function(source, "trgLibraryPath")
                    + f"\nconst ids = trgLibraryIds({json.dumps(items)});\nconsole.log(JSON.stringify([ids, trgLibraryPath(ids.judge)]));\n")
        assert out == [{"judge": flow_id}, f"/?flow={flow_id}"]
        # The id Flow opens (GET /visualflows/{id}, its library's load path) is that flow.
        got = c.get(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS)
        assert got.status_code == 200 and got.json()["name"] == "Judge what arrived"
        # And the app door takes the link form (POST /apps/flow/open {path: "/?flow=<id>"}).
        assert "trgOpenApp(\"flow\", \"Flow\", trgLibraryPath(t.dataset.trgOpenflow), trgRowKey(t))" in source
