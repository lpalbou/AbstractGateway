"""Focused provider-sharing UI check, using the existing hermetic gateway fixture."""
import json
import subprocess
from pathlib import Path

import pytest

from node_requirement import require_node
from test_gateway_console_browser_providers import BOB, providers_gateway
from test_gateway_console_browser_state_toggles import _playwright_modules

pytestmark = pytest.mark.e2e


def test_admin_and_member_provider_access_ui(providers_gateway) -> None:
    base, admin = providers_gateway
    result = subprocess.run(
        [require_node(), str(Path(__file__).parent / "browser/admin_provider_sharing.mjs"), base, admin, str(_playwright_modules()), BOB],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])["checks"] == 11
