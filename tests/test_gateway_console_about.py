"""The console's About (kit 0.7.0 compact About, islands API 2): the top-bar
About action shows the gateway's identity (`appIdentity("abstractgateway",
<served version>)`), the AbstractFramework version installed on this host and
the served gateway version, read once from GET /about (`about_payload`) and
templated into the page by the serving gateway. NO package list (operator,
round 5): the per-package versions of GET /about never reach the About.

- The served page carries `GATEWAY_ABOUT` = `console_about_config()`.
- On the REAL vendored islands bundle (node vm), `consoleAboutProps` builds the
  kit props and the bundle's `AfAboutDialog` renders the compact card: name +
  version, the two versions, six links, the licence line, no package.
- The identity descriptor compiled into the bundle equals the canonical one.
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

    from abstractgateway.routes.gateway import about_payload

    cfg = console_about_config()
    payload = about_payload()
    assert cfg["version"] == metadata.version("abstractgateway")
    assert cfg["gateway"] == cfg["version"]
    assert cfg["framework"] == (payload.get("abstractframework") or None)
    assert set(cfg) == {"version", "framework", "framework_note", "gateway", "gateway_note"}, "no package list in the About config"
    html = gateway_console_html()
    m = re.search(r"const GATEWAY_ABOUT = (\{.*?\});\n", html)
    assert m, "the console page does not template GATEWAY_ABOUT"
    assert json.loads(m.group(1)) == cfg
    assert "about: islands.about," in html and "islands.about = consoleAboutProps(lib" in html


def test_about_failure_is_said_never_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    def boom() -> dict:
        raise OSError("metadata unreadable")

    monkeypatch.setattr(gw, "about_payload", boom)
    cfg = console_about_config()
    assert cfg["version"]  # the package's own __version__, never blank
    assert cfg["gateway"] == cfg["version"]  # the console IS this gateway
    assert cfg["framework"] is None


def test_framework_not_installed_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "about_payload", lambda: {"abstractgateway": "9.9.9", "abstractframework": None, "packages": {"abstractcore": "2.0.0"}})
    cfg = console_about_config()
    assert cfg == {"version": "9.9.9", "framework": None, "framework_note": "not installed on this host", "gateway": "9.9.9", "gateway_note": ""}


def test_about_card_on_the_real_islands_bundle(tmp_path: Path) -> None:
    require_node()
    from test_gateway_console_offline import _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    bundle = tmp_path / "islands.js"
    bundle.write_text(ISLANDS_JS, encoding="utf-8")
    cfg = {"version": "9.8.7", "framework": "0.9.6", "framework_note": "", "gateway": "9.8.7", "gateway_note": ""}
    nofw = {**cfg, "framework": None, "framework_note": "not installed on this host"}
    script = f"""
import fs from "node:fs"; import vm from "node:vm";
const ctx = {{ console }}; ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(bundle))}, "utf8"), ctx);
const lib = ctx.AfConsoleIslands;
const errors = [];
const console2 = {{ error: (m) => errors.push(String(m)) }};
{_slice_function(source, "consoleAboutProps").replace("console.error", "console2.error")}
const ok = consoleAboutProps(lib, {json.dumps(cfg)});
const nofw = consoleAboutProps(lib, {json.dumps(nofw)});
const empty = consoleAboutProps(lib, {{ version: "9.9.9" }});
const noLib = consoleAboutProps({{ mountTopBar() {{}} }}, {json.dumps(cfg)});
console.log(JSON.stringify([{{ ok, nofw, empty, noLib, errors, api: lib.apiVersion, fns: [typeof lib.mountAbout, typeof lib.mountTopBar, typeof lib.aboutVersionsFromGateway], kit: lib.kitVersion }}]));
"""
    out = _node_json(script)[0]
    desc = _canonical_descriptor()
    app = desc["apps"]["abstractgateway"]
    assert out["api"] == "2"
    assert out["ok"]["identity"] == {"id": "abstractgateway", "name": app["name"], "version": "9.8.7", "website": app["website"],
                                     "repo": app["repo"], "docs": app["docs"], "issues": app["issues"], "feedback": app["feedback"]}
    assert out["ok"]["versions"] == {"framework": "0.9.6", "gateway": "9.8.7"}
    assert "extraRows" not in out["ok"]
    assert out["nofw"]["versions"] == {"framework": None, "gateway": "9.8.7", "frameworkNote": "not installed on this host"}
    assert out["empty"]["versions"]["gatewayNote"] == "unavailable (the console page carries no gateway version)"
    assert out["noLib"] is None and any("appIdentity" in e for e in out["errors"])
    assert out["fns"] == ["function", "function", "function"]


def test_about_dialog_markup_from_the_bundle_has_no_package_list(tmp_path: Path) -> None:
    """The vendored bundle carries the 0.7.0 compact card (its class names and
    the framework note) and no longer the 0.6 rows dialog (red on a stale
    re-sync)."""
    for needle in ("af-about-card__versions", "af-about-card__links", "af-about-card__legal", '"not installed on the gateway host"'):
        assert needle in ISLANDS_JS, needle
    # the 0.6 rows dialog is gone from the vendored bundle
    assert "af-about__rows" not in ISLANDS_JS


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
