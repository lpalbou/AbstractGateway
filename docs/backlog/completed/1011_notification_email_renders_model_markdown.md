# 1011-abstractgateway: [IMPROVEMENT] Notification emails render the automation's Markdown safely

> Created: 2026-11-07
> Type: improvement
> Priority: P2
> Labels: mail, notifications, automations, security
> Status: completed 2026-11-07 (planned 2026-11-07 for the 0.21.0 cut, operator scope G4; branch `feat/0210-mail-and-fixes`, unreleased)

- Work-item id: abstractgateway-1011
- Owner: framework (intake); implemented by Gateway
- Thread: Unavailable; agora not connected at intake.
- Hub refs: None.

## Summary

An automation result email shows the model's Markdown literally: `**First, the technology story**`
arrives with its asterisks (operator report, 2026-11-07, occurrence 29 of a daily news briefing).
Render the model-authored block from Markdown to HTML with a safe converter, styled inline for mail
clients, while the plain-text copy keeps the text exactly as the model wrote it.

## Context and current code reality

- `src/abstractgateway/mail/notifications.py::render_notice` builds every notice (automation result,
  `notify_me` from a flow, approvals, failures) as a fixed branded HTML layout plus a plain-text twin.
- The model-authored title/body go through `_block_model`, which escapes them with `_h`
  (`html.escape`) and shows the body in a `white-space:pre-wrap` div. This is deliberate: the text is
  untrusted model output, so it is never interpreted as HTML. Markdown is therefore shown raw.
- The block is labelled "Written by the automation (model-authored text)" or "Written by the flow";
  that labelling stays.
- `render_digest` builds its own simpler HTML from the plain-text lines (`<p>` per line, escaped); it
  carries the same raw Markdown when a digest includes a result.
- No Markdown library is a declared gateway dependency. `markdown-it-py` 4.0 is present in the
  gateway's environment today only transitively (required by `rich` and AbstractAssistant), so it must
  be declared in `pyproject.toml` before the gateway imports it.

## Proposed direction and scope

1. Convert the model body with `markdown-it-py` in CommonMark mode with raw HTML disabled
   (`html=False`), so any HTML the model writes is escaped, never rendered.
2. Allow only `http`, `https` and `mailto` link targets; drop or neutralise every other scheme
   (`javascript:`, `data:`, `vbscript:`, relative). Images: render as links, never load remote images
   (the layout promises no remote resources).
3. Apply inline styles per element (headings, paragraphs, bold/italic, lists, block quotes, code,
   tables, horizontal rules) in the layout's palette: Gmail and Outlook ignore `<style>` blocks.
   Keep long lines and tables from widening the mail past the layout on phones (wrap; tables scroll
   or stack).
4. Keep the plain-text twin byte-for-byte the model's text (Markdown reads well as text).
5. Decide whether the digest renders the same way or keeps plain lines (one rule for both, recorded).
6. The model title stays plain text (escaped), never Markdown.

Non-goals: rendering Markdown anywhere the gateway does not already show model text; sanitising with a
regex or any text heuristic (operator rule: no regex/NLP heuristics); changing what the model is
asked to write.

## Dependencies

- None upstream. Ships with the next AbstractGateway release that the operator schedules it for; the
  root pins then carry it.

## Validation

- Unit tests on `render_notice`: headings, bold, lists, a table and a code block render as styled HTML;
  `<script>`, an `onerror=` attribute, raw `<img src=...>` and `[x](javascript:alert(1))` from the
  model come out inert (escaped text or dropped link); the plain-text part equals the input.
- Mutation check: switch `html=False` to `True` (or remove the scheme allowlist) and watch the
  injection tests go red.
- One real send through a scratch gateway and mailbox, viewed in Gmail web, Apple Mail and Outlook
  (desktop or web), plus a 400 px phone view; screenshots kept under the gateway's `untracked/`.

## Outcome (2026-11-07)

- `src/abstractgateway/mail/mail_markdown.py` renders the model body: `MarkdownIt("commonmark", {"html": False,
  "breaks": True})` with the `table` and `strikethrough` rules enabled. `validateLink` is `allowed_link`
  (`urllib.parse.urlsplit`: scheme `http`/`https` with a host, or `mailto` with an address), so any other target
  is never parsed as a link and stays visible as escaped text. Images render as a link labelled
  "Image: <alt>" (checked again against the allowlist where the href is written); fenced and indented code
  render as a styled `<pre>` without the language class. A core rule puts an inline `style` on every
  element (palette constants shared with `notifications.py`, a test keeps them equal).
