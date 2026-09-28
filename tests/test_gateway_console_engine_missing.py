"""AbstractCore 2.18's `engine_missing` and `needs_gpu_limit` in the web
console (wave 2, 2026-09-28): the routes grid, the apply report and the setup
guide's model cards say "engine missing: <reason> — install: <command>" and
"fits after raising the GPU memory limit: <command> (admin; resets at
restart)" — distinct from "cannot run here" and from "not downloaded".
Shapes from abstractcore/config/route_engines.py + utils/model_fit.py."""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node
from test_gateway_console_voice_provider_states import _function, _html

pytestmark = pytest.mark.basic

MISSING = {
    "engine": "mlx",
    "name": "MLX (mlx-lm)",
    "reason": "MLX (mlx-lm) is not installed in this Python environment (mlx_lm missing); the mlx provider runs models with it. Install it with: pip install mlx-lm",
    "install": "pip install mlx-lm",
    "engine_row": "mlx",
}
GPU = {
    "sysctl": "iogpu.wired_limit_mb", "current_mb": 0, "required_mb": 117760,
    "command": "sudo sysctl iogpu.wired_limit_mb=117760", "needs_admin": True,
    "resets_at_restart": True, "verdict_with_limit": "tight",
}


def _run(expr: str) -> object:
    html = _html()
    fns = "\n".join(_function(html, n) for n in ("esc", "engineMissingInfo", "engineMissingText", "engineMissingMarkup", "gpuLimitText"))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(fns + f"\nconsole.log(JSON.stringify({expr}));")
    proc = subprocess.run([require_node(), f.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_engine_missing_text_and_markup() -> None:
    row = json.dumps({"key": "input.text", "engine_missing": MISSING})
    assert _run(f"engineMissingText({row})") == f"engine missing: {MISSING['reason']} — install: pip install mlx-lm"
    markup = _run(f"engineMissingMarkup({row})")
    assert "Engine missing:" in markup and "<code>pip install mlx-lm</code>" in markup and "Engines tab" in markup
    assert _run('engineMissingText({"key": "input.text"})') == ""
    assert _run('engineMissingMarkup({"engine_missing": {"reason": ""}})') == ""


def test_needs_gpu_limit_names_the_command_and_its_cost() -> None:
    plan_row = json.dumps({"fit_verdict": "needs_gpu_limit", "gpu_limit": GPU})
    assert _run(f"gpuLimitText({plan_row})") == (
        "fits after raising the GPU memory limit: sudo sysctl iogpu.wired_limit_mb=117760 (admin; resets at restart)"
    )
    catalog_like = json.dumps({"fit": {"verdict": "needs_gpu_limit", "gpu_limit": GPU}})
    assert _run(f"gpuLimitText({catalog_like})").endswith("(admin; resets at restart)")
    assert _run('gpuLimitText({"fit_verdict": "needs_gpu_limit"})') == "fits after raising the GPU memory limit"
    assert _run('gpuLimitText({"fit_verdict": "fits"})') == ""


def test_the_three_surfaces_use_them() -> None:
    html = _html()
    assert 'if (defaultRowConfigured(row) && engineMissingInfo(row)) return { label: "engine missing", cls: "off" };' in html
    assert '${defaultRowConfigured(row) ? engineMissingMarkup(row) : ""}</td>' in html
    assert "const missing = rows.filter((r) => engineMissingInfo(r));" in html
    assert 'engineMissingMarkup(r, "ui-alert tone-warn")' in html and "first-run-gpu-limit" in html
