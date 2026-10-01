"""Workflows page backend (DESIGN-v2 §4, §6, item 2): every agent-default row carries the plain
interface name, the app, one sentence of help, its group and a state that is never a warning
when nothing is set ("clients_choose"); "broken" only when a saved value no longer resolves.
`GET /bundles` items carry `source` (shipped / published / imported) and `description`."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from test_gateway_default_agent_workflow import ASSIST, CODE, _client, standard_bundles, write_bundle


def _payload(tmp_path: Path, stored: dict | None = None, *, drop_coder: bool = False):
    from abstractgateway.agent_defaults import default_workflows_payload, disk_entrypoint_index
    from abstractgateway.runtime_config import write_runtime_config

    bundles = tmp_path / "bundles"
    standard_bundles(bundles)
    write_bundle(bundles, bundle_id="custom", version="1.0.0",
                 entrypoints=[{"flow_id": "c", "name": "Custom", "interfaces": ["acme.custom.v1"]}], default_entrypoint="c")
    data_dir = tmp_path / "data"
    if stored:
        write_runtime_config(data_dir, {"agents": {"default_workflow": stored}}, actor="t", agent_index=disk_entrypoint_index([bundles]))
    if drop_coder:
        for p in bundles.glob("coder@*.flow"):
            p.unlink()
    return default_workflows_payload(disk_entrypoint_index([bundles]), data_dir)["default_workflow"]


def test_unset_rows_are_builtin_or_clients_choose_never_a_warning(tmp_path: Path) -> None:
    rows = _payload(tmp_path)
    code = rows[CODE]
    assert code["state"] == "builtin" and code["value"] == "basic-agent:ba" and code["reason"] is None
    assert (code["interface"], code["label"], code["app"], code["group"]) == (CODE, "AbstractCode — chat agent", "AbstractCode", "apps")
    assert code["help"].endswith("a prompt in, a reply out.")
    assist = rows[ASSIST]
    assert assist["state"] == "clients_choose" and assist["reason"] is None and assist["value"] is None
    assert (assist["label"], assist["group"]) == ("Assistant", "apps")
    custom = rows["acme.custom.v1"]
    assert custom["state"] == "clients_choose" and custom["group"] == "other" and custom["label"] == "acme.custom.v1"
    assert custom["help"] == "Declared by the custom workflow; no app asks for it by default."


def test_saved_rows_are_set_and_turn_broken_when_the_workflow_goes(tmp_path: Path) -> None:
    rows = _payload(tmp_path, {CODE: "coder:code"})
    assert rows[CODE]["state"] == "set" and rows[CODE]["value"] == "coder:code" and rows[CODE]["reason"] is None
    broken = _payload(tmp_path / "again", {CODE: "coder:code"}, drop_coder=True)[CODE]
    assert broken["state"] == "broken" and broken["value"] == "coder:code"
    assert broken["reason"] == (
        "Broken: workflow bundle 'coder' is not on this gateway — pick another workflow or choose “Clients choose”."
    )


def test_interface_table_covers_every_interface_a_shipped_workflow_declares() -> None:
    from abstractgateway.agent_defaults import INTERFACE_TABLE, disk_entrypoint_index
    from abstractgateway.config import _default_flows_dir

    declared = {i for r in disk_entrypoint_index([Path(_default_flows_dir())]) for i in r.get("interfaces") or []}
    assert declared, "the shipped flows declare interfaces"
    assert declared <= set(INTERFACE_TABLE), sorted(declared - set(INTERFACE_TABLE))
    for iface, row in INTERFACE_TABLE.items():
        assert row["label"] and row["help"] and row["group"] in ("apps", "other"), iface
        assert row["help"].endswith("."), iface


def _bundle(path: Path, *, bundle_id: str, version: str, description: str, metadata: dict) -> None:
    flow = {
        "id": "f", "name": "F", "description": description, "interfaces": [],
        "nodes": [
            {"id": "n1", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "n2", "type": "on_flow_end", "position": {"x": 200, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "end", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [{"id": "e1", "source": "n1", "sourceHandle": "exec-out", "target": "n2", "targetHandle": "exec-in"}],
        "entryNode": "n1",
    }
    manifest = {
        "bundle_format_version": "1", "bundle_id": bundle_id, "bundle_version": version, "created_at": "2026-09-25T00:00:00+00:00",
        "entrypoints": [{"flow_id": "f", "name": "F", "description": description, "interfaces": []}],
        "default_entrypoint": "f", "flows": {"f": "flows/f.json"}, "artifacts": {}, "assets": {}, "metadata": metadata,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/f.json", json.dumps(flow))


def test_bundles_carry_source_and_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.workflow_sources import shipped_bundle_names

    assert "deep-research@0.1.8.flow" in shipped_bundle_names()
    bundles = tmp_path / "bundles"
    standard_bundles(bundles)
    published = {"lifecycle": {"channel": "published", "source": "abstractflow.editor"},
                 "publisher": {"host": "abstractgateway", "published_at": "2026-09-30T00:00:00+00:00"}}
    _bundle(bundles / "mine@1.0.0.flow", bundle_id="mine", version="1.0.0", description="Sorts my invoices.", metadata=published)
    _bundle(bundles / "upload@1.0.0.flow", bundle_id="upload", version="1.0.0", description="An uploaded one.", metadata={})
    # A file the package ships (same name as the wheel's): shipped, whatever folder serves it.
    _bundle(bundles / "deep-research@0.1.8.flow", bundle_id="deep-research", version="0.1.8", description="Research.", metadata=published)
    client, h = _client(tmp_path, monkeypatch)
    with client:
        items = {it["bundle_id"]: it for it in client.get("/api/gateway/bundles", headers=h).json()["items"]}
    assert items["mine"]["source"] == "published" and items["mine"]["description"] == "Sorts my invoices."
    assert items["upload"]["source"] == "imported" and items["upload"]["description"] == "An uploaded one."
    assert items["deep-research"]["source"] == "shipped"
    assert items["coder"]["source"] == "imported" and items["coder"]["description"] == ""


def test_every_bundle_the_checkout_keeps_in_its_flows_folder_is_shipped() -> None:
    """Adversary pass 2 (F4): map-reduce, structured-extract, adversarial-review and the meta-*
    agents live in the gateway's own flows/bundles but not in the wheel force-include list; on a
    repository-checkout deploy they are still files this gateway ships, never "Imported"."""
    from abstractgateway.workflow_sources import bundle_source, shipped_bundle_names

    repo = Path(__file__).resolve().parent.parent
    tracked = {p.name for p in (repo / "flows" / "bundles").glob("*.flow")}
    for name in ("map-reduce@0.1.0.flow", "structured-extract@0.1.0.flow", "adversarial-review@0.1.0.flow",
                 "meta-debate@0.1.1.flow", "meta-baseline@0.1.1.flow", "co-scientist@0.1.0.flow"):
        assert name in tracked, name
        assert bundle_source(str(repo / "flows" / "bundles" / name), {}) == "shipped", name
    assert tracked <= shipped_bundle_names(), sorted(tracked - shipped_bundle_names())


def test_checkout_list_is_the_gitignore_negations_plus_the_wheel_list(tmp_path: Path) -> None:
    from abstractgateway.workflow_sources import checkout_shipped_names

    assert checkout_shipped_names(tmp_path) == frozenset()  # not a checkout
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel.force-include]\n"flows/bundles/a@1.0.0.flow" = "abstractgateway/flows/bundles/a@1.0.0.flow"\n'
    )
    (tmp_path / ".gitignore").write_text("flows/bundles/*\n!flows/bundles/b@0.1.0.flow\n!docs/keep.md\n# !flows/bundles/c@1.flow\n")
    # An import written into the folder (c, d) is git-ignored: on neither list.
    assert checkout_shipped_names(tmp_path) == frozenset({"a@1.0.0.flow", "b@0.1.0.flow"})
