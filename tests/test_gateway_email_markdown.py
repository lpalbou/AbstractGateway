"""Notification emails render the automation's model-authored Markdown safely (backlog 1011).

The model's text is untrusted: a CommonMark parser with raw HTML off turns it into inline-styled
HTML, only http/https/mailto targets become links, images never load, the plain-text part keeps
the model's text exactly as written and the model's title stays plain text."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Dict, List, Tuple

import pytest

pytestmark = pytest.mark.basic

BRIEFING = """## Today's briefing

**First, the technology story**
A new open model tops the *reasoning* leaderboard; see [the announcement](https://example.org/news/model).

1. Markets: indexes up `+0.4%`
2. Weather: rain after 15:00

- Source one
- Source two

| Topic | Change | Note |
|:------|-------:|------|
| Rates | -0.25 | first cut this year |

```
curl https://example.org/api/v1/briefing?date=2026-11-07
```

> Quoted from the agency.

---
Done."""


class _Tags(HTMLParser):
    """Every start tag with its attributes, as a browser's parser would see them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, str]]] = []
        self.text: List[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D401
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


def _parse(markup: str) -> _Tags:
    p = _Tags()
    p.feed(markup)
    return p


def _model_block(html_body: str) -> str:
    """The model-authored part of the notice's HTML."""

    label = html_body.index("model-authored text)</p>")
    start = html_body.index("word-break:break-word\">", label) + len("word-break:break-word\">")
    # The layout's own rule under the body (the model's rules use margin:14px).
    return html_body[start:html_body.index('<hr style="border:0;border-top:1px solid #dce1e8;margin:22px 0">', start)]


def _render(body: str, **extra):
    from abstractgateway.mail.notifications import render_notice

    facts = {"title": "Daily news", "context": [["Automation", "Daily news"]], "model_title": "Briefing", "model_body": body}
    facts.update(extra)
    return render_notice("automation_result", facts)


def test_headings_bold_lists_table_and_code_render_as_styled_html() -> None:
    _, _, html_body = _render(BRIEFING)
    block = _model_block(html_body)
    tags = _parse(block).tags
    names = [t for t, _ in tags]
    for tag in ("h2", "strong", "em", "ol", "ul", "li", "table", "th", "td", "pre", "code", "blockquote", "hr", "a"):
        assert tag in names, tag
    assert "**" not in block  # the operator's report: asterisks shown literally
    # Every element the parser made carries its own inline style (Gmail/Outlook drop <style>).
    for tag, attrs in tags:
        if tag in ("p", "h2", "strong", "em", "ol", "ul", "li", "table", "th", "td", "pre", "code", "blockquote", "hr", "a"):
            assert attrs.get("style"), (tag, attrs)
    assert "<style" not in html_body
    # A right-aligned column keeps its alignment next to our style.
    assert any(t == "td" and "text-align:right" in a["style"] for t, a in tags)
    # Headings stay below the layout's own 24px title.
    assert any(t == "h2" and "font-size:18px" in a["style"] for t, a in tags)


def test_long_lines_and_tables_never_widen_the_mail() -> None:
    long_word = "x" * 400
    _, _, html_body = _render(f"{long_word}\n\n| a | b |\n|---|---|\n| {long_word} | y |\n\n```\n{long_word}\n```")
    tags = _parse(_model_block(html_body)).tags
    # The layout's one column is pinned to its width (a wide model table cannot widen the mail).
    assert "max-width:600px;width:100%;margin:0 auto;table-layout:fixed" in html_body
    table_index = [t for t, _ in tags].index("table")
    assert "overflow-x:auto" in tags[table_index - 1][1]["style"]  # the table's own scroller
    for tag, attrs in tags:
        if tag in ("p", "pre"):
            assert "overflow-wrap:anywhere" in attrs["style"], tag
        if tag in ("td", "th"):
            # Cells break a long token but never a short word; the table scrolls in its own box.
            assert "overflow-wrap:break-word;word-break:normal" in attrs["style"], tag
        if tag == "pre":
            assert "white-space:pre-wrap" in attrs["style"]


@pytest.mark.parametrize(
    "body",
    [
        "<script>alert(1)</script>",
        '<img src="https://tracker.example/p.gif" onerror="alert(1)">',
        "<a href=\"javascript:alert(1)\">click</a>",
        '<div style="position:fixed">x</div>',
        "Inline <b onmouseover=alert(1)>bold</b> html",
    ],
)
def test_raw_html_from_the_model_is_escaped_text(body: str) -> None:
    _, _, html_body = _render(body)
    block = _model_block(html_body)
    tags = _parse(block)
    assert {t for t, _ in tags.tags} <= {"p", "div", "br"}, tags.tags
    assert not any(k.startswith("on") for _, a in tags.tags for k in a)
    assert "&lt;" in block  # shown as text


@pytest.mark.parametrize(
    "body",
    [
        "[x](javascript:alert(1))",
        "[x](JAVASCRIPT:alert(1))",
        "[x](&#106;avascript:alert(1))",
        "<javascript:alert(1)>",
        "[x](vbscript:msgbox(1))",
        "[x](data:text/html;base64,PHNjcmlwdD4=)",
        # The parser's own default validator lets these through; the scheme allowlist does not.
        "[x](/relative/path)",
        "[x](ftp://files.example/a)",
        "[x](tel:+33100000000)",
        "[x](https:///no-host)",
        "![pixel](data:image/png;base64,iVBORw0KGgo=)",
        "[x][r]\n\n[r]: file:///etc/passwd",
        # Empty destinations: the parser never asks validateLink about them (review F3).
        "[x]()",
        "[x](<>)",
        "[x][r]\n\n[r]: <>",
    ],
)
def test_only_http_https_and_mailto_targets_become_links(body: str) -> None:
    _, _, html_body = _render(body)
    tags = _parse(_model_block(html_body)).tags
    assert not [a for t, a in tags if t == "a"], tags
    assert not [t for t, _ in tags if t == "img"]


def test_allowed_links_and_images_as_links() -> None:
    _, _, html_body = _render(
        "[site](https://example.org/a) <mailto:me@example.org> [plain](http://example.org)\n\n"
        "![chart of rates](https://cdn.example.org/chart.png)"
    )
    tags = _parse(_model_block(html_body)).tags
    hrefs = [a["href"] for t, a in tags if t == "a"]
    assert hrefs == ["https://example.org/a", "mailto:me@example.org", "http://example.org", "https://cdn.example.org/chart.png"]
    assert not [t for t, _ in tags if t == "img"]  # no remote load
    assert "Image: chart of rates" in _model_block(html_body)
    assert "<img" not in html_body


def test_plain_text_part_is_the_model_text_exactly() -> None:
    body = BRIEFING + "\n\n<script>alert(1)</script> [x](javascript:alert(1))"
    _, text, _ = _render(body)
    start = text.index("Written by the automation (model-authored text):\nBriefing\n") + len(
        "Written by the automation (model-authored text):\nBriefing\n"
    )
    assert text[start:start + len(body)] == body
    assert text[start + len(body)] == "\n"


def test_the_model_title_stays_plain_text() -> None:
    _, text, html_body = _render("body", model_title="**Big** <i>news</i>")
    assert "&lt;i&gt;news&lt;/i&gt;" in html_body and "**Big**" in html_body
    assert "<strong" not in html_body.split("**Big**")[0][-200:]
    assert "**Big** <i>news</i>" in text


def test_the_digest_keeps_plain_lines() -> None:
    """The recorded digest rule: Markdown only inside a single notice's model block."""

    from abstractgateway.mail.notifications import render_digest

    subject, text, _ = _render("**bold** line")
    _, digest_text, digest_html = render_digest([{"subject": subject, "text": text}])
    assert "**bold** line" in digest_text and "**bold** line" in digest_html
    assert "<strong" not in digest_html


def test_link_allowlist_unit() -> None:
    from abstractgateway.mail.mail_markdown import allowed_link

    assert allowed_link("https://example.org") and allowed_link("http://example.org/x?y=1") and allowed_link("mailto:a@b.c")
    for bad in ("", "javascript:alert(1)", "/x", "//example.org", "ftp://example.org", "data:image/png;base64,AA", "mailto:", "https://", "http://[::1"):
        assert not allowed_link(bad), bad


def test_palette_matches_the_layout() -> None:
    from abstractgateway.mail import mail_markdown, notifications

    for name in ("C_TEXT", "C_MUTED", "C_LINE", "C_DEEP", "C_CARD"):
        assert getattr(mail_markdown, name) == getattr(notifications, name), name


def test_markdown_it_py_is_a_declared_dependency() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    from pathlib import Path

    deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
    from packaging.requirements import Requirement
    from packaging.version import Version

    (req,) = [Requirement(d) for d in deps if Requirement(d).name == "markdown-it-py"]
    # 4.0.0 bounds the table cells the parser completes; 3.0.0 does not (review F2, 2026-11-07).
    assert any(spec.operator == ">=" and Version(spec.version) >= Version("4.0.0") for spec in req.specifier)


def test_the_installed_parser_bounds_completed_table_cells() -> None:
    import importlib

    # `markdown_it.rules_block.table` the attribute is the rule function; the module holds the bound.
    table = importlib.import_module("markdown_it.rules_block.table")
    assert table.MAX_AUTOCOMPLETED_CELLS > 0


def _amplifying_table() -> str:
    """The review's case (2026-11-07): 5.2 KB of model text, a 300-column header and 2,000 one-word
    rows, which the parser completes cell by cell into 8 MB of styled HTML."""

    return "|a" * 300 + "|\n" + "|-" * 300 + "|\n" + "x\n" * 2000


def test_an_amplifying_text_falls_back_to_the_whole_text_as_plain_text() -> None:
    from abstractgateway.mail.mail_markdown import MAX_RENDERED_HTML_BYTES, render_model_markdown

    body = _amplifying_table()
    assert 5_000 < len(body) < 5_500
    assert render_model_markdown(body) is None  # rendered, it is megabytes: over the budget
    _, text, html_body = _render(body)
    assert len(html_body.encode("utf-8")) < MAX_RENDERED_HTML_BYTES
    block = _model_block(html_body)
    assert "white-space:pre-wrap" in html_body and "<table" not in block and "<td" not in block
    # Not truncated: every line of the text is in the HTML, escaped, and the plain part is unchanged.
    assert block.count("\nx") == 2000 and ("|a" * 300) in block
    assert body in text


def test_an_over_long_text_is_never_parsed() -> None:
    from abstractgateway.mail import mail_markdown

    body = "**word** " * (mail_markdown.MAX_MARKDOWN_INPUT_CHARS // 9 + 1)
    assert len(body) > mail_markdown.MAX_MARKDOWN_INPUT_CHARS
    assert mail_markdown.render_model_markdown(body) is None
    _, _, html_body = _render(body)
    assert "<strong" not in _model_block(html_body) and "**word**" in html_body


def test_a_long_briefing_stays_rendered() -> None:
    from abstractgateway.mail.mail_markdown import render_model_markdown

    long_briefing = "\n\n".join([BRIEFING] * 20)  # ~20 KB of ordinary Markdown
    assert render_model_markdown(long_briefing) is not None


def test_a_refused_link_keeps_its_text() -> None:
    _, _, html_body = _render("before [the label]() after")
    block = _model_block(html_body)
    assert "the label" in block and "<a" not in block

