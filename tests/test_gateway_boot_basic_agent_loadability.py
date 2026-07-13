"""Boot loadability check for the default framework agent (flow c756 →
gateway c758, option (b)).

'Published basic-agent' means LOADS + DECLARES abstractcode.agent.v1 —
presence-only was the audited gap (flow c713 item 4: a corrupt file passed
boot then died as a load warning, and the three flows-dir envs bypassed even
the presence check). The check runs in GatewayHostConfig.from_env() on the
EFFECTIVE flows dir, whatever its source.

ENFORCEMENT SPLIT (one refusal site per fact — the ceiling-seam principle):
boot guarantees LOADABILITY (the loader can serve the file at all; corrupt =
refuse, never boot-then-die-later); AGENT-SUITABILITY (the interface
declaration) warns loudly at boot and REFUSES at the ruled pick-time
interface gate (c721/c722: config PUT edit-time + phase-session open
run-time). A boot-time interface refusal would be a second site for the same
fact and would break legitimate deployments whose basic-agent stand-in never
serves agent lanes (the gateway's own test fixtures are exactly that class).

Postures pinned here:
- present + valid + interface -> boots silently
- present + corrupt zip       -> RuntimeError at boot (never boot-then-die)
- present + no interface      -> boots with a LOUD warning naming the gate
- present + empty entrypoints -> RuntimeError (the reader's own manifest
                                 validation rejects the shape = unloadable)
- ABSENT from operator dir    -> boots with a loud warning (custom-bundle
                                 deployments stay legitimate; not silent)
- the SHIPPED bundle          -> passes its own check (default path unbroken)
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.config import (  # noqa: E402
    BASIC_AGENT_INTERFACE,
    GatewayHostConfig,
    _default_flows_dir,
    verify_basic_agent_loadable,
)


def _write_bundle(path: Path, *, interfaces=None, entrypoints=True, default_entrypoint="main") -> None:
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "basic-agent",
        "bundle_version": "0.0.0",
        "entrypoints": (
            [{"flow_id": "main", "interfaces": list(interfaces or [])}] if entrypoints else []
        ),
    }
    if default_entrypoint:
        manifest["default_entrypoint"] = default_entrypoint
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/main.flow.json", json.dumps({"id": "main", "nodes": [], "edges": []}))


def test_valid_bundle_with_interface_passes(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "basic-agent.flow", interfaces=[BASIC_AGENT_INTERFACE])
    verify_basic_agent_loadable(tmp_path)  # must not raise


def test_corrupt_bundle_refuses_boot(tmp_path: Path) -> None:
    (tmp_path / "basic-agent.flow").write_bytes(b"this is not a zip archive")
    with pytest.raises(RuntimeError) as exc:
        verify_basic_agent_loadable(tmp_path)
    assert "NOT USABLE" in str(exc.value)
    assert "refusing at boot" in str(exc.value)


def test_missing_interface_warns_loudly_and_boots(tmp_path: Path, caplog) -> None:
    """Interface REFUSAL belongs to the pick-time gate (c721/c722) — boot
    warns loudly (naming the gate consequence) and proceeds, so non-agent
    deployments with a stand-in basic-agent keep booting."""
    import logging

    _write_bundle(tmp_path / "basic-agent.flow", interfaces=["something.else.v1"])
    with caplog.at_level(logging.WARNING, logger="abstractgateway.config"):
        verify_basic_agent_loadable(tmp_path)  # must not raise
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert BASIC_AGENT_INTERFACE in joined and "interface gate" in joined


def test_no_entrypoints_refuses_boot(tmp_path: Path) -> None:
    # A manifest with zero entrypoints fails the bundle reader's own
    # validation (entrypoints must be a non-empty list) — that is the
    # UNLOADABLE class: refuse at boot, never boot-then-die.
    _write_bundle(tmp_path / "basic-agent.flow", entrypoints=False, default_entrypoint=None)
    with pytest.raises(RuntimeError) as exc:
        verify_basic_agent_loadable(tmp_path)
    assert "NOT USABLE" in str(exc.value)


def test_absent_bundle_warns_and_proceeds(tmp_path: Path, caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="abstractgateway.config"):
        verify_basic_agent_loadable(tmp_path)  # empty dir: no basic-agent.flow
    assert any("no basic-agent.flow" in r.getMessage() for r in caplog.records), (
        "absence from an operator-provided dir must be LOUD, never silent"
    )


def test_shipped_bundle_passes_its_own_check() -> None:
    """The default path must keep booting: the SHIPPED basic-agent loads and
    declares the interface (flow verified it by unzipping; this pins it in
    the gateway suite so a bad repack lands RED here)."""
    verify_basic_agent_loadable(Path(_default_flows_dir()))


def test_from_env_runs_the_check_on_env_provided_dirs(tmp_path: Path, monkeypatch) -> None:
    """The three env overrides used to bypass even the presence check —
    from_env now verifies the EFFECTIVE dir (corrupt bundle -> boot refusal)."""
    (tmp_path / "basic-agent.flow").write_bytes(b"garbage")
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(RuntimeError):
        GatewayHostConfig.from_env()
