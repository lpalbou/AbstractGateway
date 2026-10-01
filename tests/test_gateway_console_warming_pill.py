"""Console "Warming up…" pill (boot-time lane, 2026-10-01).

GET /api/health answers `warming_up: true` while the gateway builds a service
(the default model client). The console's top bar shows a quiet pill with the
reason as its tooltip and drops it when the flag is false — no toast, no modal.
Tests run the SHIPPED console functions (sliced from the served page) in node.
"""

from __future__ import annotations

import json
import re

import pytest
from node_requirement import require_node

from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic


def _source() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))


def test_mount_starts_the_health_watch() -> None:
    from test_gateway_console_offline import _slice_function

    mount = _slice_function(_source(), "mountConsoleIslands")
    assert "pollWarming();" in mount


def test_pill_follows_the_health_flag() -> None:
    require_node()
    from test_gateway_console_offline import _node, _slice_function

    src = _source()
    m = re.search(r"const WARMING_REASON = [^\n]*\n", src)
    assert m, "WARMING_REASON missing from the console JavaScript"
    fns = "\n".join(_slice_function(src, n) for n in ("warmingExtra", "applyHealthWarming", "topBarIslandProps"))
    script = f"""
const islands = {{ warming: false, phase: "connected", signingOut: false, identity: "", about: null }};
const state = {{ principal: null }}; const assistantState = {{ open: false }};
let renders = 0; function renderIslands() {{ renders += 1; }}
function netPrimaryUrl() {{ return ""; }} function openAppearance() {{}} function toggleAssistant() {{}}
function uiCopy() {{}} function signOut() {{}} function $() {{ return null; }}
{m.group(0)}
{fns}
const pill = () => topBarIslandProps().extras.find((x) => x && x.id === "island-warming");
const before = pill();
const on = applyHealthWarming({{ status: "healthy", warming_up: true }});
const during = pill(); const rendersOn = renders;
applyHealthWarming({{ status: "healthy", warming_up: true }});
const rendersSame = renders;
const off = applyHealthWarming({{ status: "healthy" }});
const after = pill();
console.log(JSON.stringify([{{ before, on, during, rendersOn, rendersSame, off, after }}]));
"""
    out = _node(script)[0]
    assert out["before"]["hidden"] is True
    assert out["on"] is True
    assert out["during"] == {
        "id": "island-warming",
        "label": "Building the default model client; some pages wait until it is ready.",
        "text": "Warming up…",
        "hidden": False,
    }
    assert out["rendersOn"] == 1 and out["rendersSame"] == 1  # re-render only on change
    assert out["off"] is False and out["after"]["hidden"] is True
