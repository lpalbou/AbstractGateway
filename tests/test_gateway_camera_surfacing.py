"""Camera toolset surfacing through the gateway (0012 door half; laurent's
ruling commons c3873, gate met at c3917 after runtime c3903 + camera c3915;
door-half adversary folded 2026-07-21).

The gateway's half is COMPOSITION, not new plumbing — and these pins are the
proof that the composition holds on the door:

1. RUN LANES DERIVE, NEVER COPY: `/discovery/tools` and every
   default-constructed `ToolApprovalPolicy` ride runtime's
   `list_default_tool_specs()` / `default_approval_policy_sets()` fold, so
   camera's own classification (fail-closed after camera's P1 fix) reaches
   gateway runs with ZERO gateway-side camera code. There is deliberately no
   `abstractcamera` import anywhere in the gateway tree. The pins exercise
   the GATEWAY composition points (route handler + policy construction), not
   just the runtime fold beneath them (door-half adversary P2: pinning only
   the fold would stay green if a gateway surface hand-copied a list).

2. OFF MEANS ABSENT: without `ABSTRACT_ENABLE_CAMERA_TOOLS`, no camera name
   appears in discovery, in approval sets, or anywhere else — a camera is
   physical hardware; exposure is an operator act, never a side effect of
   the package being importable.

3. THE WALLED ENTITY SURFACES STAY SHUT: camera tools are NOT walled rows,
   so they never appear in the entity tool inventory, the phase-capability
   matrix, or a home's grantable set (`write_policy_file` refuses unknown
   names) — even flag-on. HONEST SCOPE (door-half adversary P1): this is
   structural for the WALLED lanes (visit / chat / loop / grants / matrix).
   The workplace SUMMON lane starts an ordinary run on the shared bundle
   host, whose tool surface is the run-lane map — flag-on, camera rides it
   like any run tool and the home's per-phase grant is not consulted.
   RULED (laurent, 2026-07-21, commons c3938 — user-right reading): that
   exposure ships AS BUILT. Camera is a tool like any other: capture/detect
   verbs ASK BY DEFAULT (a default, not a floor — the user may auto-accept
   camera or any tool per-run/per-session); the reference/status tools
   auto-approve per camera's own classification. The 0012 integration is
   complete with zero pending words.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

pytestmark = pytest.mark.basic

_HAS_CAMERA = importlib.util.find_spec("abstractcamera") is not None

# Subprocess probes: the enable flag is read at import/call time inside
# abstractruntime, and other tests in this session may have imported those
# modules already — a fresh interpreter per arm keeps the env truth exact
# (the parameter-explicit-isolation rule). The probe exercises the GATEWAY
# composition points: the /discovery/tools handler, a default-constructed
# ToolApprovalPolicy, and the entity walled inventory + phase matrix.
_PROBE = r"""
import asyncio, json, sys

from abstractgateway.routes.gateway import discovery_tools
from abstractgateway.tool_inventory import entity_walled_inventory, phase_capability_matrix
from abstractruntime.integrations.abstractcore.tool_executor import ToolApprovalPolicy
from abstractruntime.identity.tools import walled_tool_rows

discovery = asyncio.run(discovery_tools())
discovery_names = [i.get("name") for i in discovery.get("items") or []]

policy = ToolApprovalPolicy()
auto = getattr(policy, "auto_approve_tools", None) or getattr(policy, "_auto_approve_tools", set())
require = getattr(policy, "require_approval_tools", None) or getattr(policy, "_require_approval_tools", set())

walled = [r.get("name") for r in walled_tool_rows()]
inventory_names = [r.get("name") for r in entity_walled_inventory()]
matrix = phase_capability_matrix()
matrix_ids = [i.get("id") for s in matrix.get("sections") or [] for i in s.get("items") or []]

