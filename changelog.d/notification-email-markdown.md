### Changed

- **Notification emails show the automation's text formatted, not as raw Markdown**
  (`mail/mail_markdown.py`, `mail/notifications.py`; backlog 1011). An automation result email showed
  `**First, the technology story**` with its asterisks. The model-written body of an automation result
  and of a flow's Notify me step is now rendered by a CommonMark parser (`markdown-it-py`, now a declared
  dependency, `>=3.0.0`): headings, bold and italic, lists, quotes, code, tables and links, each element
  styled inline in the layout's palette (Gmail and Outlook drop `<style>` blocks). Line breaks inside a
  paragraph are kept. The plain-text part still carries the text exactly as written, the model's title is
  never formatted, and the digest of held-back notices keeps its plain lines.
- **A notification email never grows wider than its layout**: the layout's column is fixed to its width,
  a wide table scrolls inside its own box and long words, links and code wrap, so a long briefing reads on
  a 400 px phone screen.

### Security

- **Model text in an email stays inert.** Raw HTML is off (`<script>`, `<img onerror=…>` show as text),
  only `http`/`https` (with a host) and `mailto` targets become links (`javascript:`, `data:`, `file:`,
  `tel:`, relative paths stay visible as written, never a link), and an image becomes a link to its
  address, never a remote load.
