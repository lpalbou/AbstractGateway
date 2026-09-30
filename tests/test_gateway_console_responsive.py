"""Responsive layout contract of the served console (DESIGN.md of the 2026-09-30
responsive workstream): viewport meta, named breakpoints only, the sidebar ->
drawer markup, and the scoped header rule whose global form broke the model
catalog's card heads below 940 px."""

from __future__ import annotations

import re

import pytest

from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic

NAMED_MAX_WIDTHS = {"479.98px", "767.98px", "1023.98px", "1439.98px"}
NAMED_MIN_WIDTHS = {"480px", "768px", "1024px", "1440px"}


def _styles(html: str) -> str:
    return "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S))


def test_viewport_meta_allows_pinch_zoom_and_safe_areas() -> None:
    html = gateway_console_html()
    meta = re.search(r'<meta name="viewport" content="([^"]+)">', html)
    assert meta, "viewport meta missing"
    content = meta.group(1)
    assert "width=device-width" in content and "viewport-fit=cover" in content
    assert "maximum-scale" not in content and "user-scalable" not in content


def test_every_media_query_uses_a_named_breakpoint() -> None:
    css = _styles(gateway_console_html())
    queries = re.findall(r"@media\s*([^{]+)\{", css)
    assert queries, "no media queries found (the style extraction broke)"
    widths = re.findall(r"\((max|min)-width:\s*([0-9.]+px)\)", " ".join(queries))
    assert widths
    stray = sorted({f"{kind}-width: {value}" for kind, value in widths
                    if value not in (NAMED_MAX_WIDTHS if kind == "max" else NAMED_MIN_WIDTHS)})
    assert stray == [], f"app-local breakpoints left (map them to 480/768/1024/1440): {stray}"
    heights = set(re.findall(r"\(max-height:\s*([0-9.]+px)\)", " ".join(queries)))
    assert heights <= {"500px"}, heights


def test_no_rule_targets_every_header_element() -> None:
    """`header { flex-direction: column }` inside a width query restyled the
    catalog's <header class="mc-card__head"> too; header rules must be scoped."""
    css = re.sub(r"/\*.*?\*/", "", _styles(gateway_console_html()), flags=re.S)
    assert not re.search(r"(^|[\s{};,])header\s*[{,]", css), "a bare `header` selector is back"


def test_sidebar_becomes_a_drawer_below_md() -> None:
    html = gateway_console_html()
    assert 'id="console-nav" class="shell_sidebar' in html
    toggle = re.search(r'<button id="nav-toggle"[^>]*>', html)
    assert toggle and 'aria-controls="console-nav"' in toggle.group(0) and 'aria-expanded="false"' in toggle.group(0)
    assert 'id="nav-close"' in html and 'id="nav-backdrop"' in html
    css = _styles(html)
    drawer = re.search(r"@media \(max-width: 1023\.98px\) \{\s*\.shell_sidebar \{(.*?)\}", css, flags=re.S)
    assert drawer and "position: fixed" in drawer.group(1) and "translateX(-105%)" in drawer.group(1)
    assert "body.nav-open .shell_sidebar" in css
    # Wired at boot; its behaviour (Escape, backdrop, close, section pick, inert,
    # one layer per Escape) is exercised by the node tests below.
    assert "installNavDrawer(document, " in html and 'matchMedia("(min-width: 1024px)")' in html


def test_shell_uses_the_dynamic_viewport_height() -> None:
    css = _styles(gateway_console_html())
    assert re.search(r"\.shell \{[^}]*height: var\(--vh-full, 100vh\)", css)
    assert "--vh-full" in css and "--tap-min" in css  # kit 0.3.0 tokens reach the page


def test_every_css_variable_the_page_uses_is_declared_or_has_a_fallback() -> None:
    """An undefined `var(--x)` without a fallback makes its declaration invalid at
    computed-value time (silently `unset`): a 44 px touch rule would do nothing.
    The kit's base and responsive tokens (--tap-min, --vh-full, --safe-*, ...)
    reach the page through console_islands.ISLANDS_CSS (kit theme.css minus the
    per-theme blocks, drift-pinned); the theme blocks through console_themes."""
    css = re.sub(r"/\*.*?\*/", "", _styles(gateway_console_html()), flags=re.S)
    declared = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", css))
    assert {"--tap-min", "--vh-full", "--safe-top", "--font-size-input", "--gutter"} <= declared
    undefined = sorted({name for name, sep in re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)\s*([,)])", css)
                        if sep == ")" and name not in declared})
    assert undefined == [], f"CSS variables used without a declaration or fallback: {undefined}"


# ---------------------------------------------------------------- nav drawer behaviour
# The drawer logic is one self-contained function in the page (installNavDrawer);
# these tests run it in node against a small fake DOM, so a behaviour mutation
# (Escape ignored, backdrop dead, no inert, a dialog's Escape also closing the
# drawer) turns them red, not only a missing string.

