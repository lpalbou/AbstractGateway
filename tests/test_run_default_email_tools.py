"""A run started without `input_data.tools` gets the email tools when (and only when)
the user's Agent email tools are active (0.7.0 Linux E2E finding F2).

The shipped basic-agent bundle is loaded for real; only the email-state predicate
(`mail.accounts.agent_tools_active`) is pinned, so the host's own toolset build and
this module read the same answer.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

from abstractgateway import run_default_tools as rdt

pytestmark = pytest.mark.basic

SHIPPED_BASIC_AGENT = Path(__file__).resolve().parents[1] / "flows" / "bundles" / "basic-agent.flow"
EMAIL_NAMES = rdt.email_tool_names()


def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, email_active: bool) -> Any:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractgateway.mail import accounts

    monkeypatch.setattr(accounts, "agent_tools_active", lambda plane: email_active)
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    shutil.copy2(SHIPPED_BASIC_AGENT, bundles / "basic-agent.flow")
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )
    return host


def _entry_workflow_id(host: Any) -> str:
    version = host.latest_bundle_versions["basic-agent"]
    bundle = host.bundles["basic-agent"][version]
    return f"basic-agent@{version}:{bundle.manifest.default_entrypoint}"


def test_email_names_are_the_runtime_email_kind() -> None:
    assert "send_email" in EMAIL_NAMES and "read_email" in EMAIL_NAMES
    assert all("email" in n for n in EMAIL_NAMES)


def test_shipped_basic_agent_start_default_has_no_email_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host(tmp_path, monkeypatch, email_active=False)
    flow = rdt.entry_visualflow(host, _entry_workflow_id(host))
    assert flow is not None
    defaults = rdt.start_node_default_tools(flow)
    assert defaults and "execute_command" in defaults
    assert not set(defaults) & set(EMAIL_NAMES)


def test_active_email_tools_extend_the_start_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host(tmp_path, monkeypatch, email_active=True)
    wid = _entry_workflow_id(host)
    defaults = rdt.start_node_default_tools(rdt.entry_visualflow(host, wid))
    vars0: Dict[str, Any] = {"prompt": "mail me"}
    added = rdt.apply_default_email_tools(host, workflow_id=wid, vars0=vars0)
    assert added == EMAIL_NAMES
    assert vars0["tools"] == defaults + EMAIL_NAMES


def test_inactive_email_tools_leave_the_default_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host(tmp_path, monkeypatch, email_active=False)
    vars0: Dict[str, Any] = {"prompt": "hi"}
    assert rdt.apply_default_email_tools(host, workflow_id=_entry_workflow_id(host), vars0=vars0) == []
    assert "tools" not in vars0


@pytest.mark.parametrize("explicit", [[], ["read_file"], None])
def test_an_explicit_tool_list_is_never_widened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: Any) -> None:
    host = _host(tmp_path, monkeypatch, email_active=True)
    vars0: Dict[str, Any] = {"tools": explicit}
    assert rdt.apply_default_email_tools(host, workflow_id=_entry_workflow_id(host), vars0=vars0) == []
    assert vars0["tools"] == explicit


def test_non_bundle_workflow_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host(tmp_path, monkeypatch, email_active=True)
    vars0: Dict[str, Any] = {}
    assert rdt.apply_default_email_tools(host, workflow_id="scheduled:123", vars0=vars0) == []
    assert vars0 == {}


def test_start_node_without_a_tools_pin_is_untouched() -> None:
    flow = {
        "entryNode": "s",
        "nodes": [
            {"id": "s", "type": "on_flow_start", "data": {"outputs": [{"id": "prompt", "type": "string"}], "pinDefaults": {"tools": ["x"]}}}
        ],
    }
    assert rdt.start_node_default_tools(flow) is None
    flow["nodes"][0]["data"]["outputs"].append({"id": "tools", "type": "tools"})
    assert rdt.start_node_default_tools(flow) == ["x"]
    flow["entryNode"] = "other"
    assert rdt.start_node_default_tools(flow) is None


def test_email_names_are_not_duplicated() -> None:
    assert rdt.with_email_tools(["read_file", "send_email"], ["send_email", "read_email"]) == ["read_file", "send_email", "read_email"]


def test_host_start_run_applies_the_email_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host(tmp_path, monkeypatch, email_active=True)
    run_id = host.start_run(flow_id="", bundle_id="basic-agent", input_data={"prompt": "mail me"})
    run = host.run_store.load(run_id)
    assert set(EMAIL_NAMES) <= set(run.vars.get("tools") or [])
