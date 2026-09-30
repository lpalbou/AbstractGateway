"""Same-gateway links come from the page origin (COORD 23:20, operator-confirmed): behind
`tailscale serve` the console is https://<host>.<tailnet>.ts.net, and a link built from the
gateway machine's own address (http://100.x:8080, http://127.0.0.1:8080) is wrong and not a secure
context. The real functions are cut out of the served page and run in a node VM with that origin.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic

ORIGIN = "https://mac-mini.tail43a344.ts.net"


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _fn(html: str, start: str) -> str:
    a = html.index(start)
    # up to the next top-level function at the same indentation
    indent = html[html.rindex("\n", 0, a) + 1 : a]
    end = html.index("\n" + indent + "}", a) + len(indent) + 2
    return html[a:end]


def test_same_gateway_links_use_the_page_origin() -> None:
    html = _html()
    fns = "\n".join(_fn(html, s) for s in ("function gatewayBaseUrl()", "function netPrimaryUrl()", "function appBrowserOrigin()"))
    js = (
        "const vm = require('vm');\n"
        f"const ctx = vm.createContext({{ location: {{ origin: {json.dumps(ORIGIN)}, protocol: 'https:' }},"
        " firstRun: { host: { gateway: { url: 'http://127.0.0.1:8080' } } },"
        " netStore: { data: { copy_hint: 'http://100.91.176.118:8080' } } });\n"
        f"vm.runInContext({json.dumps(fns)}, ctx);\n"
        "console.log(JSON.stringify(vm.runInContext('[gatewayBaseUrl(), netPrimaryUrl(), appBrowserOrigin() + \"/apps/code/\"]', ctx)));\n"
    )
    node = require_node()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(js, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == [ORIGIN, ORIGIN, f"{ORIGIN}/apps/code/"]
    for url in out:
        assert "100.91" not in url and ":8080" not in url and "127.0.0.1" not in url
