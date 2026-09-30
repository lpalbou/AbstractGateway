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
    # Escape, backdrop, close button and a picked section close it; a resize past md drops it.
    for needle in ('$("nav-backdrop").onclick', '$("nav-close").onclick', '$("console-nav").onclick',
                   'event.key === "Escape" && navDrawerOpen()', 'matchMedia("(min-width: 1024px)")'):
        assert needle in html, needle


def test_shell_uses_the_dynamic_viewport_height() -> None:
    css = _styles(gateway_console_html())
    assert re.search(r"\.shell \{[^}]*height: var\(--vh-full, 100vh\)", css)
    assert "--vh-full" in css and "--tap-min" in css  # kit 0.3.0 tokens reach the page
