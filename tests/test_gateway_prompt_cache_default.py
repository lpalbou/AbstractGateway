"""Gateway prompt-cache handoff defaults (backlog 0212).

Prompt caching defaults ON: the runtime derives a session-scoped cache key, so reuse
cannot cross sessions. Precedence at run start:
  explicit run-level `_runtime.prompt_cache` > ABSTRACTGATEWAY_PROMPT_CACHE env > default ON.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

pytestmark = pytest.mark.basic


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "cache-demo", flow_id: str = "root") -> Path:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": flow_id,
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
                "id": "e-start-end",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
    }
    manifest = {
        "bundle_format_version": 1,
        "bundle_id": bundle_id,
        "bundle_version": "0.0.1",
        "created_at": "2026-07-07T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return path


def _start_run_prompt_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env_value: Optional[str],
    input_data: Optional[Dict[str, Any]] = None,
) -> Any:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    if env_value is None:
        monkeypatch.delenv("ABSTRACTGATEWAY_PROMPT_CACHE", raising=False)
    else:
        monkeypatch.setenv("ABSTRACTGATEWAY_PROMPT_CACHE", env_value)

    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    run_store = InMemoryRunStore()
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=run_store,
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )
    run_id = host.start_run(flow_id="root", bundle_id="cache-demo", input_data=dict(input_data or {}))
    run = run_store.load(run_id)
    assert run is not None
    runtime_ns = run.vars.get("_runtime")
    assert isinstance(runtime_ns, dict)
    return runtime_ns.get("prompt_cache")


def test_prompt_cache_defaults_on_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start_run_prompt_cache(tmp_path, monkeypatch, env_value=None) == {"enabled": True, "version": 1}


def test_prompt_cache_env_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start_run_prompt_cache(tmp_path, monkeypatch, env_value="0") == {"enabled": False, "version": 1}


def test_prompt_cache_env_opt_in_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start_run_prompt_cache(tmp_path, monkeypatch, env_value="1") == {"enabled": True, "version": 1}


def test_prompt_cache_explicit_run_config_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Run-scoped explicit config (any shape, including bool False) must not be stomped
    # by the env default: the runtime consumes bool/dict `_runtime.prompt_cache` directly.
    result = _start_run_prompt_cache(
        tmp_path,
        monkeypatch,
        env_value="1",
        input_data={"_runtime": {"prompt_cache": False}},
    )
    assert result is False
