"""A root event entry is already a listener; only other branches need children."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
from abstractgateway.stores import build_file_stores


def _node(node_id: str, node_type: str, **data: object) -> dict:
    return {"id": node_id, "type": node_type, "data": {"nodeType": node_type, **data}}


def _edge(source: str, target: str) -> dict:
    return {"source": source, "sourceHandle": "exec-out", "target": target, "targetHandle": "exec-in"}


def _load_host(tmp_path: Path, *, entry: str | None, extra_listener: bool = False) -> WorkflowBundleGatewayHost:
    # Put the event first so a flow without explicit entryNode exercises the
    # compiler's inferred entry, not merely the authoring JSON's declaration.
    nodes = [
        _node("listen", "on_event", eventConfig={"name": "fixture.ping", "scope": "session"}),
        _node("answer", "answer_user", pinDefaults={"message": "Event delivered", "level": "message"}),
        _node("end", "on_flow_end"),
    ]
    edges = [_edge("listen", "answer"), _edge("answer", "end")]
    if entry == "start":
        nodes.extend([_node("start", "on_flow_start"), _node("start-end", "on_flow_end")])
        edges.append(_edge("start", "start-end"))
    if extra_listener:
        nodes.extend([
            _node("other-listen", "on_event", eventConfig={"name": "fixture.other", "scope": "session"}),
            _node("other-end", "on_flow_end"),
        ])
        edges.append(_edge("other-listen", "other-end"))
    flow = {"id": "root", "name": "Event entry contract", "nodes": nodes, "edges": edges}
    if entry is not None:
        flow["entryNode"] = entry
    manifest = {
        "bundle_format_version": "1", "bundle_id": "event-contract", "bundle_version": "0.0.1",
        "created_at": "2026-09-20T00:00:00+00:00", "default_entrypoint": "root",
        "entrypoints": [{"flow_id": "root", "name": "Event entry contract", "interfaces": ["event"]}],
        "flows": {"root": "flows/root.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    with zipfile.ZipFile(bundles / "event-contract.flow", "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("flows/root.json", json.dumps(flow))
    data = tmp_path / "data"
    stores = build_file_stores(base_dir=data)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles, data_dir=data, run_store=stores.run_store,
        ledger_store=stores.ledger_store, artifact_store=stores.artifact_store,
    )


def _start(host: WorkflowBundleGatewayHost) -> tuple[str, object]:
    run_id = host.start_run(bundle_id="event-contract", flow_id="root", input_data={}, session_id="fixture-session")
    runtime, workflow = host.runtime_and_workflow_for_run(run_id)
    runtime.tick(workflow=workflow, run_id=run_id)
    return run_id, workflow


@pytest.mark.parametrize("entry", ["listen", None], ids=["explicit-event-entry", "inferred-event-entry"])
def test_root_event_entry_does_not_spawn_duplicate_listener(tmp_path: Path, entry: str | None) -> None:
    host = _load_host(tmp_path, entry=entry)
    root_id, workflow = _start(host)
    assert workflow.entry_node == "listen"
    assert host.event_listener_specs_by_root.get(workflow.workflow_id, []) == []
    runs = host.run_store.list_runs(limit=20)
    assert [run.run_id for run in runs] == [root_id]
    assert runs[0].status.value == "waiting"
    assert runs[0].waiting is not None
    assert runs[0].waiting.reason.value == "event"
    assert runs[0].waiting.wait_key.endswith(":fixture.ping")


@pytest.mark.parametrize("root_entry", ["start", "listen"])
def test_non_entry_event_branch_still_gets_its_own_listener(tmp_path: Path, root_entry: str) -> None:
    host = _load_host(tmp_path, entry=root_entry, extra_listener=root_entry == "listen")
    root_id, workflow = _start(host)
    expected_listener_entry = "listen" if root_entry == "start" else "other-listen"
    listener_ids = host.event_listener_specs_by_root.get(workflow.workflow_id, [])
    assert len(listener_ids) == 1
    assert host.specs[listener_ids[0]].entry_node == expected_listener_entry
    runs = host.run_store.list_runs(limit=20)
    assert len(runs) == 2
    children = [run for run in runs if run.parent_run_id == root_id]
    assert len(children) == 1
    assert children[0].workflow_id == listener_ids[0]
    assert children[0].current_node == expected_listener_entry
    assert children[0].status.value == "waiting"
    assert children[0].session_id == "fixture-session"
    assert children[0].waiting is not None
    assert children[0].waiting.wait_key.endswith(":fixture.ping" if root_entry == "start" else ":fixture.other")
