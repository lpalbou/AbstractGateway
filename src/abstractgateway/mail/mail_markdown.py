"""The automation's model-authored text, rendered from Markdown for a notification email (backlog 1011).

The text is untrusted model output, so the converter is the safety boundary and it is a real
CommonMark parser, never a regex or a text heuristic:

- `markdown-it-py` in CommonMark mode with raw HTML OFF: any HTML the model writes (`<script>`,
  `<img onerror=...>`) is parsed as text and escaped, never rendered.
- Links: only `http`/`https` (with a host) and `mailto` targets are links (`allowed_link`, the
  parser's `validateLink`). Any other target (`javascript:`, `data:`, `vbscript:`, `file:`, `tel:`,
  relative paths) is not a link at all: the Markdown stays visible as escaped text.
- Images never load (the layout promises no remote resources): an image is a link to its address
  labelled "Image: <alt>", under the same allowlist.
- Every element carries its own inline `style` in the layout's palette (Gmail and Outlook drop
  `<style>` blocks); long words, URLs and code wrap, and a table sits in its own horizontal
  scroller (its cells wrap at word boundaries), so nothing widens the mail past the layout on a
  phone.
- Line breaks inside a paragraph are kept (`breaks`): a model's "one fact per line" reads the
  same as in the plain-text part.

Over the size budgets (`MAX_MARKDOWN_INPUT_CHARS`, `MAX_RENDERED_HTML_BYTES`) nothing is rendered: the
caller shows the whole text escaped, as plain text.

The plain-text part of the email is NOT produced here: it carries the model's text exactly as
written. The model's title is never Markdown (the caller escapes it).

The digest (`render_digest`) does not use this: it is assembled from the stored plain-text parts,
where the gateway's own lines and the model's lines are no longer told apart, so rendering it as
Markdown would interpret the gateway's text too. One rule for both: Markdown is rendered only
inside the labelled model block of a single notice; everything else stays plain text.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

ALLOWED_SCHEMES = ("http", "https", "mailto")

# Size budgets. Every rendered element carries its own inline style (~100-170 bytes), and the parser
# completes missing table cells, so a few KB of model text can render to megabytes (a 5.2 KB table with
# a 300-column header renders to 8 MB): Gmail clips a message above ~102 KB, SMTP servers refuse
# 10-25 MB, and the HTML is stored in the outbox. Past either budget the body is NOT rendered and the
# email shows the whole text as escaped plain text, the way it did before Markdown (never truncated);
# the input budget also bounds the parser's own time on long input.
MAX_MARKDOWN_INPUT_CHARS = 100_000
MAX_RENDERED_HTML_BYTES = 200_000

# The layout's palette (notifications.C_*), repeated here so this module has no import cycle;
# a test keeps the two equal.
C_TEXT = "#202938"
C_MUTED = "#526071"
C_LINE = "#dce1e8"
C_DEEP = "#0f3460"
C_CARD = "#ffffff"
C_CODE_BG = "#eef1f5"

_MONO = "Menlo,Consolas,Monaco,monospace"
_WRAP = "overflow-wrap:anywhere;word-break:break-word"
# Table cells: break a long token only when it cannot fit, never a short word ("Tod|ay"): a
# table whose columns cannot fit scrolls inside its own box instead (_TABLE_WRAP). The values
# are explicit because the model block's anywhere/break-word would be inherited.
_CELL_WRAP = "overflow-wrap:break-word;word-break:normal"

_HEADING_SIZES = {"h1": "20px", "h2": "18px", "h3": "16px", "h4": "15px", "h5": "15px", "h6": "15px"}

# Opening token type -> inline style. Headings stay below the layout's own 24px title.
_STYLES: Dict[str, str] = {
    "paragraph_open": f"margin:0 0 10px;color:{C_TEXT};font-size:15px;line-height:1.55;{_WRAP}",
    "bullet_list_open": f"margin:0 0 10px;padding-left:22px;color:{C_TEXT};font-size:15px;line-height:1.5",
    "ordered_list_open": f"margin:0 0 10px;padding-left:22px;color:{C_TEXT};font-size:15px;line-height:1.5",
    "list_item_open": f"margin:0 0 4px;{_WRAP}",
    "blockquote_open": f"margin:0 0 10px;padding:2px 0 2px 12px;border-left:4px solid {C_LINE};color:{C_MUTED}",
    "strong_open": "font-weight:bold",
    "em_open": "font-style:italic",
    "s_open": "text-decoration:line-through",
    "link_open": f"color:{C_DEEP};text-decoration:underline;{_WRAP}",
    "code_inline": f"padding:1px 4px;background:{C_CODE_BG};border-radius:4px;font-family:{_MONO};font-size:13px;{_WRAP}",
    "hr": f"border:0;border-top:1px solid {C_LINE};margin:14px 0",
    "table_open": f"width:100%;margin:0;border-collapse:collapse;font-size:14px;color:{C_TEXT}",
    "th_open": f"padding:6px 8px;border:1px solid {C_LINE};background:{C_CODE_BG};font-weight:bold;text-align:left;vertical-align:top;{_CELL_WRAP}",
    "td_open": f"padding:6px 8px;border:1px solid {C_LINE};vertical-align:top;{_CELL_WRAP}",
}
_PRE = (
    f"margin:0 0 10px;padding:10px 12px;background:{C_CARD};border:1px solid {C_LINE};border-radius:6px;"
    f"color:{C_TEXT};font-family:{_MONO};font-size:13px;line-height:1.45;white-space:pre-wrap;{_WRAP}"
)
_TABLE_WRAP = "margin:0 0 10px;max-width:100%;overflow-x:auto"


def allowed_link(url: str) -> bool:
    """True only for an http(s) URL with a host or a mailto address: the parser makes no link,
    and no image link, for anything else."""

    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return False
    if scheme == "mailto":
        return bool(parts.path)
    return bool(parts.hostname)


def _style_tokens(tokens: List[Token]) -> None:
    for token in tokens:
        style = _STYLES.get(token.type)
        if token.type == "heading_open":
            style = f"margin:16px 0 8px;color:{C_TEXT};font-size:{_HEADING_SIZES.get(token.tag, '15px')};line-height:1.3;font-weight:bold"
        if style:
            # th/td carry the column alignment as `style="text-align:…"`; it stays, after ours.
            own = token.attrGet("style")
            token.attrSet("style", f"{style};{own}" if own else style)
        if token.children:
            _style_tokens(token.children)


def _demote_refused_links(tokens: List[Token]) -> None:
    """A link whose href is not allowed becomes its text. The parser asks `validateLink` only for a
    non-empty destination, so `[x]()` would otherwise keep `href=""` (review F3, 2026-11-07)."""

    for token in tokens:
        if token.children:
            children = token.children
            i = 0
            while i < len(children):
                child = children[i]
                if child.type == "link_open" and not allowed_link(str(child.attrGet("href") or "")):
                    close = next(j for j in range(i + 1, len(children)) if children[j].type == "link_close")
                    del children[close]
                    del children[i]
                    continue
                i += 1


def _core_styles(state: Any) -> None:
    _demote_refused_links(state.tokens)
    _style_tokens(state.tokens)


def _render_fence(self: Any, tokens: List[Token], idx: int, options: Any, env: Any) -> str:
    return f'<pre style="{_PRE}">{html.escape(tokens[idx].content, quote=True)}</pre>\n'


def _render_image(self: Any, tokens: List[Token], idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    alt = self.renderInlineAsText(token.children or [], options, env).strip()
    src = str(token.attrGet("src") or "")
    label = f"Image: {alt}" if alt else "Image"
    # `src` passed validateLink (the image is not parsed otherwise); checked again here, at the
    # one place an address becomes an href.
    if not allowed_link(src):
        return html.escape(label, quote=True)
    return f'<a href="{html.escape(src, quote=True)}" style="{_STYLES["link_open"]}">{html.escape(label, quote=True)}</a>'


def _render_table_open(self: Any, tokens: List[Token], idx: int, options: Any, env: Any) -> str:
    return f'<div style="{_TABLE_WRAP}">' + self.renderToken(tokens, idx, options, env)


def _render_table_close(self: Any, tokens: List[Token], idx: int, options: Any, env: Any) -> str:
    return self.renderToken(tokens, idx, options, env) + "</div>\n"


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.validateLink = allowed_link  # type: ignore[method-assign]
    md.core.ruler.push("mail_inline_styles", _core_styles)
    md.add_render_rule("fence", _render_fence)
    md.add_render_rule("code_block", _render_fence)
    md.add_render_rule("image", _render_image)
    md.add_render_rule("table_open", _render_table_open)
    md.add_render_rule("table_close", _render_table_close)
    return md


_MD = _build()


def render_model_markdown(text: str) -> Optional[str]:
    """HTML (inline-styled, no raw HTML, allowlisted links, no images) for the model's text, or None
    when the text or its rendering is over budget (the caller then shows the text as plain text)."""

    source = str(text or "")
    if len(source) > MAX_MARKDOWN_INPUT_CHARS:
        return None
    rendered = _MD.render(source)
    if len(rendered.encode("utf-8")) > MAX_RENDERED_HTML_BYTES:
        return None
    return rendered
