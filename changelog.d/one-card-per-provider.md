### Fixed

- **A saved API key no longer shows as a second provider card** (operator report 2026-11-03: "Anthropic" and
  "Anthropic (API key) · Not connected" side by side). The second card was the saved connection itself:
  the Gateway stores a connection in AbstractCore's `provider_profiles`, so AbstractCore also lists it as a
  bare `endpoint:anthropic-key` row, and `GET /api/gateway/discovery/providers` kept that row and dropped its
  own row for the same name, the one carrying `provider_endpoint_profile` that the kit's cloud provider cards
  use to tell a connection from a provider. The route now adds the connection's fields to AbstractCore's row
  (`routes/gateway.py`, `discovery_providers`); registry rows such as `anthropic` are unchanged. Every saved
  cloud connection was affected (`openrouter-key` too), not only Anthropic.
