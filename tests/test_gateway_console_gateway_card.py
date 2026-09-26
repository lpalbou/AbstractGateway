"""The console's Gateway card + paused banner (host pause / desktop tray /
restart / update, 2026-09-05) — markup and wiring pins, plus a JS syntax
check when node is available."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def test_gateway_card_and_paused_banner_are_served() -> None:
    html = _html()
    # Card (Resources tab) — above Memory & GPU, existing recipes only.
    assert html.index('id="gateway-host-section"') < html.index('id="models-host-section"')
    for needle in (
        'id="gateway-host-state"',
        'id="gateway-host-pause"',
        'id="gateway-host-tray-note"',
        'id="gateway-host-restart"',
        'id="gateway-host-quit"',
        'id="gateway-host-update-check"',
        'id="gateway-host-update-start"',
    ):
        assert needle in html, needle
    # Banner on EVERY tab: it sits right after the header, outside the tab panels.
    assert html.index('id="paused-banner"') < html.index('id="tab-models"')
    assert 'id="paused-banner-resume"' in html
    # Copy: paused must never read as stopped.
    assert "the gateway keeps answering" in html
    assert "Paused — still running" in html


def test_gateway_card_wiring_and_endpoints() -> None:
    html = _html()
    for fn in ("renderGatewayHost", "loadGatewayHost", "toggleGatewayPause", "restartGateway", "quitGateway", "checkGatewayUpdate", "startGatewayUpdate", "renderPausedBanner", "startPausedPoll"):
        assert html.count(f"function {fn}(") == 1, fn
    for route in ("/api/gateway/host/runner", "/api/gateway/host/tray", "/api/gateway/host/pause", "/api/gateway/host/resume", "/api/gateway/host/restart", "/api/gateway/host/shutdown", "/api/gateway/host/update", "/api/gateway/host/update/check", "/api/gateway/host/update/start"):
        assert route in html, route
    # THE DESKTOP ICON IS A STATUS, NOT A SWITCH (operator ruling 2026-09-06):
    # while the gateway runs, the icon is there. A checkbox whose only effect
    # is to remove the one entry point a non-technical user knows is a way to
    # lose the product, so there is no checkbox and no knob to write.
    assert "desktop_tray" not in html
    assert 'id="gateway-host-tray"' not in html
    assert 'id="gateway-host-tray-note"' in html
    # A `#<tab>` fragment is a deep link to ANY tab, not a `#models` special
    # case: the tray's "Open Runs in Console" needs `#runtimes`, and the next
    # entry point should not need another branch here.
    assert 'TABS.includes(wantedTab)' in html
    assert 'location.hash || "").replace(/^#/, "")' in html
    # Every control has its onclick wired (a missing wiring line throws at boot and kills every later handler).
    for wid in ("gateway-host-pause", "gateway-host-restart", "gateway-host-quit", "gateway-host-update-check", "gateway-host-update-start", "paused-banner-resume"):
        assert f'$("{wid}").onclick' in html, wid



def test_console_javascript_parses() -> None:
    node = require_node()
    html = _html()
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, flags=re.S)
    assert blocks
    for js in blocks:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(js)
            path = fh.name
        try:
            proc = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=60, check=False)
            assert proc.returncode == 0, proc.stderr[:2000]
        finally:
            os.unlink(path)
