# 1011-abstractgateway: [IMPROVEMENT] Notification emails render the automation's Markdown safely

> Created: 2026-11-07
> Type: improvement
> Priority: P2
> Labels: mail, notifications, automations, security
> Status: proposed

- Work-item id: abstractgateway-1011
- Owner: framework (intake; implementation belongs to Gateway)
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
