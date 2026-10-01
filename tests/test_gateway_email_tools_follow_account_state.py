"""The agents' email tools follow the account state (operator report 2026-10-01, Mac mini).

The Run settings of AbstractCode listed `send_email` as enabled (GET /discovery/tools decides
per request) while the run never received it: the agents' tool LISTS are built with the host,
and only the "Agent email tools" switch reloaded it — a mailbox connected, paused or
disconnected afterwards, or an admin's capability change, left the lists stale.

Now (a) every route that moves the rule reloads the caller's host (an admin change reloads
every built host) and (b) the host re-checks the rule at every run start and rebuilds when it
moved — so what the client lists as enabled is what the run gets.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN_TOKEN, ALICE, connect_body

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

from tests.test_gateway_session_history_seed import _write_min_bundle

pytestmark = pytest.mark.integration


def _host(tmp_path: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )


def test_run_start_rebuilds_the_toolsets_when_the_email_rule_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.mail.accounts as mail_accounts

    host = _host(tmp_path)
    assert host.email_plane is not None and host.email_tools_listed is False
    reloads: list[Any] = []
    real_reload = host.reload_bundles_from_disk

    def counting_reload() -> Any:
        reloads.append(1)
        return real_reload()

    monkeypatch.setattr(host, "reload_bundles_from_disk", counting_reload)
    # Unchanged rule: a start never rebuilds.
    host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "x"}, session_id="s1")
    assert reloads == []
    # The rule moved (a mailbox was connected + the switch is on): the next start rebuilds once,
    # and the rebuilt host lists the tools.
    monkeypatch.setattr(mail_accounts, "agent_tools_active", lambda plane: True)
    host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "y"}, session_id="s1")
    assert len(reloads) == 1 and host.email_tools_listed is True
    host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "z"}, session_id="s1")
    assert len(reloads) == 1
    # …and back when the mailbox goes away.
    monkeypatch.setattr(mail_accounts, "agent_tools_active", lambda plane: False)
    host.start_run(flow_id="root", bundle_id="history-demo", input_data={"prompt": "w"}, session_id="s1")
    assert len(reloads) == 2 and host.email_tools_listed is False


def test_connect_disconnect_and_active_reload_the_toolsets(gateway, imap, smtp) -> None:
    c = gateway["client"]
    alice = gateway["alice"]
    from abstractgateway import service as service_mod
    from abstractgateway.security.principal import GatewayPrincipal

    principal = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",))
    # Build alice's host first (the routes reload only an already-built host — the Mac mini
    # case: a host built at sign-in, the mailbox connected later).
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is False
    # Admin makes agent tools available; alice turns her switch on BEFORE connecting a mailbox.
    assert c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_agent_tools": True}).status_code == 200
    assert c.put("/api/gateway/me/email/agent-tools", headers=alice, json={"enabled": True}).status_code == 200
    rows = {r["name"]: r for r in c.get("/api/gateway/discovery/tools", headers=alice).json()["items"]}
    assert rows["send_email"]["enabled"] is False  # no mailbox yet
    # Connecting the mailbox moves the rule: the connect route reloads the host, and the
    # listing the client reads agrees with the host's own state.
    body = c.put("/api/gateway/me/email", headers=alice, json=connect_body(ALICE, imap, smtp)).json()
    assert body["ok"] is True and body["tools_reloaded"] is True
    rows = {r["name"]: r for r in c.get("/api/gateway/discovery/tools", headers=alice).json()["items"]}
    assert rows["send_email"]["enabled"] is True and rows["search_emails"]["enabled"] is True
    host = service_mod.get_gateway_service_for_principal(principal).host
    assert host.email_tools_listed is True and host.email_tools_current()
    # Active off: the tools leave the lists at once.
    off = c.put("/api/gateway/me/email/enabled", headers=alice, json={"enabled": False}).json()
    assert off["tools_reloaded"] is True
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is False
    on = c.put("/api/gateway/me/email/enabled", headers=alice, json={"enabled": True}).json()
    assert on["tools_reloaded"] is True
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is True
    # Disconnect: gone again.
    gone = c.delete("/api/gateway/me/email", headers=alice).json()
    assert gone["tools_reloaded"] is True
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is False
    rows = {r["name"]: r for r in c.get("/api/gateway/discovery/tools", headers=alice).json()["items"]}
    assert rows["send_email"]["enabled"] is False


def test_admin_capability_change_reloads_every_built_host(gateway, imap, smtp) -> None:
    c = gateway["client"]
    alice = gateway["alice"]
    from abstractgateway import service as service_mod
    from abstractgateway.security.principal import GatewayPrincipal

    principal = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",))
    service_mod.get_gateway_service_for_principal(principal)
    assert c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_agent_tools": True}).status_code == 200
    assert c.put("/api/gateway/me/email/agent-tools", headers=alice, json={"enabled": True}).status_code == 200
    assert c.put("/api/gateway/me/email", headers=alice, json=connect_body(ALICE, imap, smtp)).json()["ok"] is True
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is True
    # The admin withdraws agent email tools for everyone: alice's built host is rebuilt now.
    out = c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_agent_tools": False}).json()
    assert out["tools_reloaded"] >= 1
    assert service_mod.get_gateway_service_for_principal(principal).host.email_tools_listed is False
