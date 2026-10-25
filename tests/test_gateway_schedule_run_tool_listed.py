"""The gateway's ReAct tool registry offers `schedule_run` beside `delegate_agent` (framework
backlog 1045 wiring): an agent run whose allowlist names it gets the schema, the runtime maps the
call to SCHEDULE_RUN and the gateway's handler (wakes_host) creates the wake."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_schedule_run_is_in_the_react_registry_next_to_delegate_agent(tmp_path: Path) -> None:
    builtins = pytest.importorskip("abstractagent.logic.builtins")
    if not hasattr(builtins, "SCHEDULE_RUN_TOOL"):
        pytest.fail("this test needs an AbstractAgent with SCHEDULE_RUN_TOOL (root backlog 1045)")
    from abstractgateway.hosts.bundle_host import _build_gateway_react_logic

    logic = _build_gateway_react_logic(email_tools_listed=False, mcp_registry_dir=tmp_path / "mcp")
    names = [getattr(t, "name", None) for t in logic.tools]
    assert "delegate_agent" in names and "schedule_run" in names
    assert names.count("schedule_run") == 1
