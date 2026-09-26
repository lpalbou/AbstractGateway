"""The served console page carries no maintainer notes.

The console's HTML, CSS and JS sources keep their comments for whoever edits
them; the page a browser receives does not (project codenames, review
references, dates of past decisions mean nothing to a user).
"""

import re

import pytest

from abstractgateway.console import (
    _strip_css_comments,
    _strip_html_comments,
    _strip_js_line_comments,
    gateway_console_html,
)

# `mission` is case-sensitive: "a mission the entity works on" is UI text,
# "mission Z" is a project codename. The author's full name is shown on
# purpose (the AbstractFramework identity every About screen carries); a bare
# "laurent" in the page is still maintainer narrative.
_NARRATIVE = re.compile(r"[Mm]ission [A-Z]{1,2}\d?\b|(?i:charter|2026-0\d|laurent(?!-philippe albou)|dm#\d)")


@pytest.fixture(scope="module")
def page() -> str:
    return gateway_console_html()


def test_served_console_has_no_maintainer_narrative(page: str) -> None:
    hits = [page[max(0, m.start() - 60) : m.end() + 20] for m in _NARRATIVE.finditer(page)]
    assert hits == []


def test_served_console_has_no_html_comments_outside_scripts(page: str) -> None:
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", page, flags=re.S)
    assert "<!--" not in markup


def test_every_placeholder_was_filled(page: str) -> None:
    assert not re.search(r"<!--__[A-Z_]+__-->|/\*__[A-Z_]+__\*/", page)
    for token in ("__KIT_THEME_SPECS_JSON__", "__CORE_CONSOLE_CONFIG_JSON__"):
        assert token not in page


def test_css_comments_go_placeholders_stay() -> None:
    css = "a { color: red; } /* why red (review 2026-07-01) */\n/*__KEEP__*/\nb { x: 1; }"
    out = _strip_css_comments(css)
    assert "review" not in out and "/*__KEEP__*/" in out and "b { x: 1; }" in out


def test_js_whole_line_comments_go_code_and_strings_stay() -> None:
    js = (
        "const a = 1;\n"
        "  // mission Z: why\n"
        "  /* one-line block */\n"
        "  /* multi\n"
        "     line */\n"
        "  /*__KEEP__*/\n"
        'const url = "http://x/y"; // trailing stays\n'
    )
    out = _strip_js_line_comments(js)
    assert "mission" not in out and "one-line" not in out and "multi" not in out
    assert "/*__KEEP__*/" in out
    assert 'const url = "http://x/y"; // trailing stays' in out
    assert out.startswith("const a = 1;\n")


def test_html_comments_go_but_not_inside_scripts() -> None:
    page = (
        "<body>\n<!-- a note\n  on two lines -->\n<p>x</p><!--__SLOT__-->\n"
        "<style>/* css note */ p { a: b; }</style>\n"
        '<script>\n// js note\nconst s = "<!-- not markup -->";\n</script>\n</body>'
    )
    out = _strip_html_comments(page)
    assert "a note" not in out and "css note" not in out and "js note" not in out
    assert "<!--__SLOT__-->" in out and "p { a: b; }" in out
    assert 'const s = "<!-- not markup -->";' in out