- `_block_model` uses it for the body; the title stays `_h()`-escaped plain text. The text part is unchanged.
- Width: the layout's one-column table is `table-layout:fixed` (nothing inside can widen it), paragraphs, list
  items, links, inline code and `<pre>` wrap anywhere, a table sits in an `overflow-x:auto` box and its cells
  wrap at word boundaries (`overflow-wrap:break-word;word-break:normal`), so short words are never split.
- `markdown-it-py>=4.0.0` declared in `pyproject.toml` (first declared `>=3.0.0`; raised after the review found 3.0.0 has no bound on auto-completed table cells, F2).
- Decided: `breaks: True` (a model's one-fact-per-line text keeps its lines, as in the plain part).

### The digest rule (decided and recorded)

Markdown is rendered only inside the labelled model block of a single notice; everything else stays plain
text. The digest (`render_digest`) is assembled from the stored plain-text parts, where the gateway's own
lines (names, causes, fixes) and the model's lines are no longer told apart, so rendering it as Markdown
would interpret the gateway's text too. It keeps its escaped plain lines, which read well because the
plain part is the model's Markdown as written. Recorded in `mail_markdown.py`'s docstring and
`docs/email.md`.

## Evidence

- `tests/test_gateway_email_markdown.py` (26 tests): headings, bold, italic, ordered and bullet lists, a table
  with column alignment, code (inline and fenced), quote, rule and link render with an inline style on every
  element and no `<style>` block; raw HTML (`<script>`, `<img onerror>`, `<a href="javascript:">`, an inline
  `onmouseover`, a styled `<div>`) is escaped text; twelve link/image targets (`javascript:` in three
  spellings, `vbscript:`, `data:`, relative, `ftp:`, `tel:`, host-less `https:///`, a `data:` image, a
  `file:` reference link) produce no `<a>` and no `<img>`; allowed links and an image-as-link keep their
  hrefs; the plain-text part carries the model text byte for byte; the title stays plain; the digest keeps
  plain lines; the dependency is declared.
- Mutation check (2026-11-07): `html=True` turns the 5 raw-HTML tests red; removing the `validateLink`
  allowlist turns 4 link tests red (relative, `ftp:`, `tel:`, host-less https: the parser's own default
  validator lets those through).
- Existing email tests unchanged and green: `test_gateway_email_notice_template.py`, `test_notify_me_channels.py`,
  `test_gateway_email_notifications.py`, `test_gateway_notifications_test_reasons.py` (85 passed with the new
  module, on markdown-it-py 4.0.0 and on 3.0.0).
- Rendered examples (Chromium via Playwright on the HTML file, every network request blocked and none
  attempted; document scroll width equal to the viewport at 600 and 400 px): a long daily news briefing, a
  mail triage result with a table and an image, a flow's Notify me with a code block and injection attempts,
  and a stress case (a 300-character token in a 6-column table). HTML, text parts and screenshots are under
  `abstractgateway/untracked/mail-markdown-1011/` (not tracked).

## Review follow-ups (2026-11-07, `review-gateway-g4g6.md`)

- F1: size budgets (`MAX_MARKDOWN_INPUT_CHARS` 100,000, `MAX_RENDERED_HTML_BYTES` 200,000): over either, the whole
  text is shown escaped as plain text (the review's 5.2 KB table that rendered to 8 MB).
- F2: floor `markdown-it-py>=4.0.0`.
- F3: a link whose href is refused (an empty target, `[x]()`) becomes its text.
- F4: autolink text is the address as written (`normalizeLinkText` decodes no punycode or percent-escapes).

## Not done

- The real send through a scratch gateway and mailbox viewed in Gmail web, Apple Mail and Outlook (named in
  Validation above) needs a scratch mailbox and the operator's eyes; it was not run. The 400 px check was
  made in Chromium on the rendered file. Outlook desktop (Word engine) ignores `overflow`, so a very wide
  table can still scroll the whole message there; it cannot widen the 600 px layout in clients that honour
  `table-layout`.
