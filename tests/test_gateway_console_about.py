"""The console's About (kit 0.1.12 islands): the top-bar About action shows
the gateway's identity (`appIdentity("abstractgateway", <served version>)`)
plus the gateway-version rows of GET /about, formatted once by AbstractCore's
`gateway_version_rows` (contract A-9) and templated into the page by the
serving gateway.

- The served page carries `GATEWAY_ABOUT` = `console_about_config()`, whose
  version is the running abstractgateway and whose rows are the Python twin's
  rows for `about_payload()`.
- On the REAL vendored islands bundle (node vm), `consoleAboutProps` builds the
  kit props: identity == the canonical descriptor's abstractgateway entry at the
  served version, extraRows == those rows, and a failure is a visible row.
- The identity descriptor compiled into the bundle equals the canonical one
  (AbstractCore's vendored copy, byte-identical to the framework root's
  `identity/abstractframework.json` by `scripts/check_identity_sync.py`).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from node_requirement import require_node

from abstractgateway.console import console_about_config, gateway_console_html
from abstractgateway.console_islands import ISLANDS_JS

pytestmark = pytest.mark.basic


def _canonical_descriptor() -> dict:
    from abstractcore.utils import identity

    raw = (Path(identity.__file__).resolve().parents[1] / "assets" / "abstractframework_identity.json").read_bytes()
    root = Path(__file__).resolve().parents[2] / "identity" / "abstractframework.json"
    if root.is_file():  # monorepo checkout: the vendored core copy IS the root file
        assert raw == root.read_bytes(), "abstractcore's vendored identity drifted from identity/abstractframework.json"
    return json.loads(raw)


def _node_json(script: str):
    from test_gateway_console_offline import _node

    return _node(script)


def test_served_page_carries_the_about_facts_of_this_gateway() -> None:
    from importlib import metadata

    from abstractcore.utils.identity import gateway_version_rows

    from abstractgateway.routes.gateway import about_payload

    cfg = console_about_config()
    assert cfg["version"] == metadata.version("abstractgateway")
    assert cfg["rows"] == [list(r) for r in gateway_version_rows(about_payload())]
    assert cfg["rows"][0] == ["Gateway", f"AbstractGateway {cfg['version']}"]
    html = gateway_console_html()
    m = re.search(r"const GATEWAY_ABOUT = (\{.*?\});\n", html)
    assert m, "the console page does not template GATEWAY_ABOUT"
    assert json.loads(m.group(1)) == cfg
    assert "about: islands.about," in html and "islands.about = consoleAboutProps(lib" in html


def test_about_failure_is_one_visible_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    def boom() -> dict:
        raise OSError("metadata unreadable")

    monkeypatch.setattr(gw, "about_payload", boom)
    cfg = console_about_config()
    assert cfg["rows"] == [["Gateway", "unavailable (OSError: metadata unreadable)"]]
    assert cfg["version"]  # the package's own __version__, never blank


def test_about_props_on_the_real_islands_bundle_match_the_descriptor(tmp_path: Path) -> None:
    require_node()
    from abstractcore.utils.identity import about_fields, app_identity

    from test_gateway_console_offline import _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    bundle = tmp_path / "islands.js"
    bundle.write_text(ISLANDS_JS, encoding="utf-8")
    cfg = console_about_config()
    script = f"""
import fs from "node:fs"; import vm from "node:vm";
const ctx = {{ console }}; ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(bundle))}, "utf8"), ctx);
const lib = ctx.AfConsoleIslands;
const errors = [];
const console2 = {{ error: (m) => errors.push(String(m)) }};
{_slice_function(source, "consoleAboutProps").replace("console.error", "console2.error")}
const ok = consoleAboutProps(lib, {json.dumps(cfg)});
const empty = consoleAboutProps(lib, {{ version: "9.9.9", rows: [] }});
const noLib = consoleAboutProps({{ mountTopBar() {{}} }}, {json.dumps(cfg)});
console.log(JSON.stringify([{{ ok, empty, noLib, errors, fns: [typeof lib.mountAbout, typeof lib.mountTopBar], kit: lib.kitVersion }}]));
"""
    out = _node_json(script)[0]
    desc = _canonical_descriptor()
    app = desc["apps"]["abstractgateway"]
    ident = out["ok"]["identity"]
    assert ident == {"id": "abstractgateway", "name": app["name"], "version": cfg["version"], "website": app["website"],
                     "repo": app["repo"], "docs": app["docs"], "issues": app["issues"], "feedback": app["feedback"]}
    assert out["ok"]["extraRows"] == cfg["rows"]
    assert out["ok"]["label"] == "About AbstractGateway"
    assert out["empty"]["extraRows"] == [["Gateway", "unavailable (the console page carries no gateway version rows)"]]
    assert out["noLib"] is None and any("appIdentity" in e for e in out["errors"])
    assert out["fns"] == ["function", "function"]
    # What the dialog lists = the kit's aboutRows(identity, extraRows), whose
    # Python twin is about_fields (parity pinned by the kit's checks).
    rows = about_fields(app_identity("abstractgateway", cfg["version"]), dict(cfg["rows"]))
    text = "\n".join(f"{k}: {v}" for k, v in rows)
    for needle in (
        f"Application: AbstractGateway {cfg['version']}",
        "Part of: AbstractFramework — https://abstractframework.ai",
        "Author: Laurent-Philippe Albou, PhD (2023-2026)",
        "Copyright: © 2023-2026 Laurent-Philippe Albou, PhD. Released under the MIT License.",
        f"Website: {app['website']}",
        f"Source: {app['repo']}",
        f"Documentation: {app['docs']}",
        f"Report an issue: {app['issues']}",
        f"Give feedback: {app['feedback']}",
        "Contact: contact@abstractframework.ai",
        f"Gateway: AbstractGateway {cfg['version']}",
    ):
        assert needle in text, needle


def test_identity_descriptor_in_the_bundle_is_the_canonical_one(tmp_path: Path) -> None:
    """The descriptor the kit compiled into the islands bundle (an object
    literal starting `{schema:"abstractframework.identity.v1"`) evaluates to
    exactly the canonical JSON — a descriptor change without a bundle re-sync
    fails here."""
    require_node()
    start = ISLANDS_JS.find('{schema:"abstractframework.identity.v1"')
    assert start >= 0, "the islands bundle carries no identity descriptor"
    depth, i, quote = 0, start, None
    while True:
        ch = ISLANDS_JS[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    literal = ISLANDS_JS[start : i + 1]
    out = _node_json(f"console.log(JSON.stringify([({literal})]));")[0]
    assert out == _canonical_descriptor()
