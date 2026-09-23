"""The shipped basic-agent must not sleep after it has answered (0.0.5).

basic-agent@0.0.4's root flow pinned `post_delay: 3` on node-5 ("AGENTIC LOOP
DONE"), the subflow call that emits the final `abstract.status` event. The
helper (15f19f7f) emits the event and THEN parks on `wait_until(post_delay)`,
so the run stayed alive three seconds past its own answer and every no-tool
chat turn paid it — 3.0s of a measured 6.8s of non-model time on the live box
(run 7ca0e31f).

The 3 was an undocumented editor pinDefault from 2026-01-10 (commit
"ongoing"), inert until the 2026-07-20 adversary wave exec-wired the Delay,
and no consumer depends on it: the web REPL and the TUI both CLEAR the
activity line on a "Done" status, and both drain the ledger by REST before
concluding, so neither needs a grace window after the terminal save.

This pins the shipped artifact, not the source flow: a repack that
re-introduces a delay (or ships a stale version) lands RED here.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.config import _default_flows_dir  # noqa: E402

STATUS_HELPER_FLOW_ID = "15f19f7f"
ROOT_FLOW_ID = "81795ea9"


def _root_flow(bundle_path: Path) -> dict:
    with zipfile.ZipFile(bundle_path) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        rel = (manifest.get("flows") or {})[ROOT_FLOW_ID]
        return json.loads(zf.read(rel).decode("utf-8"))


def _status_subflow_nodes(flow: dict) -> list[dict]:
    out = []
    for node in flow.get("nodes") or []:
        data = node.get("data") or {}
        if (node.get("type") or data.get("nodeType")) == "subflow" and data.get("subflowId") == STATUS_HELPER_FLOW_ID:
            out.append(node)
    return out


def test_shipped_basic_agent_status_calls_carry_no_post_delay() -> None:
    bundle = Path(_default_flows_dir()) / "basic-agent.flow"
    if not bundle.is_file():  # pragma: no cover - a deployment may ship its own
        pytest.skip(f"no shipped basic-agent bundle at {bundle}")
    flow = _root_flow(bundle)
    nodes = _status_subflow_nodes(flow)
    assert nodes, "shipped basic-agent no longer calls the ac-update-status helper"
    for node in nodes:
        pins = (node.get("data") or {}).get("pinDefaults") or {}
        delay = pins.get("post_delay", 0)
        assert float(delay) == 0.0, (
            f"shipped basic-agent node {node.get('id')} "
            f"({(node.get('data') or {}).get('label')!r}) pins post_delay={delay!r}: "
            "the run would stay alive that long after its own answer"
        )


def test_shipped_basic_agent_is_at_least_version_0_0_5() -> None:
    bundle = Path(_default_flows_dir()) / "basic-agent.flow"
    if not bundle.is_file():  # pragma: no cover
        pytest.skip(f"no shipped basic-agent bundle at {bundle}")
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    version = tuple(int(p) for p in str(manifest.get("bundle_version") or "0.0.0").split("."))
    assert version >= (0, 0, 5), f"shipped basic-agent is {manifest.get('bundle_version')}, expected >= 0.0.5"
