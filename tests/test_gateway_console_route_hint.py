"""Round 16: AbstractCore's served `route_hint` on the console's defaults grid.

A faster-whisper speech-input route on Apple silicon runs on the processor while mlx-whisper
runs the same model on the GPU: AbstractCore computes the sentence (and the route "Apply
recommended" would write); the console shows it verbatim next to the row. No new control, no
client-side logic. Shape from abstractcore/config/recommendations.py `voice_input_hint`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node
from test_gateway_console_voice_provider_states import _function, _html

pytestmark = pytest.mark.basic

HINT = {
    "code": "apple_gpu_engine",
    "sentence": "Runs on the processor: faster-whisper has no Apple GPU backend. mlx-whisper runs large-v3 on this Mac's GPU, about 15 times faster.",
    "route": {"key": "input.voice", "provider": "mlx-whisper", "model": "large-v3"},
}


def _run(expr: str) -> object:
    html = _html()
    fns = "\n".join(_function(html, n) for n in ("esc", "routeHintMarkup"))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(fns + f"\nconsole.log(JSON.stringify({expr}));")
    proc = subprocess.run([require_node(), f.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_served_sentence_is_shown_verbatim_with_the_apply_pointer() -> None:
    markup = _run(f"routeHintMarkup({json.dumps({'key': 'input.voice', 'route_hint': HINT})})")
    assert HINT["sentence"].replace("'", "&#39;") in markup
    assert markup.endswith("Apply recommended switches it.</div>")
    assert 'data-hint="apple_gpu_engine"' in markup and "capability-route-hint" in markup


def test_a_hint_without_a_route_has_no_apply_pointer() -> None:
    hint = dict(HINT, code="apple_gpu_engine_not_installed", route=None)
    markup = _run(f"routeHintMarkup({json.dumps({'route_hint': hint})})")
    assert "Apply recommended" not in markup and "capability-route-hint" in markup


def test_no_hint_no_markup() -> None:
    assert _run('routeHintMarkup({"key": "input.voice"})') == ""
    assert _run('routeHintMarkup({"route_hint": {"sentence": "  "}})') == ""
