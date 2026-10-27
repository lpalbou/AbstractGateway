"""The web console shows a cloud voice provider's `needs_key` state in the
voice pickers (route editor), never in the global provider
labels where "openai" also names the TEXT provider (wave 2, 2026-09-28)."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _function(html: str, name: str) -> str:
    start = re.search(rf"function {name}\(", html).start()
    depth, i = 0, html.index("{", start)
    while True:
        c = html[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return html[start:i]


def test_state_labels_are_scoped_to_the_voice_pickers() -> None:
    html = _html()
    assert "labelMap: state.providerStateLabels.get(catalog.scope) || null," in html
    assert "state.providerStateLabels.set(catalog.scope, catalogProviderStateLabels(payload));" in html
    fns = "\n".join(_function(html, n) for n in ("textValue", "arrayValue", "objectValue", "catalogProviderFromItem", "catalogLabelFromItem", "catalogProviderStateLabels"))
    payload = {"items": [
        {"id": "supertonic", "provider": "supertonic", "label": "supertonic"},
        {"id": "openai", "provider": "openai", "label": "OpenAI", "needs_key": True, "status": "needs an API key (add it under Providers)"},
        {"id": "openai-compatible", "provider": "openai-compatible", "label": "OpenAI-compatible", "needs_key": False, "status": "ready"},
    ]}
    script = fns + f"\nconsole.log(JSON.stringify([...catalogProviderStateLabels({json.dumps(payload)})]));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
    proc = subprocess.run([require_node(), f.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [["openai", "OpenAI — needs an API key (add it under Providers)"]]


def test_voice_pickers_show_why_no_voices_are_listed() -> None:
    """Both voice pickers are the kit's VoiceSettings (the entity's since round
    3, the route editor's since 0947), which shows the catalog's
    `unavailable_reason` when an engine lists no voice (abstractuic
    check_voice_library 0947). The console keeps no copy of that logic."""
    html = _html()
    assert "voiceUnavailableReason" not in html and '"No voices — see why below"' not in html
    from abstractgateway.console_islands import ISLANDS_JS

    assert '"data-voice-note":"unavailable"' in ISLANDS_JS and "unavailable_reason" in ISLANDS_JS
