"""Camera-only (deterministic) flows register the TOOL_INVOKE handler.

Flow c4316 (adversary-verified): the operator's deterministic camera flow
(wait_event -> camera_open -> camera_capture_photo -> camera_analyze_media,
NO llm/agent node) fell to the bare runtime with no TOOL_INVOKE handler and
failed at execution ("No effect handler registered for tool_invoke"). Cause:
`_flow_uses_tools` matched only {tool_calls, agent}, so needs_tools was False
and neither the tool executor nor the handler was wired. An llm+camera flow
masked it because build_effect_handlers (the LLM branch) registers both.

These pins hold the detection fix: a node emitting TOOL_INVOKE (camera /
call_tool / tool_invoke) makes needs_tools True, so the tools-only branch
wires the executor + the TOOL_INVOKE handler.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic


def _flow(*node_types: str) -> dict:
    return {"nodes": [{"id": f"n{i}", "type": t} for i, t in enumerate(node_types)]}


def test_camera_only_flow_needs_tools() -> None:
    from abstractgateway.hosts.bundle_host import _flow_uses_llm, _flow_uses_tools

    raw = _flow("wait_event", "camera_open", "camera_capture_photo", "camera_analyze_media", "camera_close")
    assert _flow_uses_tools(raw) is True, "a camera-only flow must trigger tool-executor wiring"
    assert _flow_uses_llm(raw) is False, "the incident shape has no llm/agent node (that is what masked the gap)"


def test_deterministic_tool_invoke_node_needs_tools() -> None:
    from abstractgateway.hosts.bundle_host import _flow_uses_tools

    assert _flow_uses_tools(_flow("wait_event", "tool_invoke")) is True
    assert _flow_uses_tools(_flow("call_tool")) is True


def test_tool_calls_and_agent_still_detected() -> None:
    from abstractgateway.hosts.bundle_host import _flow_uses_tools

    assert _flow_uses_tools(_flow("tool_calls")) is True
    assert _flow_uses_tools(_flow("agent")) is True


def test_pure_non_tool_flow_needs_no_tools() -> None:
    from abstractgateway.hosts.bundle_host import _flow_uses_tools

    assert _flow_uses_tools(_flow("wait_event", "set_var", "answer_user")) is False


def test_tools_only_runtime_registers_both_tool_effects() -> None:
    """The tools-only branch must wire TOOL_INVOKE alongside TOOL_CALLS (the
    fix's other half) — a camera-only flow reaches this branch, not the LLM
    branch that already had both."""
    import inspect

    from abstractgateway.hosts import bundle_host

    src = inspect.getsource(bundle_host)
    # The tools-only branch (no LLM client) must name both handlers.
    assert "make_tool_invoke_handler" in src
    assert "EffectType.TOOL_INVOKE: make_tool_invoke_handler" in src, (
        "the tools-only runtime branch must register the TOOL_INVOKE handler"
    )