def _function_source(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
    depth, i = 0, html.index("{", start)
    while True:
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
        i += 1


_DRAWER_HARNESS = r"""
const results = {};
class El {
  constructor(id) { this.id = id; this.cls = new Set(); this.attrs = {}; this.visible = true; this.onclick = null;
    const self = this;
    this.classList = {
      add: (n) => self.cls.add(n), remove: (n) => self.cls.delete(n), contains: (n) => self.cls.has(n),
      toggle: (n, force) => { const on = force === undefined ? !self.cls.has(n) : !!force; if (on) self.cls.add(n); else self.cls.delete(n); return on; },
    }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  getClientRects() { return this.visible && !this.cls.has("hidden") ? [{}] : []; }
  get offsetParent() { return this.visible ? {} : null; }
  focus() { doc.activeElement = this; }
}
const ids = {}; const byId = (id) => (ids[id] ||= new El(id));
const body = new El("body"); const shell = new El("shell_main"); const activeTab = new El("tab-button-users");
const modal = byId("log-modal-backdrop"); modal.cls.add("modal-backdrop"); modal.cls.add("hidden");
const firstRun = byId("first-run-backdrop"); firstRun.cls.add("first-run-page"); firstRun.cls.add("hidden");
const doc = { body, activeElement: null,
  querySelector: (sel) => ({ ".shell_main": shell, "#console-nav .tab-button.active": activeTab, "#console-nav .tab-button": activeTab })[sel] || null,
  querySelectorAll: (sel) => (sel.includes(".modal-backdrop") ? [modal, firstRun] : []),
};
const keyHandlers = [];
const win = { addEventListener: (type, fn, capture) => { if (type === "keydown") keyHandlers.push({ fn, capture }); },
  matchMedia: () => ({ matches: false, addEventListener() {} }) };
const press = () => { const ev = { key: "Escape", defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const h of keyHandlers) h.fn(ev); return ev; };
const state = () => ({ open: body.cls.has("nav-open"), inert: shell.hasAttribute("inert"), expanded: byId("nav-toggle").attrs["aria-expanded"] || null, focus: doc.activeElement && doc.activeElement.id });
__INSTALL__
installNavDrawer(doc, win, byId);
results.captureListener = keyHandlers.length === 1 && keyHandlers[0].capture === true;
byId("nav-toggle").onclick(); results.opened = state();
let ev = press(); results.escape = { ...state(), prevented: ev.defaultPrevented };
byId("nav-toggle").onclick(); modal.cls.delete("hidden");
ev = press(); results.escapeWithDialog = { ...state(), prevented: ev.defaultPrevented };
modal.cls.add("hidden"); ev = press(); results.escapeAfterDialog = state();
byId("nav-toggle").onclick(); ev = { key: "Escape", defaultPrevented: true, preventDefault() {} };
for (const h of keyHandlers) h.fn(ev); results.escapeConsumed = state();
byId("nav-backdrop").onclick(); results.backdrop = state();
byId("nav-toggle").onclick(); byId("nav-close").onclick(); results.closeButton = state();
byId("nav-toggle").onclick(); byId("console-nav").onclick({ target: { closest: (s) => (s === ".tab-button" ? activeTab : null) } }); results.pickSection = state();
console.log(JSON.stringify(results));
"""


def _run_drawer() -> dict:
    import json
    import subprocess

    from node_requirement import require_node

    node = require_node()
    src = _function_source(gateway_console_html(), "installNavDrawer")
    script = _DRAWER_HARNESS.replace("__INSTALL__", src)
    out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_nav_drawer_opens_inerts_the_shell_and_escape_closes_it() -> None:
    r = _run_drawer()
    assert r["captureListener"], "Escape must be read in the capture phase (before the dialogs' own handlers)"
    assert r["opened"] == {"open": True, "inert": True, "expanded": "true", "focus": "tab-button-users"}
    assert r["escape"] == {"open": False, "inert": False, "expanded": "false", "focus": "nav-toggle", "prevented": True}


def test_escape_closes_one_layer_the_dialog_above_the_drawer_first() -> None:
    r = _run_drawer()
    assert r["escapeWithDialog"]["open"] is True and r["escapeWithDialog"]["prevented"] is False
    assert r["escapeAfterDialog"]["open"] is False
    assert r["escapeConsumed"]["open"] is True  # a key another handler consumed is not ours


def test_backdrop_close_button_and_section_pick_close_the_drawer() -> None:
    r = _run_drawer()
    for key in ("backdrop", "closeButton", "pickSection"):
        assert r[key]["open"] is False and r[key]["inert"] is False, key


def test_dialogs_stack_above_the_drawers_and_the_toggle_shows_below_md() -> None:
    css = _styles(gateway_console_html())
    assert re.search(r"--z-drawer:\s*900", css) and re.search(r"--z-connect-modal:\s*1000", css)
    assert re.search(r"\.modal-backdrop \{[^}]*z-index: var\(--z-connect-modal, 1000\)", css)
    assert re.search(r"\.shell_sidebar \{[^}]*z-index: var\(--z-drawer, 900\)", css)
    md = css[css.index("@media (max-width: 1023.98px) {\n\t      .shell_sidebar {"):]
    assert re.search(r"body\.signed-in \.shell_nav_toggle \{ display: inline-flex; \}", md[:2000])


def test_sandbox_card_never_clips_its_composer() -> None:
    """At 1280x800 / 1366x768 the composer was cut off by the card's overflow:hidden."""
    css = _styles(gateway_console_html())
    rule = re.search(r"\n\t    \.sandbox-chat \{([^}]*)\}", css)
    assert rule and "overflow: hidden" not in rule.group(1) and "overflow-y: auto" in rule.group(1)
