"""R14.7: the Multimodal Capabilities grid in a real browser.

Operator review 2026-10-07: the ROUTE code pill (`input.text`) was painted over
by the CAPABILITY name ("Text Input") in every row, and the WEIGHTS cell
wrapped the probe's long explanation into a ten-line cell. This test seeds the
operator's kind of routes (long model ids, a faster-whisper voice input whose
weights ARE cached, remote-free media routes, one provider nobody knows) on a
hermetic scratch gateway and asserts, at 1440 / 1280 / 834 px in light and
dark (tests/browser/r14w7_multimodal.mjs):

  * no route pill's box intersects its row's capability text, and the pill
    stays inside its own cell;
  * the Weights cell is the state pill plus ONE short sentence (AbstractCore's
    `summary`), the full detail in the pill's kit tooltip;
  * the page never scrolls horizontally; the narrow width shows cards;
  * the Voice Input row reads "installed" with its snapshot path.

Opt-in like the other console browser tests (ABSTRACTGATEWAY_BROWSER_TESTS=1,
playwright-core from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or
abstractcode/web/node_modules). The gateway runs from this checkout with a
scratch HOME, data dir and Hugging Face cache, no provider keys, HF offline.
R14W7_SHOTS=<dir> also writes the screenshots.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _free_port, _playwright_modules, _start, _stop

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "r14w7_multimodal.mjs"

# (kind, modality, task or None, provider, model)
ROUTES = [
    ("input", "text", None, "mlx", "Jundot/Qwen3.8-27B-oQ4e-mtp"),
    ("input", "voice", None, "faster-whisper", "large-v3"),
    ("input", "music", None, "mystery-engine", "acme/some-model"),
    ("output", "image", "text_to_image", "mlx-gen", "AbstractFramework/flux.2-klein-9b-8bit"),
    ("output", "image", "image_upscale", "mlx-gen", "AbstractFramework/seedvr2-7b-8bit"),
    ("output", "video", "text_to_video", "mlx-gen", "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit"),
    ("output", "voice", None, "piper", "en_US-amy-medium"),
    ("output", "sound", None, "stable-audio-3", "stabilityai/stable-audio-3-small-sfx"),
    ("output", "music", None, "stable-audio-3", "stabilityai/stable-audio-3-medium"),
    ("output", "scene3d", "image_to_scene3d", "triposr", "stabilityai/TripoSR"),
]


def _hf_repo(root: Path, repo_id: str, files=("model.safetensors",)) -> None:
    repo = root / ("models--" + repo_id.replace("/", "--"))
    snap = repo / "snapshots" / "rev1"
    (repo / "blobs").mkdir(parents=True)
    snap.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("rev1")
    for index, name in enumerate(files):
        blob = repo / "blobs" / f"sha{index}"
        blob.write_bytes(b"\x00" * 16)
        (snap / name).symlink_to(blob)


@pytest.fixture()
def multimodal_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data, hf = tmp_path / "home", tmp_path / "data", tmp_path / "hf"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    hub = hf / "hub"
    hub.mkdir(parents=True)
    # The operator's machine: faster-whisper large-v3 and the music model are cached.
    _hf_repo(hub, "Systran/faster-whisper-large-v3", files=("model.bin", "config.json", "tokenizer.json"))
    _hf_repo(hub, "stabilityai/stable-audio-3-medium")
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(HERE.parent / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
        "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8",
        "ABSTRACTGATEWAY_DATA_DIR": str(data),
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
        "HF_HOME": str(hf), "HF_HUB_CACHE": str(hub),
        "OLLAMA_BASE_URL": "http://127.0.0.1:9", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
    }
    log = tmp_path / "gateway.log"
    base = f"http://127.0.0.1:{port}"
    proc = _start(port, env, log)
    try:
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        for kind, modality, task, provider, model in ROUTES:
            path = f"/config/capability-defaults/{kind}/{modality}" + (f"/{task}" if task else "")
            code, out = _call(base, "PUT", path, admin, {"provider": provider, "model": model})
            assert code == 200, (path, out)
        yield base, admin
    finally:
        _stop(proc)


def test_route_and_capability_never_overlap_and_weights_say_one_sentence(multimodal_gateway) -> None:
    base, admin = multimodal_gateway
    node = require_node()
    modules = _playwright_modules()
    argv = [node, str(SCRIPT), base, admin, str(modules)]
    shots = os.getenv("R14W7_SHOTS", "").strip()
    if shots:
        argv.append(shots)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 60