print(json.dumps({
    "discovery_camera": sorted(n for n in discovery_names if "camera" in str(n)),
    "auto_camera": sorted(n for n in auto if "camera" in str(n)),
    "require_camera": sorted(n for n in require if "camera" in str(n)),
    "walled_camera": sorted(n for n in walled if "camera" in str(n)),
    "inventory_camera": sorted(n for n in inventory_names if "camera" in str(n)),
    "matrix_camera": sorted(n for n in matrix_ids if "camera" in str(n)),
    "discovery_total": len(discovery_names),
}))
"""


def _probe(*, enabled: bool) -> dict:
    import json
    import os

    env = {k: v for k, v in os.environ.items() if k != "ABSTRACT_ENABLE_CAMERA_TOOLS"}
    if enabled:
        env["ABSTRACT_ENABLE_CAMERA_TOOLS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_flag_off_no_camera_name_anywhere() -> None:
    out = _probe(enabled=False)
    assert out["discovery_camera"] == [], "disabled camera must be absent from /discovery/tools"
    assert out["auto_camera"] == [], "disabled camera must not appear in auto-approve"
    assert out["require_camera"] == [], "disabled camera must not appear in require-approval"
    assert out["walled_camera"] == [], "camera must never be an entity walled row"
    assert out["inventory_camera"] == [], "camera must never enter the entity tool inventory"
    assert out["matrix_camera"] == [], "camera must never enter the phase matrix"


@pytest.mark.skipif(not _HAS_CAMERA, reason="abstractcamera is not installed")
def test_flag_on_camera_rides_gateway_run_lanes_with_derived_partition() -> None:
    out = _probe(enabled=True)
    # The toolset surfaces through the GATEWAY's /discovery/tools handler.
    # No exact count: the set is camera's to evolve (2026-07-21: an 11th
    # tool, camera_preview_photo, arrived via their VLM-gap fold and the
    # ==10 pin broke on a healthy widening) — the INVARIANTS below are the
    # gateway's contract, plus a floor of names that must exist.
    assert len(out["discovery_camera"]) >= 10, out["discovery_camera"]
    for expected in ("camera_list_devices", "camera_status", "camera_capture_photo"):
        assert expected in out["discovery_camera"]
    # A default-constructed ToolApprovalPolicy (bundle_host's construction)
    # carries the camera partition DERIVED from camera's own classification.
    auto = set(out["auto_camera"])
    require = set(out["require_camera"])
    assert auto and require, "both halves of the partition must be non-empty"
    assert not (auto & require), "a name must never be in both halves"
    assert auto | require == set(out["discovery_camera"]), (
        "every surfaced camera tool must land in exactly one approval half"
    )
    # Ask-by-DEFAULT for every environment-capturing verb (laurent's ruling
    # c3938: a DEFAULT, not a floor — the user may auto-accept camera like
    # any tool; these names pin the shipped default partition and should be
    # re-based consciously if camera reclassifies).
    for capturing in ("camera_capture_photo", "camera_capture_video", "camera_start_detection"):
        assert capturing in require, f"{capturing} must ask by default (ruled default, user-overridable)"


@pytest.mark.skipif(not _HAS_CAMERA, reason="abstractcamera is not installed")
def test_flag_on_walled_entity_surfaces_stay_shut() -> None:
    """Camera never reaches the WALLED entity surfaces even when enabled:
    not a walled row, hence absent from the entity inventory, the phase
    matrix, and any home's grantable universe. This probe runs flag-ON in
    the subprocess (door-half adversary P2: the old in-process check ran in
    the flag-off test interpreter, so a flag-conditional gateway row would
    have passed). The summon-lane run-tool exposure is RULED as-built
    (c3938, see module docstring) and rides the run lanes pinned above."""
    out = _probe(enabled=True)
    assert out["walled_camera"] == []
    assert out["inventory_camera"] == []
    assert out["matrix_camera"] == []


def test_gateway_tree_has_no_camera_import() -> None:
    """Derive-never-copy at the import level: the gateway consumes camera
    exclusively through runtime's fold. A direct abstractcamera import here
    would create the second copy the whole chain exists to prevent."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"
    offenders: list[str] = []
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "import abstractcamera" in text or "from abstractcamera" in text:
            offenders.append(str(p))
    assert not offenders, f"gateway must never import abstractcamera directly: {offenders}"
