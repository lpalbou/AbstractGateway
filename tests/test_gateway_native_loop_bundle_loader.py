"""Native-loop bundle loader (gateway loop-surfacing unit 4)."""

from __future__ import annotations

import builtins
import json
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _write_native_react_bundle(path: Path, *, bundle_id: str = "react-agent") -> None:
    _write_native_loop_bundle(path, bundle_id=bundle_id, factory="react", entrypoint="react")


def _write_native_loop_bundle(
    path: Path,
    *,
    bundle_id: str,
    factory: str,
    entrypoint: str | None = None,
) -> None:
    ep = entrypoint or factory
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.1.0",
        "created_at": "2026-07-28T00:00:00+00:00",
        "entrypoints": [
            {
                "flow_id": ep,
                "name": ep,
                "description": f"Native {factory} loop",
                "interfaces": ["abstractcode.agent.v1"],
            }
        ],
        "default_entrypoint": ep,
        "flows": {},
        "artifacts": {},
        "assets": {},
        "metadata": {
            "native_loop_factory": factory,
            "loop_family": factory,
        },
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))


def _write_visual_bundle(path: Path, *, bundle_id: str = "visual-demo") -> None:
    flow = {
        "id": "root",
        "name": "root",
        "entryNode": "start",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {"outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {"inputs": [{"id": "exec-in", "label": "", "type": "execution"}]},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.1",
        "created_at": "2026-07-28T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {"root": "flows/root.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))


