### Changed

- **The console's entity Talk panel uses the shared chat composer** (`console.py`, `console_ui.py`).
  The visit message field is now the AbstractUIC `mountChatComposer` island, the same rounded multiline
  composer as the Sandbox and Docs chats, in place of the plain `<textarea>` and Send button. Enter sends,
  Shift+Enter starts a new line, the field stays disabled until a visit is open, a send in flight cannot be
  repeated, a failed send keeps the draft, and a reply that arrives after the visit changed is dropped. The
  console refuses to start with a clear error when the vendored bundle lacks `mountChatComposer`.
- The vendored console islands bundle (`console_islands.py`) now carries `@abstractframework/ui-kit` 0.9.11.
