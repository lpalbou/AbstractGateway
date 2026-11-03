### Fixed

- **A saved API key no longer shows as a second provider card** (for example "Anthropic" next to "Anthropic (API
  key) · Not connected"). `GET /api/gateway/discovery/providers` now merges a saved connection's fields into
  AbstractCore's row for it, so the console's cloud provider cards show one card per provider with its connection.
  Registry rows such as `anthropic` are unchanged. This applied to every saved cloud connection (`openrouter-key`
  too).