def _load_host(tmp_path: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractgateway.stores import build_file_stores

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    stores = build_file_stores(base_dir=data_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=tmp_path / "bundles",
        data_dir=data_dir,
        run_store=stores.run_store,
        ledger_store=stores.ledger_store,
        artifact_store=stores.artifact_store,
    )


def test_native_react_bundle_registers_spec_without_visual_flows(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    _write_native_react_bundle(bundles_dir / "react-agent.flow")

    host = _load_host(tmp_path)

    assert "react-agent" in host.bundles
    assert "react-agent@0.1.0:react" in host.specs


@pytest.mark.parametrize(
    ("bundle_id", "factory"),
    [
        ("codeact-agent", "codeact"),
        ("memact-agent", "memact"),
    ],
)
def test_native_codeact_and_memact_bundles_register_specs(tmp_path: Path, bundle_id: str, factory: str) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    _write_native_loop_bundle(bundles_dir / f"{bundle_id}.flow", bundle_id=bundle_id, factory=factory)

    host = _load_host(tmp_path)

    assert bundle_id in host.bundles
    assert f"{bundle_id}@0.1.0:{factory}" in host.specs


def test_native_and_visual_bundles_load_together(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    _write_native_react_bundle(bundles_dir / "react-agent.flow")
    _write_visual_bundle(bundles_dir / "visual-demo.flow")

    host = _load_host(tmp_path)

    assert "react-agent@0.1.0:react" in host.specs
    assert "visual-demo@0.0.1:root" in host.specs


def test_empty_flows_without_native_factory_still_refuses_boot(tmp_path: Path) -> None:
    from abstractruntime.workflow_bundle import WorkflowBundleError

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "broken",
        "bundle_version": "0.0.1",
        "created_at": "2026-07-28T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / "broken.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(WorkflowBundleError, match="has no flows"):
        _load_host(tmp_path)


def test_normalize_agent_loop_input_maps_prompt_to_context_task() -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    vars0: dict = {"prompt": "Reply with exactly: REACT_SMOKE_OK"}
    WorkflowBundleGatewayHost._normalize_agent_loop_input(vars0)
    assert vars0["context"]["task"] == "Reply with exactly: REACT_SMOKE_OK"


def test_normalize_agent_loop_input_does_not_overwrite_existing_task() -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    vars0 = {"prompt": "new", "context": {"task": "keep", "messages": []}}
    WorkflowBundleGatewayHost._normalize_agent_loop_input(vars0)
    assert vars0["context"]["task"] == "keep"


def test_shipped_react_agent_bundle_file_loads(tmp_path: Path) -> None:
    """The repo-shipped react-agent@0.1.0.flow must load without the full bundles dir."""
    shipped = Path(__file__).resolve().parents[1] / "flows" / "bundles" / "react-agent@0.1.0.flow"
    if not shipped.is_file():
        pytest.skip("shipped react-agent bundle not present in this checkout")

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    bundles_dir.joinpath("react-agent@0.1.0.flow").write_bytes(shipped.read_bytes())

    host = _load_host(tmp_path)

    assert "react-agent" in host.bundles
    assert "react-agent@0.1.0:react" in host.specs


def test_native_loop_loader_falls_back_without_registry_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Older abstractagent builds (for example 0.3.12) still need to load shipped native bundles."""
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "abstractagent.adapters.native_loop_registry":
            err = ModuleNotFoundError("No module named 'abstractagent.adapters.native_loop_registry'")
            err.name = "abstractagent.adapters.native_loop_registry"
            raise err
        return original_import(name, globals, locals, fromlist, level)

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    _write_native_react_bundle(bundles_dir / "react-agent.flow")

    sys.modules.pop("abstractagent.adapters.native_loop_registry", None)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    host = _load_host(tmp_path)

    assert "react-agent" in host.bundles
    assert "react-agent@0.1.0:react" in host.specs


@pytest.mark.parametrize(
    ("filename", "bundle_key", "spec_id"),
    [
        ("codeact-agent@0.1.0.flow", "codeact-agent", "codeact-agent@0.1.0:codeact"),
        ("memact-agent@0.1.0.flow", "memact-agent", "memact-agent@0.1.0:memact"),
    ],
)
def test_shipped_codeact_memact_bundle_files_load(
    tmp_path: Path, filename: str, bundle_key: str, spec_id: str
) -> None:
    """Repo-shipped codeact/memact native-loop bundles must load like react-agent."""
    shipped = Path(__file__).resolve().parents[1] / "flows" / "bundles" / filename
    if not shipped.is_file():
        pytest.skip(f"shipped bundle not present: {filename}")

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    bundles_dir.joinpath(filename).write_bytes(shipped.read_bytes())

    host = _load_host(tmp_path)

    assert bundle_key in host.bundles
    assert spec_id in host.specs


def test_entrypoint_input_schema_for_native_loop_includes_prompt() -> None:
    from abstractgateway.hosts.native_loop_bundles import entrypoint_input_schema_for_native_loop

    schema = entrypoint_input_schema_for_native_loop(factory="react")
    assert schema["version"] == 1
    assert schema["input_data_schema"]["required"] == ["prompt"]
    pin_ids = [item["id"] for item in schema["inputs"]]
    assert pin_ids == ["prompt", "provider", "model"]


@pytest.mark.parametrize("factory", ["react", "codeact", "memact"])
def test_native_loop_input_schema_for_loaded_bundle(tmp_path: Path, factory: str) -> None:
    from abstractgateway.hosts.native_loop_bundles import (
        entrypoint_input_schema_for_native_loop,
        manifest_lists_entrypoint,
        native_loop_factory,
    )
    from abstractgateway.routes.gateway import _resolve_bundle_entrypoint

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    bundle_id = f"{factory}-agent"
    _write_native_loop_bundle(bundles_dir / f"{bundle_id}.flow", bundle_id=bundle_id, factory=factory)
    host = _load_host(tmp_path)

    bid_base, selected_ver, fid, bundle = _resolve_bundle_entrypoint(
        host=host,
        bundle_id=bundle_id,
        flow_id=factory,
    )
    assert bid_base == bundle_id
    assert selected_ver == "0.1.0"
    assert fid == factory
    assert native_loop_factory(bundle.manifest) == factory
    assert manifest_lists_entrypoint(bundle.manifest, factory)

    schema = entrypoint_input_schema_for_native_loop(factory=factory)
    assert schema["input_data_schema"]["required"] == ["prompt"]
    assert [item["id"] for item in schema["inputs"]] == ["prompt", "provider", "model"]


def test_unsupported_native_factory_skips_without_bricking_boot(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True)
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "bad-native",
        "bundle_version": "0.0.1",
        "created_at": "2026-07-28T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {},
        "artifacts": {},
        "assets": {},
        "metadata": {"native_loop_factory": "not-a-loop"},
    }
    with zipfile.ZipFile(bundles_dir / "bad-native.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    _write_visual_bundle(bundles_dir / "visual-demo.flow")

    host = _load_host(tmp_path)

    assert "bad-native" not in host.bundles
    assert "visual-demo@0.0.1:root" in host.specs
