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
    html = _html()
    # The route editor's voice select says the reason (the entity's voice is
    # the kit's shared VoiceSettings since round 3; it shows the catalog error).
    assert html.count('"No voices — see why below"') == 1
    assert "state.voiceReasons.set(cacheKey, voiceUnavailableReason(payload));" in html
    fns = "\n".join(_function(html, n) for n in ("textValue", "voiceUnavailableReason"))
    cases = [
        {"items": [], "unavailable_reason": "Supertonic is not installed: onnxruntime is missing"},
        {"items": [], "error": "OpenAI audio requires OPENAI_API_KEY"},
        {"items": []},
    ]
    script = fns + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(voiceUnavailableReason)));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
    proc = subprocess.run([require_node(), f.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "Supertonic is not installed: onnxruntime is missing",
        "OpenAI audio requires OPENAI_API_KEY",
        "",
    ]
