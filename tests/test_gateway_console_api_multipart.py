"""The console's `api()` must let a FormData body (Import .flow) carry the browser's own
multipart Content-Type: forcing `application/json` made every import a 422 ("file" missing),
in the released 0.10.0 console. Runs the served `api()` source in node with a recording fetch."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

NODE = shutil.which("node")


def _api_source() -> str:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    m = re.search(r"\n(\s*)async function api\(path, options = \{\}\) \{.*?\n\1\}\n", html, re.S)
    assert m, "the console's api() helper was not found"
    return m.group(0)


@pytest.mark.skipif(NODE is None, reason="node is required to run the console's api() helper")
def test_api_sends_formdata_as_multipart_and_json_as_json():
    script = (
        "const API_TIMEOUT_MS = 5000, API_SLOW_TIMEOUT_MS = 5000; const csrf = () => 'tok';\n"
        "const seen = [];\n"
        "globalThis.fetch = async (path, init) => { seen.push({ path, ct: new Headers(init.headers).get('Content-Type'), form: init.body instanceof FormData });\n"
        "  return { ok: true, status: 200, text: async () => '{}' }; };\n"
        + _api_source()
        + "\n(async () => { const f = new FormData(); f.append('file', new Blob(['x']), 'a.flow');\n"
        "  await api('/api/gateway/bundles/upload', { method: 'POST', body: f });\n"
        "  await api('/api/gateway/bundles/x/archive', { method: 'POST', body: JSON.stringify({}) });\n"
        "  console.log(JSON.stringify(seen)); })();\n"
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    upload, archive = json.loads(out.stdout.strip().splitlines()[-1])
    assert upload["form"] is True and upload["ct"] is None, upload  # fetch sets multipart + boundary itself
    assert archive["ct"] == "application/json", archive
