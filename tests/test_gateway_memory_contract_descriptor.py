"""The memory contract descriptor names the CALLER's plane store (audit 2026-10-27 §2.1).

`contracts.common.memory` (served by `/discovery/capabilities`) used to resolve the store from
the ROOT data folder, so every account was told about the operator's store. The descriptor is
computed here under a bound principal, exactly as the capabilities route runs it (its worker
inherits the request's context); the route itself also probes remote music/voice catalogues,
which the network guard refuses."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _descriptor_for(principal) -> dict:
    from abstractgateway.routes import gateway as gateway_routes
    from abstractgateway.security.principal import (
        reset_current_gateway_principal,
        set_current_gateway_principal,
    )

    token = set_current_gateway_principal(principal)
    try:
        return gateway_routes._memory_contract_descriptor({"abstractmemory": {"installed": True}})
    finally:
        reset_current_gateway_principal(token)


@pytest.mark.parametrize("backend,leaf", [("lancedb", "kg"), ("sqlite", "kg.sqlite3")])
def test_memory_descriptor_path_is_the_callers_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, leaf: str
) -> None:
    from abstractgateway.security.principal import GatewayPrincipal, local_admin_principal

    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", backend)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_PATH", raising=False)

    alice = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",), runtime_id="alice")
    mine = _descriptor_for(alice)
    assert mine["backend"] == backend
    expected = (data / "users" / "default" / "alice" / "runtime" / "abstractmemory" / leaf).resolve()
    assert Path(mine["path"]) == expected

    admin = _descriptor_for(local_admin_principal())
    assert Path(admin["path"]) == (data / "abstractmemory" / leaf).resolve()

    # User accounts off: one plane, the data folder.
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "0")
    single = _descriptor_for(alice)
    assert Path(single["path"]) == (data / "abstractmemory" / leaf).resolve()
